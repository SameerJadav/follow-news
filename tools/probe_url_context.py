#!/usr/bin/env python3
"""Probes whether the url_context tool actually works on this free tier.

    GEMINI_API_KEY=... uv run tools/probe_url_context.py

A ONE-OFF, like tools/capture_grounding.py. The pipeline never imports or
calls this.

dossier.md §4 Pass D is built on url_context: grounding returns search
*snippets*, and reading whole articles is the entire depth fix. But this
repo has already been burned once by a documented-but-unavailable feature —
CLAUDE.md records Gemini 2.5 Pro returning a hard `limit: 0` on this free
tier for both requests and input tokens, despite being what product.md
names. The SDK accepting a config proves nothing: url_context,
google_search and response_schema all validate client-side and are
enforced server-side only.

So this asks the server three questions before any code is built on the
answers:

  1. does url_context work at all, on its own?
  2. does url_context + google_search work in the SAME request?
  3. does search-off + response_schema work (Pass E depends on this)?

What matters in the output is `url_retrieval_status` per URL —
SUCCESS / ERROR / PAYWALL / UNSAFE — which is also what dossier.py records
so a URL it could not read is never a silent gap.

Re-run if 429s or tool availability start looking different from what
CLAUDE.md documents.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent

MODEL = "gemini-2.5-flash"  # matches ground.GROUND_MODEL

# Two real article URLs from the story that motivated dossier.md, so the
# probe exercises exactly the kind of page Pass D has to read. Falls back to
# a hardcoded pair if data/ has been cleared.
_FALLBACK_URLS = [
    "https://www.thehindu.com/news/national/",
    "https://www.hindustantimes.com/india-news/",
]


def _urls() -> list[str]:
    path = ROOT / "data" / "2026-07-27.json"
    try:
        day = json.loads(path.read_text())
    except (OSError, ValueError):
        return _FALLBACK_URLS
    for story in day.get("stories", []):
        if "exam reform" in story.get("headline", ""):
            urls = [s["url"] for s in story.get("sources", [])[:2] if s.get("url")]
            if len(urls) == 2:
                return urls
    return _FALLBACK_URLS


def _report(name: str, resp) -> None:
    print(f"\n--- {name}: OK ---")
    text = (resp.text or "").strip()
    print(f"response: {len(text)} chars")
    print(f"  {text[:300]!r}")

    cand = resp.candidates[0] if resp.candidates else None
    meta = getattr(cand, "url_context_metadata", None) if cand else None
    if meta is None:
        print("  url_context_metadata: ABSENT")
    else:
        for um in getattr(meta, "url_metadata", None) or []:
            print(f"  {um.url_retrieval_status} {um.retrieved_url}")

    gm = getattr(cand, "grounding_metadata", None) if cand else None
    if gm is not None:
        chunks = len(getattr(gm, "grounding_chunks", None) or [])
        queries = list(getattr(gm, "web_search_queries", None) or [])
        print(f"  grounding: {chunks} chunk(s), queries={queries}")


def _attempt(name: str, client, prompt: str, config) -> bool:
    try:
        resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
    except Exception as exc:  # noqa: BLE001 - the failure IS the result here
        print(f"\n--- {name}: FAILED ---")
        print(f"  {exc!r}"[:600])
        details = getattr(exc, "details", None)
        if details:
            print(f"  details: {json.dumps(details)[:600]}")
        return False
    _report(name, resp)
    return True


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")

    urls = _urls()
    print(f"model: {MODEL}")
    print("urls:")
    for u in urls:
        print(f"  {u}")

    client = genai.Client(api_key=key)
    joined = "\n".join(urls)
    results: dict[str, bool] = {}

    results["1. url_context alone"] = _attempt(
        "1. url_context alone",
        client,
        f"Read these pages and list, in one sentence each, the three most "
        f"specific dated facts they report:\n{joined}",
        types.GenerateContentConfig(
            tools=[types.Tool(url_context=types.UrlContext())],
            max_output_tokens=800,
            temperature=0.3,
        ),
    )

    results["2. url_context + google_search"] = _attempt(
        "2. url_context + google_search",
        client,
        f"Read these pages, then search for anything they refer to but do not "
        f"explain. List the three most specific dated facts:\n{joined}",
        types.GenerateContentConfig(
            tools=[
                types.Tool(url_context=types.UrlContext()),
                types.Tool(google_search=types.GoogleSearch()),
            ],
            max_output_tokens=800,
            temperature=0.3,
        ),
    )

    results["3. search OFF + response_schema"] = _attempt(
        "3. search OFF + response_schema",
        client,
        "Return two short dated events from the 2026 Indian exam-leak protests.",
        types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"date": {"type": "string"}, "what": {"type": "string"}},
                            "required": ["date", "what"],
                        },
                    }
                },
                "required": ["events"],
            },
            max_output_tokens=800,
            temperature=0.3,
        ),
    )

    print("\n=== VERDICT ===")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    if not results["1. url_context alone"]:
        print("\n-> Pass D must degrade to extract.py only.")
    elif not results["2. url_context + google_search"]:
        print("\n-> Pass D works, but url_context and google_search need SEPARATE calls.")
    else:
        print("\n-> Pass D can combine url_context and google_search in one call.")
    if not results["3. search OFF + response_schema"]:
        print("-> Pass E cannot use response_schema; fall back to delimited text.")
        sys.exit(1)


if __name__ == "__main__":
    main()
