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
from pathlib import Path

import rank

DEAD_DAYS = 3  # consecutive silent days before a feed is called dead
HISTORY_DAYS = 7


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


def feed_health(data_dir: Path) -> tuple[str, list[str], bool]:
    """(markdown_table, warnings, ok). `ok` is False only for a decay pattern
    worth waking someone up for — a feed dead DEAD_DAYS+ running, or today's
    run itself degraded. A single quiet day for an otherwise-live feed is not
    decay and does not clear `ok`."""
    days = load_days(data_dir, HISTORY_DAYS)
    if not days:
        return "(no digest carries feed health data yet)", [], True

    outlets: dict[str, list[bool]] = {}  # outlet -> [live?, ...] oldest to newest
    for day in reversed(days):  # oldest first, so index -1 is always "today"
        live_today = {f["outlet"] for f in day.get("health", {}).get("feeds", []) if f.get("usable", 0) > 0}
        seen_today = {f["outlet"] for f in day.get("health", {}).get("feeds", [])}
        for outlet in seen_today | outlets.keys():
            outlets.setdefault(outlet, []).append(outlet in live_today)

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
