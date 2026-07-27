"""Tests for debug capture.

The first group is the one that matters most: with DIGEST_DEBUG off, this
whole subsystem must be inert. It ships switched on for the calibration
window and is expected to be switched off afterwards, and "off" has to mean
nothing is written, nothing is created, and no call site raises.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

import tracer

DAY = date(2026, 7, 27)


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """Capture enabled, writing into a tmp dir. Always restores the module
    globals, so one test can't leak an open run into the next."""
    monkeypatch.setattr(tracer, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(tracer, "LEVEL", 1)
    monkeypatch.setattr(tracer, "_RUN", None)
    yield tmp_path / "debug" / f"{DAY:%Y-%m-%d}"
    tracer.finish(True)
    monkeypatch.setattr(tracer, "_RUN", None)


@pytest.fixture
def off(tmp_path, monkeypatch):
    monkeypatch.setattr(tracer, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(tracer, "LEVEL", 0)
    monkeypatch.setattr(tracer, "_RUN", None)
    return tmp_path / "debug"


# --- off is genuinely off ---------------------------------------------------


def test_disabled_writes_nothing(off):
    tracer.start("digest", DAY)
    tracer.event("feeds", outlet="BBC", http=200)
    tracer.count(articles=12)
    tracer.artifact("extract/1.html", "<html></html>")
    tracer.artifact_json("rank.json", {"a": 1})
    tracer.extra("window", {"since": "x"})
    with tracer.stage("gather"):
        pass
    tracer.finish(True)

    assert not off.exists(), "debug/ must not be created when capture is off"


def test_disabled_artifact_returns_none(off):
    assert tracer.artifact("x.txt", "hi") is None
    assert tracer.artifact_json("x.json", {}) is None


def test_disabled_dbg_still_prints(off, capsys):
    """dbg() is unconditional — it predates this module and the Actions log
    is still the first place anyone looks."""
    tracer.dbg("hello")
    assert "hello" in capsys.readouterr().err


def test_level_from_env(monkeypatch):
    for raw, expected in [("", 0), ("0", 0), ("off", 0), ("false", 0),
                          ("1", 1), ("on", 1), ("true", 1), ("2", 2), ("nonsense", 1)]:
        monkeypatch.setenv("DIGEST_DEBUG", raw)
        assert tracer._level_from_env() == expected, raw
    monkeypatch.delenv("DIGEST_DEBUG")
    assert tracer._level_from_env() == 0


def test_configure_overrides_env(monkeypatch):
    monkeypatch.setattr(tracer, "LEVEL", 1)
    tracer.configure(0)
    assert not tracer.enabled()
    tracer.configure(1)
    assert tracer.enabled()
    tracer.configure(None)  # None means "leave it alone"
    assert tracer.enabled()


# --- on, it captures --------------------------------------------------------


def test_run_writes_run_and_funnel(capture):
    tracer.start("digest", DAY)
    tracer.count(articles_fetched=42)
    tracer.extra("stopped_at", "quorum")
    tracer.finish(False)

    run = json.loads((capture / "run.json").read_text())
    assert run["ok"] is False
    assert run["kind"] == "digest"
    assert run["day"] == "2026-07-27"
    assert run["stopped_at"] == "quorum"
    assert json.loads((capture / "funnel.json").read_text()) == {"articles_fetched": 42}


def test_trace_is_valid_jsonl(capture):
    tracer.start("digest", DAY)
    tracer.dbg("a log line")
    tracer.event("feeds", outlet="BBC", http=200)
    tracer.finish(True)

    lines = [json.loads(x) for x in (capture / "trace.jsonl").read_text().splitlines()]
    assert all("run" in line and "t" in line for line in lines)
    assert any(line.get("kind") == "dbg" and line["msg"] == "a log line" for line in lines)
    assert any(line.get("stage") == "feeds" and line["http"] == 200 for line in lines)


def test_artifacts_land_at_their_relpath(capture):
    tracer.start("digest", DAY)
    assert tracer.artifact("extract/001-bbc.html", "<html>hi</html>") == "extract/001-bbc.html"
    tracer.artifact_json("rank.json", {"clusters": []})
    tracer.finish(True)

    assert (capture / "extract" / "001-bbc.html").read_text() == "<html>hi</html>"
    assert json.loads((capture / "rank.json").read_text()) == {"clusters": []}


def test_stage_records_timing_and_reraises(capture):
    tracer.start("digest", DAY)
    with pytest.raises(ValueError):
        with tracer.stage("select"):
            raise ValueError("boom")
    tracer.finish(False)

    stages = json.loads((capture / "run.json").read_text())["stages"]
    assert stages[0]["stage"] == "select"
    assert "boom" in stages[0]["error"]


def test_follow_run_does_not_clobber_digest_run(capture):
    """digest.yml runs `digest.py follow` as a separate process against the
    same day; its evidence must not overwrite the digest's."""
    tracer.start("digest", DAY)
    tracer.count(stories_published=6)
    tracer.finish(True)
    tracer._RUN = None

    tracer.start("follow", DAY)
    tracer.count(follow_records=2)
    tracer.finish(True)

    assert json.loads((capture / "funnel.json").read_text()) == {"stories_published": 6}
    assert json.loads((capture / "funnel-follow.json").read_text()) == {"follow_records": 2}
    # One shared, chronological timeline, each line tagged with its run.
    kinds = {json.loads(x)["run"] for x in (capture / "trace.jsonl").read_text().splitlines()}
    assert kinds == {"digest", "follow"}


def test_dials_are_recorded_from_imported_modules(capture):
    import rank  # noqa: F401 - importing is what makes its dials visible

    tracer.start("digest", DAY)
    tracer.finish(True)
    dials = json.loads((capture / "run.json").read_text())["dials"]
    assert dials["rank.WEIGHT_FLOOR"] == rank.WEIGHT_FLOOR


# --- secrets ----------------------------------------------------------------


def test_scrub_redacts_live_env_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "supersecretvalue123")
    monkeypatch.setenv("GITHUB_TOKEN", "anothersecret456789")
    out = tracer.scrub("key=supersecretvalue123 tok=anothersecret456789")
    assert "supersecretvalue123" not in out
    assert "anothersecret456789" not in out
    assert "[REDACTED:GEMINI_API_KEY]" in out


def test_scrub_redacts_key_shapes_not_in_env():
    """A key that arrived from somewhere we can't read — echoed back by a
    model, pasted into a prompt — must still never reach a public repo."""
    gemini = "AIza" + "B" * 35
    ghp = "ghp_" + "C" * 36
    out = tracer.scrub(f"{gemini} and {ghp}")
    assert gemini not in out and ghp not in out
    assert out.count("[REDACTED:key-shaped]") == 2


def test_artifacts_and_events_are_scrubbed(capture, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "supersecretvalue123")
    tracer.start("digest", DAY)
    tracer.artifact("llm/1-select.prompt.txt", "auth: supersecretvalue123")
    tracer.artifact_json("x.json", {"nested": ["supersecretvalue123"]})
    tracer.event("llm", detail="supersecretvalue123")
    tracer.finish(True)

    for path in capture.rglob("*"):
        if path.is_file():
            assert "supersecretvalue123" not in path.read_text(), path


# --- size guards ------------------------------------------------------------


def test_oversized_artifact_is_truncated_visibly(capture, monkeypatch):
    monkeypatch.setattr(tracer, "MAX_ARTIFACT_BYTES", 100)
    tracer.start("digest", DAY)
    tracer.artifact("big.txt", "x" * 5000)
    tracer.finish(True)

    body = (capture / "big.txt").read_text()
    assert "truncated at 100 bytes of 5000" in body, "a silent cut is worse than no capture"


def test_run_byte_cap_stops_writing_and_says_so(capture, monkeypatch):
    monkeypatch.setattr(tracer, "MAX_RUN_BYTES", 500)
    tracer.start("digest", DAY)
    tracer.artifact("a.txt", "x" * 400)
    assert tracer.artifact("b.txt", "y" * 400) is None
    tracer.finish(True)

    assert json.loads((capture / "run.json").read_text())["byte_cap_reached"] is True
    assert not (capture / "b.txt").exists()


def test_capture_never_raises_on_a_bad_path(capture):
    """A capture failure must never take the digest down with it."""
    tracer.start("digest", DAY)
    assert tracer.artifact("nope/\0bad", "x") is None  # NUL is invalid in a path
    tracer.finish(True)


def test_slug_is_filesystem_safe():
    assert tracer.slug("BBC World") == "bbc-world"
    assert tracer.slug("https://example.com/a/b?c=d") == "https-example-com-a-b-c-d"
    assert tracer.slug("!!!") == "unnamed"
