"""Deep research for a followed story: the evidence base prose is written from.

The digest's discipline is that the write pass sees claims, never raw article
text, so a fact that is not an anchored claim has no way into the prose. Follow
gains the same discipline here, with ledger entries in place of claims:

    The dossier is the source of truth for a followed story's evidence.
    Prose is derived from it, the way docs/ is derived from data/.

`followed/<issue>/dossier.json` and `corpus.json` are written by this module
and nothing else. The dossier is APPEND-ONLY — entries are added, corrected and
merged, never silently dropped, so whatever a reader saw yesterday is still
accounted for today.

Why this exists (dossier.md §1): the first live follow produced a 524-word
backstory from a 188-character prompt and one grounded call, while the ten
anchored claims the digest had already gathered for that same story — including
a student losing an eye to pellet guns — were resolved, confirmed to exist, and
then thrown away. Grounding returns search snippets, and snippets favour what is
cleanly stateable in a headline; a multi-day escalation lives in article bodies.

This module never imports the genai SDK. Every Gemini call goes through
ground.py, every [eN] marker goes through anchor.py, and every page fetch goes
through extract.py. It also never calls tracer.start() — it runs inside
follow.run()'s already-open "follow" run, which is what keeps its evidence in
run-follow.json instead of overwriting the digest's own run.json.

Three API facts this design is shaped by, all verified live on 2026-07-27:

- **Any tool use forbids `response_mime_type="application/json"`**, not just
  google_search. `url_context` + response_schema returns the same
  `400 INVALID_ARGUMENT: Tool use with a response mime type: 'application/json'
  is unsupported`. So a pass either uses a tool OR gets validated JSON, never
  both, and reading pages (D) and structuring them (E) must be separate calls.
- **url_context reads, it does not extract.** It lets the model use a page as
  context; it never hands back storable text. extract.py stays the only way
  real article text enters the corpus, and url_context is what covers the pages
  extract.py cannot get.
- **Output budget must be generous.** The same call returned an empty response
  at 800 output tokens and 2,610 characters at 8192, because thinking plus
  url_context overhead consumed the whole budget first.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anchor
import extract
import ground
import tracer
from tracer import dbg

# ---------- dials (dossier.md §15) ----------

# The free tier meters PER MODEL, so there are two pools, not one:
#
#   grounded pool  gemini-2.5-flash   Passes B, C, D, G          — Follow only
#   schema pool    gemini-3.6-flash   Pass E, the write passes   — shared with
#                                                                  the digest
#
# The split is forced, not chosen. Probed 2026-07-28
# (tools/probe_ground_model.py): gemini-3.6-flash CANNOT use google_search —
# an immediate 429 with no quota detail, twice, while a url_context call on the
# same model in the same run succeeded and gemini-2.5-flash grounded fine
# minutes later. So it is neither a dead pool nor a project-wide outage:
# search grounding is unavailable on Gemini 3 on this tier, the same way 2.5
# Pro is `limit: 0`. research.md §3.1's "5,000 prompts/month on Gemini 3
# models" does not hold here.
#
# Consequence for anyone re-planning this: Pass C (search) is PINNED to the
# 2.5 family and its capacity does not grow by adding models. Pass D
# (url_context) is not pinned — it was verified working on 3.6 — so it is the
# one grounded pass that could be moved off this pool to free a call a round.
#
# Both ceilings are now MEASURED, not guessed. AI Studio's rate-limit page
# (aistudio.google.com/rate-limit) reports, for this key, on 2026-07-28:
#
#   gemini-2.5-flash   RPM 5   RPD 20   TPM ~250K
#   gemini-3.6-flash   RPM 5   RPD 20   TPM ~250K
#
# RPD 20 per model is the ONLY binding meter. The other two have enormous
# headroom and can be ignored: observed peak input tokens were ~60K against
# ~250K, and the Google Search grounding allowance is 1,500/day — which 20
# model requests a day cannot reach in seventy-five days. Sizing anything
# against the grounding allowance (as the previous 120 did) is sizing against
# a meter that physically cannot fire first.
#
# The 2026-07-27 run confirms the number exactly: 19 requests to
# gemini-2.5-flash, then a 429 on the 20th.
#
# The margin is subtracted here rather than left to `_cap_for`, which applies
# it only to a *learned* ceiling — so the default has to arrive pre-margined or
# the first run of the day spends right up to the wall.
MAX_GROUNDED_CALLS_PER_DAY = 18  # 20 measured - QUOTA_SAFETY_MARGIN (2)
# The schema pool is shared with the digest's own morning calls, and the digest
# must never be starved by Follow. Re-derived 2026-08-23 (ANALYSIS-2026-08-23.md
# §H2), because the old "20 - 4 for the digest at its worst - 2 margin = 14" was
# sized against a worst case that had already been exceeded: three days spent 5
# calls, not 4, every extra one a re-do of work an aborted run had paid for.
# Two things changed at once:
#   - the claims pass is now one call PER SECTION, so a nominal morning is 4
#     (select + world + india + write), not 3;
#   - ratelimit.py now retries a transient 5xx in-run, which is what those
#     re-dos were, so a mid-run abort costs at most one extra attempt rather
#     than a whole second run's select-and-claims.
# 20 measured - 6 for the digest at its worst (4 nominal + 2 for one aborted
# fire's already-paid stages) - 2 margin = 12.
MAX_SCHEMA_CALLS_PER_DAY = 12
# Held back from BOTH ceilings above (already subtracted into their literals)
# and from any learned one, so the last call of the day is ours rather than a
# wasted 429.
QUOTA_SAFETY_MARGIN = 2
# Learning is now the backstop for a MOVED limit, not how the limit is found —
# read the AI Studio dashboard for that. `_cap_for` takes
# min(default, learned - margin), so a learned value can only lower the cap.
# research.md §3.1 records the free tier being cut 50-80% in December 2025, and
# limits move upward too, so a learned ceiling expires after this many days and
# the measured default applies again.
LEARNED_LIMIT_TTL_DAYS = 14

# A round's grounded cost is driven by the number of BATCHES, not by
# QUESTIONS_PER_ROUND / QUESTIONS_PER_CALL. batch_questions() groups by
# checklist dimension FIRST and only then splits each group at
# QUESTIONS_PER_CALL, so QUESTIONS_PER_CALL caps how big a batch may get — it
# does not reduce how many there are. Questions from distinct dimensions never
# share a call, which means the worst case is one call per question:
#
#   Pass C  1..QUESTIONS_PER_ROUND calls   (one per dimension group)
#   Pass D  0 or 1                         (one url_context batch per round)
#   Pass G  1 every CRITIC_EVERY rounds
#
# The 2026-07-27 run is the evidence: round 1 spent EIGHT search calls, round 2
# five — against the two that the old "10 / 5" reading predicted. Two rounds
# consumed 16 grounded calls and the whole day's pool.
#
# So these are sized against the real ceiling of 18: six questions a round is
# at worst 6 + 1 + 1 = 8 grounded calls, which fits twice a day with room over,
# and three per call keeps a batch one topic asked three ways rather than three
# unrelated questions blurring one search.
QUESTIONS_PER_ROUND = 6  # how many frontier questions a round pops
QUESTIONS_PER_CALL = 3
# 8 rounds x ~8 grounded calls is ~64, which is what MAX_CALLS_PER_FOLLOW below
# is set to cover. At 18 a day that is a follow spanning three to four days —
# slow, but it is what RPD 20 physically allows, and checkpointing is what makes
# a multi-day follow a pause rather than a failure. Cutting the span needs more
# pools (more keys, or moving Pass D to the schema model), not a bigger number.
MAX_ROUNDS = 8
CRITIC_EVERY = 2  # run the completeness critic every Nth round, not every round
SATURATION_ENTRIES = 3  # a round adding fewer than this is a lean round
SATURATION_ROUNDS = 2  # ...this many lean rounds in a row ends research
MAX_CALLS_PER_FOLLOW = 60  # lifetime ceiling; a big story spans days, bounded per day
MAX_QUESTION_DEPTH = 3  # branch depth before a line of enquiry is cut
MIN_QUESTION_SCORE = 0.45  # relevance floor for entering the frontier
MAX_URLS_PER_CONTEXT_CALL = 20  # url_context API maximum
MAX_FETCH_PER_ROUND = 25  # extract.py fetches per round (wall clock, not quota)
PHASED_WRITE_ENTRIES = 30  # ledger size above which prose is written per phase
GAP_DENSITY_RATIO = 0.34  # how sparse a week must be to raise a gap question
# A hole only counts as a hole if the story is actually running on BOTH sides of
# it. Without this the detector reads an 11-year span as thousands of "gaps":
# follow #3 spanned 2015-05-01 to 2026-07-28 — 587 weekly buckets, 40 of them
# with any entry — and raised 547 gap questions, 616 of the 631 left in its
# frontier. Every round then spent its calls on empty weeks in 2015-2019 while
# `saturated()` (which requires the detector to be clean) could never fire, so
# research could only ever end on a ceiling. Bracketing caps the widest hole the
# detector will look at: with a hole W weeks wide, a week inside it needs an
# active week within GAP_CONTEXT_WEEKS before AND after, so nothing at all is
# raised once W > 2*GAP_CONTEXT_WEEKS - 1. Eight weeks keeps dossier.md §7's
# motivating case — a seven-week silence mid-story — and drops #3 from 547 to
# 44 (measured 2026-08-01 against the committed dossier).
GAP_CONTEXT_WEEKS = 8
MAX_GAP_QUESTIONS = 12  # per detector run, most recent first; a backstop, logged when it bites
MIN_ENTRY_COVERAGE = 0.6  # share of ledger entries the prose must actually cite
MERGE_SIMILARITY = 0.5  # Jaccard floor for calling two entries the same event
MAX_RESEARCH_SECONDS = 45 * 60  # wall-clock guard on one follow's research loop

# extract.py fetches cost no quota, so they must never count against
# MAX_CALLS_PER_FOLLOW — a big story could otherwise hit `capped` having spent
# no Gemini quota at all. `calls` counts Gemini calls and nothing else.

_STOPWORDS = frozenset(
    "the a an of in on at to for and or is was were by with from as that this it its "
    "after before during over under into out up down off about against between".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# ---------- the mandatory dimension checklist (dossier.md §4 Pass B) ----------
#
# Four of these eight were never touched by the failing run, and three of those
# four are where the owner's missing facts lived. So coverage is CHECKED IN
# PYTHON, not requested in a prompt: the whole feature exists because a model
# narrowed scope on its own initiative, and asking a second model nicely is not
# structurally different from asking the first one. Any dimension the model
# leaves empty gets its fallback question injected mechanically.

DIMENSIONS: dict[str, str] = {
    "origin": "What are the origins and root causes of this story?",
    "people": "Who are the key individuals on every side of this story, by name — organisers, activists, officials, and victims?",
    "movement": "What organising, tactics and protest actions — marches, sit-ins, strikes, hunger strikes — has the protesting or opposing side used?",
    "state": "How has the state responded — police action, force used, detentions, injuries, deaths, and any curbs on assembly or communication?",
    "legal": "What have courts, inquiries, investigating agencies and arrests contributed to this story?",
    "political": "Which parties, resignations, official statements and opposition responses have shaped this story?",
    "human": "Who has been hurt or has lost something in this story, and what happened to them specifically?",
    "contested": "Which facts in this story are disputed, and who disputes them?",
}


# ---------- pure helpers: text, dates, figures ----------


def normalise(text: str) -> str:
    """Casefold, collapse whitespace, strip stopwords. Used for the `asked`
    set so a reworded repeat of a spent question is recognised as one."""
    words = _WORD_RE.findall(text.casefold())
    return " ".join(w for w in words if w not in _STOPWORDS)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(text.casefold()) if w not in _STOPWORDS)


def _figures(text: str) -> set[str]:
    return {m.replace(",", "") for m in _FIGURE_RE.findall(text)}


def _valid_date(value: str, precision: str) -> bool:
    if precision == "day":
        return bool(_ISO_DAY_RE.match(value))
    if precision == "month":
        return bool(_ISO_MONTH_RE.match(value))
    return False


def _day_of(entry: dict) -> date | None:
    """An entry's date as a real date. A month-precision entry sorts and
    buckets from the first of its month — imprecise, but never invented."""
    value = str(entry.get("date") or "")
    try:
        if entry.get("precision") == "month" and _ISO_MONTH_RE.match(value):
            return date.fromisoformat(f"{value}-01")
        if _ISO_DAY_RE.match(value):
            return date.fromisoformat(value)
    except ValueError:
        return None
    return None


def _dates_compatible(a: dict, b: dict) -> bool:
    """Two entries can only be the same event if their dates can be the same
    day. A month-precision entry matches any day inside that month; two
    undated entries never auto-merge, because "unknown" is not "equal"."""
    da, db = _day_of(a), _day_of(b)
    if da is None or db is None:
        return False
    if a.get("precision") == "month" or b.get("precision") == "month":
        return (da.year, da.month) == (db.year, db.month)
    return da == db


def _blocks_on_numbers(a: dict, b: dict) -> bool:
    """product.md is emphatic that numbers are never blended: two sources
    disagreeing produces two entries, not one averaged figure. So when both
    texts carry figures and none of them match, this is a DISAGREEMENT and
    must not be merged away — it becomes a contested question instead."""
    fa, fb = _figures(str(a.get("what") or "")), _figures(str(b.get("what") or ""))
    return bool(fa) and bool(fb) and fa.isdisjoint(fb)


def _similarity(a: dict, b: dict) -> float:
    ta, tb = _tokens(str(a.get("what") or "")), _tokens(str(b.get("what") or ""))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------- the ledger ----------


def _outlets(sources: list[dict]) -> set[str]:
    return {str(s.get("outlet") or "") for s in sources if s.get("outlet")}


def merge_entries(
    ledger: list[dict], candidates: list[dict], round_no: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fold this round's candidate entries into the ledger.

    Returns (ledger, newly_added, contested_questions). A duplicate event found
    in a second outlet appends its source and raises `outlet_count` rather than
    creating a second entry; a same-date entry whose figures DISAGREE is kept
    separate and raises a contested question, per §13.

    Deterministic and cheap on purpose — same-date bucketing plus a Jaccard
    overlap on stopword-stripped tokens. No embeddings, no extra model call, so
    it is unit-testable without an API key like every other gate in this repo.
    """
    added: list[dict] = []
    contested: list[dict] = []
    next_id = max((int(e.get("id", 0)) for e in ledger), default=0) + 1

    for cand in candidates:
        match = None
        for existing in ledger:
            if not _dates_compatible(cand, existing):
                continue
            if _blocks_on_numbers(cand, existing):
                if _similarity(cand, existing) >= MERGE_SIMILARITY:
                    contested.append(
                        _question(
                            f"Sources give different figures for what happened on "
                            f"{existing.get('date')}: {existing.get('what')!r} versus "
                            f"{cand.get('what')!r}. Which is right, or do both stand attributed?",
                            origin="contested",
                            score=0.8,
                        )
                    )
                continue
            if _similarity(cand, existing) >= MERGE_SIMILARITY:
                match = existing
                break

        if match is not None:
            known = {str(s.get("url") or "") for s in match.get("sources") or []}
            fresh = [s for s in cand.get("sources") or [] if str(s.get("url") or "") not in known]
            if fresh:
                match["sources"] = (match.get("sources") or []) + fresh
                match["outlet_count"] = len(_outlets(match["sources"]))
            continue

        entry = dict(cand)
        entry["id"] = next_id
        next_id += 1
        entry["added_round"] = round_no
        entry["sources"] = entry.get("sources") or []
        entry["outlet_count"] = len(_outlets(entry["sources"]))
        entry.setdefault("actors", [])
        entry.setdefault("phase", None)
        ledger.append(entry)
        added.append(entry)

    return ledger, added, contested


def merge_entities(entities: list[dict], candidates: list[dict], today: str) -> list[dict]:
    """Name-keyed accumulation. An entity introduced in one round without a
    role and re-mentioned later with one must be UPDATED, not duplicated —
    otherwise §7's entity-asymmetry count is measuring noise. A known role or
    side is never silently overwritten by a vaguer one."""
    by_name = {normalise(str(e.get("name") or "")): e for e in entities}
    for cand in candidates:
        key = normalise(str(cand.get("name") or ""))
        if not key:
            continue
        existing = by_name.get(key)
        if existing is None:
            entity = dict(cand)
            entity.setdefault("first_seen", today)
            entity["last_seen"] = today
            by_name[key] = entity
            continue
        if not existing.get("role") and cand.get("role"):
            existing["role"] = cand["role"]
        if existing.get("side") in (None, "", "unknown") and cand.get("side"):
            existing["side"] = cand["side"]
        if not existing.get("kind") and cand.get("kind"):
            existing["kind"] = cand["kind"]
        existing["last_seen"] = today
    return list(by_name.values())


def recompute_span(ledger: list[dict]) -> dict:
    """The story's own timeline, recomputed from the ledger EVERY round.

    Fixing this at Pass B time would be a silent bug: later rounds are exactly
    when earlier-dated events surface, and a stale window makes the gap
    detector below bucket against a period that is already wrong — missing the
    pre-history this whole feature exists to find."""
    days = [d for d in (_day_of(e) for e in ledger) if d is not None]
    if not days:
        return {"start": None, "end": None}
    return {"start": min(days).isoformat(), "end": max(days).isoformat()}


# ---------- the detectors (dossier.md §7) ----------


def gap_questions(ledger: list[dict], span: dict, asked: Iterable[str] | None = None) -> list[dict]:
    """Weeks that are sparse relative to the story's OWN median density become
    questions. The failing run's implied ledger was May 3, May 12, June 6,
    nothing for seven weeks, then July 24-27 — and a seven-week hole in a
    rapidly escalating national story is not a quiet period. Detecting it takes
    arithmetic, not judgement, which is the entire point.

    A hole must be interior: a sparse week only becomes a question if the story
    is active within GAP_CONTEXT_WEEKS on both sides of it (see the note there).
    The decade of silence before a story's first precedent is not a hole in the
    record, it is the story not having started yet, and treating it as one is
    what made saturation unreachable on the first long-span follow.

    `asked` is the frontier's spent-question keys. Passing it does two things:
    it stops MAX_GAP_QUESTIONS being spent every round on the same already-asked
    weeks, hiding the ones behind them; and it is what lets `saturated()` mean
    "every hole we can see has been asked about" rather than "every hole has
    been filled" — a week that stayed empty after we searched it is answered,
    not outstanding."""
    start_s, end_s = span.get("start"), span.get("end")
    if not start_s or not end_s:
        return []
    try:
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError:
        return []
    if end <= start:
        return []

    buckets: dict[int, int] = {}
    weeks = int((end - start).days // 7) + 1
    for i in range(weeks):
        buckets[i] = 0
    for entry in ledger:
        d = _day_of(entry)
        if d is None or d < start or d > end:
            continue
        buckets[min(int((d - start).days // 7), weeks - 1)] += 1

    counts = sorted(buckets.values())
    if not counts:
        return []
    median = counts[len(counts) // 2]
    if median <= 0:
        median = max(1, sum(counts) // max(1, len(counts)))
    floor = median * GAP_DENSITY_RATIO
    active = {i for i, count in buckets.items() if count > floor}

    def _bracketed(i: int) -> bool:
        before = range(max(0, i - GAP_CONTEXT_WEEKS), i)
        after = range(i + 1, min(weeks, i + GAP_CONTEXT_WEEKS + 1))
        return any(j in active for j in before) and any(j in active for j in after)

    spent = set(asked or ())
    out: list[dict] = []
    for i in sorted(buckets):
        if i in active or not _bracketed(i):
            continue
        w_start = start + timedelta(days=7 * i)
        w_end = min(w_start + timedelta(days=6), end)
        question = _question(
            f"What happened in this story between {w_start.isoformat()} and "
            f"{w_end.isoformat()}? The record is nearly empty for that period.",
            origin="gap",
            score=0.75,
        )
        if normalise(question["text"]) in spent:
            continue
        out.append(question)

    if len(out) > MAX_GAP_QUESTIONS:
        # Most recent first: on a live story the near past is what a reader is
        # about to be told about. The rest are not lost — the detector runs
        # again every round, and `asked` moves the window along.
        dbg(f"dossier: gap detector held back {len(out) - MAX_GAP_QUESTIONS} of {len(out)} question(s)")
        out = out[-MAX_GAP_QUESTIONS:]
    return out


def entity_asymmetry_questions(entities: list[dict]) -> list[dict]:
    """A protest story whose entity table is ten officials and nobody from the
    movement has researched one half of its subject. Also arithmetic; also
    would have fired on the failing run."""
    sides = [str(e.get("side") or "unknown") for e in entities]
    state = sides.count("state")
    movement = sides.count("movement")
    if state >= 3 and movement == 0:
        return [
            _question(
                "Who are the named organisers, activists, students and protesters driving "
                "this story, and what have they personally done or had done to them?",
                origin="entity",
                score=0.85,
            )
        ]
    if movement >= 3 and state == 0:
        return [
            _question(
                "Which named officials, ministers, police commanders and agencies have "
                "acted in this story, and what did each of them do?",
                origin="entity",
                score=0.85,
            )
        ]
    return []


def role_questions(ledger: list[dict], entities: list[dict]) -> list[dict]:
    """An entity that appears in the ledger with no role is a detectable hole:
    a name in one July event and nowhere else is exactly the shape of a fact
    nobody followed up."""
    named = {normalise(a) for e in ledger for a in (e.get("actors") or [])}
    out: list[dict] = []
    for entity in entities:
        key = normalise(str(entity.get("name") or ""))
        if key and key in named and not entity.get("role"):
            out.append(
                _question(
                    f"Who is {entity.get('name')} and what part have they played in this story?",
                    origin="entity",
                    score=0.5,
                )
            )
    return out


def assign_phases(ledger: list[dict]) -> list[dict]:
    """Contiguous dense runs become phase labels, reused by the write pass so
    long prose follows the story's own shape rather than a flat chronology."""
    dated = sorted(
        (e for e in ledger if _day_of(e) is not None),
        key=lambda e: (_day_of(e) or date.min, e.get("id", 0)),
    )
    if not dated:
        return ledger
    phase_no = 1
    previous: date | None = None
    for entry in dated:
        d = _day_of(entry)
        if d is None:  # unreachable: `dated` is filtered, but keeps the types honest
            continue
        if previous is not None and (d - previous).days > 14:
            phase_no += 1
        entry["phase"] = f"phase-{phase_no}"
        previous = d
    for entry in ledger:
        if _day_of(entry) is None and not entry.get("phase"):
            entry["phase"] = "undated"
    return ledger


# ---------- the frontier (dossier.md §5, §6, §8) ----------


def _question(
    text: str, *, origin: str, score: float, depth: int = 0, parent: int | None = None,
    dimension: str | None = None,
) -> dict:
    return {
        "text": text.strip(),
        "origin": origin,
        "depth": depth,
        "score": round(float(score), 3),
        "parent": parent,
        "dimension": dimension,
    }


def admit(frontier: dict, questions: list[dict]) -> tuple[int, list[dict]]:
    """Add questions to the open queue, applying the drift guards.

    Recursive research drifts — "who is Nandan Nilekani" becomes "history of
    the Indian IT industry" — so a question is refused if it is already spent,
    already queued, too deep, or scored below the relevance floor. A refusal is
    RECORDED, never silent (§13)."""
    admitted = 0
    discarded: list[dict] = []
    open_keys = {normalise(q["text"]) for q in frontier["open"]}
    for q in questions:
        key = normalise(q["text"])
        if not key:
            continue
        if key in frontier["asked"] or key in open_keys:
            continue
        if int(q.get("depth", 0)) > MAX_QUESTION_DEPTH:
            discarded.append({**q, "why": "depth"})
            continue
        if float(q.get("score", 0.0)) < MIN_QUESTION_SCORE:
            discarded.append({**q, "why": "score"})
            continue
        frontier["open"].append(q)
        open_keys.add(key)
        admitted += 1
    if discarded:
        frontier["discarded"].extend(discarded)
        dbg(f"dossier: discarded {len(discarded)} question(s) below the drift guards")
    return admitted, discarded


def ensure_dimension_coverage(questions: list[dict]) -> list[dict]:
    """Inject a templated question for any checklist dimension the model left
    empty. "Mandatory" has to mean something a machine enforces — see the note
    on DIMENSIONS above."""
    covered = {q.get("dimension") for q in questions if q.get("dimension")}
    missing = [k for k in DIMENSIONS if k not in covered]
    if missing:
        dbg(f"dossier: dimension checklist gaps {missing}; injecting fallback question(s)")
    return [
        _question(DIMENSIONS[k], origin="gap", score=0.9, dimension=k) for k in missing
    ]


def pop_round(frontier: dict, limit: int = QUESTIONS_PER_ROUND) -> list[dict]:
    """Highest-scoring questions first, mechanical origins ahead of the model's
    own free association at equal score — a gap or an asymmetry is a measured
    hole, a model suggestion is a hunch."""
    priority = {"gap": 0, "entity": 1, "contested": 2, "dangling": 3, "model": 4}
    frontier["open"].sort(key=lambda q: (-float(q.get("score", 0)), priority.get(q.get("origin"), 9)))
    popped = frontier["open"][:limit]
    frontier["open"] = frontier["open"][len(popped) :]
    return popped


def batch_questions(questions: list[dict]) -> list[list[dict]]:
    """Group by checklist dimension, then origin, before splitting into calls.

    Batching whatever is next in the queue would reproduce the original failure
    in miniature: several unrelated questions in one call blur the searches the
    model runs, and narrow scope is the entire reason Pass C is many small calls
    instead of one big one."""
    groups: dict[str, list[dict]] = {}
    for q in questions:
        groups.setdefault(str(q.get("dimension") or q.get("origin") or "model"), []).append(q)
    batches: list[list[dict]] = []
    for group in groups.values():
        for i in range(0, len(group), QUESTIONS_PER_CALL):
            batches.append(group[i : i + QUESTIONS_PER_CALL])
    return batches


# ---------- saturation ----------


def saturated(dsr: dict) -> bool:
    """Loop-until-dry, not loop-N-times: a fixed count either quits early on a
    large story or burns calls on a small one. Both gap detectors must also be
    clear — a lean round with a seven-week hole still open is not saturation."""
    if dsr.get("lean_rounds", 0) < SATURATION_ROUNDS:
        return False
    if gap_questions(dsr["ledger"], dsr["span"], dsr["questions"]["asked"]):
        return False
    if entity_asymmetry_questions(dsr["entities"]):
        return False
    return True


def write_reserve(ledger: list[dict]) -> int:
    """Calls held back so research can never starve the write pass.

    Scales with the ledger, because phased writing above PHASED_WRITE_ENTRIES
    needs one call per phase — a flat reserve would starve exactly the large,
    well-researched dossiers this feature exists to produce."""
    return max(3, math.ceil(len(ledger) / PHASED_WRITE_ENTRIES) + 1)


# ---------- storage (dossier.md §10) ----------


def issue_dir(followed_dir: Path, issue: int) -> Path:
    return followed_dir / str(issue)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_dossier(issue: int, subject: str) -> dict:
    return {
        "issue": issue,
        "subject": subject,
        "span": {"start": None, "end": None},
        "research_state": "pending",
        # True only when the round ceiling stopped research with questions
        # still open — a dossier that is complete for the reader but did not
        # run dry. render.py says so on the page; see research()'s else branch.
        "rounds_exhausted": False,
        "rounds": 0,
        "calls": 0,
        "lean_rounds": 0,
        "ledger": [],
        "entities": [],
        "origin_claims": [],
        "questions": {"open": [], "in_flight": [], "asked": [], "discarded": []},
        "chips": [],
        "unreadable": {},
        "written_through": 0,
        "checkpoint": {"stage": "A", "round": 0, "updated_at": _now_iso()},
    }


def load(followed_dir: Path, issue: int) -> tuple[dict | None, dict]:
    """(dossier, corpus) for one issue. (None, {}) if Pass A never ran."""
    d = issue_dir(followed_dir, issue)
    try:
        dsr = json.loads((d / "dossier.json").read_text())
    except (OSError, ValueError):
        return None, {}
    try:
        corpus = json.loads((d / "corpus.json").read_text())
    except (OSError, ValueError):
        corpus = {}
    dsr.setdefault("questions", {"open": [], "in_flight": [], "asked": [], "discarded": []})
    dsr["questions"].setdefault("in_flight", [])
    dsr["questions"].setdefault("discarded", [])
    dsr.setdefault("unreadable", {})
    dsr.setdefault("lean_rounds", 0)
    dsr.setdefault("written_through", 0)
    return dsr, corpus


def save(followed_dir: Path, issue: int, dsr: dict, corpus: dict, stage: str) -> None:
    """Checkpoint. Called after EVERY completed Gemini call, not once per
    round — that is what converts every failure mode into a pause rather than
    a loss (§9). A killed process, an exhausted wait budget and a day-scoped
    429 all leave the same thing behind: a dossier that resumes."""
    d = issue_dir(followed_dir, issue)
    d.mkdir(parents=True, exist_ok=True)
    dsr["checkpoint"] = {"stage": stage, "round": dsr.get("rounds", 0), "updated_at": _now_iso()}
    (d / "dossier.json").write_text(json.dumps(dsr, indent=2, ensure_ascii=False) + "\n")
    (d / "corpus.json").write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")


def needs_research(dsr: dict | None) -> bool:
    return dsr is not None and dsr.get("research_state") in ("pending", "researching")


def is_readable(dsr: dict | None) -> bool:
    """Whether there is enough evidence to write prose from at all."""
    return dsr is not None and dsr.get("research_state") in ("complete", "capped")


# ---------- the shared daily budget (dossier.md §9, §14) ----------


class Budget:
    """One day's Gemini allowance, shared across every follow — two pools.

    §12 gives each active follow its own research instead of one call batched
    across all of them, so recurring cost now scales with the number of
    follows. This bounds it. Lives at followed/_budget/<date>.json, which
    matches neither of load_all()'s glob patterns and so is structurally
    invisible to record loading.

    Two counters because the free tier meters per model. A grounded search and
    a schema-only ledger call come out of different daily allowances, and
    counting them together would idle half the capacity — the first live run
    stopped at three rounds with an entire second pool untouched.

    No locking: digest.yml, follow.yml and render.yml all share
    `concurrency: group: pages` with cancel-in-progress false, so GitHub
    already guarantees these never run at once."""

    def __init__(
        self,
        path: Path,
        cap: int = MAX_GROUNDED_CALLS_PER_DAY,
        schema_cap: int = MAX_SCHEMA_CALLS_PER_DAY,
    ) -> None:
        self.path = path
        self.limits_path = path.parent / "limits.json"
        self.learned = self._load_learned()
        # A ceiling the server has actually enforced always beats a guess.
        self.cap = self._cap_for("grounded", cap)
        self.schema_cap = self._cap_for("schema", schema_cap)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        self.spent = int(state.get("spent", 0))
        self.schema_spent = int(state.get("schema_spent", 0))
        self.day_quota_hit = bool(state.get("daily_quota_exhausted", False))
        self.schema_quota_hit = bool(state.get("schema_quota_exhausted", False))
        self.spends: list[dict] = list(state.get("spends") or [])
        self.deferred: list[dict] = list(state.get("deferred") or [])

    def _load_learned(self) -> dict:
        try:
            return json.loads(self.limits_path.read_text())
        except (OSError, ValueError):
            return {}

    def _cap_for(self, pool: str, default: int) -> int:
        """A learned ceiling wins over the optimistic default, minus a margin
        so the last call of the day is ours rather than a wasted 429 — until
        it goes stale, at which point we re-discover rather than trust a
        number the provider may have moved."""
        entry = self.learned.get(pool) or {}
        rpd = entry.get("rpd")
        if not isinstance(rpd, int) or rpd <= 0:
            return default
        if _stale(str(entry.get("learned_at") or "")):
            dbg(f"dossier: {pool} ceiling of {rpd} is over {LEARNED_LIMIT_TTL_DAYS} days old; re-probing")
            return default
        return max(1, min(default, rpd - QUOTA_SAFETY_MARGIN))

    def learn(self, pool: str, rpd: int, model: str = "") -> None:
        """Remember a ceiling the server just enforced, so tomorrow stops one
        call short of it instead of rediscovering it the hard way.

        A ceiling the server RE-CONFIRMS refreshes `learned_at`, even when the
        number has not moved. Returning early on `known == rpd` (as this did
        until 2026-08-23) meant a still-correct ceiling could never clear its
        own staleness: every run after the TTL printed "over 14 days old;
        re-probing", nothing re-probed, and the only thing that could ever
        silence the line was the limit CHANGING (ANALYSIS-2026-08-23.md §L1)."""
        if rpd <= 0:
            return
        known = (self.learned.get(pool) or {}).get("rpd")
        self.learned[pool] = {"rpd": rpd, "model": model, "learned_at": _now_iso()}
        if known == rpd:
            dbg(f"dossier: {pool} daily ceiling re-confirmed at {rpd}; staleness clock reset")
        else:
            dbg(f"dossier: learned {pool} daily ceiling = {rpd} request(s); recorded for future runs")
            tracer.event("dossier", verdict="quota_learned", pool=pool, rpd=rpd, model=model)
        try:
            self.limits_path.parent.mkdir(parents=True, exist_ok=True)
            self.limits_path.write_text(json.dumps(self.learned, indent=2) + "\n")
        except OSError as exc:
            dbg(f"dossier: could not write {self.limits_path} ({exc!r})")

    def remaining(self) -> int:
        return max(0, self.cap - self.spent)

    def schema_remaining(self) -> int:
        return max(0, self.schema_cap - self.schema_spent)

    def spend(self, issue: int, n: int = 1, *, pool: str = "grounded") -> None:
        if pool == "schema":
            self.schema_spent += n
        else:
            self.spent += n
        self.spends.append({"issue": issue, "calls": n, "pool": pool, "at": _now_iso()})
        self.save()

    def defer(self, issue: int, reason: str) -> None:
        dbg(f"dossier: #{issue} deferred ({reason})")
        self.deferred.append({"issue": issue, "reason": reason, "at": _now_iso()})
        tracer.event("dossier", issue=issue, verdict="deferred", reason=reason)
        self.save()

    def mark_daily_quota(self, pool: str = "grounded") -> None:
        """One pool being exhausted says nothing about the other — the limit is
        per model. Marking both would idle capacity that is still there."""
        if pool == "schema":
            self.schema_quota_hit = True
        else:
            self.day_quota_hit = True
        self.save()

    def exhausted(self, pool: str = "grounded") -> bool:
        if pool == "schema":
            return self.schema_quota_hit or self.schema_remaining() <= 0
        return self.day_quota_hit or self.remaining() <= 0

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "cap": self.cap,
                        "spent": self.spent,
                        "schema_cap": self.schema_cap,
                        "schema_spent": self.schema_spent,
                        "daily_quota_exhausted": self.day_quota_hit,
                        "schema_quota_exhausted": self.schema_quota_hit,
                        "spends": self.spends,
                        "deferred": self.deferred,
                    },
                    indent=2,
                )
                + "\n"
            )
        except OSError as exc:
            dbg(f"dossier: could not write budget {self.path} ({exc!r})")


def _stale(learned_at: str) -> bool:
    try:
        when = datetime.strptime(learned_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - when).days >= LEARNED_LIMIT_TTL_DAYS


def load_budget(followed_dir: Path, today: date) -> Budget:
    return Budget(followed_dir / "_budget" / f"{today:%Y-%m-%d}.json")


class DailyQuotaExhausted(Exception):
    """A PerDay 429. Account-wide, so the whole sweep stops rather than each
    follow rediscovering it in turn."""


# ---------- prompts ----------
#
# _PROSE_RULES moved here from follow.py: dossier.py owns followed-story prose
# now, and follow.py imports this module (so the reverse import would cycle).
# The rules themselves are UNCHANGED and still binding — dossier.md §11 is
# explicit that removing the word limit does not license a research dump.

_PROSE_RULES = """\
Write in plain adult English for a non-native reader: short sentences, \
active voice, concrete nouns, no jargon, no idioms. Clear, but not \
simplified — a good explainer site, not a children's news service.

PLAIN TEXT ONLY. Never use markdown — no headings, no "#", no "*" or "-" \
bullets, no bold, no links. Your output is inserted directly into a web \
page as prose; any markup would appear as literal characters.

Write one continuous piece of prose per story, paragraphs separated by a \
single blank line. Never use a heading, a label, or a section such as \
"Why this matters" or "What to watch".

Never present a contested claim as settled. Where sources disagree, or a \
fact is somebody's assertion, keep the attribution — "the ministry says", \
"the BBC reported" — and say plainly where reporting disagrees or facts \
are still uncertain.

Never blend or average a figure from two sources into one number. If \
sources disagree on a number, state both and attribute each."""

_PLAN_SYSTEM = f"""You are planning deep research into a news story someone \
has chosen to follow closely. You are NOT writing anything yet.

First, name the story at its FULL SCOPE. The headline you are given is the \
day's news peg, not the story. If the real subject is a months-long movement, \
a scandal, or a conflict that the headline is merely the latest instalment \
of, say so — renaming it away from the peg is expected, not a liberty.

Then propose research questions. Cover EVERY one of these dimensions:

{chr(10).join(f"  {k}: {v}" for k, v in DIMENSIONS.items())}

Output format, exactly — no markdown, no extra prose:

SUBJECT: <the story at full scope, one line>

Then one line per question, in this shape:
Q|<dimension key>|<0.0-1.0 relevance>|<the question>

Ask 25 to 40 questions. A question is relevant if answering it makes THIS \
story clearer to someone following it — not if it is merely interesting. \
Prefer narrow, answerable questions naming people, dates and places over \
broad ones. Never ask about general background unconnected to this story."""

_SEARCH_SYSTEM = """You are researching specific questions about one news \
story using Google Search. For EACH question block in the input, in the same \
order, emit a block starting with the identical "=== BLOCK <key> ===" header \
line, then your findings for that question.

Report concrete, checkable facts: what happened, on what date, to whom, said \
by whom. Give exact dates wherever the sources give them. Name people. If the \
sources disagree, say so and give both accounts. If you find nothing, say \
"Nothing found" for that block and move on — an empty answer is correct and \
useful; an invented one is not.

PLAIN TEXT ONLY. No markdown, no headings, no bullets."""

_READ_SYSTEM = """You are reading news pages that a scraper could not \
extract. Report every dated, factual event each page describes: what \
happened, when, to whom, and who said so. Quote figures exactly as the page \
gives them and never average two figures together.

Attribute every fact to the URL it came from. If a page could not be read or \
contains nothing relevant, say so plainly for that URL.

PLAIN TEXT ONLY. No markdown, no headings, no bullets."""

_LEDGER_SYSTEM = """You are building a dated evidence ledger for one news \
story. You are given research findings and article text. Extract every \
distinct, dated event into a ledger entry, and every named person, \
organisation and place into the entity table.

Rules:
- One entry per EVENT, not per sentence. "Police charged the crowd and fired \
tear gas" on one day is one entry.
- `date` must be YYYY-MM-DD with precision "day", or YYYY-MM with precision \
"month" when only the month is known. Never guess a day you were not told.
- Never blend figures. If two sources give different numbers for the same \
event, emit TWO entries, each with its own source.
- `what` is ONE plain sentence stating what happened.
- `sources` must be real URLs drawn from the material you were given. Never \
invent a URL, and never cite a page you were not shown.
- `side` for an entity: "state" for governments, ministers, police, agencies \
and courts; "movement" for protesters, organisers, activists, students, \
unions and opposition campaigners; "other" for anyone else; "unknown" if you \
genuinely cannot tell.
- Also propose follow-up questions: things the material refers to but does \
not explain, and gaps you noticed. Score each 0.0-1.0 for how much answering \
it would clarify THIS story."""

_WRITE_SYSTEM = f"""You are writing the full-picture explainer for a story \
someone is following closely, from a dated evidence ledger.

You may use ONLY the ledger entries given. A fact that is not in the ledger \
has no place in the prose. Do not add background, context or analysis from \
your own knowledge.

CITE EVERY STATEMENT. Immediately after each statement of fact, before its \
full stop, put that entry's marker: [e12]. A statement drawn from two entries \
takes both: [e12, e18]. This is not optional decoration — an uncited sentence \
is treated as unsourced and the whole piece is rejected. Aim for a marker on \
every sentence that states a fact.

Where an entry's outlet_count is 1, attribute it in the prose ("Hindustan \
Times reported that..."). Where several outlets carry it, state it plainly.

Write chronologically: how it started, what drove it, what happened since, \
and where it stands now. Assume the reader has read nothing about this \
before. Cover every entry you are given — a reader following this story needs \
the whole arc, not the highlights.

There is no word limit. Write as much as the evidence needs and no more.

{_PROSE_RULES}"""

_UPDATE_SYSTEM = f"""You are writing one dated update for a story someone is \
already following. You are given ONLY the new ledger entries for this period; \
the reader has already read everything before them.

Report what is new. Never restate the backstory. Cover EVERY entry you are \
given — leaving one out is as serious an error as getting one wrong.

CITE EVERY STATEMENT with its entry marker before the full stop: [e12]. An \
uncited sentence is treated as unsourced.

{_PROSE_RULES}"""

# ---------- schemas (search OFF, so response_schema is legal) ----------

_LEDGER_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "precision": {"type": "string", "enum": ["day", "month"]},
                    "what": {"type": "string"},
                    "actors": {"type": "array", "items": {"type": "string"}},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"outlet": {"type": "string"}, "url": {"type": "string"}},
                            "required": ["outlet", "url"],
                        },
                    },
                },
                "required": ["date", "precision", "what", "sources"],
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["person", "org", "place"]},
                    "role": {"type": "string"},
                    "side": {"type": "string", "enum": ["state", "movement", "other", "unknown"]},
                },
                "required": ["name", "kind", "side"],
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "score": {"type": "number"},
                    "origin": {"type": "string", "enum": ["model", "dangling", "contested"]},
                },
                "required": ["text", "score"],
            },
        },
    },
    "required": ["entries"],
}

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {"body": {"type": "string"}},
    "required": ["body"],
}

_CRITIC_SYSTEM = """You are auditing a research ledger for completeness, using \
Google Search to check what is missing. You are given a story's subject, its \
dated ledger and its entity table.

Name what is ABSENT. Significant events not in the ledger. People central to \
the story who do not appear. Injuries, deaths, arrests or turning points that \
are uncovered. Periods that are thinly recorded.

Output ONLY questions, one per line, in this shape — no prose, no preamble:
Q|<0.0-1.0 relevance>|<the question>

If the ledger genuinely looks complete, output nothing at all."""


# ---------- call accounting ----------


def _afford(dsr: dict, budget: Budget, reserve: int, pool: str = "grounded") -> bool:
    """Whether one more RESEARCH call is affordable from `pool`.

    `reserve` is held back against the LIFETIME ceiling so a dossier can never
    research itself into a state it cannot publish. The write pass draws on the
    schema pool, so a grounded round is only ever blocked by grounded quota."""
    if budget.exhausted(pool):
        return False
    return dsr["calls"] + reserve < MAX_CALLS_PER_FOLLOW


def _spend(dsr: dict, budget: Budget, pool: str = "grounded") -> None:
    dsr["calls"] += 1
    dsr.setdefault("calls_by_pool", {"grounded": 0, "schema": 0})
    dsr["calls_by_pool"][pool] = dsr["calls_by_pool"].get(pool, 0) + 1
    budget.spend(int(dsr["issue"]), pool=pool)


def _guarded(fn, dsr: dict, budget: Budget, pool: str = "grounded"):
    """Run one Gemini call, converting a day-scoped 429 into a clean stop.

    ratelimit.call_with_resume already waits out a minute-scoped 429 and
    re-raises a PerDay one immediately (correctly — sleeping for hours inside
    one job is worse than letting tomorrow's staggered cron pick it up). This
    turns that re-raise into DailyQuotaExhausted so the whole sweep stops
    rather than each remaining follow rediscovering it in turn."""
    import ratelimit

    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - classified immediately below
        if ratelimit.is_daily_quota(exc):
            spent = budget.spent if pool == "grounded" else budget.schema_spent
            dbg(
                f"dossier: #{dsr['issue']} {pool} daily quota exhausted after {spent} call(s) "
                f"{ratelimit.quota_facts(exc)}; checkpointing"
            )
            limit = ratelimit.daily_limit(exc)
            if limit is not None:
                budget.learn(pool, limit, ground.GROUND_MODEL if pool == "grounded" else ground.SCHEMA_MODEL)
            else:
                # The server did not name a number, so the only evidence of the
                # ceiling is what we managed to spend before it said no.
                budget.learn(pool, max(1, spent), "")
            budget.mark_daily_quota(pool)
            raise DailyQuotaExhausted(pool) from exc
        raise


# ---------- Pass A: seed from what is already known. 0 API calls. ----------


def _origin_story(data_dir: Path, origin: dict) -> dict | None:
    """The digest story this follow was opened on, from data/<date>.json."""
    try:
        day = json.loads((data_dir / f"{origin.get('date')}.json").read_text())
    except (OSError, ValueError):
        return None
    stories = day.get("stories") or []
    section = str(origin.get("section") or "")
    position = origin.get("position")
    in_section = [s for s in stories if s.get("section") == section]
    if isinstance(position, int) and 1 <= position <= len(in_section):
        candidate = in_section[position - 1]
        if not origin.get("headline") or candidate.get("headline") == origin.get("headline"):
            return candidate
    for story in stories:
        if story.get("headline") == origin.get("headline"):
            return story
    return None


def seed(followed_dir: Path, data_dir: Path, issue: int, origin: dict, headline: str) -> tuple[dict, dict]:
    """Pass A. Zero API calls, so it is always affordable and always runs first.

    The pipeline ALREADY HAD the missing facts and discarded them — that is
    finding #1 of dossier.md §1. Every claim the digest anchored for this story
    is staged here, and every source URL is extracted into the corpus.

    Claims are staged, not converted: anchor.Claim carries no date, and Python
    cannot reliably read "nine days later" out of prose into a controlled
    day/month precision. Pass B is the first point with both a model and this
    evidence in hand, so it does the dating."""
    dsr = new_dossier(issue, headline)
    corpus: dict[str, dict] = {}

    story = _origin_story(data_dir, origin)
    if story is None:
        dbg(f"dossier: #{issue} -> origin story not found in data/{origin.get('date')}.json")
        return dsr, corpus

    dsr["origin_claims"] = [
        {
            "text": str(c.get("text") or ""),
            "outlet": str(c.get("outlet") or ""),
            "url": str(c.get("url") or ""),
        }
        for c in story.get("claims") or []
    ]

    urls = [str(s.get("url") or "") for s in story.get("sources") or [] if s.get("url")]
    for url in urls[:MAX_FETCH_PER_ROUND]:
        text = extract.article_text(url)
        if text:
            corpus[url] = {
                "outlet": _outlet_for(url, story),
                "fetched_at": _now_iso(),
                "chars": len(text),
                "text": text,
                "via": "extract",
            }
        else:
            dsr["unreadable"][url] = "extract_failed"

    dbg(
        f"dossier: #{issue} Pass A -> {len(dsr['origin_claims'])} claim(s) staged, "
        f"{len(corpus)} page(s) in corpus, {len(dsr['unreadable'])} unreadable"
    )
    tracer.count(dossier_seed_claims=len(dsr["origin_claims"]), dossier_seed_pages=len(corpus))
    return dsr, corpus


def _outlet_for(url: str, story: dict) -> str:
    for s in story.get("sources") or []:
        if s.get("url") == url:
            return str(s.get("outlet") or "")
    return ""


# ---------- Pass B: name the story and plan the research. 1 grounded call. ----------


def _parse_plan(text: str) -> tuple[str, list[dict]]:
    subject = ""
    questions: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not subject and line.upper().startswith("SUBJECT:"):
            subject = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("Q|"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        _, dimension, score_s, question = parts
        try:
            score = float(score_s.strip())
        except ValueError:
            score = 0.5
        dimension = dimension.strip().lower()
        questions.append(
            _question(
                question,
                origin="model",
                score=score,
                dimension=dimension if dimension in DIMENSIONS else None,
            )
        )
    return subject, questions


def pass_b(dsr: dict, budget: Budget) -> None:
    """Name the story at full scope, and plan the research against the
    mandatory checklist. Grounded: deciding that "PM Modi creates a task
    force" is really "the 2026 exam-leak protest movement" is discovery work,
    not reasoning over what we already hold."""
    claims = "\n".join(f"- ({c['outlet']}) {c['text']}" for c in dsr.get("origin_claims") or [])
    prompt = (
        f"HEADLINE AS PUBLISHED: {dsr['subject']}\n\n"
        f"WHAT THE DIGEST ALREADY GATHERED ABOUT IT:\n{claims or '(nothing)'}\n\n"
        "Name this story at its full scope, then plan the research."
    )
    text, _meta = _guarded(
        lambda: ground._generate(prompt, _PLAN_SYSTEM, f"dossier-{dsr['issue']}-plan"), dsr, budget
    )
    _spend(dsr, budget)

    subject, questions = _parse_plan(text)
    if subject:
        dsr["subject"] = subject
        dbg(f"dossier: #{dsr['issue']} subject -> {subject!r}")
    questions.extend(ensure_dimension_coverage(questions))
    admitted, _ = admit(dsr["questions"], questions)
    dbg(f"dossier: #{dsr['issue']} Pass B -> {admitted} question(s) admitted")
    tracer.count(dossier_plan_questions=admitted)


# ---------- Pass C: search, narrowly and in parallel ----------


def pass_c(dsr: dict, budget: Budget, questions: list[dict]) -> list[str]:
    """One grounded call per small group of CLOSELY RELATED questions. Breadth
    comes from many narrow calls; a broad call produces broad queries, which is
    how the failing run ended up with four headline-shaped searches."""
    findings: list[str] = []
    for batch in batch_questions(questions):
        if not _afford(dsr, budget, write_reserve(dsr["ledger"])):
            dsr["questions"]["open"] = batch + dsr["questions"]["open"]
            break
        keys = [f"q{i}" for i in range(len(batch))]
        prompt = "\n\n".join(
            f"=== BLOCK {k} ===\nSTORY: {dsr['subject']}\nQUESTION: {q['text']}"
            for k, q in zip(keys, batch)
        )
        result = _guarded(
            lambda: ground.research_blocks(
                prompt, _SEARCH_SYSTEM, f"dossier-{dsr['issue']}-r{dsr['rounds']}-search", keys
            ),
            dsr,
            budget,
        )
        _spend(dsr, budget)
        for k, q in zip(keys, batch):
            _status, block = result.get(k, ("quiet", None))
            if block is not None and block.body.strip():
                findings.append(f"QUESTION: {q['text']}\nFINDINGS: {block.body}")
                for src in block.sources:
                    findings.append(f"SOURCE: {src.get('outlet')} {src.get('url')}")
            dsr["questions"]["asked"].append(normalise(q["text"]))
            if block is not None and block.search_suggestions:
                if block.search_suggestions not in dsr["chips"]:
                    dsr["chips"].append(block.search_suggestions)
    return findings


# ---------- Pass D: read the sources ----------


def pass_d(dsr: dict, corpus: dict, budget: Budget, urls: list[str]) -> list[str]:
    """extract.py first because it is free; url_context only for what it
    cannot get. url_context READS a page, it does not return it — so what
    comes back is the model's account of pages it was shown, recorded with
    via="url_context" so the corpus never pretends to hold verbatim text it
    does not have."""
    fresh = [u for u in urls if u and u not in corpus and u not in dsr["unreadable"]][:MAX_FETCH_PER_ROUND]
    unread: list[str] = []
    for url in fresh:
        text = extract.article_text(url)
        if text:
            corpus[url] = {
                "outlet": "",
                "fetched_at": _now_iso(),
                "chars": len(text),
                "text": text,
                "via": "extract",
            }
        else:
            unread.append(url)

    notes: list[str] = []
    if unread and _afford(dsr, budget, write_reserve(dsr["ledger"])):
        batch = unread[:MAX_URLS_PER_CONTEXT_CALL]
        prompt = "Read these pages and report every dated event each one describes:\n" + "\n".join(batch)
        block = _guarded(
            lambda: ground.read_urls(
                prompt, _READ_SYSTEM, f"dossier-{dsr['issue']}-r{dsr['rounds']}-read", batch
            ),
            dsr,
            budget,
        )
        _spend(dsr, budget)
        if block is not None and block.body.strip():
            notes.append(block.body)
            for url in batch:
                corpus[url] = {
                    "outlet": "",
                    "fetched_at": _now_iso(),
                    "chars": None,
                    "text": None,
                    "via": "url_context",
                }
        for url in unread[MAX_URLS_PER_CONTEXT_CALL:]:
            dsr["unreadable"][url] = "over_url_context_batch"
    else:
        for url in unread:
            dsr["unreadable"][url] = "extract_failed_no_budget"
    return notes


# ---------- Pass E: extend the ledger. 1 call, search OFF, schema ON. ----------


def _corpus_block(corpus: dict, urls: list[str], cap: int = 4000) -> str:
    out = []
    for url in urls:
        item = corpus.get(url) or {}
        text = item.get("text")
        if text:
            out.append(f"--- {url} ---\n{text[:cap]}")
    return "\n\n".join(out)


def pass_e(dsr: dict, corpus: dict, budget: Budget, material: list[str], fresh_urls: list[str]) -> int:
    """Turn this round's material into dated ledger entries, entities and
    follow-up questions. Search is OFF, so response_schema works and this pass
    gets validated JSON instead of a delimited format to re-parse by hand.

    Pass F (next questions) rides along in the same call — a separate round
    trip for "what should I ask next" would cost a call to learn something the
    model already knows at the end of this one."""
    body = "\n\n".join(material)
    corpus_text = _corpus_block(corpus, fresh_urls)
    entity_table = "\n".join(
        f"- {e.get('name')} ({e.get('kind')}, {e.get('side')}): {e.get('role') or 'role unknown'}"
        for e in dsr["entities"]
    )
    prompt = (
        f"STORY: {dsr['subject']}\n\n"
        f"ENTITIES ALREADY KNOWN (update these, do not duplicate them):\n{entity_table or '(none yet)'}\n\n"
        f"RESEARCH FINDINGS:\n{body or '(none)'}\n\n"
        f"ARTICLE TEXT:\n{corpus_text or '(none)'}"
    )
    payload = _guarded(
        lambda: ground.structured(
            prompt,
            _LEDGER_SYSTEM,
            f"dossier-{dsr['issue']}-r{dsr['rounds']}-ledger",
            _LEDGER_SCHEMA,
        ),
        dsr,
        budget,
        "schema",
    )
    _spend(dsr, budget, "schema")
    if not isinstance(payload, dict):
        return 0

    candidates = []
    for raw in payload.get("entries") or []:
        date_s = str(raw.get("date") or "").strip()
        precision = str(raw.get("precision") or "day").strip()
        if not _valid_date(date_s, precision):
            continue
        what = str(raw.get("what") or "").strip()
        if len(what) < 20:
            continue
        sources = [
            {"outlet": str(s.get("outlet") or ""), "url": str(s.get("url") or "")}
            for s in raw.get("sources") or []
            if s.get("url")
        ]
        if not sources:
            continue
        candidates.append(
            {
                "date": date_s,
                "precision": precision,
                "what": what,
                "actors": [str(a) for a in raw.get("actors") or []],
                "sources": sources,
            }
        )

    dsr["ledger"], added, contested = merge_entries(dsr["ledger"], candidates, dsr["rounds"])
    dsr["entities"] = merge_entities(
        dsr["entities"], list(payload.get("entities") or []), _now_iso()[:10]
    )
    dsr["span"] = recompute_span(dsr["ledger"])

    followups = [
        _question(
            str(q.get("text") or ""),
            origin=str(q.get("origin") or "model"),
            score=float(q.get("score", 0.5) or 0.5),
            depth=dsr["rounds"],
        )
        for q in payload.get("questions") or []
        if str(q.get("text") or "").strip()
    ]
    admit(dsr["questions"], followups + contested)
    dbg(
        f"dossier: #{dsr['issue']} Pass E -> +{len(added)} entr(y/ies), "
        f"{len(dsr['ledger'])} total, {len(dsr['entities'])} entities"
    )
    return len(added)


# ---------- Pass G: completeness critic. 1 grounded call. ----------


def pass_g(dsr: dict, budget: Budget) -> int:
    ledger_view = "\n".join(
        f"- {e.get('date')}: {e.get('what')}" for e in sorted(
            dsr["ledger"], key=lambda e: str(e.get("date") or "")
        )
    )
    entity_view = ", ".join(str(e.get("name")) for e in dsr["entities"])
    prompt = (
        f"SUBJECT: {dsr['subject']}\n\n"
        f"LEDGER SO FAR:\n{ledger_view or '(empty)'}\n\n"
        f"ENTITIES SO FAR: {entity_view or '(none)'}\n\n"
        "What significant parts of this story are missing?"
    )
    text, _meta = _guarded(
        lambda: ground._generate(
            prompt, _CRITIC_SYSTEM, f"dossier-{dsr['issue']}-r{dsr['rounds']}-critic"
        ),
        dsr,
        budget,
    )
    _spend(dsr, budget)

    questions = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Q|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            score = float(parts[1].strip())
        except ValueError:
            score = 0.5
        questions.append(_question(parts[2], origin="model", score=score, depth=dsr["rounds"]))
    admitted, _ = admit(dsr["questions"], questions)
    dbg(f"dossier: #{dsr['issue']} Pass G -> {admitted} gap question(s)")
    return admitted


# ---------- the research loop ----------


def _urls_in(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>\"')\]]+", text)


def research(followed_dir: Path, issue: int, dsr: dict, corpus: dict, budget: Budget) -> str:
    """Run rounds until saturation, a ceiling, or the budget stops us.

    Checkpointed after every call, so an interrupted run resumes rather than
    restarting. Returns the terminal research_state: complete | capped |
    researching (out of budget, resume next run).
    """
    import time

    started = time.monotonic()
    dsr["research_state"] = "researching"
    save(followed_dir, issue, dsr, corpus, "B")

    if not dsr["questions"]["asked"] and not dsr["questions"]["open"]:
        pass_b(dsr, budget)
        save(followed_dir, issue, dsr, corpus, "B")

    while dsr["rounds"] < MAX_ROUNDS:
        reserve = write_reserve(dsr["ledger"])
        if not _afford(dsr, budget, reserve):
            reason = (
                "daily_quota" if budget.day_quota_hit
                else "day_budget" if budget.remaining() <= 0
                else "call_ceiling"
            )
            if reason == "call_ceiling":
                dsr["research_state"] = "capped"
                dbg(
                    f"dossier: #{issue} CAPPED at {dsr['calls']} call(s); "
                    f"{len(dsr['questions']['open'])} question(s) left in the frontier"
                )
                tracer.event(
                    "dossier", issue=issue, verdict="capped", calls=dsr["calls"],
                    frontier_left=len(dsr["questions"]["open"]),
                )
            else:
                budget.defer(issue, reason)
            break

        if time.monotonic() - started > MAX_RESEARCH_SECONDS:
            dbg(f"dossier: #{issue} wall-clock guard hit; checkpointing for a later run")
            budget.defer(issue, "wall_clock")
            break

        questions = pop_round(dsr["questions"])
        if not questions:
            dbg(f"dossier: #{issue} frontier empty")
            dsr["research_state"] = "complete"
            dsr["rounds_exhausted"] = False
            break

        dsr["rounds"] += 1
        before = len(dsr["ledger"])

        findings = pass_c(dsr, budget, questions)
        save(followed_dir, issue, dsr, corpus, "C")

        urls = [u for f in findings for u in _urls_in(f)]
        notes = pass_d(dsr, corpus, budget, urls)
        save(followed_dir, issue, dsr, corpus, "D")

        material = findings + notes
        if dsr["rounds"] == 1 and dsr.get("origin_claims"):
            material.insert(
                0,
                "CLAIMS THE DIGEST ALREADY ANCHORED FOR THIS STORY:\n"
                + "\n".join(
                    f"- ({c['outlet']}) {c['text']} [{c['url']}]" for c in dsr["origin_claims"]
                ),
            )
        fresh_urls = [u for u in urls if u in corpus] + [
            u for u, v in corpus.items() if v.get("via") == "extract"
        ][:MAX_FETCH_PER_ROUND]

        if _afford(dsr, budget, reserve, "schema"):
            pass_e(dsr, corpus, budget, material, list(dict.fromkeys(fresh_urls)))
            save(followed_dir, issue, dsr, corpus, "E")
        else:
            dbg(f"dossier: #{issue} skipped Pass E — schema pool exhausted")

        dsr["ledger"] = assign_phases(dsr["ledger"])
        admit(dsr["questions"], gap_questions(dsr["ledger"], dsr["span"], dsr["questions"]["asked"]))
        admit(dsr["questions"], entity_asymmetry_questions(dsr["entities"]))
        admit(dsr["questions"], role_questions(dsr["ledger"], dsr["entities"]))

        if dsr["rounds"] % CRITIC_EVERY == 0 and _afford(dsr, budget, reserve):
            pass_g(dsr, budget)
            save(followed_dir, issue, dsr, corpus, "G")

        gained = len(dsr["ledger"]) - before
        dsr["lean_rounds"] = dsr["lean_rounds"] + 1 if gained < SATURATION_ENTRIES else 0
        dbg(
            f"dossier: #{issue} round {dsr['rounds']} -> +{gained} entr(y/ies), "
            f"{dsr['calls']} call(s) spent, {len(dsr['questions']['open'])} question(s) open"
        )

        if saturated(dsr):
            dbg(f"dossier: #{issue} saturated after {dsr['rounds']} round(s)")
            dsr["research_state"] = "complete"
            dsr["rounds_exhausted"] = False
            break
    else:
        # The ROUND ceiling, not saturation: this branch wrote the identical
        # "complete" the two branches above write, with no dbg() and no event,
        # so a dossier that simply ran out of rounds rendered "The full picture"
        # exactly like one that had genuinely run dry (ANALYSIS-2026-08-23.md
        # §M2). It stays "complete" — the state that lets tomorrow's delta pass
        # run at all, and `capped` would freeze the follow forever — but it says
        # so, and `rounds_exhausted` is what render.py shows the reader.
        #
        # Note _update_follow deliberately lowers MAX_ROUNDS to rounds+1 for a
        # one-round delta, and lands here every single day. That is not a cap:
        # what distinguishes them is an open frontier this run could not reach.
        left = len(dsr["questions"]["open"])
        dsr["research_state"] = "complete"
        dsr["rounds_exhausted"] = bool(left)
        if left:
            dbg(
                f"dossier: #{issue} ROUND CEILING at {dsr['rounds']} round(s) "
                f"(MAX_ROUNDS={MAX_ROUNDS}) with {left} question(s) still open; "
                "reporting complete-but-capped, not saturated"
            )
            tracer.event(
                "dossier", issue=issue, verdict="rounds_exhausted",
                rounds=dsr["rounds"], max_rounds=MAX_ROUNDS, frontier_left=left,
            )
        else:
            dbg(f"dossier: #{issue} frontier empty at the round ceiling; complete")

    save(followed_dir, issue, dsr, corpus, "DONE")
    _capture(dsr)
    return dsr["research_state"]


def _capture(dsr: dict) -> None:
    """The dossier's own evidence, alongside ground.py's per-call artifacts.

    corpus.json is deliberately NOT duplicated here — it is already committed
    at followed/<issue>/corpus.json, which is the record. Copying a few hundred
    KB of article text into debug/ as well would inflate the calibration window
    for nothing."""
    tracer.artifact_json(
        f"dossier/{dsr['issue']}/index.json",
        {
            "issue": dsr["issue"],
            "subject": dsr["subject"],
            "research_state": dsr["research_state"],
            "rounds_exhausted": bool(dsr.get("rounds_exhausted")),
            "rounds": dsr["rounds"],
            "calls": dsr["calls"],
            "span": dsr["span"],
            "ledger_entries": len(dsr["ledger"]),
            "entities": len(dsr["entities"]),
            "sides": {
                s: sum(1 for e in dsr["entities"] if e.get("side") == s)
                for s in ("state", "movement", "other", "unknown")
            },
            "questions_asked": len(dsr["questions"]["asked"]),
            "questions_open": len(dsr["questions"]["open"]),
            "questions_discarded": len(dsr["questions"]["discarded"]),
            "unreadable": dsr["unreadable"],
            "chips": len(dsr["chips"]),
        },
    )
    tracer.artifact_json(
        f"dossier/{dsr['issue']}/discarded-questions.json", dsr["questions"]["discarded"]
    )
    tracer.count(
        dossier_rounds=dsr["rounds"],
        dossier_calls=dsr["calls"],
        dossier_entries=len(dsr["ledger"]),
    )


# ---------- the write pass (dossier.md §11) ----------


def _cites(entries: list[dict]) -> dict[int, list[tuple[str, str]]]:
    return {
        int(e["id"]): [
            (str(s.get("outlet") or ""), str(s.get("url") or "")) for s in e.get("sources") or []
        ]
        for e in entries
        if e.get("sources")
    }


def entry_coverage(entries: list[dict], markers: list) -> float:
    """Share of the ledger the prose actually cited.

    anchor.unanchored_share measures prose -> markers: how much of the writing
    is unsupported. This measures the other direction: how much of the evidence
    made it into the writing. §18's acceptance test is "the prose carries all
    of them", and nothing else in this codebase checks that direction."""
    if not entries:
        return 1.0
    cited = {m.claim_id for m in markers}
    return round(len(cited & {int(e["id"]) for e in entries}) / len(entries), 3)


def _entry_lines(entries: list[dict]) -> str:
    out = []
    for e in sorted(entries, key=lambda x: (str(x.get("date") or ""), x.get("id", 0))):
        outlets = ", ".join(sorted(_outlets(e.get("sources") or []))) or "unattributed"
        out.append(
            f"[e{e['id']}] {e.get('date')} ({e.get('outlet_count', 1)} outlet(s): {outlets}) "
            f"{e.get('what')}"
        )
    return "\n".join(out)


def _compose(entries: list[dict], raw_body: str) -> dict | None:
    """Parse [eN] prose into the render-facing block shape, and apply the
    digest's own anchoring floors to followed-story prose — for the first time.

    MIN_MARKERS is measured in DISTINCT SPANS, not raw markers: an entry
    corroborated by three outlets fans out to three Markers over one span, and
    render._accepted_markers renders only the first of them, so counting raw
    markers would inflate the floor exactly where sourcing is strongest."""
    cites = _cites(entries)
    body, markers, dropped = anchor.parse_anchored(raw_body, cites, "e")
    words = len(body.split())
    spans = anchor.distinct_spans(markers)
    share = anchor.unanchored_share(body, markers)
    coverage = entry_coverage(entries, markers)

    metrics = {
        "word_count": words,
        "marker_count": len(markers),
        "distinct_spans": spans,
        "dropped_markers": dropped,
        "unanchored_share": share,
        "entry_coverage": coverage,
        "entries_offered": len(entries),
    }
    if words < anchor.MIN_BODY_WORDS:
        dbg(f"dossier: write REJECTED — body too short ({words} words)")
        tracer.event("dossier", verdict="write_rejected", why="body_too_short", **metrics)
        return None
    if spans < anchor.MIN_MARKERS:
        dbg(f"dossier: write REJECTED — only {spans} anchored span(s)")
        tracer.event("dossier", verdict="write_rejected", why="too_few_markers", **metrics)
        return None
    if share > anchor.MAX_UNANCHORED_SHARE:
        dbg(f"dossier: write REJECTED — {share:.0%} unanchored")
        tracer.event("dossier", verdict="write_rejected", why="unanchored", **metrics)
        return None
    if coverage < MIN_ENTRY_COVERAGE:
        dbg(f"dossier: write REJECTED — prose carried only {coverage:.0%} of the ledger")
        tracer.event("dossier", verdict="write_rejected", why="entry_coverage", **metrics)
        return None

    tracer.event("dossier", verdict="write_kept", **metrics)
    return {
        "body": body,
        "markers": [
            {"start": m.start, "end": m.end, "outlet": m.outlet, "url": m.url} for m in markers
        ],
        "sources": [dict(s) for s in anchor.cited_sources(markers)],
        "queries": [],
        "search_suggestions": "",
        "metrics": metrics,
    }


def _concat(blocks: list[dict]) -> dict:
    """Stitch phase blocks into one, rebasing every later block's marker
    offsets past everything already written. Same arithmetic as
    anchor.parse_body's trim-and-shift, one level up: these markers are
    already parsed, and only their origin moves."""
    sep = "\n\n"
    body_parts: list[str] = []
    markers: list[dict] = []
    offset = 0
    for block in blocks:
        text = block["body"]
        for m in block["markers"]:
            markers.append({**m, "start": m["start"] + offset, "end": m["end"] + offset})
        body_parts.append(text)
        offset += len(text) + len(sep)
    body = sep.join(body_parts)

    sources: list[dict] = []
    seen = set()
    for m in markers:
        key = (m.get("outlet"), m.get("url"))
        if m.get("url") and key not in seen:
            seen.add(key)
            sources.append({"outlet": m.get("outlet", ""), "url": m.get("url", "")})
    return {
        "body": body,
        "markers": markers,
        "sources": sources,
        "queries": [],
        "search_suggestions": "",
    }


def write_groups(entries: list[dict]) -> list[list[dict]]:
    """Phases coalesced into chunks each worth spending a call on.

    assign_phases splits on 14-day quiet stretches, which is the right shape
    for the narrative but the wrong unit of work. A real ledger has a long
    tail of phases holding one entry each — issue #3's first run produced
    eight phases, five of them a single entry — and a one-entry phase cannot
    clear MIN_BODY_WORDS or MIN_MARKERS. Writing it would spend a call to
    produce prose the gate then rejects, and the entry would vanish from the
    page with it.

    So adjacent phases accumulate in date order until a group is big enough
    to write, and the tail is folded into the previous group rather than left
    to fail alone."""
    phases: dict[str, list[dict]] = {}
    for e in entries:
        phases.setdefault(str(e.get("phase") or "undated"), []).append(e)

    def _key(name: str) -> str:
        return min((str(e.get("date") or "9999") for e in phases[name]), default="9999")

    groups: list[list[dict]] = []
    current: list[dict] = []
    for name in sorted(phases, key=_key):
        block = phases[name]
        # Close the group BEFORE overshooting, not after: appending first and
        # checking afterwards lets one large phase land on top of a nearly-full
        # group and produce a single enormous call, which is the exact thing
        # phased writing exists to avoid.
        if current and len(current) + len(block) > PHASED_WRITE_ENTRIES:
            groups.append(current)
            current = []
        current.extend(block)
        while len(current) > PHASED_WRITE_ENTRIES:
            groups.append(current[:PHASED_WRITE_ENTRIES])
            current = current[PHASED_WRITE_ENTRIES:]
    if current:
        if groups and len(current) < PHASED_WRITE_ENTRIES // 2:
            groups[-1].extend(current)  # a short tail rides with the group before it
        else:
            groups.append(current)
    return groups


def _write_call(dsr: dict, budget: Budget, entries: list[dict], system: str, label: str) -> dict | None:
    prompt = (
        f"STORY: {dsr['subject']}\n\n"
        f"LEDGER ENTRIES — cite each with its [eN] marker:\n{_entry_lines(entries)}"
    )
    payload = _guarded(
        lambda: ground.structured(prompt, system, label, _WRITE_SCHEMA), dsr, budget, "schema"
    )
    _spend(dsr, budget, "schema")
    if not isinstance(payload, dict):
        return None
    return _compose(entries, str(payload.get("body") or ""))


def write_backstory(followed_dir: Path, issue: int, dsr: dict, corpus: dict, budget: Budget) -> dict | None:
    """The full picture, written from the ledger and nothing else.

    Prose is regenerable; research is not repeated. dossier.md §11 supersedes
    follow.py's old "the backstory is never regenerated" docstring rule — that
    existed to protect quota and product.md's "grows the fuller picture"
    promise, and both survive, because regenerating costs one call over a
    ledger that only ever grows."""
    entries = [e for e in dsr["ledger"] if e.get("phase") != "off-subject"]
    if not entries:
        dbg(f"dossier: #{issue} nothing in the ledger to write from")
        return None
    if budget.exhausted("schema"):
        # The ledger is safe on disk and prose is regenerable, so a later run
        # writes it. Publishing half a picture would be worse than waiting.
        dbg(f"dossier: #{issue} schema pool exhausted; deferring the write")
        budget.defer(issue, "schema_pool_for_write")
        return None

    if len(entries) <= PHASED_WRITE_ENTRIES:
        block = _write_call(
            dsr, budget, entries, _WRITE_SYSTEM, f"dossier-{issue}-write"
        )
        save(followed_dir, issue, dsr, corpus, "WRITE")
        return block

    # Phased: each call stays small and no phase gets squeezed. Gated per
    # group, so one thin stretch fails alone instead of taking the whole piece
    # down with it.
    groups = write_groups(entries)
    dbg(f"dossier: #{issue} writing {len(entries)} entries as {len(groups)} group(s)")
    blocks = []
    for i, group in enumerate(groups, start=1):
        block = _write_call(
            dsr, budget, group, _WRITE_SYSTEM, f"dossier-{issue}-write-{i}"
        )
        save(followed_dir, issue, dsr, corpus, "WRITE")
        if block is not None:
            blocks.append(block)
        else:
            dbg(f"dossier: #{issue} write group {i}/{len(groups)} failed its gate; continuing")
    if not blocks:
        return None
    dbg(f"dossier: #{issue} wrote {len(blocks)}/{len(groups)} group(s)")
    return _concat(blocks)


def write_update(followed_dir: Path, issue: int, dsr: dict, corpus: dict, budget: Budget) -> dict | None:
    """One dated timeline entry covering only what is new since the last write.

    "Quiet" is mechanical here — zero new ledger entries for the period — not a
    model judgement about whether today felt quiet."""
    new = [
        e
        for e in dsr["ledger"]
        if int(e.get("added_round", 0)) > int(dsr.get("written_through", 0))
        and e.get("phase") != "off-subject"
    ]
    if not new:
        dbg(f"dossier: #{issue} no new ledger entries; quiet")
        return None
    return _write_call(dsr, budget, new, _UPDATE_SYSTEM, f"dossier-{issue}-update")
