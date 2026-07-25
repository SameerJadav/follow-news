"""Gemini calls — the entire quota strategy for this project.

Free-tier requests-per-day is unpublished and was cut 50-80% without notice
in the past (research.md §3.1), so the pipeline must never scale call count
with article count. Selection reads only cheap headlines/summaries; full
article text is fetched (extract.py) only for the articles selection chose;
writing then reads that text once. Two calls per day, independent of how
many articles were ingested.

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

MODEL = "gemini-3.6-flash"  # pinned here; change in exactly one place

MAX_SELECT_ARTICLES = 250  # cap on how many headlines pass one sees
MAX_STORIES = 8  # Phase 1 only — Phase 2 makes this float with the news
MAX_ARTICLES_PER_STORY = 5  # bounds how much text pass two must read per story
SUMMARY_CAP = 240

RATE_LIMIT_SLEEP = 60  # seconds; Phase 6 replaces this with real wait-and-resume
MAX_ATTEMPTS = 3

_SELECT_SCHEMA = {
    "type": "object",
    "required": ["stories"],
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["headline_hint", "article_ids"],
                "properties": {
                    "headline_hint": {"type": "string"},
                    "article_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        }
    },
}

_SELECT_SYSTEM = f"""You are an editor picking the day's genuinely biggest news \
stories from a numbered list of headlines and summaries.

Group article ids that cover the SAME underlying story into one cluster — \
different outlets often cover the same event. Pick at most {MAX_STORIES} \
stories: only the ones that are genuinely significant. Do not pad the list \
to hit a target count, and do not invent a story that isn't clearly \
supported by the input."""

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


def select_stories(articles: list[Article]) -> list[list[int]]:
    """Pass one: send cheap headlines+summaries, get back which article ids
    cluster into which stories. No article text is fetched for this call."""
    sent = articles[:MAX_SELECT_ARTICLES]
    lines = [
        f"[{i}] ({a.outlet}) {a.title} — {a.summary[:SUMMARY_CAP]}" for i, a in enumerate(sent)
    ]
    prompt = "\n".join(lines)

    result = _generate(prompt, _SELECT_SCHEMA, _SELECT_SYSTEM)

    clusters: list[list[int]] = []
    rejected = 0
    for story in result.get("stories", []):
        ids = story.get("article_ids", [])
        seen: set[int] = set()
        valid: list[int] = []
        for i in ids:
            if not isinstance(i, int) or not (0 <= i < len(sent)):
                rejected += 1
                continue
            if i in seen:
                continue
            seen.add(i)
            valid.append(i)
            if len(valid) >= MAX_ARTICLES_PER_STORY:
                break
        if valid:
            clusters.append(valid)
        if len(clusters) >= MAX_STORIES:
            break

    dbg(f"llm: select_stories -> {len(clusters)} cluster(s), {rejected} id(s) rejected as out of range")
    return clusters


def write_stories(clusters: list[list[Article]], texts: dict[str, str]) -> list[dict]:
    """Pass two: write every story in a single call, from the full text
    fetched only for the articles selection chose."""
    blocks = []
    for cid, articles in enumerate(clusters):
        parts = [f"=== STORY {cid} ==="]
        for a in articles:
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
        by_cluster_id[cid] = {
            "headline": story["headline"],
            "body": body,
            "sources": [{"outlet": a.outlet, "url": a.url} for a in clusters[cid]],
            "vocab": [
                {"term": v["term"], "say": v["say"], "meaning": v["meaning"]}
                for v in story.get("vocab", [])
                if "term" in v and "say" in v and "meaning" in v
            ],
        }

    # Preserve the original cluster order regardless of what order the model
    # returned entries in.
    stories = [by_cluster_id[cid] for cid in sorted(by_cluster_id)]
    dbg(f"llm: write_stories -> {len(stories)} story/stories written, {dropped} dropped")
    return stories
