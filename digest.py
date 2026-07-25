#!/usr/bin/env python3
"""CLI entrypoint and orchestration for the daily digest pipeline.

    uv run digest.py                # full pipeline, overwrites today's file
    uv run digest.py --if-missing   # no-op if today's data/*.json exists
    uv run digest.py render         # re-render docs/ from data/ only, no API key

data/YYYY-MM-DD.json is the single source of truth. Every page in docs/ is
derived from it and is overwritten wholesale on every render — never
hand-edit generated HTML. Only this pipeline writes data/.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from feeds import article_window_start, dbg, gather

IST = timezone(timedelta(hours=5, minutes=30))

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
FEEDS_PATH = ROOT / "feeds.txt"

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


def write_digest(d: date, stories: list[dict], generated_at: datetime) -> None:
    """The data/ contract. Later phases extend this object; they must not
    reshape these keys. Per story: `section` ("world"/"india", never both),
    `headline`, `body`, `sources`, `vocab`, and `signals` (category/tier/
    distinct_outlets/wiki_backed/weight — the ranking evidence, kept in data/
    so Phase 6 can calibrate from committed files rather than Actions logs).
    Phase 3 adds claim anchors on top; `date`/`date_label`/`generated_at`
    stay as they are."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": f"{d:%Y-%m-%d}",
        "date_label": d.strftime("%A, %-d %B %Y"),
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stories": stories,
    }
    data_path(d).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    dbg(f"digest: wrote {data_path(d)} with {len(stories)} story/stories")


def run_pipeline() -> None:
    """The full pipeline: gather -> shape pool -> select -> rank/score ->
    fetch text for kept clusters only -> write -> save -> render. Exactly
    two LLM calls, independent of how many articles were ingested or how
    many clusters survive ranking — the whole quota strategy."""
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

    articles = gather(FEEDS_PATH, since)
    if not articles:
        dbg("digest: no articles gathered in window; not writing a digest for today")
        return

    pool = rank.build_select_pool(articles)
    events = wikipedia.current_events(today)

    selected = llm.select_stories(pool, wikipedia.prompt_block(events))
    if not selected:
        dbg("digest: selection returned no clusters; not writing a digest for today")
        return

    ranked = rank.rank_clusters(selected, pool, events)
    if not ranked:
        dbg("digest: ranking kept no clusters; not writing a digest for today")
        return
    rank.wiki_coverage_report(events, ranked)

    texts: dict[str, str] = {}
    for cluster in ranked:
        for article in cluster.articles:
            texts[article.url] = extract.article_text(article.url)

    stories = llm.write_stories(ranked, texts)
    if not stories:
        dbg("digest: writing pass returned no valid stories; not writing a digest for today")
        return

    write_digest(today, stories, now)
    render.render_all(DATA_DIR, DOCS_DIR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily digest pipeline")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["render"],
        help="omit for the full pipeline; 'render' to re-render docs/ from data/ only",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="no-op if today's data/YYYY-MM-DD.json already exists",
    )
    args = parser.parse_args()

    if args.command == "render":
        import render

        render.render_all(DATA_DIR, DOCS_DIR)
        return

    if args.if_missing and data_path(digest_date()).exists():
        dbg(f"digest: {data_path(digest_date())} already exists, nothing to do")
        return

    run_pipeline()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        dbg(f"digest: FATAL {exc!r}")
        sys.exit(1)
