#!/usr/bin/env python3
"""Captures one real grounded Gemini response as a test fixture.

    GEMINI_API_KEY=... uv run tools/capture_grounding.py

A ONE-OFF. The pipeline never imports or calls this. tests/test_follow.py
and tests/test_ground.py must exercise the byte-offset and block-splitting
logic against a shape Gemini actually returns, not a shape we imagine it
returns — so this makes one real grounded call and freezes the parts of the
response those tests need: the text, the grounding chunks, the grounding
supports, the search queries, and the Search Suggestions HTML.

search_suggestions is truncated to its first 200 characters in the fixture:
tests only need to know it's a non-empty string, and the full ~6KB payload
doesn't belong committed to the repo twice (it's also visible, in full, in
any followed/*.json once Follow has actually run).

Re-run only if ground.py's parsing of the response shape needs a fresh
example to test against.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google import genai
from google.genai import types

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "grounding.json"

MODEL = "gemini-2.5-flash"  # matches ground.GROUND_MODEL; verified working with google_search
PROMPT = "In three short paragraphs: what is the background of the 2026 US-Iran military escalation, and where does it stand today?"


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")

    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=MODEL,
        contents=PROMPT,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=4000,
            temperature=0.3,
        ),
    )
    text = resp.text or ""
    metadata = resp.candidates[0].grounding_metadata

    chunks = []
    for chunk in metadata.grounding_chunks or []:
        web = chunk.web
        chunks.append({"title": web.title if web else "", "uri": web.uri if web else ""})

    supports = []
    for support in metadata.grounding_supports or []:
        seg = support.segment
        supports.append(
            {
                "start_index": seg.start_index,
                "end_index": seg.end_index,
                "grounding_chunk_indices": list(support.grounding_chunk_indices or []),
            }
        )

    sep = metadata.search_entry_point
    suggestions = (sep.rendered_content or "")[:200] if sep else ""

    fixture = {
        "text": text,
        "chunks": chunks,
        "supports": supports,
        "queries": list(metadata.web_search_queries or []),
        "search_suggestions": suggestions,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {FIXTURE} ({len(text)} chars, {len(chunks)} chunk(s), {len(supports)} support(s))")


if __name__ == "__main__":
    main()
