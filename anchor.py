"""The claim-anchoring mechanism.

Phase 2's rank.py decides *which* stories run; this module decides whether a
written story is trustworthy enough to ship. Claim-anchored generation means
the write pass composes prose only from claims already anchored to exactly
one source URL (research.md §6) — a claim that cannot be anchored never
enters the text. This module turns the model's inline `[cN]` markers into
exact character spans, measures how much of a story is actually anchored,
and measures thin sourcing rather than letting a model judge it.

No network I/O, no LLM call — fully unit-testable without an API key, and
Phase 6 calibrates by turning these named constants rather than rewording a
prompt (the same rationale as rank.py's dials).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from feeds import dbg
from rank import RankedCluster

CLAIM_KINDS = ("event", "background", "figure", "quote")
SOURCE_KINDS = ("fulltext", "summary")

# Below this, extraction is treated as having failed and the RSS summary is
# used instead (mirrors extract.MIN_CHARS) — claims drawn from a summary are
# weaker and are labelled as such in the claims prompt.
MIN_FULLTEXT_CHARS = 600

MAX_CLAIMS_PER_STORY = 24  # ceiling on claims kept per story
MIN_CLAIM_CHARS = 20  # a claim is a statement, not a fragment

# Thin sourcing is MEASURED over the outlets the prose actually cites, never
# over the whole cluster's articles, and never assessed by the model itself.
THIN_MIN_CLAIM_OUTLETS = 2
THIN_MAX_OUTLET_SHARE = 0.8

# Prose needs connective tissue that isn't a claim. Past this share of the
# body sitting outside any marker, the model was composing freely rather
# than writing from claims, and the story is dropped rather than shipped.
MAX_UNANCHORED_SHARE = 0.2
MIN_MARKERS = 2
MIN_BODY_WORDS = 80

# decisions.md §Editorial: variable by weight — lead ~500 words, secondary ~200.
WORD_TARGET = {"lead": 500, "major": 200, "notable": 200}

# Despite rule 1 asking for one marker per statement, the model sometimes
# corroborates a single statement with two sources and writes "[c45, c50]"
# rather than two separate markers. Match that form too so it becomes two
# Markers sharing one span, rather than a leftover "[c...]" token in body.
_MARKER_RE = re.compile(r"\[c(\d+(?:\s*,\s*c?\d+)*)\]")
# Pull a marker flush against the word it follows before parsing, so
# stripping the token never leaves a stray " ." behind and every computed
# offset stays exact.
_MARKER_WS_RE = re.compile(r"[ \t]+(\[c\d+(?:\s*,\s*c?\d+)*\])")
# Leading characters to skip when starting a new span: whitespace plus the
# full stop and connective punctuation left over from the PRECEDING
# sentence (a marker sits before its own full stop, so the previous
# sentence's "." lands at the very start of the next span's raw range).
_SPAN_TRIM_CHARS = " \t\n.,;:—–-"
# A marker anchors the statement immediately before it, not an unbounded
# backlog of prose since whatever marker came before it. Without this cap, a
# huge block of unmarked filler capped off by a single trailing marker would
# read as fully "anchored" to that one marker — exactly the coverage gap
# unanchored_share exists to catch. ~2-3 sentences of run-up is normal
# connective tissue; anything beyond that is left uncovered on purpose.
MAX_SPAN_CHARS = 300
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Claim:
    """One atomic factual statement anchored to exactly ONE source URL. Two
    sources giving different figures produce two Claims, never one blended
    figure (product.md §Trust and correctness)."""

    id: int  # globally unique across the day, assigned in Python
    cluster_id: int
    text: str
    kind: str  # one of CLAIM_KINDS
    outlet: str
    url: str
    source_kind: str  # one of SOURCE_KINDS


@dataclass(frozen=True, slots=True)
class Marker:
    """A stretch of body text and the claim behind it. start/end are
    character offsets into the story's final body; outlet/url are
    denormalised so Phase 4 can render a tappable source with no lookup."""

    start: int
    end: int
    claim_id: int
    outlet: str
    url: str


def source_kind(full_text: str) -> str:
    """Whether extract.py actually got the article body or extraction
    failed and the RSS summary is being used instead."""
    return "fulltext" if len(full_text) >= MIN_FULLTEXT_CHARS else "summary"


def _strip_markers(raw: str, allowed: dict[int, Claim]) -> tuple[str, list[tuple[int, list[int]]], int]:
    """Remove every [cN] / [cN, cM] token from raw prose in a single pass.
    Returns the clean text, the (offset_in_clean, claim_ids) hits in
    document order — one entry per token occurrence, holding every id in
    that token that is a real claim of THIS story — and how many individual
    ids were dropped (unknown id, or citing another story's claim). A token
    whose ids are ALL invalid contributes no hit at all."""
    raw = _MARKER_WS_RE.sub(r"\1", raw)

    parts: list[str] = []
    hits: list[tuple[int, list[int]]] = []
    out_len = 0
    dropped = 0
    pos = 0
    for m in _MARKER_RE.finditer(raw):
        chunk = raw[pos : m.start()]
        parts.append(chunk)
        out_len += len(chunk)
        ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
        valid = [cid for cid in ids if cid in allowed]
        dropped += len(ids) - len(valid)
        if valid:
            hits.append((out_len, valid))
        pos = m.end()
    parts.append(raw[pos:])
    clean = "".join(parts)
    return clean, hits, dropped


def _spans(clean: str, hits: list[tuple[int, list[int]]], claims_by_id: dict[int, Claim]) -> list[Marker]:
    """A span is the run of prose since the previous marker (or the start of
    its paragraph, or MAX_SPAN_CHARS back — whichever is latest) up to the
    marker's own position — never crossing a paragraph break, capped in
    length, and trimmed of leading/trailing connective punctuation and
    whitespace. A token citing more than one claim id produces one Marker
    per id, all sharing that same span — several outlets corroborating one
    statement, not several statements."""
    markers: list[Marker] = []
    prev_end = 0
    for offset, ids in hits:
        para = clean.rfind("\n\n", 0, offset)
        start = max(prev_end, 0 if para < 0 else para + 2, offset - MAX_SPAN_CHARS)
        while start < offset and clean[start] in _SPAN_TRIM_CHARS:
            start += 1
        end = offset
        while end > start and clean[end - 1] in " \t\n":
            end -= 1
        prev_end = offset  # the next span begins where this marker sat
        if end > start:  # drop degenerate/empty spans
            for cid in ids:
                c = claims_by_id[cid]
                markers.append(Marker(start, end, cid, c.outlet, c.url))
    return markers


def parse_body(raw_body: str, claims: list[Claim]) -> tuple[str, list[Marker], int]:
    """Turn the model's raw body (inline [cN] markers) into clean prose plus
    exact character spans. Only markers citing a claim of THIS story are
    honoured; a marker pointing at another story's claim id is dropped like
    an unknown one, never silently accepted."""
    allowed = {c.id: c for c in claims}
    clean, hits, dropped = _strip_markers(raw_body, allowed)
    markers = _spans(clean, hits, allowed)

    # Stripping markers can leave leading/trailing whitespace (e.g. a marker
    # was the very first or last token). Trim it and shift every span so
    # offsets stay exact against the final, trimmed body.
    lead = len(clean) - len(clean.lstrip())
    body = clean.strip()
    shifted: list[Marker] = []
    for m in markers:
        start = max(0, min(len(body), m.start - lead))
        end = max(0, min(len(body), m.end - lead))
        if end > start:
            shifted.append(Marker(start, end, m.claim_id, m.outlet, m.url))
    return body, shifted, dropped


def unanchored_share(body: str, markers: list[Marker]) -> float:
    """Fraction of the body's non-whitespace characters that fall outside
    every marker's span. Whitespace is excluded so paragraph breaks and the
    gaps between spans don't inflate the number. Distinct spans never
    overlap by construction, but a statement corroborated by more than one
    source produces several Markers sharing the SAME span (see _spans) —
    dedupe on (start, end) first so that shared span isn't counted twice."""
    total = len(_WS_RE.sub("", body))
    if not total:
        return 1.0
    seen: set[tuple[int, int]] = set()
    covered = 0
    for m in markers:
        key = (m.start, m.end)
        if key in seen:
            continue
        seen.add(key)
        covered += len(_WS_RE.sub("", body[m.start : m.end]))
    return round(1.0 - covered / total, 3)


def is_thin_sourced(markers: list[Marker]) -> tuple[bool, int]:
    """Thin sourcing is measured over the DISTINCT claims cited per outlet —
    a claim cited by two markers must not count twice — never assessed by
    the model. True if fewer than THIN_MIN_CLAIM_OUTLETS outlets are cited,
    or if one outlet supplies more than THIN_MAX_OUTLET_SHARE of the cited
    claims. Returns (thin, distinct_outlet_count)."""
    per_outlet: dict[str, set[int]] = defaultdict(set)
    for m in markers:
        per_outlet[m.outlet].add(m.claim_id)
    if len(per_outlet) < THIN_MIN_CLAIM_OUTLETS:
        return True, len(per_outlet)
    counts = [len(v) for v in per_outlet.values()]
    return (max(counts) / sum(counts)) > THIN_MAX_OUTLET_SHARE, len(per_outlet)


def clean_vocab(items: list[dict], body: str) -> list[dict]:
    """Keep a vocab item only if it has all three fields non-empty AND the
    term actually appears in the body — the current pipeline lets the model
    list words ("quagmire", "obfuscation") that never made it into the
    written story, which clean_vocab now catches. Deduped, capped at 6."""
    body_lower = body.lower()
    seen: set[str] = set()
    out: list[dict] = []
    dropped = 0
    for v in items:
        term = str(v.get("term", "")).strip()
        say = str(v.get("say", "")).strip()
        meaning = str(v.get("meaning", "")).strip()
        if not (term and say and meaning):
            dropped += 1
            continue
        key = term.lower()
        if key not in body_lower or key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append({"term": term, "say": say, "meaning": meaning})
        if len(out) == 6:
            break
    if dropped:
        dbg(f"anchor: clean_vocab dropped {dropped} item(s)")
    return out


def unsourced_figures(body: str, claims: list[Claim]) -> list[str]:
    """Diagnostic for the never-blend-numbers rule: figures present in the
    body that appear in no cited claim's text. Logged and recorded in
    signals but never used to drop a story — spelled-out numbers and
    rephrasings make this too noisy to gate on."""
    body_figures = {f.replace(",", "") for f in _FIGURE_RE.findall(body)}
    claim_figures: set[str] = set()
    for c in claims:
        claim_figures |= {f.replace(",", "") for f in _FIGURE_RE.findall(c.text)}
    return sorted(body_figures - claim_figures)


def cited_sources(markers: list[Marker]) -> list[dict]:
    """{outlet, url} for claims actually cited in the prose, deduped, in
    first-cited order. Replaces the old "every article in the cluster"
    list, which overclaimed sourcing the body didn't actually use."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for m in markers:
        key = (m.outlet, m.url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"outlet": m.outlet, "url": m.url})
    return out


def build_story(cluster: RankedCluster, cluster_id: int, raw: dict, claims: list[Claim]) -> dict | None:
    """The single semantic gate on the write pass's output for one story.
    Returns None (and dbg()s why) if the story fails to qualify as
    genuinely claim-anchored prose; otherwise returns the story dict in the
    data/ contract's key order."""
    headline = str(raw.get("headline", "")).strip()
    hint = cluster.headline_hint or headline or "(untitled)"
    if not headline:
        dbg(f"anchor: DROPPED [{cluster.section}] {hint!r} — empty headline")
        return None

    # Defensive: `claims` should already be exactly this cluster's own
    # claims (the caller looks them up by cluster_id), so this should never
    # fire. If it ever does, a claim from another story leaked in upstream.
    foreign = [c for c in claims if c.cluster_id != cluster_id]
    if foreign:
        dbg(f"anchor: {len(foreign)} claim(s) passed to build_story belong to another cluster; ignoring")
        claims = [c for c in claims if c.cluster_id == cluster_id]

    body, markers, dropped_markers = parse_body(str(raw.get("body", "")), claims)

    words = len(body.split())
    if words < MIN_BODY_WORDS:
        dbg(f"anchor: DROPPED [{cluster.section}] {hint!r} — body too short ({words} words)")
        return None

    if len(markers) < MIN_MARKERS:
        dbg(f"anchor: DROPPED [{cluster.section}] {hint!r} — only {len(markers)} marker(s)")
        return None

    share = unanchored_share(body, markers)
    if share > MAX_UNANCHORED_SHARE:
        dbg(
            f"anchor: DROPPED [{cluster.section}] {hint!r} — "
            f"{share:.0%} unanchored, {len(markers)} marker(s)"
        )
        return None

    thin, claim_outlets = is_thin_sourced(markers)
    if thin:
        dbg(f"anchor: THIN-SOURCED [{cluster.section}] {hint!r} — {claim_outlets} outlet(s) cited")

    cited_ids = {m.claim_id for m in markers}
    cited_claims = [c for c in claims if c.id in cited_ids]
    # Preserve first-citation order rather than extraction order.
    order = {cid: i for i, cid in enumerate(dict.fromkeys(m.claim_id for m in markers))}
    cited_claims.sort(key=lambda c: order[c.id])

    return {
        "section": cluster.section,
        "headline": headline,
        "body": body,
        "markers": [
            {"start": m.start, "end": m.end, "claim_id": m.claim_id, "outlet": m.outlet, "url": m.url}
            for m in markers
        ],
        "claims": [
            {
                "id": c.id,
                "text": c.text,
                "kind": c.kind,
                "outlet": c.outlet,
                "url": c.url,
                "source_kind": c.source_kind,
            }
            for c in cited_claims
        ],
        "thin_sourced": thin,
        "sources": cited_sources(markers),
        "vocab": clean_vocab(raw.get("vocab", []), body),
        "signals": {
            "category": cluster.category,
            "tier": cluster.tier,
            "distinct_outlets": cluster.distinct_outlets,
            "wiki_backed": cluster.wiki_backed,
            "weight": cluster.weight,
            "claim_count": len(cited_claims),
            "claim_outlets": claim_outlets,
            "marker_count": len(markers),
            "dropped_markers": dropped_markers,
            "unanchored_share": share,
            "word_count": words,
            "word_target": WORD_TARGET.get(cluster.tier, 200),
            "unsourced_figures": unsourced_figures(body, cited_claims),
        },
    }


def clusters_fixture(ranked: list[RankedCluster]) -> list[dict]:
    """JSON-serialisable projection of ranked clusters, used only to save a
    fixture alongside a real LLM response so tests can reconstruct the
    inputs that produced it."""
    return [
        {
            "headline_hint": c.headline_hint,
            "section": c.section,
            "category": c.category,
            "tier": c.tier,
            "distinct_outlets": c.distinct_outlets,
            "wiki_backed": c.wiki_backed,
            "weight": c.weight,
            "articles": [{"outlet": a.outlet, "url": a.url} for a in c.articles],
        }
        for c in ranked
    ]
