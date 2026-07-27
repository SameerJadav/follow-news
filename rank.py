"""The editorial mechanism.

Everything that decides *how many* stories a day gets, *which section* they
land in, and *what order* they appear in lives here as named constants. This
file performs no network I/O and makes no LLM call, so it is fully
unit-testable without an API key, and Phase 6 calibrates by turning these
dials rather than rewording a prompt.

Prominence is measured as distinct outlets covering a cluster, not article
volume (research.md §2.5) — a volume-based count over-weights whatever
outlet happens to churn, so a celebrity story could out-publish a coup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tracer
from feeds import Article
from tracer import dbg
from wikipedia import WikiEvent

SECTIONS = ("world", "india")
TIERS = ("lead", "major", "notable")
CATEGORIES = (
    "politics",
    "conflict",
    "economy",
    "disaster",
    "sport",
    "science",
    "health",
    "climate",
    "technology",
    "culture",
    "entertainment",
    "obituary",
)
# decisions.md §Editorial: out of scope, dropped mechanically rather than
# merely discouraged in the prompt.
OUT_OF_SCOPE = frozenset({"culture", "entertainment", "obituary"})

# Pool shaping. Measured 2026-07-25: 433 articles in a 24h window, Hindustan
# Times 92 vs NPR World 3. Without a per-outlet cap a churny feed crowds the
# small ones out of the select prompt entirely — the same volume bias
# distinct-outlet counting exists to avoid.
MAX_ARTICLES_PER_OUTLET = 40
MAX_SELECT_ARTICLES = 400
MAX_ARTICLES_PER_STORY = 5  # how much text pass two must read per story

# The cutoff. Story count floats with the news by comparing a measured
# weight against this floor, rather than hitting a fixed target count.
TIER_WEIGHT = {"lead": 4, "major": 2, "notable": 0}
WIKI_BONUS = 2
WEIGHT_FLOOR = 5  # <-- Phase 6 turns this dial first
HARD_CAP = 12  # a ceiling, never a target
MIN_STORIES_IF_ANY = 3  # only fires when the floor cuts everything

WIKI_MATCH_MIN_TOKENS = 2  # shared proper nouns needed to call a cluster wiki-backed

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’-]+")
_STOPWORDS = frozenset(
    """the this that they their there after before says said from with have
    been also when what while which first more than into over near amid
    during following report reports reported announced""".split()
)


@dataclass(frozen=True, slots=True)
class SelectedCluster:
    """Raw, unvalidated output of the select pass."""

    headline_hint: str
    section: str
    category: str
    tier: str
    article_ids: list[int]


@dataclass(frozen=True, slots=True)
class RankedCluster:
    """A cluster that survived scoring. Phase 3's input contract."""

    headline_hint: str
    section: str
    category: str
    tier: str
    articles: list[Article]  # <= MAX_ARTICLES_PER_STORY, outlet-diverse
    distinct_outlets: int  # counted over ALL valid ids, before truncation
    wiki_backed: bool
    weight: int


def build_select_pool(articles: list[Article]) -> list[Article]:
    """Cap each outlet's contribution to the select prompt before the global
    cap, so a high-volume feed can't crowd out a low-volume one — the same
    bias distinct-outlet counting exists to correct for."""
    per_outlet: dict[str, int] = {}
    capped: dict[str, int] = {}
    pool: list[Article] = []
    for a in articles:
        n = per_outlet.get(a.outlet, 0)
        if n >= MAX_ARTICLES_PER_OUTLET:
            capped[a.outlet] = capped.get(a.outlet, 0) + 1
            continue
        per_outlet[a.outlet] = n + 1
        pool.append(a)

    over_global = max(0, len(pool) - MAX_SELECT_ARTICLES)
    pool = pool[:MAX_SELECT_ARTICLES]
    dbg(f"rank: select pool {len(articles)} -> {len(pool)}; capped outlets: {capped}")

    tracer.count(pool_in=len(articles), pool_out=len(pool),
                 pool_cut_outlet_cap=sum(capped.values()), pool_cut_global_cap=over_global)
    # The exact pool, with the [i] index the select prompt uses — so a bad
    # selection can be read against precisely what the model was shown.
    tracer.artifact_json(
        "pool.json",
        {
            "max_articles_per_outlet": MAX_ARTICLES_PER_OUTLET,
            "max_select_articles": MAX_SELECT_ARTICLES,
            "dropped_by_outlet_cap": capped,
            "dropped_by_global_cap": over_global,
            "articles": [
                {"i": i, "outlet": a.outlet, "title": a.title, "url": a.url,
                 "published": a.published, "summary": a.summary}
                for i, a in enumerate(pool)
            ],
        },
    )
    return pool


def _proper_nouns(text: str) -> set[str]:
    """Capitalised, non-stopword tokens — a discriminating signal for
    matching a cluster against a Wikipedia event, unlike comparing all
    tokens (which would match on common words like "says" or "military")."""
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(text):
        if tok[0].isupper() and len(tok) >= 4 and tok.lower() not in _STOPWORDS:
            out.add(tok.lower())
    return out


def _wiki_match(cluster_text: str, events: list[WikiEvent]) -> WikiEvent | None:
    """The Wikipedia event sharing the most proper nouns with cluster_text,
    provided the overlap clears WIKI_MATCH_MIN_TOKENS; else None."""
    cluster_nouns = _proper_nouns(cluster_text)
    if not cluster_nouns:
        return None
    best: WikiEvent | None = None
    best_overlap = 0
    for event in events:
        event_nouns = _proper_nouns(f"{event.text} {event.topic}")
        overlap = len(cluster_nouns & event_nouns)
        if overlap > best_overlap:
            best = event
            best_overlap = overlap
    if best is not None and best_overlap >= WIKI_MATCH_MIN_TOKENS:
        return best
    return None


def _diverse_subset(ids: list[int], pool: list[Article]) -> list[Article]:
    """Pick up to MAX_ARTICLES_PER_STORY articles for the write pass,
    preferring one per distinct outlet first — broadest sourcing per story is
    what Phase 3's claim anchoring and thin-source detection need."""
    seen_outlets: set[str] = set()
    first_pass: list[Article] = []
    remainder: list[Article] = []
    for i in ids:
        a = pool[i]
        if a.outlet not in seen_outlets:
            seen_outlets.add(a.outlet)
            first_pass.append(a)
        else:
            remainder.append(a)
    chosen = (first_pass + remainder)[:MAX_ARTICLES_PER_STORY]
    return chosen


def rank_clusters(
    selected: list[SelectedCluster],
    pool: list[Article],
    events: list[WikiEvent],
) -> list[RankedCluster]:
    """Validate, score, filter and order the raw select-pass output into the
    ranked clusters Phase 3 consumes."""
    claimed: set[int] = set()
    scored: list[RankedCluster] = []
    rejected_ids = 0
    row_of: dict[int, dict] = {}  # id(RankedCluster) -> its debug row

    # One row per cluster the model proposed, carrying its verdict all the
    # way to the end. Everything the digest didn't publish is decided here,
    # and today none of it survives the run — this is the record that lets
    # WEIGHT_FLOOR and the tier weights be tuned against evidence.
    rows: list[dict] = [
        {
            "headline_hint": c.headline_hint,
            "section": c.section,
            "category": c.category,
            "tier_requested": c.tier,
            "article_ids": list(c.article_ids),
            "verdict": "pending",
        }
        for c in selected
    ]

    prelim: list[tuple[SelectedCluster, list[int], int]] = []
    for pos, cluster in enumerate(selected):
        row = rows[pos]
        if cluster.section not in SECTIONS:
            dbg(f"rank: dropped invalid section {cluster.section!r} {cluster.headline_hint!r}")
            row["verdict"] = "dropped:bad_section"
            continue
        category = cluster.category
        if category not in CATEGORIES:
            dbg(f"rank: dropped invalid category {category!r} {cluster.headline_hint!r}")
            row["verdict"] = "dropped:bad_category"
            continue
        tier = cluster.tier if cluster.tier in TIERS else "notable"
        if cluster.tier not in TIERS:
            dbg(f"rank: invalid tier {cluster.tier!r} for {cluster.headline_hint!r}, using 'notable'")
            row["tier_coerced"] = True

        if category in OUT_OF_SCOPE:
            dbg(f"rank: dropped out-of-scope [{category}] {cluster.headline_hint!r}")
            row["verdict"] = "dropped:out_of_scope"
            continue

        valid_ids: list[int] = []
        for i in cluster.article_ids:
            if not isinstance(i, int) or not (0 <= i < len(pool)):
                rejected_ids += 1
                row.setdefault("bad_ids", []).append(i)
                continue
            if i in claimed:
                row.setdefault("ids_claimed_by_earlier_cluster", []).append(i)
                continue
            claimed.add(i)
            valid_ids.append(i)

        if not valid_ids:
            row["verdict"] = "dropped:no_valid_articles"
            continue

        row["valid_ids"] = valid_ids
        prelim.append((SelectedCluster(cluster.headline_hint, cluster.section, category, tier, valid_ids), valid_ids, pos))

    # Demote surplus leads: within each section, only the cluster with the
    # most distinct outlets keeps tier "lead" — guards against tier
    # inflation floating everything over the floor.
    by_section: dict[str, list[int]] = {}
    for idx, (c, ids, _pos) in enumerate(prelim):
        if c.tier == "lead":
            by_section.setdefault(c.section, []).append(idx)

    lead_keep: dict[int, bool] = {}
    for idxs in by_section.values():
        best_idx = max(idxs, key=lambda i: len({pool[j].outlet for j in prelim[i][1]}))
        for i in idxs:
            lead_keep[i] = i == best_idx

    for idx, (cluster, ids, pos) in enumerate(prelim):
        row = rows[pos]
        tier = cluster.tier
        if tier == "lead" and not lead_keep.get(idx, True):
            dbg(f"rank: demoted surplus lead to major [{cluster.section}] {cluster.headline_hint!r}")
            tier = "major"
            row["demoted_surplus_lead"] = True

        distinct_outlets = len({pool[i].outlet for i in ids})
        cluster_text = cluster.headline_hint + " " + " ".join(pool[i].title for i in ids)
        wiki_event = _wiki_match(cluster_text, events)
        wiki_backed = wiki_event is not None
        weight = distinct_outlets + TIER_WEIGHT[tier] + (WIKI_BONUS if wiki_backed else 0)
        articles = _diverse_subset(ids, pool)

        dbg(
            f"rank: [{cluster.section}] outlets={distinct_outlets} tier={tier} "
            f"wiki={'yes' if wiki_backed else 'no'} weight={weight} {cluster.headline_hint!r}"
        )

        # The weight arithmetic spelled out, so a bad cutoff is readable
        # without re-deriving it from the dials by hand.
        row.update(
            {
                "tier_final": tier,
                "distinct_outlets": distinct_outlets,
                "wiki_backed": wiki_backed,
                "wiki_event": wiki_event.text if wiki_event else "",
                "weight": weight,
                "weight_terms": {
                    "distinct_outlets": distinct_outlets,
                    "tier_weight": TIER_WEIGHT[tier],
                    "wiki_bonus": WIKI_BONUS if wiki_backed else 0,
                },
                "weight_floor": WEIGHT_FLOOR,
                "articles": [{"outlet": a.outlet, "title": a.title, "url": a.url} for a in articles],
                "articles_dropped_by_diversity_cap": max(0, len(ids) - len(articles)),
                "verdict": "scored",
            }
        )

        ranked = RankedCluster(
            headline_hint=cluster.headline_hint,
            section=cluster.section,
            category=cluster.category,
            tier=tier,
            articles=articles,
            distinct_outlets=distinct_outlets,
            wiki_backed=wiki_backed,
            weight=weight,
        )
        row_of[id(ranked)] = row
        scored.append(ranked)

    scored.sort(key=lambda c: (c.weight, c.distinct_outlets), reverse=True)

    floor_relaxed = False
    kept = [c for c in scored if c.weight >= WEIGHT_FLOOR]
    if not kept and scored:
        kept = scored[:MIN_STORIES_IF_ANY]
        floor_relaxed = True
        dbg(
            f"rank: FLOOR RELAXED — nothing cleared weight>={WEIGHT_FLOOR}; "
            f"keeping top {len(kept)} by weight. Tune WEIGHT_FLOOR."
        )

    over_cap = kept[HARD_CAP:]
    kept = kept[:HARD_CAP]

    world = sorted((c for c in kept if c.section == "world"), key=lambda c: c.weight, reverse=True)
    india = sorted((c for c in kept if c.section == "india"), key=lambda c: c.weight, reverse=True)
    final = world + india

    per_section = {"world": len(world), "india": len(india)}
    dbg(
        f"rank: {len(scored)} scored, {rejected_ids} id(s) rejected, "
        f"{len(final)} kept {per_section}; weights={[c.weight for c in final]}"
    )

    # Resolve each scored cluster's final fate. row_of is keyed by id() of
    # the RankedCluster, which stays correct even when two clusters share a
    # headline hint and a weight.
    kept_ids = {id(c) for c in final}
    cap_ids = {id(c) for c in over_cap}
    for cl in scored:
        row = row_of.get(id(cl))
        if row is None:
            continue
        if id(cl) in kept_ids:
            row["verdict"] = "kept"
        elif id(cl) in cap_ids:
            row["verdict"] = f"cut:hard_cap(>{HARD_CAP})"
        else:
            row["verdict"] = f"cut:below_floor({cl.weight}<{WEIGHT_FLOOR})"

    tracer.count(
        clusters_proposed=len(selected),
        clusters_scored=len(scored),
        clusters_kept=len(final),
        clusters_world=len(world),
        clusters_india=len(india),
        rejected_article_ids=rejected_ids,
    )
    tracer.artifact_json(
        "rank.json",
        {
            "dials": {
                "WEIGHT_FLOOR": WEIGHT_FLOOR,
                "TIER_WEIGHT": TIER_WEIGHT,
                "WIKI_BONUS": WIKI_BONUS,
                "HARD_CAP": HARD_CAP,
                "MIN_STORIES_IF_ANY": MIN_STORIES_IF_ANY,
                "MAX_ARTICLES_PER_STORY": MAX_ARTICLES_PER_STORY,
            },
            "floor_relaxed": floor_relaxed,
            "kept_weights": [c.weight for c in final],
            "per_section": per_section,
            "clusters": rows,
        },
    )
    return final


def wiki_coverage_report(events: list[WikiEvent], ranked: list[RankedCluster]) -> None:
    """Purely diagnostic: shows which curated events matched a selected
    story and which did not, so the Wikipedia cross-check is visible in the
    Actions log rather than a silent input to scoring."""
    if not events:
        dbg("wiki: no curated events fetched; cross-check skipped")
        return

    matched = 0
    unmatched: list[WikiEvent] = []
    for event in events:
        hit = any(
            _wiki_match(c.headline_hint + " " + " ".join(a.title for a in c.articles), [event]) is not None
            for c in ranked
        )
        if hit:
            matched += 1
        else:
            unmatched.append(event)

    dbg(f"wiki: {len(events)} curated event(s); {matched} matched a selected story, {len(unmatched)} not covered")
    for event in unmatched:
        topic = f"{event.topic} — " if event.topic else ""
        dbg(f"wiki: NOT COVERED [{event.category}] {topic}{event.text}")

    # "missed:" in calibration.md's morning entry is answered from exactly
    # this list — keep it as data, not just a log line to grep for.
    tracer.count(wiki_matched=matched, wiki_not_covered=len(unmatched))
    tracer.artifact_json(
        "wikipedia/coverage.json",
        {
            "events": len(events),
            "matched": matched,
            "not_covered": [
                {"category": e.category, "topic": e.topic, "text": e.text} for e in unmatched
            ],
        },
    )
