"""Tests for the fragile edges in follow.py: parsing an attacker-controlled
issue body into a story reference, resolving that reference against data/,
the security boundary that filters GitHub issues to the repo owner, the
14-day closing predicate, the followed/<issue>/ storage layout and its legacy
fallback, and the guarantee that RESEARCH is never re-run once a follow
exists (prose, by contrast, is regenerable — dossier.md §11).

No network call and no Gemini call is ever made here — requests.get/patch/post
and dossier's research entry points are always monkeypatched out.
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


# ---------- research happens once; prose is regenerable ----------


def _record(issue: int = 7, **over) -> dict:
    record = {
        "issue": issue,
        "status": "active",
        "title": "Some story",
        "section": "world",
        "origin": {"date": "2026-07-20", "section": "world", "position": 1, "headline": "Some story"},
        "started_at": "2026-07-20T00:00:00Z",
        "closed_at": None,
        "close_reason": None,
        "last_development": "2026-07-20",
        "backstory": {"body": "Already researched.", "markers": [], "sources": [],
                      "queries": [], "search_suggestions": ""},
        "timeline": [],
    }
    record.update(over)
    return record


def test_research_is_not_reseeded_for_an_issue_that_already_has_a_record(monkeypatch, tmp_path):
    """Supersedes the old "backstory is never regenerated" guarantee.

    dossier.md §11 makes PROSE regenerable — it costs one call over an
    append-only ledger. What must never happen twice is the RESEARCH: Pass A
    reseeding an existing follow would throw away its ledger and re-spend a
    whole burst of calls."""
    records = {7: _record()}
    issues = [{"number": 7, "user": {"login": follow.OWNER}, "state": "open",
               "body": "digest: 2026-07-20\nheadline: Some story"}]

    def explode(*a, **kw):
        raise AssertionError("dossier.seed must not run for an issue that already has a record")

    monkeypatch.setattr(follow.dossier, "seed", explode)

    follow._new_follows(records, issues, tmp_path, tmp_path)

    assert records[7]["backstory"]["body"] == "Already researched."


def test_a_closed_follow_is_never_researched_again(monkeypatch, tmp_path):
    """Closing the issue is the owner's kill switch. It has to stop the
    research loop too, not just the old cheap timeline call — an unfollowed
    story with half a frontier left must not keep spending quota."""
    import dossier

    records = {7: _record(status="closed", close_reason="unfollowed")}
    dsr = dossier.new_dossier(7, "Some story")
    dsr["research_state"] = "researching"
    dossier.save(tmp_path, 7, dsr, {}, "C")

    def explode(*a, **kw):
        raise AssertionError("a closed follow must never resume research")

    monkeypatch.setattr(follow.dossier, "research", explode)

    budget = dossier.Budget(tmp_path / "_budget" / "2026-07-27.json")
    assert follow._sweep(records, tmp_path, date(2026, 7, 27), budget) == []


# ---------- the daily update: capped, quiet, and closing ----------
#
# All three regressions here were live on 2026-07-31 and cost follow #3 three
# no-op runs a day while its page claimed to be a finished, current story.


def _updatable(tmp_path, monkeypatch, issue: int = 7):
    """An already-researched follow, ready for a daily update pass. Every path
    out of Gemini and GitHub is stubbed; only follow.py's own logic runs."""
    import dossier

    dsr = dossier.new_dossier(issue, "Some story")
    dsr["research_state"] = "complete"
    dsr["rounds"] = 4
    dossier.save(tmp_path, issue, dsr, {}, "DONE")
    closed: list[int] = []
    monkeypatch.setattr(follow, "close_issue", lambda n, *a, **kw: closed.append(n))
    return dsr, closed


def test_a_capped_dossier_is_never_relabelled_complete(tmp_path, monkeypatch):
    """CLAUDE.md: "A capped dossier reports itself as capped", and "no silent
    caps". research() writes `capped` to disk and _update_follow used to
    discard the return value and overwrite it two lines later — which is how
    follow #3 rendered "The full picture" with 631 questions still open."""
    import dossier

    dsr, _closed = _updatable(tmp_path, monkeypatch)
    record = _record(7, last_development="2026-07-30")
    monkeypatch.setattr(follow.dossier, "research", lambda *a, **kw: "capped")

    budget = dossier.Budget(tmp_path / "_budget" / "2026-07-31.json")
    follow._update_follow(tmp_path, 7, record, dsr, {}, date(2026, 7, 31), budget)

    assert dsr["research_state"] == "capped"
    assert record["status"] == "active"  # capped, but only one day quiet


def test_a_capped_dossier_does_not_research_again(tmp_path, monkeypatch):
    """Out of lifetime budget means there is nothing to ask. Asking anyway
    admitted a delta question a day that could never be popped."""
    import dossier

    dsr, _closed = _updatable(tmp_path, monkeypatch)
    dsr["research_state"] = "capped"

    def explode(*a, **kw):
        raise AssertionError("a capped dossier must not start another round")

    monkeypatch.setattr(follow.dossier, "research", explode)

    budget = dossier.Budget(tmp_path / "_budget" / "2026-07-31.json")
    follow._update_follow(tmp_path, 7, _record(7), dsr, {}, date(2026, 7, 31), budget)
    assert dsr["research_state"] == "capped"


def test_a_quiet_fortnight_closes_the_follow(tmp_path, monkeypatch):
    """The stale check used to sit AFTER the quiet-path return, so a story that
    went genuinely silent returned early every day and never closed."""
    import dossier

    dsr, closed = _updatable(tmp_path, monkeypatch)
    record = _record(7, last_development="2026-07-20", started_at="2026-07-20T00:00:00Z",
                     timeline=[{"date": "2026-07-20", "kind": "development", "body": "x"}])
    monkeypatch.setattr(follow.dossier, "research", lambda *a, **kw: "complete")

    budget = dossier.Budget(tmp_path / "_budget" / "2026-08-03.json")
    follow._update_follow(tmp_path, 7, record, dsr, {}, date(2026, 8, 3), budget)

    assert record["status"] == "closed"
    assert record["close_reason"] == "no_development"
    assert closed == [7]
    # No prose was written today, so no entry is appended; the last real
    # update becomes the final one.
    assert len(record["timeline"]) == 1
    assert record["timeline"][-1]["kind"] == "final"


def test_news_after_a_long_silence_does_not_close_the_follow(tmp_path, monkeypatch):
    """The inverted half of the same bug: a fortnight of silence followed by a
    real development used to close the follow and label that development
    "final". A development is proof the story is alive."""
    import dossier

    dsr, closed = _updatable(tmp_path, monkeypatch)
    record = _record(7, last_development="2026-07-20", started_at="2026-07-20T00:00:00Z")

    def research(followed_dir, n, d, corpus, budget):
        d["ledger"].append({"id": 1, "date": "2026-08-03", "text": "Something happened"})
        return "complete"

    monkeypatch.setattr(follow.dossier, "research", research)
    monkeypatch.setattr(
        follow.dossier, "write_update",
        lambda *a, **kw: {"body": "An update.", "markers": [], "sources": []},
    )

    budget = dossier.Budget(tmp_path / "_budget" / "2026-08-03.json")
    follow._update_follow(tmp_path, 7, record, dsr, {}, date(2026, 8, 3), budget)

    assert record["status"] == "active"
    assert closed == []
    assert record["timeline"][-1]["kind"] == "development"
    assert record["last_development"] == "2026-08-03"


def test_a_follow_started_from_an_archived_digest_is_not_born_stale(tmp_path, monkeypatch):
    """last_development starts as the ORIGIN DIGEST's date. Following a story
    off an archive page a month old must not close it on day one."""
    import dossier

    dsr, closed = _updatable(tmp_path, monkeypatch)
    record = _record(7, last_development="2026-06-01", started_at="2026-07-31T00:00:00Z")
    monkeypatch.setattr(follow.dossier, "research", lambda *a, **kw: "complete")

    budget = dossier.Budget(tmp_path / "_budget" / "2026-08-01.json")
    follow._update_follow(tmp_path, 7, record, dsr, {}, date(2026, 8, 1), budget)

    assert record["status"] == "active"
    assert closed == []


# ---------- storage: the directory layout and its legacy fallback ----------


def test_load_all_reads_a_new_style_directory_record(tmp_path):
    d = tmp_path / "7"
    d.mkdir()
    (d / "record.json").write_text(json.dumps(_record(7)))

    records = follow.load_all(tmp_path)
    assert set(records) == {7}
    assert records[7]["title"] == "Some story"


def test_load_all_still_reads_a_legacy_flat_record(tmp_path):
    """Follows made before dossiers existed must keep rendering."""
    (tmp_path / "7.json").write_text(json.dumps(_record(7)))
    assert set(follow.load_all(tmp_path)) == {7}


def test_the_directory_wins_when_both_exist(tmp_path):
    (tmp_path / "7.json").write_text(json.dumps(_record(7, title="stale")))
    d = tmp_path / "7"
    d.mkdir()
    (d / "record.json").write_text(json.dumps(_record(7, title="current")))

    assert follow.load_all(tmp_path)[7]["title"] == "current"


def test_a_double_digit_issue_is_not_lost_to_string_sorting(tmp_path):
    """`glob("*.json")` is non-recursive and would miss every new-style record
    entirely, which reads as "this follow has no record" and reseeds its
    research from scratch on every run."""
    for n in (2, 10):
        d = tmp_path / str(n)
        d.mkdir()
        (d / "record.json").write_text(json.dumps(_record(n)))

    assert set(follow.load_all(tmp_path)) == {2, 10}


def test_writing_a_record_migrates_the_legacy_file_into_its_directory(tmp_path):
    (tmp_path / "7.json").write_text(json.dumps(_record(7)))

    follow._write_record(tmp_path, _record(7, title="updated"))

    assert (tmp_path / "7" / "record.json").exists()
    assert not (tmp_path / "7.json").exists(), "the legacy file must not linger and shadow the directory"
    assert follow.load_all(tmp_path)[7]["title"] == "updated"


def test_a_brand_new_record_goes_straight_into_the_directory_layout(tmp_path):
    follow._write_record(tmp_path, _record(11))
    assert (tmp_path / "11" / "record.json").exists()
    assert not (tmp_path / "11.json").exists()


def test_a_new_follow_gets_its_dossier_in_the_same_step_as_its_record(tmp_path, monkeypatch):
    """render.py reads a record with no dossier.json beside it as a legacy
    one-shot follow whose prose is finished. A window where record.json exists
    and dossier.json does not would publish an empty page as a complete one."""
    import dossier

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "2026-07-27.json").write_text(json.dumps({
        "stories": [{"section": "india", "headline": "A story", "claims": [], "sources": []}]
    }))
    followed_dir = tmp_path / "followed"

    monkeypatch.setattr(follow, "comment", lambda *a, **kw: None)
    monkeypatch.setattr(dossier.extract, "article_text", lambda url: "")

    records = {}
    issues = [{"number": 3, "user": {"login": follow.OWNER}, "state": "open",
               "body": "digest: 2026-07-27\nsection: india\nstory: 1\nheadline: A story"}]
    follow._new_follows(records, issues, data_dir, followed_dir)

    assert (followed_dir / "3" / "record.json").exists()
    assert (followed_dir / "3" / "dossier.json").exists()
    assert json.loads((followed_dir / "3" / "dossier.json").read_text())["research_state"] == "pending"


def test_only_one_new_follow_is_started_per_run(tmp_path, monkeypatch):
    """dossier.md §14: a new follow now costs a burst of research calls, so a
    burst of requests must not stack bursts of research in one morning."""
    assert follow.MAX_NEW_FOLLOWS_PER_RUN == 1

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "2026-07-27.json").write_text(json.dumps({
        "stories": [{"section": "india", "headline": "A story", "claims": [], "sources": []}]
    }))
    monkeypatch.setattr(follow, "comment", lambda *a, **kw: None)
    monkeypatch.setattr(follow, "close_issue", lambda *a, **kw: None)
    monkeypatch.setattr(follow.dossier.extract, "article_text", lambda url: "")

    body = "digest: 2026-07-27\nsection: india\nstory: 1\nheadline: A story"
    issues = [{"number": n, "user": {"login": follow.OWNER}, "state": "open", "body": body}
              for n in (3, 4, 5)]
    records = {}
    follow._new_follows(records, issues, data_dir, tmp_path / "followed")

    assert len(records) == 1
