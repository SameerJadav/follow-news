"""Gemini calls — the entire quota strategy for this project.

Free-tier requests-per-day is unpublished and was cut 50-80% without notice
in the past (research.md §3.1), so the pipeline must never scale call count
with article count. Selection reads only cheap headlines/summaries; full
article text is fetched (extract.py) only for the articles selection chose;
writing then reads that text once. Two calls per day, independent of how
many articles were ingested.

Selection now emits a section/category/tier per cluster rather than just a
grouping — rank.py turns those into a measured weight and decides which
clusters survive. All semantic validation of the model's output belongs to
rank.rank_clusters; this module only guarantees syntactically sane types.

Phase 3 replaces write_stories with a claims pass plus claim-anchored
writing. Sources being pipeline-derived here (never emitted by the model)
is the seam that keeps that swap local to this file.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from google import genai
from google.genai import types

from feeds import Article, dbg
from rank import CATEGORIES, RankedCluster, SECTIONS, SelectedCluster, TIERS

MODEL = "gemini-3.6-flash"  # pinned here; change in exactly one place

SUMMARY_CAP = 240

RATE_LIMIT_SLEEP = 60  # seconds; Phase 6 replaces this with real wait-and-resume
MAX_ATTEMPTS = 3

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
non-native reader — clear and jargon-free, but not a children's version.

Each story is ONE continuous piece of prose. Weave in why the story matters \
naturally as you go — never a separate labelled section for significance. \
Write so a first-time reader needs no earlier digest to understand the \
story completely. Use neutral attribution ("the ministry says", "witnesses \
told the BBC") and do not invent facts beyond what the source text \
supports.

For each story, also list 2-6 harder vocabulary words used in it. For each: \
`term` is the word as used in the story, `meaning` is a simple one-line \
definition, and `say` is a PHONETIC RESPELLING with the stressed syllable \
in capital letters (e.g. "sovereignty" -> "SOV-rin-tee") — never IPA."""


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=key)


def _generate(prompt: str, schema: dict, system: str) -> Any:
    global _CALLS
    client = _client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        system_instruction=system,
        temperature=0.3,
    )
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _CALLS += 1
            dbg(f"llm: call #{_CALLS} model={MODEL}")
            resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            return json.loads(resp.text)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            text = str(exc)
            if "429" in text or "RESOURCE_EXHAUSTED" in text:
                dbg(f"llm: rate limited (attempt {attempt}/{MAX_ATTEMPTS}), sleeping {RATE_LIMIT_SLEEP}s")
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            raise
    assert last_exc is not None  # loop above only exits via return/raise/continue
    raise last_exc  # exhausted retries on repeated 429s


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

    result = _generate(prompt, _SELECT_SCHEMA, _SELECT_SYSTEM)

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


def write_stories(clusters: list[RankedCluster], texts: dict[str, str]) -> list[dict]:
    """Pass two: write every story in a single call, from the full text
    fetched only for the articles selection chose."""
    blocks = []
    for cid, cluster in enumerate(clusters):
        parts = [f"=== STORY {cid} [{cluster.section}] ==="]
        for a in cluster.articles:
            body = texts.get(a.url) or a.summary
            parts.append(f"SOURCE: {a.outlet} <{a.url}>\n{body}")
        blocks.append("\n\n".join(parts))
    prompt = "\n\n".join(blocks)

    result = _generate(prompt, _WRITE_SCHEMA, _WRITE_SYSTEM)

    by_cluster_id: dict[int, dict] = {}
    dropped = 0
    for story in result.get("stories", []):
        cid = story.get("cluster_id")
        body = story.get("body", "")
        if not isinstance(cid, int) or not (0 <= cid < len(clusters)) or cid in by_cluster_id:
            dropped += 1
            continue
        if not story.get("headline") or len(body) < 200:
            dropped += 1
            continue
        cluster = clusters[cid]
        by_cluster_id[cid] = {
            "section": cluster.section,
            "headline": story["headline"],
            "body": body,
            "sources": [{"outlet": a.outlet, "url": a.url} for a in cluster.articles],
            "vocab": [
                {"term": v["term"], "say": v["say"], "meaning": v["meaning"]}
                for v in story.get("vocab", [])
                if "term" in v and "say" in v and "meaning" in v
            ],
            "signals": {
                "category": cluster.category,
                "tier": cluster.tier,
                "distinct_outlets": cluster.distinct_outlets,
                "wiki_backed": cluster.wiki_backed,
                "weight": cluster.weight,
            },
        }

    # Preserve the original cluster order regardless of what order the model
    # returned entries in.
    stories = [by_cluster_id[cid] for cid in sorted(by_cluster_id)]
    dbg(f"llm: write_stories -> {len(stories)} story/stories written, {dropped} dropped")
    return stories
