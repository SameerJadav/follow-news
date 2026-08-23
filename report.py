"""Read-only reports over committed data/*.json — no network, no API key.

Two jobs:

- `feed_health`: a cross-day view a single morning's stderr log can't give.
  Over a year of no maintenance, feeds don't only die outright (a status
  code catches that) — they go quiet at HTTP 200 with zero items (research.md
  SS2.3/SS7.1: The Wire, The Print) or just stop being picked up. Comparing
  the last HISTORY_DAYS committed digests is what turns "quiet today" into
  "dead for three days running" before it costs a bad morning.
- `morning_review`: the calibration evidence for Phase 6 Part B — read the
  actual dials (rank.WEIGHT_FLOOR, anchor thresholds) against what a real
  morning produced, instead of tuning from memory or intuition.

Both read only what digest.py already commits to data/ — the `health` key
per day and each story's `signals` — so neither needs the Actions log.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import rank

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

DEAD_DAYS = 3  # consecutive silent days before a feed is called dead
HISTORY_DAYS = 7

# How many days of debug/<date>/ survive a commit. Measured 2026-08-23: capture
# wrote 16.9 MB a day and debug/ had become 472 MB of a 510 MB depth-1 clone —
# 89% of the objects at HEAD — which every one of the three daily crons pays for
# on checkout, projecting to ~6.2 GB over the stated one-year unattended run
# (ANALYSIS-2026-08-23.md §H5). tracer.MAX_RUN_BYTES bounds a single run (its 64
# MB has never bitten; the largest run ever captured wrote 17.7 MB) and nothing
# bounded the directory. This does. It matches HISTORY_DAYS deliberately: the
# bundle's own default window is what a morning is actually diagnosed from, and
# git history keeps everything older for anyone who wants to go digging.
RETAIN_DAYS = 7


def load_days(data_dir: Path, limit: int = HISTORY_DAYS) -> list[dict]:
    """The most recent `limit` data/*.json that carry a "health" key, newest
    first. Older files (predating Phase 6) are skipped rather than crashing
    the check — the decay check simply stays quiet until enough hardened
    days exist to say anything meaningful."""
    days = []
    for path in sorted(data_dir.glob("*.json"), reverse=True):
        try:
            day = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if "health" in day:
            days.append(day)
        if len(days) >= limit:
            break
    return days


def feed_health(data_dir: Path, configured: set[str] | None = None) -> tuple[str, list[str], bool]:
    """(markdown_table, warnings, ok). `ok` is False only for a decay pattern
    worth waking someone up for — a feed dead DEAD_DAYS+ running, or today's
    run itself degraded. A single quiet day for an otherwise-live feed is not
    decay and does not clear `ok`.

    `configured` is the outlet names currently in feeds.txt. An outlet in the
    window but no longer configured has been *retired*, not lost, so it is
    dropped from the check entirely — otherwise retiring a dead feed keeps
    the job red for HISTORY_DAYS more days while the outlet ages out of the
    window, training the owner to ignore the one alarm that matters
    (calibration.md 2026-08-23). Passing None keeps every outlet seen, which
    is what the unit tests construct against. Note this only ever *removes*
    rows: a configured outlet missing from a day's health still counts as
    dead that day, which is what catches a feed that stops being fetched at
    all rather than failing loudly."""
    days = load_days(data_dir, HISTORY_DAYS)
    if not days:
        return "(no digest carries feed health data yet)", [], True

    outlets: dict[str, list[bool]] = {}  # outlet -> [live?, ...] oldest to newest
    for day in reversed(days):  # oldest first, so index -1 is always "today"
        live_today = {f["outlet"] for f in day.get("health", {}).get("feeds", []) if f.get("usable", 0) > 0}
        seen_today = {f["outlet"] for f in day.get("health", {}).get("feeds", [])}
        for outlet in seen_today | outlets.keys():
            if configured is not None and outlet not in configured:
                continue
            outlets.setdefault(outlet, []).append(outlet in live_today)

    if not outlets:
        return "(no configured feed appears in the last {}d of data)".format(HISTORY_DAYS), [], True

    warnings: list[str] = []
    ok = True
    rows = ["| feed | live/{}d | today | note |".format(HISTORY_DAYS), "| --- | --- | --- | --- |"]
    for outlet in sorted(outlets):
        history = outlets[outlet]
        live_count = sum(history)
        today_live = history[-1]

        streak = 0
        for live in reversed(history):
            if live:
                break
            streak += 1

        note = ""
        if streak >= DEAD_DAYS:
            note = f"DEAD — no items for {streak} day(s)"
            warnings.append(f"{outlet} DEAD — no items for {streak} consecutive day(s)")
            ok = False
        elif not today_live and live_count >= max(1, len(history) // 2):
            note = "silent today"
            warnings.append(f"{outlet} silent today (usually live)")

        rows.append(f"| {outlet} | {live_count}/{len(history)} | {'yes' if today_live else 'no'} | {note} |")

    newest = days[0]
    newest_health = newest.get("health", {})
    if newest_health.get("degraded"):
        live = newest_health.get("live")
        configured = newest_health.get("configured")
        warnings.append(f"today's run only had {live}/{configured} feeds live")
        ok = False

    return "\n".join(rows), warnings, ok


def morning_review(data_dir: Path, day_iso: str | None = None) -> str:
    """The calibration evidence for one day: story counts, weights against
    rank.WEIGHT_FLOOR, anchoring quality, and that day's feed health — the
    numbers Phase 6 Part B tunes dials against instead of intuition."""
    if day_iso:
        path = data_dir / f"{day_iso}.json"
        if not path.exists():
            return f"no digest for {day_iso}"
        day = json.loads(path.read_text())
    else:
        candidates = sorted(data_dir.glob("*.json"))
        if not candidates:
            return "no digests yet"
        day = json.loads(candidates[-1].read_text())

    stories = day.get("stories", [])
    by_section: dict[str, int] = {}
    lines = [f"{day.get('date', '?')}  {len(stories)} stories"]

    weights = []
    unanchored = []
    dropped_total = 0
    thin = []
    for s in stories:
        by_section[s.get("section", "?")] = by_section.get(s.get("section", "?"), 0) + 1
        sig = s.get("signals", {})
        weights.append(sig.get("weight", 0))
        unanchored.append(sig.get("unanchored_share", 0.0))
        dropped_total += sig.get("dropped_markers", 0)
        if s.get("thin_sourced"):
            thin.append(s.get("headline", "?"))

        target = sig.get("word_target")
        wc = sig.get("word_count")
        figs = sig.get("unsourced_figures") or []
        vocab_n = len(s.get("vocab") or [])
        lines.append(
            f"  [{sig.get('tier', '?'):<7}] w={sig.get('weight', '?'):<3} {s.get('section', '?'):<5} "
            f"{s.get('headline', '(untitled)')!r} — {wc}/{target}w, "
            f"outlets={sig.get('claim_outlets', '?')}, vocab={vocab_n}"
            + (f", UNSOURCED FIGURES={figs}" if figs else "")
        )

    lines.insert(1, f"  sections: {by_section} (world/india split)")
    if weights:
        lines.append(f"  weights: {weights}  (floor={rank.WEIGHT_FLOOR})")
    if unanchored:
        lines.append(f"  unanchored_share: mean {sum(unanchored) / len(unanchored):.1%}, dropped_markers total {dropped_total}")
    lines.append(f"  thin_sourced: {thin or 'none'}")

    health = day.get("health")
    if health:
        lines.append(
            f"  feeds: {health.get('live')}/{health.get('configured')} live, "
            f"{health.get('articles')} articles" + (" — DEGRADED" if health.get("degraded") else "")
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The debug bundle (tracer.py's counterpart: it writes debug/, this reads it)
# ---------------------------------------------------------------------------

# Funnel keys in pipeline order. Anything a stage reported that isn't listed
# here still gets shown, appended after these — a new counter must never go
# missing from the bundle just because this list wasn't updated.
_FUNNEL_ORDER = (
    "feeds_configured", "feeds_live", "articles_fetched", "articles_in_window",
    "articles_after_dedupe", "pool_out", "wiki_events_used", "clusters_proposed",
    "clusters_scored", "clusters_kept", "articles_extracted", "articles_extract_weak",
    "articles_extract_failed", "claims_calls", "claims_total", "claims_clusters_out",
    "stories_written", "stories_dropped", "stories_published", "llm_calls",
)


def prune_debug(debug_dir: Path, keep_days: int = RETAIN_DAYS) -> tuple[list[str], int]:
    """Delete captured run directories older than the newest `keep_days` of
    them. Returns (removed day names, bytes freed) — the caller prints it, so
    a checkout that lost 21 days of evidence says so out loud rather than
    quietly shrinking (CLAUDE.md §No silent caps).

    Only `debug/<YYYY-MM-DD>/` directories are considered: README.md,
    ANALYSIS.md and anything else a human put here are never touched. Keeps at
    least one day whatever is asked, so a mistuned dial can't empty the
    directory."""
    if not debug_dir.exists():
        return [], 0
    days = sorted(
        (p for p in debug_dir.iterdir() if p.is_dir() and _DAY_RE.fullmatch(p.name)),
        reverse=True,
    )
    doomed = days[max(1, keep_days):]
    removed: list[str] = []
    freed = 0
    for path in doomed:
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        removed.append(path.name)
        freed += size
    return sorted(removed), freed


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _load_runs(debug_dir: Path, limit: int) -> tuple[list[dict], int]:
    """The newest `limit` captured run directories, newest first, with their
    run.json and funnel.json attached — plus how many exist in total, so the
    caller can say what it left out. A day whose run.json is missing still
    appears; whether that means "died before finish()" or "only a follow ran
    that day" is decided by looking for run-follow.json, which is the
    difference F14 got wrong about 2026-07-27 for a month."""
    dirs = [p for p in sorted(debug_dir.glob("*/"), reverse=True) if p.is_dir()]
    runs = []
    for path in dirs[:limit]:
        run = _read_json(path / "run.json")
        follow = _read_json(path / "run-follow.json")
        runs.append(
            {
                "day": path.name,
                "dir": path,
                "run": run,
                "funnel": _read_json(path / "funnel.json") or {},
                "incomplete": run is None,
                "follow_only": run is None and follow is not None,
                "follow": follow,
            }
        )
    return runs, len(dirs)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |",
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in rows:
        out.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    return "\n".join(out)


def debug_bundle(debug_dir: Path, data_dir: Path, days: int | None = None) -> tuple[Path | None, str]:
    """One self-contained document over every captured run.

    The point of this file is that someone who has never seen the repo can
    read it top to bottom and say where the pipeline is going wrong, without
    running anything. It states what each stage does, tabulates the funnels
    side by side so a stage that lost articles stands out as a column that
    drops, lists every rejection grouped by reason, and points at the raw
    artifacts backing each claim. Returns (written_path, text).
    """
    limit = days or HISTORY_DAYS
    runs, captured = _load_runs(debug_dir, limit)
    if not runs:
        return None, (
            f"No captured runs under {debug_dir}/.\n"
            "Debug capture is off or has not run yet — set DIGEST_DEBUG=1 "
            "(digest.yml sets it for scheduled runs) or pass --debug."
        )

    out: list[str] = []
    w = out.append

    w("# Debug bundle")
    w("")
    w(f"Showing {len(runs)} of {captured} captured run(s), newest first: "
      f"{', '.join(r['day'] for r in runs)}.")
    w("")
    if captured > len(runs):
        # CLAUDE.md §No silent caps. At the default window this used to hide
        # every failed day and every aborted run while reporting the truncated
        # count as if it were the total (ANALYSIS-2026-08-23.md §M4).
        oldest = sorted(p.name for p in debug_dir.glob("*/") if p.is_dir())
        w(f"> **{captured - len(runs)} older captured day(s) are NOT in this bundle** "
          f"(window = {limit} day(s), earliest captured = {oldest[0]}). "
          f"Run `uv run digest.py debug --days {captured}` for all of them.")
        w("")
    w("This file is generated by `uv run digest.py debug`. It summarises the")
    w("contents of `debug/<date>/`; every number below is backed by a file in")
    w("that directory, named at the end of each section.")
    w("")

    w("## What the pipeline does")
    w("")
    w("A GitHub Action runs `digest.py` at ~02:00 IST. Stages, in order:")
    w("")
    w("1. **gather** (`feeds.py`) — fetch every RSS feed in `feeds.txt`, keep")
    w("   articles inside the time window, dedupe by canonical URL and by")
    w("   normalised title. `quorum_ok` decides whether there is enough news.")
    w("2. **pool** (`rank.py`) — cap each outlet's share so a high-volume feed")
    w("   can't crowd out the rest. This is what the select prompt sees.")
    w("3. **wikipedia** (`wikipedia.py`) — the Current Events Portal, a curated")
    w("   human check against volume bias.")
    w("4. **select** (`llm.py`, LLM call 1) — headlines and summaries only, no")
    w("   article text. Returns clusters of article ids.")
    w("5. **rank** (`rank.py`) — score each cluster (distinct outlets + tier")
    w("   weight + wiki bonus) and cut below `WEIGHT_FLOOR`. No network.")
    w("6. **extract** (`extract.py`) — fetch full text for surviving clusters")
    w("   only: JSON-LD and paragraph extraction from one fetch, longest wins,")
    w("   escalating to the r.jina.ai reader proxy if both fall short.")
    w("7. **claims** (`llm.py`, one LLM call PER SECTION) — atomic claims, each")
    w("   anchored to exactly one article URL. Per section since 2026-08-23: one")
    w("   batched call decayed with prompt position and India was always last.")
    w("8. **write** (`llm.py`, the last LLM call) — prose from claims ONLY; the write")
    w("   pass never sees article text. `anchor.py` then validates each story")
    w("   and drops any that isn't genuinely claim-anchored.")
    w("9. **render** (`render.py`) — regenerate `docs/` wholesale.")
    w("")
    w("Four LLM calls per morning, total. Any stage returning nothing ends the")
    w("run: `docs/` is re-rendered with an honest \"not ready yet\" banner and")
    w("the process exits 1.")
    w("")

    # ---- funnel -----------------------------------------------------------
    w("## Funnel")
    w("")
    w("Counts at each stage, one column per day. A stage that lost everything")
    w("is where to look first; `stopped_at` names it outright when a run died —")
    w("including a stage that RAISED, which before 2026-08-23 left the field blank")
    w("(the per-day section below still quotes the exception either way).")
    w("")
    keys = [k for k in _FUNNEL_ORDER if any(k in r["funnel"] for r in runs)]
    keys += sorted({k for r in runs for k in r["funnel"]} - set(keys))
    headers = ["metric"] + [r["day"] for r in runs]
    rows = [[k] + [str(r["funnel"].get(k, "-")) for r in runs] for k in keys]
    status = ["status"] + [
        ("follow only" if r["follow_only"] else
         "INCOMPLETE" if r["incomplete"] else
         ("ok" if (r["run"] or {}).get("ok") else "FAILED"))
        for r in runs
    ]
    stopped = ["stopped_at"] + [str((r["run"] or {}).get("stopped_at", "-")) for r in runs]
    elapsed = ["elapsed_ms"] + [str((r["run"] or {}).get("elapsed_ms", "-")) for r in runs]

    # Read back from the published file rather than trusting the counter:
    # if these two disagree, the bug is between write_digest and the page.
    on_disk = ["stories in data/"]
    for r in runs:
        day = _read_json(data_dir / f"{r['day']}.json")
        on_disk.append(str(len(day.get("stories", []))) if day else "no file")

    w(_table(headers, [status, stopped, elapsed] + rows + [on_disk]))
    w("")
    w("Backed by: `debug/<date>/funnel.json`, `debug/<date>/run.json`, `data/<date>.json`.")
    w("")

    # ---- per-run detail ---------------------------------------------------
    for r in runs:
        day, d = r["day"], r["dir"]
        w(f"## {day}")
        w("")
        run = r["run"] or {}
        if r["follow_only"]:
            w("**No digest run was captured this day** — only a `follow` run "
              "(`run-follow.json`). Whatever the digest published that day is in "
              "`data/`; capture simply was not on for it. This is not a failure.")
            w("")
        elif r["incomplete"]:
            w("**No `run.json`** — the process died before it could finish writing.")
            w("`trace.jsonl` is the only record; read its last lines.")
            w("")
        else:
            w(f"- outcome: **{'ok' if run.get('ok') else 'FAILED'}**"
              + (f", stopped at **{run['stopped_at']}**" if run.get("stopped_at") else ""))
            w(f"- started {run.get('started_at')}, took {run.get('elapsed_ms')} ms, "
              f"git `{str(run.get('git_sha', ''))[:8]}`")
            stages = run.get("stages") or []
            if stages:
                w("- stage timings: " + ", ".join(f"{s['stage']} {s['ms']}ms" for s in stages))
            errs = [s for s in stages if s.get("error")]
            for s in errs:
                w(f"  - **{s['stage']} raised**: `{s['error']}`")
            w("")

        # feeds
        feeds_idx = _read_json(d / "feeds" / "index.json")
        if feeds_idx:
            dead = [f for f in feeds_idx.get("feeds", []) if not f.get("usable")]
            if dead:
                w("**Feeds contributing nothing:**")
                w("")
                w(_table(["outlet", "http", "raw_items", "error"],
                         [[str(f.get("outlet")), str(f.get("http")), str(f.get("raw_items")),
                           str(f.get("error") or "")] for f in dead]))
                w("")

        # extraction — the stage with the least visibility elsewhere
        ex = _read_json(d / "extract" / "index.json")
        if ex:
            arts = ex.get("articles", [])
            weak = [a for a in arts if a.get("below_min_chars")]
            jina = [a for a in arts if a.get("jina_fired")]
            winners: dict[str, int] = {}
            for a in arts:
                winners[a.get("winner") or "none"] = winners.get(a.get("winner") or "none", 0) + 1
            w(f"**Scraping:** {len(arts)} article(s); strategy that won: {winners}; "
              f"{len(jina)} escalated to Jina; {len(weak)} came back under "
              f"{ex.get('min_chars')} chars.")
            w("")
            if weak:
                w(_table(["url", "http", "jsonld", "paras", "jina", "final"],
                         [[str(a.get("url"))[:70], str(a.get("http")), str(a.get("jsonld_chars")),
                           str(a.get("paragraph_chars")), str(a.get("jina_chars")),
                           str(a.get("final_chars"))] for a in weak]))
                w("")
                w("For each of these, `debug/%s/extract/NNN-*.html` is the exact page "
                  "fetched and `NNN-*.txt` is what was handed to the claims pass — "
                  "diff them to see what the extractor missed." % day)
                w("")

        # rank cuts
        rk = _read_json(d / "rank.json")
        if rk:
            cut = [c for c in rk.get("clusters", []) if c.get("verdict") != "kept"]
            if cut:
                w("**Stories the ranker rejected:**")
                w("")
                w(_table(["verdict", "section", "weight", "outlets", "wiki", "headline_hint"],
                         [[str(c.get("verdict")), str(c.get("section")), str(c.get("weight", "-")),
                           str(c.get("distinct_outlets", "-")), str(c.get("wiki_backed", "-")),
                           str(c.get("headline_hint"))[:60]] for c in cut]))
                w("")
            if rk.get("floor_relaxed"):
                w(f"> **WEIGHT_FLOOR was relaxed**: nothing cleared "
                  f"{rk['dials'].get('WEIGHT_FLOOR')}, so the top few ran anyway. "
                  f"This dial is mistuned for this day's news.")
                w("")

        # anchor drops
        an = _read_json(d / "anchor" / "index.json")
        if an:
            dropped = [s for s in an.get("stories", []) if s.get("verdict", "").startswith("dropped")]
            if dropped:
                w("**Written stories the anchoring gate threw away** (three LLM calls "
                  "already spent on each):")
                w("")
                w(_table(["verdict", "words", "markers", "unanchored", "headline_hint"],
                         [[str(s.get("verdict")), str(s.get("word_count", "-")),
                           str(s.get("marker_count", "-")), str(s.get("unanchored_share", "-")),
                           str(s.get("headline_hint"))[:55]] for s in dropped]))
                w("")
                w(f"Full model output for each is in `debug/{day}/anchor/dropped-<id>.json`.")
                w("")

        # wikipedia misses
        cov = _read_json(d / "wikipedia" / "coverage.json")
        if cov and cov.get("not_covered"):
            w(f"**Curated Wikipedia events no story covered** ({len(cov['not_covered'])} of "
              f"{cov.get('events')}) — the \"missed:\" line in calibration.md:")
            w("")
            for e in cov["not_covered"][:15]:
                w(f"- [{e.get('category')}] {e.get('text')}")
            w("")

        # llm calls
        metas = sorted((d / "llm").glob("*.meta.json")) if (d / "llm").is_dir() else []
        if metas:
            rows = []
            for m in metas:
                meta = _read_json(m) or {}
                usage = meta.get("usage") or {}
                rows.append([str(meta.get("label")), str(meta.get("latency_ms")),
                             str(meta.get("prompt_chars")), str(meta.get("response_chars")),
                             str(usage.get("total_token_count", "-")),
                             str(meta.get("finish_reason")),
                             str(meta.get("json_retries"))])
            w("**LLM calls:**")
            w("")
            w(_table(["label", "ms", "prompt ch", "resp ch", "tokens", "finish", "retries"], rows))
            w("")
            w(f"Prompts and raw responses: `debug/{day}/llm/`.")
            w("")

    # ---- dials ------------------------------------------------------------
    newest = runs[0]["run"] or {}
    if newest.get("dials"):
        w("## Calibration dials (as of the newest run)")
        w("")
        w("Tune one of these against an observation before rewording a prompt —")
        w("a dial change is local, a prompt change moves editorial judgement.")
        w("")
        w(_table(["dial", "value"], [[k, str(v)] for k, v in sorted(newest["dials"].items())]))
        w("")

    # ---- where everything lives -------------------------------------------
    w("## Artifact index")
    w("")
    w("Per captured day, under `debug/<date>/`:")
    w("")
    w("| path | what it holds |")
    w("|------|---------------|")
    w("| `run.json` | outcome, stage timings, dial values, git sha, env |")
    w("| `funnel.json` | the counts in the table above |")
    w("| `trace.jsonl` | ordered timeline: every log line and structured event |")
    w("| `feeds/index.json` | per-feed health, plus every article dropped and why |")
    w("| `feeds/*.xml` | raw RSS bytes as served |")
    w("| `pool.json` | the exact article pool the select prompt saw, with its indices |")
    w("| `wikipedia/` | raw wikitext, parsed events, coverage misses |")
    w("| `rank.json` | every cluster considered, its weight arithmetic and verdict |")
    w("| `extract/index.json` | per-article scrape: status, per-strategy yields, winner |")
    w("| `extract/*.html` | the raw page fetched |")
    w("| `extract/*.txt` | the text actually handed to the claims pass |")
    w("| `extract/*.jina.txt` | the reader-proxy body, when it was used |")
    w("| `llm/*.prompt.txt` | the full prompt sent |")
    w("| `llm/*.response.json` | the raw model response |")
    w("| `llm/*.meta.json` | model, latency, tokens, finish reason, retries |")
    w("| `claims.json` | claims per cluster, with the source kind behind each |")
    w("| `anchor/index.json` | every story judged, its metrics against each threshold |")
    w("| `anchor/dropped-*.json` | full context for a story that was thrown away |")
    w("| `render.json` | pages written and their byte sizes |")
    w("| `follow/` | grounded prompts, responses, resolved redirect URLs |")
    w("| `dossier/<issue>/index.json` | a followed story's research: rounds, calls, ledger size, entity sides, gap firings |")
    w("| `dossier/<issue>/discarded-questions.json` | every question the drift guards cut, and which guard cut it |")
    w("")
    w("A `follow` run against the same day folds `-follow` into every file name")
    w("it writes (`run-follow.json`, `extract/index-follow.json`, ...), so the two")
    w("runs can never overwrite each other. Both append to `trace.jsonl`.")
    w("")
    w("Published output for the same days is in `data/<date>.json`; the site is")
    w("`docs/`, regenerated wholesale on every render.")
    w("")
    w(f"Only the newest {RETAIN_DAYS} captured day(s) are kept in the working tree "
      "(`digest.py prune-debug`, run by the commit step); anything older is in git "
      "history only.")
    w("")

    text = "\n".join(out)
    written = None
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        written = debug_dir / "ANALYSIS.md"
        written.write_text(text)
    except OSError:
        written = None
    return written, text
