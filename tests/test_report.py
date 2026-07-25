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
