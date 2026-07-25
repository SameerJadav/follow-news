"""Tests for the fragile edges in follow.py: parsing an attacker-controlled
issue body into a story reference, resolving that reference against data/,
the security boundary that filters GitHub issues to the repo owner, the
14-day closing predicate, and the guarantee that a backstory is never
regenerated once a follow exists. No network call and no Gemini call is
ever made here — requests.get/patch/post and ground.research are always
monkeypatched out.
"""

from __future__ import annotations

import json
from datetime import date

import follow


# ---------- parse_request ----------


def test_parse_request_well_formed():
    body = (
        "Follow this story.\n\n"
        "digest: 2026-07-25\n"
        "section: world\n"
        "story: 1\n"
        "headline: US and Iran Exchange Military Strikes"
    )
    r = follow.parse_request(body)
    assert r == {
        "digest": "2026-07-25",
        "section": "world",
        "story": 1,
        "headline": "US and Iran Exchange Military Strikes",
    }


def test_parse_request_tolerates_surrounding_prose():
    body = (
        "Hey, following this one!\n\n"
        "digest:2026-07-25\n"
        "  section :  india \n"
        "story:3\n"
        "headline: Something big happened\n\n"
        "Thanks!"
    )
    r = follow.parse_request(body)
    assert r is not None
    assert r["digest"] == "2026-07-25"
    assert r["section"] == "india"
    assert r["story"] == 3


def test_parse_request_missing_digest_is_rejected():
    body = "section: world\nstory: 1\nheadline: Something"
    assert follow.parse_request(body) is None


def test_parse_request_malformed_date_is_rejected():
    body = "digest: 25-07-2026\nsection: world\nstory: 1\nheadline: Something"
    assert follow.parse_request(body) is None


def test_parse_request_headline_only_is_accepted():
    body = "digest: 2026-07-25\nheadline: US and Iran Exchange Military Strikes"
    r = follow.parse_request(body)
    assert r is not None
    assert r["section"] == ""
    assert r["story"] is None


def test_parse_request_with_neither_position_nor_headline_is_rejected():
    body = "digest: 2026-07-25\nsection: world"
    assert follow.parse_request(body) is None


# ---------- resolve ----------


def _write_day(data_dir, day_date, stories):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{day_date}.json").write_text(
        json.dumps({"date": day_date, "stories": stories}, ensure_ascii=False)
    )


def test_resolve_by_section_and_position(tmp_path):
    _write_day(
        tmp_path,
        "2026-07-25",
        [
            {"section": "world", "headline": "First world story"},
            {"section": "world", "headline": "Second world story"},
            {"section": "india", "headline": "First india story"},
        ],
    )
    request = {"digest": "2026-07-25", "section": "world", "story": 2, "headline": ""}
    resolved = follow.resolve(request, tmp_path)
    assert resolved == {
        "date": "2026-07-25",
        "section": "world",
        "position": 2,
        "headline": "Second world story",
    }


def test_resolve_falls_back_to_headline_scan_when_position_shifted(tmp_path):
    # The story the reader followed was #1 in World when they tapped Follow,
    # but has since shifted to #2 by the time the issue is processed.
    _write_day(
        tmp_path,
        "2026-07-25",
        [
            {"section": "world", "headline": "A bigger story bumped it"},
            {"section": "world", "headline": "The story they actually followed"},
        ],
    )
    request = {
        "digest": "2026-07-25",
        "section": "world",
        "story": 1,
        "headline": "The story they actually followed",
    }
    resolved = follow.resolve(request, tmp_path)
    assert resolved is not None
    assert resolved["position"] == 2
    assert resolved["headline"] == "The story they actually followed"


def test_resolve_returns_none_when_digest_file_missing(tmp_path):
    request = {"digest": "2026-07-25", "section": "world", "story": 1, "headline": ""}
    assert follow.resolve(request, tmp_path) is None


def test_resolve_returns_none_when_nothing_matches(tmp_path):
    _write_day(tmp_path, "2026-07-25", [{"section": "world", "headline": "Something else entirely"}])
    request = {"digest": "2026-07-25", "section": "world", "story": 1, "headline": "Not this one"}
    assert follow.resolve(request, tmp_path) is None


# ---------- the owner-only security boundary ----------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_non_owner_issues_and_pull_requests_are_ignored(monkeypatch):
    payload = [
        {"number": 1, "user": {"login": follow.OWNER}, "state": "open", "body": "digest: 2026-07-25\nheadline: x"},
        {"number": 2, "user": {"login": "someone-else"}, "state": "open", "body": "digest: 2026-07-25\nheadline: x"},
        {"number": 3, "user": {"login": follow.OWNER}, "state": "open", "pull_request": {}, "body": ""},
    ]
    monkeypatch.setattr(follow.requests, "get", lambda *a, **kw: _FakeResponse(payload))

    issues = follow.fetch_issues()
    assert [i["number"] for i in issues] == [1]


def test_fetch_issues_returns_empty_on_failure(monkeypatch):
    def raise_get(*a, **kw):
        raise follow.requests.RequestException("network down")

    monkeypatch.setattr(follow.requests, "get", raise_get)
    assert follow.fetch_issues() == []


# ---------- a fresh follow skips a same-day timeline pass ----------


def test_a_follow_created_today_is_not_due_for_a_timeline_pass():
    # Verified live: this is exactly the wasted call the first real Follow
    # run made before _needs_timeline_pass existed.
    assert follow._needs_timeline_pass("2026-07-25", date(2026, 7, 25)) is False


def test_a_follow_from_an_earlier_day_is_due():
    assert follow._needs_timeline_pass("2026-07-24", date(2026, 7, 25)) is True


# ---------- the 14-day closing predicate ----------


def test_thirteen_quiet_days_stays_active():
    last_development = "2026-07-25"
    today = date(2026, 8, 7)  # 13 days later
    assert follow._is_closing(last_development, today) is False


def test_fourteen_quiet_days_closes():
    last_development = "2026-07-25"
    today = date(2026, 8, 8)  # 14 days later
    assert follow._is_closing(last_development, today) is True


# ---------- backstory is generated exactly once ----------


def test_backstory_is_never_regenerated_for_an_existing_follow(monkeypatch, tmp_path):
    existing_backstory = {"body": "Already researched.", "markers": [], "sources": [], "queries": [], "search_suggestions": ""}
    records = {
        7: {
            "issue": 7,
            "status": "active",
            "title": "Some story",
            "section": "world",
            "origin": {"date": "2026-07-20", "section": "world", "position": 1, "headline": "Some story"},
            "started_at": "2026-07-20T00:00:00Z",
            "closed_at": None,
            "close_reason": None,
            "last_development": "2026-07-20",
            "backstory": existing_backstory,
            "timeline": [],
        }
    }
    issues = [{"number": 7, "user": {"login": follow.OWNER}, "state": "open", "body": "digest: 2026-07-20\nheadline: Some story"}]

    def explode(*a, **kw):
        raise AssertionError("ground.research must not be called for an issue that already has a record")

    monkeypatch.setattr(follow.ground, "research", explode)

    follow._new_follows(records, issues, tmp_path)

    assert records[7]["backstory"] == existing_backstory
