#!/usr/bin/env python3
"""Probes whether SCHEMA_MODEL (gemini-3.6-flash) can also do the grounded work.

    GEMINI_API_KEY=... uv run tools/probe_ground_model.py

A ONE-OFF, like tools/probe_url_context.py. The pipeline never imports it.

`ground.GROUND_MODEL` is pinned to gemini-2.5-flash with the comment "verified
working with google_search". That verification was against 2.5 Pro failing
(`limit: 0`) -- there is no record in this repo of 3.6 Flash ever being tried
with a tool at all. It was chosen for the tool-less passes because it matches
`llm.MODEL`, not because it couldn't ground.

That matters now that the real ceiling is known. AI Studio reports RPD = 20 per
model on this key for BOTH 2.5 Flash and 3.6 Flash, and research.md §3.1 says
search grounding on Gemini 3 models is metered at 5,000 prompts/month
(~166/day) -- far above 20, so the tool allowance cannot be what stops us.

Three questions, in descending order of what they'd change:

  1. Does 3.6 Flash ground with google_search, returning the
     grounding_chunks / grounding_supports shape ground.py already parses?
     If yes, model choice is free: the better model does the research, and
     2.5 Flash becomes just another 20-RPD pool to spend rather than "the
     grounding model".

  2. Does 3.6 Flash read pages with url_context? Pass D needs this if the
     grounded work moves here wholesale.

  3. Does 3.6 Flash accept a tool AND response_schema in one request?
     On 2.5 this is a hard 400 (CLAUDE.md, re-verified 2026-07-27), which is
     the entire reason dossier.py splits Pass D (read) from Pass E (structure).
     If Gemini 3 lifted that restriction, those two calls collapse into one
     and Follow's per-round cost drops by a third.

Each question costs one request from 3.6 Flash's 20/day pool. Nothing here
writes to data/, followed/ or docs/.
"""

from __future__ import annotations

import json
import os

from google import genai
from google.genai import types

MODEL = "gemini-3.6-flash"  # matches ground.SCHEMA_MODEL and llm.MODEL

# A page that is certainly reachable and certainly has dated facts on it.
URL = "https://www.thehindu.com/news/national/"

_LEDGER_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "what": {"type": "string"},
                },
                "required": ["date", "what"],
            },
        }
    },
    "required": ["facts"],
}


def _report(name: str, resp) -> None:
    print(f"\n--- {name}: OK ---")
    text = (resp.text or "").strip()
    print(f"  response: {len(text)} chars")
    print(f"    {text[:240]!r}")

    cand = resp.candidates[0] if resp.candidates else None
    print(f"  finish_reason: {getattr(cand, 'finish_reason', None)}")

    gm = getattr(cand, "grounding_metadata", None) if cand else None
    if gm is None:
        print("  grounding_metadata: ABSENT")
    else:
        chunks = list(getattr(gm, "grounding_chunks", None) or [])
        supports = list(getattr(gm, "grounding_supports", None) or [])
        queries = list(getattr(gm, "web_search_queries", None) or [])
        print(f"  grounding: {len(chunks)} chunk(s), {len(supports)} support(s)")
        print(f"  web_search_queries: {queries}")
        # The two shapes ground.py depends on: a resolvable chunk URI, and a
        # support whose segment carries byte offsets into the response text.
        for chunk in chunks[:2]:
            web = getattr(chunk, "web", None)
            if web is not None:
                print(f"    chunk uri: {getattr(web, 'uri', None)}")
                print(f"    chunk title: {getattr(web, 'title', None)}")
        for sup in supports[:2]:
            seg = getattr(sup, "segment", None)
            print(
                f"    support: start={getattr(seg, 'start_index', None)} "
                f"end={getattr(seg, 'end_index', None)} "
                f"chunks={list(getattr(sup, 'grounding_chunk_indices', None) or [])}"
            )

    meta = getattr(cand, "url_context_metadata", None) if cand else None
    if meta is not None:
        for um in getattr(meta, "url_metadata", None) or []:
            print(f"  url: {um.url_retrieval_status} {um.retrieved_url}")

    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        print(
            f"  tokens: prompt={getattr(usage, 'prompt_token_count', None)} "
            f"output={getattr(usage, 'candidates_token_count', None)} "
            f"thoughts={getattr(usage, 'thoughts_token_count', None)}"
        )


def _attempt(name: str, client, prompt: str, config) -> bool:
    try:
        resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
    except Exception as exc:  # noqa: BLE001 - the failure IS the result here
        print(f"\n--- {name}: FAILED ---")
        print(f"  {exc!r}"[:700])
        details = getattr(exc, "details", None)
        if details:
            print(f"  details: {json.dumps(details)[:700]}")
        return False
    _report(name, resp)
    return True


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")

    print(f"model: {MODEL}")
    client = genai.Client(api_key=key)
    results: dict[str, bool] = {}

    results["1. google_search"] = _attempt(
        "1. google_search",
        client,
        "Search for what happened with the NEET UG 2026 exam paper leak. "
        "List the three most specific dated developments, one sentence each.",
        types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=8192,
            temperature=0.3,
        ),
    )

    results["2. url_context"] = _attempt(
        "2. url_context",
        client,
        f"Read this page and list, in one sentence each, the three most "
        f"specific dated facts it reports:\n{URL}",
        types.GenerateContentConfig(
            tools=[types.Tool(url_context=types.UrlContext())],
            max_output_tokens=8192,
            temperature=0.3,
        ),
    )

    results["3. google_search + response_schema"] = _attempt(
        "3. google_search + response_schema",
        client,
        "Search for the NEET UG 2026 exam paper leak and return the three most "
        "specific dated developments.",
        types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            response_schema=_LEDGER_SCHEMA,
            max_output_tokens=8192,
            temperature=0.3,
        ),
    )

    print("\n=== SUMMARY ===")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    main()
