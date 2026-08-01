"""Tests for the machinery dossier.py runs on arithmetic rather than
judgement: the gap and entity-asymmetry detectors, ledger merging (including
the numeric-disagreement case that must NOT merge), the frontier's drift
guards, saturation, the call budget, checkpoint/resume, and the [eN] write
gate.

Nothing here makes a network call or a Gemini call — ground._generate,
ground.structured and extract.article_text are monkeypatched wherever a pass
is exercised. That is deliberate: these are the parts that decide what gets
researched and what gets published, so they have to be verifiable without a
key, the same way rank.py and anchor.py are.

The fixtures deliberately reproduce the 2026-07-27 exam-leak failure
(dossier.md §1), because that is the case the detectors exist for.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import dossier


# The failing run's implied ledger: May 3, May 12, June 6, nothing for seven
# weeks, then July 24-27.
def _entry(id_: int, day: str, what: str, outlet: str = "HT", url: str | None = None, **over) -> dict:
    entry = {
        "id": id_,
        "date": day,
        "precision": "day",
        "what": what,
        "actors": [],
        "sources": [{"outlet": outlet, "url": url or f"https://{outlet}.example/{id_}"}],
        "outlet_count": 1,
        "phase": None,
        "added_round": 1,
    }
    entry.update(over)
    return entry


def _exam_leak_ledger() -> list[dict]:
    return [
        _entry(1, "2026-05-03", "NEET-UG was held for 2.2 million students"),
        _entry(2, "2026-05-12", "The government cancelled the examination"),
        _entry(3, "2026-06-06", "The CBI opened an investigation into the leak"),
        _entry(4, "2026-07-24", "The education minister resigned over the leaks"),
        _entry(5, "2026-07-26", "Modi announced a task force led by Nandan Nilekani"),
        _entry(6, "2026-07-27", "Pralhad Joshi took charge of the education ministry"),
    ]


# ---------- the gap detector ----------


def test_gap_detector_finds_the_seven_week_hole():
    """The specific failure dossier.md §7 describes. Three events in May and
    June, then nothing for seven weeks in a rapidly escalating national story
    — which is not a quiet period, and catching it takes arithmetic."""
    ledger = _exam_leak_ledger()
    span = dossier.recompute_span(ledger)
    questions = dossier.gap_questions(ledger, span)

    assert questions, "a seven-week silence must raise gap questions"
    assert all(q["origin"] == "gap" for q in questions)
    # The hole runs mid-June to mid-July; at least one question must land in it.
    assert any("2026-06" in q["text"] or "2026-07" in q["text"] for q in questions)


def test_gap_detector_is_quiet_on_an_evenly_covered_story():
    ledger = [
        _entry(i, f"2026-07-{day:02d}", f"Something happened on day {day}")
        for i, day in enumerate(range(1, 29, 2), start=1)
    ]
    span = dossier.recompute_span(ledger)
    assert dossier.gap_questions(ledger, span) == []


def test_gap_detector_handles_an_empty_or_undated_ledger():
    assert dossier.gap_questions([], {"start": None, "end": None}) == []


def test_gap_detector_ignores_the_years_before_a_story_started():
    """Measured on follow #3 (2026-07-31): span 2015-05-01 to 2026-07-28 is 587
    weekly buckets with entries in 40 of them, and the detector raised 547 gap
    questions — 616 of the 631 left in its frontier were "what happened in this
    empty week of 2017". Rounds burned their calls there, `saturated()` could
    never fire, and research could only ever end on a ceiling.

    A decade of silence before the story's first precedent is not a hole in the
    record. Only a hole with the story running on both sides of it is."""
    ledger = (
        [_entry(i, f"2015-05-{d:02d}", f"precedent {d}") for i, d in enumerate((1, 4, 8), start=1)]
        + [_entry(10 + i, f"2026-07-{d:02d}", f"event {d}") for i, d in enumerate((1, 3, 6, 9), start=1)]
    )
    span = dossier.recompute_span(ledger)
    questions = dossier.gap_questions(ledger, span)

    assert questions == [], "an eleven-year run-up is not a gap in the record"


def test_gap_detector_is_capped_and_takes_the_most_recent_holes():
    """A backstop under the bracketing: whatever the shape of the ledger, one
    detector run can never flood the frontier."""
    # Every fourth week has an entry, for two years: interior holes throughout.
    ledger = [
        _entry(i, (date(2024, 1, 1) + timedelta(days=28 * i)).isoformat(), f"event {i}")
        for i in range(26)
    ]
    span = dossier.recompute_span(ledger)
    questions = dossier.gap_questions(ledger, span)

    assert len(questions) == dossier.MAX_GAP_QUESTIONS
    dates = [q["text"].split()[6] for q in questions]
    assert dates == sorted(dates)
    assert dates[-1] > "2025-06-01", "the cap must keep the most recent holes, not the oldest"


def test_an_asked_gap_question_is_not_raised_again():
    """A week that stayed empty after we searched it is answered, not
    outstanding. Without this the same weeks fill the cap every round, hiding
    the ones behind them — and `saturated()` can never come true, because it
    would require every hole to be FILLED rather than asked about."""
    ledger = _exam_leak_ledger()
    span = dossier.recompute_span(ledger)
    first = dossier.gap_questions(ledger, span)
    assert first

    asked = [dossier.normalise(q["text"]) for q in first]
    assert dossier.gap_questions(ledger, span, asked) == []


def test_span_is_recomputed_from_the_ledger():
    """Fixing span at planning time would silently break gap detection: later
    rounds are exactly when earlier-dated events surface."""
    ledger = _exam_leak_ledger()
    assert dossier.recompute_span(ledger) == {"start": "2026-05-03", "end": "2026-07-27"}

    ledger.append(_entry(7, "2026-03-01", "The syllabus was published"))
    assert dossier.recompute_span(ledger)["start"] == "2026-03-01"


# ---------- entity asymmetry ----------


def _entity(name: str, side: str, role: str | None = "official") -> dict:
    return {"name": name, "kind": "person", "role": role, "side": side}


def test_entity_asymmetry_fires_on_a_state_only_table():
    """The failing run's entity table: Modi, Pradhan, Joshi, NTA, Nilekani and
    the rest — ten entities, all state, none from the movement. A protest
    story researched from one side only."""
    entities = [
        _entity(n, "state")
        for n in ("Modi", "Pradhan", "Joshi", "NTA", "Nilekani", "Somanath", "Deka")
    ]
    questions = dossier.entity_asymmetry_questions(entities)

    assert len(questions) == 1
    assert questions[0]["origin"] == "entity"
    assert "protesters" in questions[0]["text"] or "activists" in questions[0]["text"]


def test_entity_asymmetry_clears_once_both_sides_are_present():
    entities = [_entity(n, "state") for n in ("Modi", "Pradhan", "Joshi")]
    entities.append(_entity("Sahil Lochab", "movement", role="student hit by pellet guns"))
    assert dossier.entity_asymmetry_questions(entities) == []


def test_entity_asymmetry_fires_the_other_way_too():
    entities = [_entity(n, "movement") for n in ("A", "B", "C")]
    questions = dossier.entity_asymmetry_questions(entities)
    assert len(questions) == 1
    assert "officials" in questions[0]["text"]


def test_a_named_actor_with_no_role_raises_a_question():
    ledger = [_entry(1, "2026-07-12", "Amit Shah received a letter", **{"actors": ["Amit Shah"]})]
    entities = [_entity("Amit Shah", "state", role=None)]
    questions = dossier.role_questions(ledger, entities)
    assert len(questions) == 1
    assert "Amit Shah" in questions[0]["text"]


# ---------- ledger merge ----------


def test_the_same_event_from_a_second_outlet_merges_and_raises_outlet_count():
    ledger, _, _ = dossier.merge_entries(
        [], [{"date": "2026-07-20", "precision": "day",
              "what": "Police fired pellet guns at student protesters in Delhi",
              "sources": [{"outlet": "HT", "url": "u1"}]}], 1)
    ledger, added, _ = dossier.merge_entries(
        ledger, [{"date": "2026-07-20", "precision": "day",
                  "what": "Delhi police fired pellet guns at protesting students",
                  "sources": [{"outlet": "BBC", "url": "u2"}]}], 2)

    assert len(ledger) == 1, "the same event must not become two entries"
    assert added == []
    assert ledger[0]["outlet_count"] == 2
    assert len(ledger[0]["sources"]) == 2


def test_disagreeing_figures_stay_two_entries_and_raise_a_contested_question():
    """product.md is emphatic that numbers are never blended. Two sources
    giving different figures produce two attributed entries, and the
    disagreement becomes research rather than an averaging decision."""
    ledger, _, _ = dossier.merge_entries(
        [], [{"date": "2026-07-21", "precision": "day", "what": "Police detained 40 protesters",
              "sources": [{"outlet": "HT", "url": "u1"}]}], 1)
    ledger, added, contested = dossier.merge_entries(
        ledger, [{"date": "2026-07-21", "precision": "day", "what": "Police detained 65 protesters",
                  "sources": [{"outlet": "PTI", "url": "u2"}]}], 2)

    assert len(ledger) == 2
    assert len(added) == 1
    assert len(contested) == 1
    assert contested[0]["origin"] == "contested"


def test_different_dates_never_merge():
    ledger, _, _ = dossier.merge_entries(
        [], [{"date": "2026-07-20", "precision": "day", "what": "Police fired pellet guns at students",
              "sources": [{"outlet": "HT", "url": "u1"}]}], 1)
    ledger, added, _ = dossier.merge_entries(
        ledger, [{"date": "2026-07-25", "precision": "day", "what": "Police fired pellet guns at students",
                  "sources": [{"outlet": "HT", "url": "u2"}]}], 2)
    assert len(ledger) == 2
    assert len(added) == 1


def test_a_month_precision_entry_matches_a_day_inside_that_month():
    ledger, _, _ = dossier.merge_entries(
        [], [{"date": "2026-05", "precision": "month", "what": "The examination was cancelled nationwide",
              "sources": [{"outlet": "HT", "url": "u1"}]}], 1)
    ledger, added, _ = dossier.merge_entries(
        ledger, [{"date": "2026-05-12", "precision": "day",
                  "what": "The examination was cancelled nationwide",
                  "sources": [{"outlet": "BBC", "url": "u2"}]}], 2)
    assert len(ledger) == 1
    assert ledger[0]["outlet_count"] == 2


def test_merge_is_idempotent_so_a_resumed_round_cannot_duplicate():
    """Resume re-runs work that may already be merged. Feeding the identical
    candidate twice must be a no-op, or every interrupted run inflates the
    ledger."""
    cand = {"date": "2026-07-20", "precision": "day", "what": "Police fired pellet guns at students",
            "sources": [{"outlet": "HT", "url": "u1"}]}
    ledger, _, _ = dossier.merge_entries([], [dict(cand)], 1)
    ledger, added, _ = dossier.merge_entries(ledger, [dict(cand)], 1)
    assert len(ledger) == 1
    assert added == []


def test_entities_accumulate_by_name_rather_than_duplicating():
    entities = dossier.merge_entities([], [{"name": "Nandan Nilekani", "kind": "person",
                                            "side": "unknown"}], "2026-07-27")
    entities = dossier.merge_entities(entities, [{"name": "nandan  nilekani", "kind": "person",
                                                  "role": "task force chair", "side": "state"}],
                                      "2026-07-28")
    assert len(entities) == 1
    assert entities[0]["role"] == "task force chair"
    assert entities[0]["side"] == "state"
    assert entities[0]["first_seen"] == "2026-07-27"
    assert entities[0]["last_seen"] == "2026-07-28"


def test_a_known_role_is_never_overwritten_by_a_vaguer_one():
    entities = dossier.merge_entities([], [{"name": "X", "kind": "person", "role": "organiser",
                                            "side": "movement"}], "2026-07-27")
    entities = dossier.merge_entities(entities, [{"name": "X", "kind": "person", "side": "unknown"}],
                                      "2026-07-28")
    assert entities[0]["role"] == "organiser"
    assert entities[0]["side"] == "movement"


# ---------- the frontier and its drift guards ----------


def _frontier() -> dict:
    return {"open": [], "in_flight": [], "asked": [], "discarded": []}


def test_a_spent_question_is_never_asked_again_even_reworded():
    frontier = _frontier()
    frontier["asked"].append(dossier.normalise("What did the police do on 20 July?"))
    admitted, _ = dossier.admit(
        frontier, [dossier._question("what did THE police do on 20 July", origin="model", score=0.9)]
    )
    assert admitted == 0


def test_a_question_below_the_relevance_floor_is_discarded_and_recorded():
    """Recursive research drifts. The relevance gate is the primary defence,
    and dossier.md §13 forbids a silent cap — a discard is recorded."""
    frontier = _frontier()
    admitted, discarded = dossier.admit(
        frontier,
        [dossier._question("What is the history of the Indian IT industry?", origin="model",
                           score=dossier.MIN_QUESTION_SCORE - 0.1)],
    )
    assert admitted == 0
    assert len(frontier["discarded"]) == 1
    assert discarded[0]["why"] == "score"


def test_a_question_past_the_depth_cap_is_cut():
    frontier = _frontier()
    admitted, discarded = dossier.admit(
        frontier,
        [dossier._question("What is Infosys?", origin="model", score=0.9,
                           depth=dossier.MAX_QUESTION_DEPTH + 1)],
    )
    assert admitted == 0
    assert discarded[0]["why"] == "depth"


def test_duplicate_questions_are_admitted_once():
    frontier = _frontier()
    q = dossier._question("What happened on 20 July?", origin="model", score=0.9)
    admitted, _ = dossier.admit(frontier, [q, dict(q)])
    assert admitted == 1


def test_measured_holes_outrank_the_models_own_hunches():
    frontier = _frontier()
    dossier.admit(frontier, [
        dossier._question("A model hunch", origin="model", score=0.8),
        dossier._question("A measured gap", origin="gap", score=0.8),
    ])
    assert dossier.pop_round(frontier, 1)[0]["origin"] == "gap"


def test_every_checklist_dimension_gets_a_question_even_if_the_model_skips_it():
    """The whole feature exists because a model narrowed scope on its own
    initiative. Asking a second model nicely is not structurally different, so
    coverage is enforced in Python."""
    partial = [dossier._question("Origins?", origin="model", score=0.9, dimension="origin")]
    injected = dossier.ensure_dimension_coverage(partial)

    covered = {q["dimension"] for q in partial + injected}
    assert covered == set(dossier.DIMENSIONS)
    assert any("hunger strike" in q["text"] for q in injected), "the movement dimension must be forced"


def test_pass_c_batches_by_topic_not_by_queue_order():
    """Batching whatever is next in the queue blurs the searches a call runs —
    the same failure as the original broad prompt, one layer down."""
    questions = [
        dossier._question("state q1", origin="model", score=0.9, dimension="state"),
        dossier._question("movement q1", origin="model", score=0.9, dimension="movement"),
        dossier._question("state q2", origin="model", score=0.9, dimension="state"),
    ]
    batches = dossier.batch_questions(questions)
    for batch in batches:
        assert len({q["dimension"] for q in batch}) == 1


# ---------- saturation ----------


def test_saturation_needs_both_detectors_clear():
    """A lean round with a seven-week hole still open is not saturation."""
    dsr = dossier.new_dossier(1, "s")
    dsr["ledger"] = _exam_leak_ledger()
    dsr["span"] = dossier.recompute_span(dsr["ledger"])
    dsr["lean_rounds"] = dossier.SATURATION_ROUNDS

    assert dossier.saturated(dsr) is False  # the gap detector is still dirty

    dsr["ledger"] = [_entry(i, f"2026-07-{d:02d}", f"event {d}")
                     for i, d in enumerate(range(1, 29, 2), start=1)]
    dsr["span"] = dossier.recompute_span(dsr["ledger"])
    assert dossier.saturated(dsr) is True


def test_a_lean_streak_alone_does_not_end_research():
    dsr = dossier.new_dossier(1, "s")
    dsr["lean_rounds"] = dossier.SATURATION_ROUNDS - 1
    assert dossier.saturated(dsr) is False


# ---------- the write reserve and the call budget ----------


def test_the_write_reserve_scales_with_the_ledger():
    """A flat reserve would starve exactly the large, well-researched dossiers
    this feature exists to produce: phased writing needs a call per phase."""
    assert dossier.write_reserve([{}] * 5) == 3
    assert dossier.write_reserve([{}] * 120) > 3


def test_research_stops_before_it_can_starve_the_write_pass():
    dsr = dossier.new_dossier(1, "s")
    dsr["ledger"] = [{}] * 10
    reserve = dossier.write_reserve(dsr["ledger"])
    budget = _budget()

    dsr["calls"] = dossier.MAX_CALLS_PER_FOLLOW - reserve
    assert dossier._afford(dsr, budget, reserve) is False
    dsr["calls"] = dossier.MAX_CALLS_PER_FOLLOW - reserve - 1
    assert dossier._afford(dsr, budget, reserve) is True


def _budget(tmp_path=None, cap: int = dossier.MAX_GROUNDED_CALLS_PER_DAY) -> dossier.Budget:
    import tempfile
    root = tmp_path or __import__("pathlib").Path(tempfile.mkdtemp())
    return dossier.Budget(root / "_budget" / "2026-07-27.json", cap=cap)


def test_the_daily_budget_is_shared_and_persists(tmp_path):
    budget = dossier.Budget(tmp_path / "b.json", cap=5)
    budget.spend(2, 3)
    assert budget.remaining() == 2

    reloaded = dossier.Budget(tmp_path / "b.json", cap=5)
    assert reloaded.spent == 3
    assert reloaded.remaining() == 2


def test_an_exhausted_day_budget_stops_further_research(tmp_path):
    budget = dossier.Budget(tmp_path / "b.json", cap=1)
    budget.spend(2, 1)
    dsr = dossier.new_dossier(2, "s")
    assert dossier._afford(dsr, budget, 3) is False


def test_a_deferral_is_recorded_never_silent(tmp_path):
    budget = dossier.Budget(tmp_path / "b.json", cap=1)
    budget.defer(9, "day_budget")
    assert json.loads((tmp_path / "b.json").read_text())["deferred"][0]["issue"] == 9


def test_a_daily_quota_hit_blocks_every_follow_not_just_the_one(tmp_path):
    budget = dossier.Budget(tmp_path / "b.json", cap=18)
    budget.mark_daily_quota()
    assert dossier._afford(dossier.new_dossier(1, "s"), budget, 3) is False
    assert dossier.Budget(tmp_path / "b.json", cap=40).day_quota_hit is True


def test_a_perday_429_becomes_a_clean_stop_not_a_crash(tmp_path):
    """ratelimit re-raises a PerDay 429 immediately rather than sleeping for
    hours. dossier turns that into DailyQuotaExhausted so the sweep stops once
    instead of every remaining follow rediscovering it."""
    import pytest

    budget = dossier.Budget(tmp_path / "b.json", cap=18)
    dsr = dossier.new_dossier(1, "s")

    class _Quota(Exception):
        code = 429
        details = {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay"}]}]}}

    def boom():
        raise _Quota("429 RESOURCE_EXHAUSTED PerDay")

    with pytest.raises(dossier.DailyQuotaExhausted):
        dossier._guarded(boom, dsr, budget)
    assert budget.day_quota_hit is True


def test_an_ordinary_failure_is_not_swallowed_as_a_quota_stop(tmp_path):
    import pytest

    budget = dossier.Budget(tmp_path / "b.json", cap=18)
    dsr = dossier.new_dossier(1, "s")

    def boom():
        raise ValueError("something else went wrong")

    with pytest.raises(ValueError):
        dossier._guarded(boom, dsr, budget)
    assert budget.day_quota_hit is False


# ---------- checkpoint and resume ----------


def test_every_checkpoint_round_trips_through_disk(tmp_path):
    dsr = dossier.new_dossier(3, "The exam-leak protests")
    dsr["ledger"] = _exam_leak_ledger()
    dsr["calls"] = 7
    dsr["rounds"] = 2
    dsr["questions"]["asked"] = ["already spent"]
    dossier.save(tmp_path, 3, dsr, {"https://a/1": {"text": "x"}}, "E")

    loaded, corpus = dossier.load(tmp_path, 3)
    assert loaded is not None
    assert loaded["checkpoint"]["stage"] == "E"
    assert loaded["calls"] == 7
    assert loaded["questions"]["asked"] == ["already spent"]
    assert corpus["https://a/1"]["text"] == "x"


def test_loading_a_dossier_that_never_ran_is_not_an_error(tmp_path):
    assert dossier.load(tmp_path, 99) == (None, {})


def test_needs_research_tracks_the_research_state():
    dsr = dossier.new_dossier(1, "s")
    assert dossier.needs_research(dsr) is True  # pending
    dsr["research_state"] = "researching"
    assert dossier.needs_research(dsr) is True
    for done in ("complete", "capped"):
        dsr["research_state"] = done
        assert dossier.needs_research(dsr) is False
        assert dossier.is_readable(dsr) is True


# ---------- Pass A ----------


def test_pass_a_seeds_the_claims_the_digest_already_gathered(tmp_path, monkeypatch):
    """Finding #1 of dossier.md §1: the pipeline already had the missing facts
    and threw them away. Pass A costs no API call and recovers all of them."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "2026-07-27.json").write_text(json.dumps({
        "stories": [{
            "section": "india",
            "headline": "PM Modi creates exam reform task force after minister resigns over leaks",
            "claims": [
                {"text": "Nineteen-year-old student Sahil Lochab faces the potential loss of an eye "
                         "after being hit by pellet guns.", "outlet": "Hindustan Times", "url": "u1"},
                {"text": "Rahul Gandhi sent a letter to Home Minister Amit Shah.",
                 "outlet": "Hindustan Times", "url": "u1"},
            ],
            "sources": [{"outlet": "Hindustan Times", "url": "u1"}],
        }]
    }))
    monkeypatch.setattr(dossier.extract, "article_text", lambda url: "page text " * 200)

    origin = {"date": "2026-07-27", "section": "india", "position": 1,
              "headline": "PM Modi creates exam reform task force after minister resigns over leaks"}
    dsr, corpus = dossier.seed(tmp_path, data_dir, 3, origin, origin["headline"])

    assert len(dsr["origin_claims"]) == 2
    assert any("pellet guns" in c["text"] for c in dsr["origin_claims"])
    assert any("Rahul Gandhi" in c["text"] for c in dsr["origin_claims"])
    assert corpus["u1"]["via"] == "extract"


def test_pass_a_records_a_page_it_could_not_read(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "2026-07-27.json").write_text(json.dumps({
        "stories": [{"section": "india", "headline": "h", "claims": [],
                     "sources": [{"outlet": "X", "url": "u1"}]}]
    }))
    monkeypatch.setattr(dossier.extract, "article_text", lambda url: "")

    origin = {"date": "2026-07-27", "section": "india", "position": 1, "headline": "h"}
    dsr, corpus = dossier.seed(tmp_path, data_dir, 3, origin, "h")

    assert corpus == {}
    assert dsr["unreadable"]["u1"] == "extract_failed"


def test_pass_a_survives_a_missing_origin_story(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    origin = {"date": "2026-01-01", "section": "india", "position": 1, "headline": "gone"}
    dsr, corpus = dossier.seed(tmp_path, data_dir, 3, origin, "gone")
    assert dsr["origin_claims"] == []
    assert corpus == {}


# ---------- the [eN] write gate ----------


def _two_entries() -> list[dict]:
    return [
        _entry(1, "2026-07-20", "Police fired pellet guns at student protesters"),
        _entry(2, "2026-07-21", "A nineteen-year-old student faced the loss of an eye"),
    ]


def test_well_anchored_prose_passes_and_carries_its_markers():
    entries = _two_entries()
    body = (
        "Police fired pellet guns at student protesters in Delhi on 20 July [e1]. "
        "A nineteen-year-old student faced the loss of an eye in the same week [e2]. "
    ) * 6
    block = dossier._compose(entries, body)

    assert block is not None
    assert "[e1]" not in block["body"], "markers must be stripped out of the prose"
    assert block["markers"]
    assert block["metrics"]["entry_coverage"] == 1.0


def test_prose_that_omits_most_of_the_ledger_is_rejected():
    """dossier.md §18 requires that the prose CARRIES the entries, not merely
    that what it says is sourced. anchor.unanchored_share measures the other
    direction and would pass this."""
    entries = [_entry(i, f"2026-07-{i:02d}", f"Event number {i} happened in Delhi") for i in range(1, 11)]
    body = ("Event number 1 happened in Delhi on the first of July [e1]. " * 20)
    block = dossier._compose(entries, body)
    assert block is None


def test_unanchored_prose_is_rejected():
    entries = _two_entries()
    body = "Police fired pellet guns [e1]. " + "Unsourced commentary about the situation. " * 40
    assert dossier._compose(entries, body) is None


def test_a_multi_source_entry_counts_as_one_anchored_span():
    """render._accepted_markers renders only the first of several markers
    sharing a span, so counting raw markers would inflate MIN_MARKERS exactly
    where corroboration is strongest."""
    entries = [
        _entry(1, "2026-07-20", "Police fired pellet guns", **{
            "sources": [{"outlet": "HT", "url": "u1"}, {"outlet": "BBC", "url": "u2"},
                        {"outlet": "PTI", "url": "u3"}],
            "outlet_count": 3,
        }),
    ]
    body = "Police fired pellet guns at protesters in Delhi on 20 July [e1]. " * 15
    block = dossier._compose(entries, body)

    assert block is not None
    assert block["metrics"]["distinct_spans"] < block["metrics"]["marker_count"]


def test_entry_coverage_measures_the_ledger_not_the_prose():
    entries = _two_entries()
    _body, markers, _ = dossier.anchor.parse_anchored(
        "Something happened [e1].", dossier._cites(entries), "e"
    )
    assert dossier.entry_coverage(entries, markers) == 0.5
    assert dossier.entry_coverage([], markers) == 1.0


def test_phase_concatenation_rebases_marker_offsets():
    """Each phase is parsed independently, so every later phase's offsets are
    relative to its own text and must be shifted past everything before it."""
    a = {"body": "First phase prose.", "markers": [{"start": 0, "end": 5, "outlet": "HT", "url": "u1"}],
         "sources": [], "queries": [], "search_suggestions": ""}
    b = {"body": "Second phase prose.", "markers": [{"start": 0, "end": 6, "outlet": "BBC", "url": "u2"}],
         "sources": [], "queries": [], "search_suggestions": ""}
    joined = dossier._concat([a, b])

    assert joined["body"] == "First phase prose.\n\nSecond phase prose."
    assert joined["markers"][1]["start"] == len(a["body"]) + 2
    assert joined["body"][joined["markers"][1]["start"]:joined["markers"][1]["end"]] == "Second"
    assert len(joined["sources"]) == 2


# ---------- phases ----------


def test_phases_split_on_a_long_quiet_stretch():
    ledger = assign = dossier.assign_phases(_exam_leak_ledger())
    phases = {e["phase"] for e in ledger}
    assert len(phases) > 1, "a seven-week gap must start a new phase"
    assert assign[0]["phase"] != assign[-1]["phase"]


# ---------- grouping the write pass ----------


def _phased(counts: list[int]) -> list[dict]:
    out, n = [], 0
    for i, c in enumerate(counts, start=1):
        for _ in range(c):
            n += 1
            out.append({"id": n, "phase": f"phase-{i}", "date": f"2026-{i:02d}-01", "what": "x"})
    return out


def test_a_small_ledger_is_written_in_one_call():
    assert len(dossier.write_groups(_phased([3, 4]))) == 1


def test_single_entry_phases_never_become_their_own_call():
    """assign_phases splits on 14-day quiet stretches, which produces a long
    tail of one-entry phases on a real story — issue #3's first run made eight
    phases, five of them a single entry. A one-entry group cannot clear
    MIN_BODY_WORDS or MIN_MARKERS, so writing it spends a call to produce
    prose the gate throws away, taking the entry off the page with it."""
    groups = dossier.write_groups(_phased([1, 1, 1, 1, 1, 15, 2, 35]))
    assert all(len(g) >= 2 for g in groups)
    assert len(groups) < 8, "eight phases must not become eight calls"


def test_one_large_phase_is_split_rather_than_sent_as_a_single_huge_call():
    groups = dossier.write_groups(_phased([80]))
    assert len(groups) > 1
    assert all(len(g) <= dossier.PHASED_WRITE_ENTRIES * 1.5 for g in groups)


def test_a_short_tail_rides_with_the_group_before_it():
    """Better a group slightly over the cap than a stub that fails its gate."""
    groups = dossier.write_groups(_phased([30, 3]))
    assert len(groups) == 1
    assert len(groups[0]) == 33


def test_grouping_never_loses_or_duplicates_an_entry():
    entries = _phased([1, 1, 1, 15, 2, 35, 4])
    groups = dossier.write_groups(entries)
    seen = [e["id"] for g in groups for e in g]
    assert sorted(seen) == sorted(e["id"] for e in entries)
    assert len(seen) == len(set(seen))


def test_the_write_reserve_covers_the_groups_it_will_need():
    for counts in ([5], [30, 3], [1, 1, 1, 15, 2, 35], [80]):
        entries = _phased(counts)
        assert dossier.write_reserve(entries) >= len(dossier.write_groups(entries))


# ---------- the two daily pools ----------


def test_the_two_pools_are_metered_separately(tmp_path):
    """The free tier meters per MODEL, so a grounded search and a schema-only
    ledger call come out of different daily allowances. Counting them together
    idles half the capacity — the first live run stopped at three rounds with
    an entire second pool untouched."""
    b = dossier.Budget(tmp_path / "b.json", cap=4, schema_cap=3)
    for _ in range(4):
        b.spend(1, pool="grounded")

    assert b.exhausted("grounded") is True
    assert b.exhausted("schema") is False, "grounded exhaustion must not block the ledger or the write"
    assert b.schema_remaining() == 3


def test_exhausting_one_pool_leaves_the_other_usable(tmp_path):
    b = dossier.Budget(tmp_path / "b.json", cap=4, schema_cap=3)
    b.mark_daily_quota("grounded")
    assert b.exhausted("grounded") is True
    assert b.exhausted("schema") is False

    dsr = dossier.new_dossier(1, "s")
    assert dossier._afford(dsr, b, 3, "grounded") is False
    assert dossier._afford(dsr, b, 3, "schema") is True


def test_both_pools_persist_across_runs(tmp_path):
    b = dossier.Budget(tmp_path / "b.json", cap=10, schema_cap=8)
    b.spend(1, pool="grounded")
    b.spend(1, pool="schema")
    b.spend(1, pool="schema")

    again = dossier.Budget(tmp_path / "b.json", cap=10, schema_cap=8)
    assert again.spent == 1
    assert again.schema_spent == 2
    assert again.remaining() == 9
    assert again.schema_remaining() == 6


def test_a_pool_quota_hit_names_the_pool_it_hit(tmp_path):
    import pytest

    b = dossier.Budget(tmp_path / "b.json", cap=10, schema_cap=8)
    dsr = dossier.new_dossier(1, "s")

    class _Quota(Exception):
        code = 429
        details = {"error": {"details": [{"violations": [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}}

    def boom():
        raise _Quota("429 RESOURCE_EXHAUSTED PerDay")

    with pytest.raises(dossier.DailyQuotaExhausted):
        dossier._guarded(boom, dsr, b, "schema")

    assert b.schema_quota_hit is True
    assert b.day_quota_hit is False, "the grounded pool is a different model and still has quota"


def test_the_schema_pool_always_leaves_the_digest_room():
    """The schema pool is shared with llm.py's 3-4 morning calls. Follow
    starving the digest is the one failure this feature must never cause, so
    this cap stays conservative whatever the grounded pool is set to."""
    assert dossier.MAX_SCHEMA_CALLS_PER_DAY <= 16


def test_a_learned_ceiling_overrides_the_optimistic_default(tmp_path):
    """The two meters on a grounded call — the model's requests-per-day and
    the Google Search grounding allowance — are orders of magnitude apart and
    both unpublished. Rather than guess, the ceiling is read off the 429 that
    actually fires and remembered."""
    b = dossier.Budget(tmp_path / "2026-07-28.json")
    assert b.cap == dossier.MAX_GROUNDED_CALLS_PER_DAY

    b.learn("grounded", 20, "gemini-2.5-flash")

    tomorrow = dossier.Budget(tmp_path / "2026-07-29.json")
    assert tomorrow.cap == 20 - dossier.QUOTA_SAFETY_MARGIN
    assert tomorrow.schema_cap == dossier.MAX_SCHEMA_CALLS_PER_DAY, "pools are learned separately"


def test_a_learned_ceiling_never_raises_a_cap_above_its_default(tmp_path):
    """A generous grounding allowance must not quietly lift the schema pool
    past the room the digest needs."""
    b = dossier.Budget(tmp_path / "2026-07-28.json")
    b.learn("schema", 1500, "gemini-3.6-flash")
    assert dossier.Budget(tmp_path / "2026-07-29.json").schema_cap == dossier.MAX_SCHEMA_CALLS_PER_DAY


def test_the_ceiling_is_learned_even_when_the_server_names_no_number(tmp_path):
    """Some 429s carry no quotaValue. What we managed to spend before being
    refused is still evidence, and better than the optimistic default."""
    import pytest

    b = dossier.Budget(tmp_path / "2026-07-28.json")
    for _ in range(9):
        b.spend(1, pool="grounded")
    dsr = dossier.new_dossier(1, "s")

    class _Bare(Exception):
        code = 429

    def boom():
        raise _Bare("429 RESOURCE_EXHAUSTED: quota exceeded PerDay")

    with pytest.raises(dossier.DailyQuotaExhausted):
        dossier._guarded(boom, dsr, b, "grounded")

    assert dossier.Budget(tmp_path / "2026-07-29.json").cap == max(1, 9 - dossier.QUOTA_SAFETY_MARGIN)


def test_daily_limit_reads_the_number_off_a_real_429():
    import ratelimit

    class E(Exception):
        code = 429
        details = {"error": {"details": [{"violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaValue": "20"}]}]}}

    assert ratelimit.daily_limit(E("429")) == 20

    class PerMinute(Exception):
        code = 429
        details = {"error": {"details": [{"violations": [{
            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "quotaValue": "5"}]}]}}

    assert ratelimit.daily_limit(PerMinute("429")) is None, "a per-minute limit is not a daily ceiling"


def test_a_round_fits_inside_one_days_grounded_pool():
    """A round has to fit twice inside one day's grounded pool.

    The cost is BATCHES, not ceil(QUESTIONS_PER_ROUND / QUESTIONS_PER_CALL) —
    batch_questions groups by dimension before splitting, so questions from
    distinct dimensions never share a call and the worst case is one call per
    question. The 2026-07-27 run spent eight search calls in a single round
    against the two that the naive division predicts, which is how a day's pool
    vanished in two rounds. Asserting the naive number here is what let that
    ship, so this models the real worst case instead."""
    worst_case_search = dossier.QUESTIONS_PER_ROUND  # every question its own dimension
    per_round = worst_case_search + 1 + 1  # + one url_context read + the critic
    rounds = dossier.MAX_GROUNDED_CALLS_PER_DAY // per_round
    assert rounds >= 2, (
        f"a round costs up to {per_round} grounded calls against a pool of "
        f"{dossier.MAX_GROUNDED_CALLS_PER_DAY}, leaving {rounds} round(s) a day"
    )


def test_the_worst_case_round_cost_is_one_call_per_question():
    """Guards the assumption the dial sizing above rests on: QUESTIONS_PER_CALL
    caps a batch's size, it does not cap the number of batches."""
    spread = [
        {"q": f"q{i}", "dimension": f"dim{i}"} for i in range(dossier.QUESTIONS_PER_ROUND)
    ]
    assert len(dossier.batch_questions(spread)) == dossier.QUESTIONS_PER_ROUND

    clustered = [{"q": f"q{i}", "dimension": "same"} for i in range(dossier.QUESTIONS_PER_ROUND)]
    import math
    assert len(dossier.batch_questions(clustered)) == math.ceil(
        dossier.QUESTIONS_PER_ROUND / dossier.QUESTIONS_PER_CALL
    )


def test_a_stale_learned_ceiling_is_re_probed(tmp_path):
    """Free-tier limits move — research.md §3.1 records a 50-80% cut in one
    month, and they go up too. A ceiling learned once and trusted forever
    would silently cap us at a number the provider has since raised."""
    import json as _json

    (tmp_path / "limits.json").write_text(_json.dumps({
        "grounded": {"rpd": 20, "model": "m", "learned_at": "2020-01-01T00:00:00Z"}
    }))
    assert dossier.Budget(tmp_path / "d.json").cap == dossier.MAX_GROUNDED_CALLS_PER_DAY


def test_a_fresh_learned_ceiling_is_respected(tmp_path):
    import json as _json

    (tmp_path / "limits.json").write_text(_json.dumps({
        "grounded": {"rpd": 20, "model": "m", "learned_at": dossier._now_iso()}
    }))
    assert dossier.Budget(tmp_path / "d.json").cap == 20 - dossier.QUOTA_SAFETY_MARGIN


def test_an_unparseable_learned_at_is_treated_as_stale(tmp_path):
    import json as _json

    (tmp_path / "limits.json").write_text(_json.dumps({"grounded": {"rpd": 5, "learned_at": "junk"}}))
    assert dossier.Budget(tmp_path / "d.json").cap == dossier.MAX_GROUNDED_CALLS_PER_DAY
