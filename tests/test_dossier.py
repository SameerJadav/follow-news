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
from datetime import date

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


def _budget(tmp_path=None, cap: int = dossier.MAX_RESEARCH_CALLS_PER_DAY) -> dossier.Budget:
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
    budget = dossier.Budget(tmp_path / "b.json", cap=40)
    budget.mark_daily_quota()
    assert dossier._afford(dossier.new_dossier(1, "s"), budget, 3) is False
    assert dossier.Budget(tmp_path / "b.json", cap=40).day_quota_hit is True


def test_a_perday_429_becomes_a_clean_stop_not_a_crash(tmp_path):
    """ratelimit re-raises a PerDay 429 immediately rather than sleeping for
    hours. dossier turns that into DailyQuotaExhausted so the sweep stops once
    instead of every remaining follow rediscovering it."""
    import pytest

    budget = dossier.Budget(tmp_path / "b.json", cap=40)
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

    budget = dossier.Budget(tmp_path / "b.json", cap=40)
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
