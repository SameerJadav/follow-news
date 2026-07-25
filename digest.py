#!/usr/bin/env python3
"""CLI entrypoint and orchestration for the daily digest pipeline.

    uv run digest.py                     # full pipeline, overwrites today's file
    uv run digest.py --if-missing        # no-op if today's data/*.json exists
    uv run digest.py --feeds PATH        # use an alternate feeds.txt (testing degradation)
    uv run digest.py render              # re-render docs/ from data/ + followed/, no API key
    uv run digest.py follow              # process Follow issues, append timelines, re-render
    uv run digest.py follow --issue 12   # restrict to one issue (used by follow.yml)
    uv run digest.py follow --date ...   # override the digest date (testing)
    uv run digest.py health              # feed-decay report over committed data/; exit 1 if unhealthy
    uv run digest.py review              # calibration evidence for the newest digest
    uv run digest.py review --date ...   # calibration evidence for one past day

data/YYYY-MM-DD.json is the single source of truth. Every page in docs/ is
derived from it and is overwritten wholesale on every render — never
hand-edit generated HTML. Only this pipeline writes data/.

followed/<issue>.json is a second source of truth, alongside data/, written
only by follow.py. Follow is additive: `run_pipeline` never calls it, so a
Follow failure can never fail the digest. The digest workflow instead calls
`digest.py follow` as its own continue-on-error step, and the issue-triggered
follow.yml workflow calls it with `--issue` for a single new request.

Reliability (Phase 6): a morning that can't produce a real digest — quorum
not met, or any pass returning nothing usable — never leaves the reader with
nothing. `_render_stale` re-renders with `today` passed as the current IST
date while the newest committed data/ file is still yesterday's, which is
exactly the condition render._stale_html renders the "isn't ready yet" banner
for. `main` then exits non-zero so the workflow itself shows red and the
owner gets GitHub's failure email — the one signal that reaches a human
without anyone having to go looking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import feeds
from feeds import article_window_start, dbg

IST = timezone(timedelta(hours=5, minutes=30))

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
FEEDS_PATH = ROOT / "feeds.txt"
FOLLOWED_DIR = ROOT / "followed"

# The deployed origin. The local checkout is named "daily-digest-new" —
# that name must never leak into a URL or a committed path.
BASE_URL = "https://sameerjadav.github.io/follow-news/"


def digest_date(now: datetime | None = None) -> date:
    """The digest day, computed in IST. The three staggered cron entries in
    digest.yml (02:00/04:00/06:00 IST) all resolve to the same IST date, so
    --if-missing correctly no-ops for whichever fires second and third."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(IST).date()


def data_path(d: date) -> Path:
    return DATA_DIR / f"{d:%Y-%m-%d}.json"


def previous_digest(before: date) -> dict | None:
    """The most recent data/*.json strictly before `before`, or None."""
    candidates = sorted(DATA_DIR.glob("*.json"))
    prior = [p for p in candidates if p.stem < f"{before:%Y-%m-%d}"]
    if not prior:
        return None
    return json.loads(prior[-1].read_text())


def write_digest(d: date, stories: list[dict], generated_at: datetime, health: dict) -> None:
    """The data/ contract. Later phases extend this object; they must not
    reshape these keys. Per story: `section` ("world"/"india", never both),
    `headline`, `body` (clean prose, no inline markers), `markers` (character
    offsets into `body` mapping a span of text to one claim/outlet/url —
    Phase 4's tappable source markers), `claims` (only the claims actually
    cited in `body`, each anchored to exactly one source), `thin_sourced`
    (measured from the cited claims' outlet spread, never model-assessed),
    `sources` (the outlets/urls actually cited), `vocab`, and `signals`
    (ranking evidence plus anchoring diagnostics — word_count/word_target,
    marker_count, unanchored_share, claim_outlets, unsourced_figures — kept
    in data/ so Phase 6 can calibrate from committed files rather than
    Actions logs). `date`/`date_label`/`generated_at` stay as they are.

    `health` (Phase 6) is feeds.health_payload() for this run — diagnostic
    only, never rendered, read only by report.py across many committed days
    to catch a feed rotting silently over a year of no maintenance."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": f"{d:%Y-%m-%d}",
        "date_label": d.strftime("%A, %-d %B %Y"),
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stories": stories,
        "health": health,
    }
    data_path(d).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    dbg(f"digest: wrote {data_path(d)} with {len(stories)} story/stories")


def _render_stale(today: date) -> None:
    """Re-render docs/ with `today` as the current IST date while data/'s
    newest file is still an earlier day — render._stale_html then emits the
    "isn't ready yet" banner unhidden on index.html. Used only when a run
    could not produce today's digest, so the reader gets yesterday's digest
    honestly labelled rather than a blank or half-built page. Never lets a
    rendering failure mask the real failure that got us here."""
    try:
        import render

        render.render_all(DATA_DIR, DOCS_DIR, today, FOLLOWED_DIR)
        dbg(f"digest: re-rendered with stale banner for {today}")
    except Exception as exc:  # noqa: BLE001 - the original failure must still surface
        dbg(f"digest: could not render stale page either ({exc!r})")


def run_pipeline(feeds_path: Path = FEEDS_PATH) -> bool:
    """The full pipeline: gather -> shape pool -> select -> rank/score ->
    fetch text for kept clusters only -> extract anchored claims -> write
    from claims only -> save -> render. Exactly three LLM calls (select,
    claims, write), independent of how many articles were ingested or how
    many clusters survive ranking — the whole quota strategy.

    Returns True only when a digest was actually written and rendered. Every
    early-exit path returns False instead of silently doing nothing — main()
    turns a False into a stale-page render plus a non-zero exit, so a failed
    morning is never invisible."""
    # Imported lazily so `digest.py render` never needs GEMINI_API_KEY.
    import extract
    import llm
    import rank
    import render
    import wikipedia

    now = datetime.now(timezone.utc)
    today = digest_date(now)
    prev = previous_digest(today)
    prev_generated_at = (
        datetime.strptime(prev["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if prev
        else None
    )
    since = article_window_start(now, prev_generated_at)
    dbg(f"digest: window since={since.isoformat()} (prev_generated_at={prev_generated_at})")

    result = feeds.gather(feeds_path, since)
    health = feeds.health_payload(result)
    if not feeds.quorum_ok(result):
        dbg("digest: QUORUM FAILED — not writing a digest for today")
        return False
    articles = result.articles

    pool = rank.build_select_pool(articles)
    events = wikipedia.current_events(today)

    selected = llm.select_stories(pool, wikipedia.prompt_block(events))
    if not selected:
        dbg("digest: selection returned no clusters; not writing a digest for today")
        return False

    ranked = rank.rank_clusters(selected, pool, events)
    if not ranked:
        dbg("digest: ranking kept no clusters; not writing a digest for today")
        return False
    rank.wiki_coverage_report(events, ranked)

    texts: dict[str, str] = {}
    for cluster in ranked:
        for article in cluster.articles:
            texts[article.url] = extract.article_text(article.url)

    claims_by_cluster = llm.extract_claims(ranked, texts)
    if not claims_by_cluster:
        dbg("digest: claims pass produced no anchored claims; not writing a digest for today")
        return False

    stories = llm.write_stories(ranked, claims_by_cluster)
    if not stories:
        dbg("digest: writing pass returned no valid stories; not writing a digest for today")
        return False

    write_digest(today, stories, now, health)
    render.render_all(DATA_DIR, DOCS_DIR, today, FOLLOWED_DIR)
    dbg(f"digest: {llm._CALLS} LLM call(s) this run")
    return True


def _cmd_health() -> None:
    """Cross-day feed-decay report over committed data/ — no network, no
    key. Exits 1 when a feed has gone dark DEAD_DAYS+ running or today's run
    was itself degraded, so this can be its own non-gating Actions job: a
    dying feed turns it red and the owner gets an email, without touching
    the digest or its deploy."""
    import report

    table, warnings, ok = report.feed_health(DATA_DIR)
    print(table)
    for w in warnings:
        print(f"::warning title=Feed health::{w}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Feed health\n\n" + table + "\n")
            for w in warnings:
                f.write(f"\n- ⚠️ {w}")
            f.write("\n")

    sys.exit(0 if ok else 1)


def _cmd_review(day_iso: str | None) -> None:
    """Calibration evidence for one day (default: newest) — Phase 6 Part B
    tunes rank.py/anchor.py dials against this, not against intuition."""
    import report

    print(report.morning_review(DATA_DIR, day_iso))


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily digest pipeline")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["render", "follow", "health", "review"],
        help="omit for the full pipeline; 'render' to re-render docs/ from data/+followed/ only; "
        "'follow' to process Follow issues, append timelines, and re-render; "
        "'health' to report cross-day feed decay (exits 1 if unhealthy); "
        "'review' to print one day's calibration evidence",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="no-op if today's data/YYYY-MM-DD.json already exists",
    )
    parser.add_argument(
        "--feeds",
        default=None,
        help="path to an alternate feeds.txt (default feeds.txt); useful for testing degradation",
    )
    parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="follow: restrict to this issue number (used by follow.yml for a single new request)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="follow/review: override the date as YYYY-MM-DD, instead of today in IST (testing)",
    )
    args = parser.parse_args()

    if args.command == "render":
        import render

        render.render_all(DATA_DIR, DOCS_DIR, digest_date(), FOLLOWED_DIR)
        return

    if args.command == "follow":
        import follow

        today = date.fromisoformat(args.date) if args.date else digest_date()
        follow.run(DATA_DIR, FOLLOWED_DIR, DOCS_DIR, today, only_issue=args.issue)
        return

    if args.command == "health":
        _cmd_health()
        return

    if args.command == "review":
        _cmd_review(args.date)
        return

    if args.if_missing and data_path(digest_date()).exists():
        dbg(f"digest: {data_path(digest_date())} already exists, nothing to do")
        return

    feeds_path = Path(args.feeds) if args.feeds else FEEDS_PATH
    if not run_pipeline(feeds_path):
        _render_stale(digest_date())
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        dbg(f"digest: FATAL {exc!r}")
        _render_stale(digest_date())
        sys.exit(1)
