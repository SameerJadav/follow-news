"""report.py reads only committed data/*.json, so these tests build synthetic
day files in a tmp_path rather than depending on real digest output."""

from __future__ import annotations

import json

import report


def _day(date: str, outlets: dict[str, int], degraded: bool = False, stories: list[dict] | None = None) -> dict:
    """outlets: {name: usable_count}. usable_count == 0 means silent that day."""
    configured = len(outlets)
    live = sum(1 for u in outlets.values() if u > 0)
    return {
        "date": date,
        "date_label": date,
        "generated_at": f"{date}T01:00:00Z",
        "stories": stories or [],
        "health": {
            "configured": configured,
            "live": live,
            "degraded": degraded,
            "articles": sum(outlets.values()),
            "feeds": [
                {"outlet": name, "http": 200, "raw_items": u, "usable": u, "error": "" if u else "zero items (HTTP 200)"}
                for name, u in outlets.items()
            ],
        },
    }


def test_feed_dead_after_three_silent_days(tmp_path):
    dates = ["2026-07-21", "2026-07-22", "2026-07-23"]
    for d in dates:
        (tmp_path / f"{d}.json").write_text(json.dumps(_day(d, {"NDTV": 0, "BBC": 5})))

    _, warnings, ok = report.feed_health(tmp_path)
    assert ok is False
    assert any("NDTV" in w and "DEAD" in w for w in warnings)


def test_one_silent_day_is_not_dead(tmp_path):
    (tmp_path / "2026-07-21.json").write_text(json.dumps(_day("2026-07-21", {"NDTV": 5, "BBC": 5})))
    (tmp_path / "2026-07-22.json").write_text(json.dumps(_day("2026-07-22", {"NDTV": 0, "BBC": 5})))

    _, warnings, ok = report.feed_health(tmp_path)
    assert ok is True
    assert any("silent today" in w for w in warnings)
    assert not any("DEAD" in w for w in warnings)


def test_degraded_day_is_reported(tmp_path):
    (tmp_path / "2026-07-25.json").write_text(
        json.dumps(_day("2026-07-25", {"A": 1, "B": 0, "C": 0}, degraded=True))
    )
    _, warnings, ok = report.feed_health(tmp_path)
    assert ok is False
    assert any("live" in w for w in warnings)


def test_retired_feed_is_dropped_from_the_check(tmp_path):
    """Retiring a dead feed from feeds.txt must clear the alarm the same day.

    The carry-forward in feed_health keeps an outlet in the table once seen,
    so without the `configured` filter a feed removed from feeds.txt would
    stay DEAD for HISTORY_DAYS more days while it ages out of the window --
    which is what kept the health job red for 23 days after Indian Express
    started 403ing from CI (calibration.md 2026-08-23)."""
    dates = ["2026-08-21", "2026-08-22", "2026-08-23"]
    for d in dates:
        (tmp_path / f"{d}.json").write_text(json.dumps(_day(d, {"Indian Express": 0, "BBC": 5})))

    # Still configured: the alarm fires, which is the behaviour being kept.
    _, warnings, ok = report.feed_health(tmp_path, {"Indian Express", "BBC"})
    assert ok is False
    assert any("Indian Express" in w and "DEAD" in w for w in warnings)

    # Retired: gone from the warnings and from the table, immediately.
    table, warnings, ok = report.feed_health(tmp_path, {"BBC"})
    assert ok is True
    assert warnings == []
    assert "Indian Express" not in table
    assert "BBC" in table


def test_configured_feed_missing_from_a_day_still_counts_as_dead(tmp_path):
    """The `configured` filter must only ever remove retired outlets. A feed
    still in feeds.txt that stops appearing in health at all -- rather than
    failing loudly with a status code -- is exactly the silent decay this
    report exists to catch, so it stays dead."""
    (tmp_path / "2026-08-21.json").write_text(json.dumps(_day("2026-08-21", {"NDTV": 5, "BBC": 5})))
    for d in ["2026-08-22", "2026-08-23", "2026-08-24"]:
        (tmp_path / f"{d}.json").write_text(json.dumps(_day(d, {"BBC": 5})))  # NDTV absent entirely

    _, warnings, ok = report.feed_health(tmp_path, {"NDTV", "BBC"})
    assert ok is False
    assert any("NDTV" in w and "DEAD" in w for w in warnings)


def test_no_configured_feed_in_window_is_quiet(tmp_path):
    """Renaming every outlet at once shouldn't crash or alarm -- there is
    simply nothing to say until the new roster has history."""
    (tmp_path / "2026-08-23.json").write_text(json.dumps(_day("2026-08-23", {"Old Name": 0})))
    table, warnings, ok = report.feed_health(tmp_path, {"New Name"})
    assert ok is True
    assert warnings == []
    assert "no configured feed" in table


def test_days_without_health_are_ignored(tmp_path):
    """Pre-Phase-6 data/ files carry no "health" key at all -- the decay
    check must stay quiet rather than crash on them."""
    old = {"date": "2026-07-01", "date_label": "x", "generated_at": "2026-07-01T01:00:00Z", "stories": []}
    (tmp_path / "2026-07-01.json").write_text(json.dumps(old))

    report_text, warnings, ok = report.feed_health(tmp_path)
    assert ok is True
    assert warnings == []
    assert "no digest carries feed health" in report_text


def test_morning_review_mentions_every_story(tmp_path):
    stories = [
        {
            "section": "world",
            "headline": "Big Thing Happens",
            "thin_sourced": False,
            "vocab": [{"term": "sanctions", "say": "SANK-shunz", "meaning": "x"}],
            "signals": {
                "tier": "lead",
                "weight": 12,
                "claim_outlets": 5,
                "word_count": 480,
                "word_target": 500,
                "unanchored_share": 0.01,
                "dropped_markers": 0,
                "unsourced_figures": [],
            },
        },
        {
            "section": "india",
            "headline": "Smaller Story Occurs",
            "thin_sourced": True,
            "vocab": [],
            "signals": {
                "tier": "notable",
                "weight": 6,
                "claim_outlets": 1,
                "word_count": 190,
                "word_target": 200,
                "unanchored_share": 0.05,
                "dropped_markers": 1,
                "unsourced_figures": [],
            },
        },
    ]
    day = _day("2026-07-25", {"BBC": 5}, stories=stories)
    (tmp_path / "2026-07-25.json").write_text(json.dumps(day))

    out = report.morning_review(tmp_path)
    assert "Big Thing Happens" in out
    assert "Smaller Story Occurs" in out
    assert "Smaller Story Occurs" in out  # thin-sourced story still surfaced
    assert "world" in out and "india" in out


def _capture(debug_dir, day: str, *, ok: bool = True, follow_only: bool = False) -> None:
    """A minimal captured run directory, the shape tracer.finish() writes."""
    d = debug_dir / day
    d.mkdir(parents=True)
    name = "run-follow.json" if follow_only else "run.json"
    (d / name).write_text(json.dumps({"kind": "follow" if follow_only else "digest",
                                      "day": day, "ok": ok, "elapsed_ms": 1000}))
    (d / "funnel.json").write_text(json.dumps({"stories_published": 3}))
    (d / "trace.jsonl").write_text('{"kind":"dbg","msg":"x"}\n')
    (d / "payload.txt").write_text("x" * 2048)


def test_prune_debug_keeps_the_newest_and_says_what_it_removed(tmp_path):
    """H5: nothing bounded debug/ across days, and it became 89% of the objects
    in a checkout the three daily crons each pay for."""
    debug = tmp_path / "debug"
    for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
        _capture(debug, day)
    (debug / "README.md").write_text("not a capture")
    (debug / "ANALYSIS.md").write_text("generated")

    removed, freed = report.prune_debug(debug, keep_days=2)

    assert removed == ["2026-08-01", "2026-08-02"]
    assert freed > 0
    assert sorted(p.name for p in debug.iterdir()) == [
        "2026-08-03", "2026-08-04", "ANALYSIS.md", "README.md"
    ]


def test_prune_debug_never_empties_the_directory(tmp_path):
    debug = tmp_path / "debug"
    _capture(debug, "2026-08-01")
    _capture(debug, "2026-08-02")
    removed, _freed = report.prune_debug(debug, keep_days=0)
    assert removed == ["2026-08-01"]
    assert (debug / "2026-08-02").is_dir()


def test_prune_debug_on_a_missing_directory_is_a_noop(tmp_path):
    assert report.prune_debug(tmp_path / "nope") == ([], 0)


def test_bundle_says_how_many_days_it_left_out(tmp_path):
    """M4: the bundle applied a 7-day window and reported the truncated count
    as if it were the total, hiding every failed day at the default. CLAUDE.md
    §No silent caps."""
    debug = tmp_path / "debug"
    data = tmp_path / "data"
    data.mkdir()
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        _capture(debug, day)

    _path, text = report.debug_bundle(debug, data, days=2)
    assert "Showing 2 of 3 captured run(s)" in text
    assert "1 older captured day(s) are NOT in this bundle" in text
    assert "--days 3" in text

    _path, text = report.debug_bundle(debug, data, days=10)
    assert "Showing 3 of 3 captured run(s)" in text
    assert "NOT in this bundle" not in text


def test_bundle_distinguishes_a_follow_only_day_from_a_dead_process(tmp_path):
    """F14: 2026-07-27 read "the process died before it could finish writing"
    for a month. It had published six stories; only a follow run was captured."""
    debug = tmp_path / "debug"
    data = tmp_path / "data"
    data.mkdir()
    _capture(debug, "2026-07-27", follow_only=True)
    (data / "2026-07-27.json").write_text(json.dumps({"stories": [{"headline": "h"}] * 6}))

    _path, text = report.debug_bundle(debug, data)
    assert "No digest run was captured this day" in text
    assert "the process died" not in text
    assert "follow only" in text
