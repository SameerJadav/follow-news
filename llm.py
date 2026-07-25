"""Gemini calls — the entire quota strategy for this project.

Free-tier requests-per-day is unpublished and was cut 50-80% without notice
in the past (research.md §3.1), so the pipeline must never scale call count
with article count. Selection reads only cheap headlines/summaries; full
article text is fetched (extract.py) only for the articles selection chose;
a claims pass then extracts atomic claims anchored to one source each; a
final writing pass composes prose ONLY from those anchored claims. Three
calls a day, independent of how many articles were ingested or how many
clusters survive ranking.

All semantic validation of the model's output belongs to rank.rank_clusters
(pass one) and anchor.py (passes two and three) — this module only
guarantees syntactically sane types and, per Gemini's own docs, "always
validate values in your application": response_schema guarantees valid JSON
shape, never valid content.

Claim-anchored generation (research.md §6): writing first and asking which
source supports each sentence produces attributions that are "coarse and
generated post hoc" (Faithful by Construction, arXiv 2606.23989) — exactly
the trust theater product.md forbids. Extracting claims BEFORE writing, and
letting the write pass see only claims and never raw article text, makes a
fact's source a property of construction rather than a second guess.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

import anchor
import ratelimit
from anchor import Claim
from feeds import Article, dbg
from rank import CATEGORIES, RankedCluster, SECTIONS, SelectedCluster, TIERS

MODEL = "gemini-3.6-flash"  # pinned here; change in exactly one place

SUMMARY_CAP = 240

MAX_OUTPUT_TOKENS = 32768  # a 500-word lead plus up to 11 shorter stories and vocab

MAX_JSON_RETRIES = 1  # a truncated/malformed response is retried once, not treated as fatal

_CALLS = 0  # total Gemini calls this process has made; greppable in dbg() output

_SELECT_SCHEMA = {
    "type": "object",
    "required": ["stories"],
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["headline_hint", "section", "category", "tier", "article_ids"],
                "properties": {
                    "headline_hint": {"type": "string"},
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "tier": {"type": "string", "enum": list(TIERS)},
                    "article_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        }
    },
}

_SELECT_SYSTEM = """You are an editor picking the day's genuinely biggest news \
stories from a numbered list of headlines and summaries. An optional curated \
checklist of today's and yesterday's significant world events may appear \
first — read the rules for it below.

1. CLUSTERING. Group every article id that covers the SAME underlying event \
into one cluster — different outlets often cover the same story. List EVERY \
id that covers it, not just the best few: the number of distinct outlets in \
a cluster is measured afterward and used to rank stories, so an incomplete \
id list understates a story's real importance. An article id may appear in \
AT MOST ONE cluster.

2. SECTION. Every story goes in exactly one section, "world" or "india". Put \
it in "india" if it has any significant India dimension: it happens in \
India, involves India, Indians, or the diaspora, or affects India's trade, \
tariffs, borders, economy, or foreign relations. Otherwise "world". THE \
INDIA ANGLE ALWAYS WINS — a US tariff decision aimed at India is "india", \
not "world". "world" is for stories that are genuinely elsewhere-only. \
India means national news, big stories from any state — never local city \
news.

3. SCOPE — IN: politics, conflict, economy, disasters; science, health, \
climate, technology.

4. SCOPE — OUT: culture, entertainment, obituaries — a film release, an \
awards ceremony, a celebrity death, a music or arts story. Never select \
these however heavily they are covered, and never assign one of these \
categories to a story you do want to keep — pick the category that actually \
fits instead.

5. SPORT. Only national moments: a World Cup final, an Olympic result that \
matters to a whole country. Never a league fixture, a transfer, or a \
routine match.

6. TIER. "lead" = the single biggest story of the day in that section, at \
most one lead per section. "major" = a genuinely important national or \
international development. "notable" = real news a clear step below.

7. NO PADDING. There is no target number of stories. A quiet day may yield \
two or three; a heavy day ten. Never add a story to round out the list, to \
balance the two sections, or to cover a region that has no big news today. \
If a story is not genuinely significant, leave it out entirely.

8. THE WIKIPEDIA CHECKLIST, IF PRESENT, IS A CHECK, NOT A SOURCE. Use it \
only to check you have not overlooked something significant among the \
articles below. Only select stories that are supported by articles in the \
numbered list — never invent a cluster from the Wikipedia list alone."""

_CLAIMS_SCHEMA = {
    "type": "object",
    "required": ["stories"],
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["cluster_id", "claims"],
                "properties": {
                    "cluster_id": {"type": "integer"},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["article_id", "kind", "text"],
                            "properties": {
                                "article_id": {"type": "integer"},
                                "kind": {"type": "string", "enum": list(anchor.CLAIM_KINDS)},
                                "text": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}

_CLAIMS_SYSTEM = """You extract atomic factual claims from news articles, \
each anchored to exactly the one article it came from, for a pipeline where \
a later step writes a story using ONLY the claims you extract here — it \
never sees the articles themselves.

1. ONE SOURCE PER CLAIM. Every claim comes from exactly one article and \
carries that article's `article_id`. Never merge facts from two articles \
into one claim.

2. ATOMIC. One fact per claim — a single event, figure, statement, or piece \
of background. Split compound sentences into separate claims.

3. NEVER BLEND NUMBERS. If two articles give different figures for the same \
thing, emit two separate claims, one per article, each with its own figure. \
Never average, round towards each other, or pick a middle value. Keep every \
figure exactly as written in its source.

4. SELF-CONTAINED. Each claim must be understandable entirely on its own — \
name the people, places, and organisations rather than writing "he", \
"there", or "the group". The writer will only see the claims, never the \
articles.

5. INCLUDE BACKGROUND. Extract the context and history present in the \
source text too, not only the newest developments — a reader with no prior \
knowledge must be able to follow the finished story from your claims alone. \
Mark those claims `kind: "background"`.

6. ATTRIBUTION BELONGS IN THE CLAIM. When a fact is somebody's assertion \
rather than an established fact, say whose: "Iran's foreign ministry \
says…", "witnesses told the BBC…".

7. SUMMARY-ONLY SOURCES. An article marked SUMMARY ONLY gives you a short \
RSS summary, not the full article. Extract only what the summary actually \
states; never extrapolate, complete a partial sentence, or infer detail it \
does not contain.

8. NO OPINION, NO ANALYSIS OF YOUR OWN. Only what the source states, \
including statements it reports other people making.

9. Aim for 8-20 claims per story, more for bigger stories, and cover every \
source article rather than mining only the longest one.

10. `kind` is one of: "event" (something that happened), "background" \
(context or history), "figure" (a number, quantity, or date), "quote" (a \
direct quotation)."""

_WRITE_SCHEMA = {
    "type": "object",
    "required": ["stories"],
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["cluster_id", "headline", "body", "vocab"],
                "properties": {
                    "cluster_id": {"type": "integer"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                    "vocab": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["term", "say", "meaning"],
                            "properties": {
                                "term": {"type": "string"},
                                "say": {"type": "string"},
                                "meaning": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}

_WRITE_SYSTEM = """You write news stories in plain adult English for a \
non-native reader, composing each story ONLY from a numbered list of \
claims — never inventing anything beyond them.

1. HOW TO CITE. Every factual statement you write must come from a claim, \
and must be followed by that claim's marker: [c12]. Put the marker \
immediately after the statement it supports, before the full stop. Example: \
"Houthi forces blockaded the Red Sea [c12]. Oil passed $100 a barrel \
[c19]."

2. USE ONLY THE CLAIMS. Never add a fact, figure, name, date, or cause that \
is not in a claim for that story. If something feels missing, leave it out \
— do not fill the gap. You may write short connective sentences that carry \
no new facts, and those need no marker.

3. NEVER BLEND NUMBERS. Write every figure exactly as its claim states it, \
and use digits when the claim uses digits. If two claims give different \
figures for the same thing, say so plainly and attribute both — "the \
ministry put the toll at 40 [c7], while the BBC reported 55 [c22]." Never \
average them or pick one silently.

4. NEVER PRESENT A CONTESTED CLAIM AS SETTLED. Where claims disagree, or a \
claim is somebody's assertion, keep the attribution: "the ministry says", \
"witnesses told the BBC". Say explicitly where outlets disagree or facts \
are still uncertain.

5. ONE CONTINUOUS PIECE OF PROSE. Separate paragraphs with a blank line. \
Never use a heading, a label, a bullet list, or a section such as "Why \
this matters" or "What to watch". Weave why the story matters into the \
writing itself.

6. EVERY STORY STANDS ALONE. Write so a first-time reader who has read no \
earlier digest understands it completely. Use the background claims for \
that.

7. THE OPENING. The headline is informative, not teasing — it says what \
happened. The first sentence carries the gist, so somebody who reads only \
that has not missed the story.

8. PLAIN ADULT ENGLISH for a non-native reader: short sentences, active \
voice, concrete nouns, no jargon, no idioms. Clear, but not simplified — a \
good explainer site, not a children's news service. Never talk down to the \
reader.

9. LENGTH. Each story's block states its target word count. Get close to \
it. A LEAD story is substantially longer than the others.

10. WORDS TO KNOW. List 2-6 of the harder words you actually used in the \
body. `term` is the word exactly as it appears in your text, `meaning` is \
a simple one-line definition, and `say` is a PHONETIC RESPELLING with the \
stressed syllable in capital letters (e.g. "sovereignty" -> "SOV-rin-tee") \
— never IPA."""


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=key)


def _dump(label: str, text: str) -> None:
    """Save a raw model response as a fixture, only when DIGEST_DUMP_DIR is
    set (never in Actions). Writes model output only — never the key, never
    the prompt."""
    d = os.environ.get("DIGEST_DUMP_DIR")
    if not d:
        return
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{label}.json").write_text(text)


def _generate(prompt: str, schema: dict, system: str, label: str) -> Any:
    global _CALLS
    client = _client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        system_instruction=system,
        temperature=0.3,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    json_retries = 0
    while True:
        _CALLS += 1
        dbg(f"llm: call #{_CALLS} model={MODEL} label={label} prompt={len(prompt)}ch")
        resp = ratelimit.call_with_resume(
            lambda: client.models.generate_content(model=MODEL, contents=prompt, config=config),
            label,
        )
        try:
            finish = resp.candidates[0].finish_reason if resp.candidates else None
        except Exception:  # noqa: BLE001
            finish = None
        dbg(f"llm: {label} finish_reason={finish}")
        text = resp.text
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            if json_retries < MAX_JSON_RETRIES:
                json_retries += 1
                dbg(f"llm: {label} malformed/truncated JSON, retrying ({json_retries}/{MAX_JSON_RETRIES})")
                continue
            raise
        _dump(label, text)
        return result


def select_stories(pool: list[Article], wiki_block: str) -> list[SelectedCluster]:
    """Pass one: send cheap headlines+summaries (plus an optional Wikipedia
    checklist), get back which article ids cluster into which stories, each
    tagged with a section/category/tier. No article text is fetched for this
    call. `pool` is used exactly as passed in — no internal slicing, since
    the caller (rank.build_select_pool) already shaped it; ids must resolve
    against this same list.

    Only type-level validation happens here (int ids, string enums). Whether
    an id is in range, whether a cluster survives the scope filter, and how
    it's weighted is rank.rank_clusters's job — Gemini guarantees
    syntactically valid JSON, never semantically valid values."""
    lines = [f"[{i}] ({a.outlet}) {a.title} — {a.summary[:SUMMARY_CAP]}" for i, a in enumerate(pool)]
    prompt = (f"{wiki_block}\n\n" if wiki_block else "") + "NEWS ARTICLES\n" + "\n".join(lines)

    result = _generate(prompt, _SELECT_SCHEMA, _SELECT_SYSTEM, "select")

    clusters: list[SelectedCluster] = []
    for story in result.get("stories", []):
        ids = [i for i in story.get("article_ids", []) if isinstance(i, int)]
        clusters.append(
            SelectedCluster(
                headline_hint=str(story.get("headline_hint", "")).strip(),
                section=str(story.get("section", "")).strip().lower(),
                category=str(story.get("category", "")).strip().lower(),
                tier=str(story.get("tier", "")).strip().lower(),
                article_ids=ids,
            )
        )

    dbg(f"llm: select_stories -> {len(clusters)} raw cluster(s)")
    return clusters


def extract_claims(clusters: list[RankedCluster], texts: dict[str, str]) -> dict[int, list[Claim]]:
    """Pass two: extract atomic claims from the full text fetched only for
    the articles selection chose, each claim anchored to exactly one
    article. This is the whole trust mechanism — a fact that is not an
    anchored claim has no way into the written story, because write_stories
    never sees article text, only these claims.

    Returns {cluster_id: [Claim, ...]}, omitting any cluster that ended with
    zero valid claims — the write pass must never be asked to write a story
    with nothing to write from."""
    blocks = []
    for cid, cluster in enumerate(clusters):
        parts = [f"=== STORY {cid} [{cluster.section}] — {cluster.headline_hint} ==="]
        for aid, a in enumerate(cluster.articles):
            full = texts.get(a.url) or ""
            kind = anchor.source_kind(full)
            body = full if kind == "fulltext" else a.summary
            label = "FULL TEXT" if kind == "fulltext" else "SUMMARY ONLY"
            parts.append(f"[a{aid}] {a.outlet} <{a.url}> ({label})\n{body}")
        blocks.append("\n\n".join(parts))
    prompt = "\n\n".join(blocks)

    result = _generate(prompt, _CLAIMS_SCHEMA, _CLAIMS_SYSTEM, "claims")

    if os.environ.get("DIGEST_DUMP_DIR"):
        _dump("clusters", json.dumps(anchor.clusters_fixture(clusters), ensure_ascii=False, indent=2))

    claims_by_cluster: dict[int, list[Claim]] = {}
    next_id = 1
    seen_cluster_ids: set[int] = set()
    rejected_claims = 0
    rejected_clusters = 0

    for story in result.get("stories", []):
        cid = story.get("cluster_id")
        if not isinstance(cid, int) or not (0 <= cid < len(clusters)) or cid in seen_cluster_ids:
            rejected_clusters += 1
            continue
        seen_cluster_ids.add(cid)
        cluster = clusters[cid]

        claims: list[Claim] = []
        for raw_claim in story.get("claims", []):
            aid = raw_claim.get("article_id")
            text = str(raw_claim.get("text", "")).strip()
            kind = raw_claim.get("kind")
            if kind not in anchor.CLAIM_KINDS:
                kind = "event"
            if not isinstance(aid, int) or not (0 <= aid < len(cluster.articles)):
                rejected_claims += 1
                continue
            if len(text) < anchor.MIN_CLAIM_CHARS:
                rejected_claims += 1
                continue
            article = cluster.articles[aid]
            full = texts.get(article.url) or ""
            claims.append(
                Claim(
                    id=next_id,
                    cluster_id=cid,
                    text=text,
                    kind=kind,
                    outlet=article.outlet,
                    url=article.url,
                    source_kind=anchor.source_kind(full),
                )
            )
            next_id += 1
            if len(claims) == anchor.MAX_CLAIMS_PER_STORY:
                break

        if not claims:
            dbg(f"llm: claims: [{cluster.section}] {cluster.headline_hint!r} -> 0 valid claims, dropping")
            continue

        outlets = len({c.outlet for c in claims})
        dbg(f"llm: claims: [{cluster.section}] {len(claims)} claim(s) from {outlets} outlet(s) {cluster.headline_hint!r}")
        claims_by_cluster[cid] = claims

    if os.environ.get("DIGEST_DUMP_DIR"):
        assigned = {cid: [dataclasses.asdict(c) for c in cs] for cid, cs in claims_by_cluster.items()}
        _dump("claims_assigned", json.dumps(assigned, ensure_ascii=False, indent=2))

    total_claims = sum(len(cs) for cs in claims_by_cluster.values())
    dbg(
        f"llm: extract_claims -> {len(claims_by_cluster)}/{len(clusters)} cluster(s), "
        f"{total_claims} claim(s) total, {rejected_clusters} cluster(s) rejected, "
        f"{rejected_claims} claim(s) rejected"
    )
    return claims_by_cluster


def write_stories(clusters: list[RankedCluster], claims_by_cluster: dict[int, list[Claim]]) -> list[dict]:
    """Pass three: write every story in a single call, from claims ONLY —
    the write pass never sees raw article text, so a fact that isn't an
    anchored claim has no way into the prose. anchor.build_story is the
    single semantic gate on what the model returns."""
    tier_label = {"lead": "LEAD", "major": "MAJOR", "notable": "NOTABLE"}
    blocks = []
    order: list[int] = []
    for cid, cluster in enumerate(clusters):
        claims = claims_by_cluster.get(cid)
        if not claims:
            continue
        order.append(cid)
        target = anchor.WORD_TARGET.get(cluster.tier, 200)
        label = tier_label.get(cluster.tier, cluster.tier.upper())
        parts = [
            f"=== STORY {cid} [{cluster.section}] — {label} — write about {target} words ===",
            f"Subject: {cluster.headline_hint}",
        ]
        for c in claims:
            kind_label = "full text" if c.source_kind == "fulltext" else "summary only"
            parts.append(f"[c{c.id}] ({c.outlet}, {kind_label}) {c.text}")
        blocks.append("\n".join(parts))

    if not blocks:
        dbg("llm: write_stories -> no clusters had claims; nothing to write")
        return []

    prompt = "\n\n".join(blocks)
    result = _generate(prompt, _WRITE_SCHEMA, _WRITE_SYSTEM, "write")

    by_cluster_id: dict[int, dict] = {}
    dropped = 0
    thin_count = 0
    for raw in result.get("stories", []):
        cid = raw.get("cluster_id")
        if not isinstance(cid, int) or cid not in claims_by_cluster or cid in by_cluster_id:
            dropped += 1
            continue
        story = anchor.build_story(clusters[cid], cid, raw, claims_by_cluster[cid])
        if story is None:
            dropped += 1
            continue
        if story["thin_sourced"]:
            thin_count += 1
        by_cluster_id[cid] = story

    missing = [cid for cid in order if cid not in by_cluster_id]
    if missing:
        dbg(f"llm: write_stories -> {len(missing)} cluster(s) with claims got no valid story: {missing}")

    stories = [by_cluster_id[cid] for cid in sorted(by_cluster_id)]
    dbg(f"llm: write_stories -> {len(stories)} story/stories written, {dropped} dropped, {thin_count} thin-sourced")
    return stories
