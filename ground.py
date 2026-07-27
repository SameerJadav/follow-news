"""The only module that talks to Gemini for Follow.

Grounding and structured output do not mix — sending
`response_mime_type="application/json"` alongside the `google_search` tool was
verified (2026-07-25) to return `400 INVALID_ARGUMENT: Tool use with a response
mime type: 'application/json' is unsupported`. So a grounded call returns plain
prose plus `grounding_metadata`: `url_citation`-style annotations carrying
byte-offset spans into that prose, keyed to a list of source chunks. This is
the same shape `render._prose_html` already consumes for claim-anchored
digest stories, so it is reused directly — only the extraction of markers
differs (from Gemini's `grounding_supports`/`grounding_chunks` here, rather
than from `[cN]` tokens as in anchor.py).

That restriction is specific to `google_search`. With the search tool OFF,
`response_schema` works normally — verified live 2026-07-27 — which is what
lets dossier.py's ledger and write passes ask for validated JSON instead of a
delimited text format.

Also verified live: Gemini 2.5 Pro returns `limit: 0` on this free tier for
both requests and input tokens — a hard zero, not a daily exhaustion — while
`gemini-2.5-flash` grounds successfully. GROUND_MODEL is pinned to the model
that actually works, not the one product.md/decisions.md name; see
meta-plan.md Phase 5's write-up for the verification.

`url_context` (dossier.md §4 Pass D) was verified working on this free tier on
2026-07-27 — grounding returns search *snippets*, and reading whole articles is
the entire depth fix. Two measured facts shape how it is used: the fetched page
content arrives as INPUT tokens (~3.3k per page, so 20 URLs is ~66k), and
combining it with `google_search` in one request makes the model read the URLs
and skip searching entirely (`web_search_queries` came back empty). So dossier
keeps searching and reading as separate calls rather than combining the tools.

Because grounding can't use response_schema, a batched multi-part grounded call
can't ask for JSON either. Instead the model is asked to emit one delimited
block per key (`=== BLOCK <key> ===`, optionally `STATUS: ...`, then prose), and
Python parses and validates that structure — the same "syntactically whatever,
semantically validate in application code" posture llm.py takes with real JSON.

429s are handled by ratelimit.call_with_resume, the same wait-and-resume
mechanism llm.py uses — see ratelimit.py for why.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from google import genai
from google.genai import types

import ratelimit
import tracer
from feeds import UA
from tracer import dbg

GROUND_MODEL = "gemini-2.5-flash"  # verified working with google_search; 2.5 Pro is limit:0 on this free tier
MAX_OUTPUT_TOKENS = 8192
# url_context puts whole pages into the request, and gemini-2.5-flash spends
# output tokens on thinking before it writes anything. Measured 2026-07-27: the
# same read-and-search call returned an EMPTY response at 800 output tokens and
# 2,610 characters at 8192. A dossier pass that asks for a long ledger needs
# more still, so passes raise this per call rather than sharing one ceiling.
MAX_OUTPUT_TOKENS_LONG = 32768
HEAD_TIMEOUT = 10

_CALLS = 0  # total grounded calls this process has made; greppable in dbg() output

# Accepts the historical "=== FOLLOW <key> ===" spelling as well as the general
# one, so a response that echoes an older prompt shape still parses.
_BLOCK_HEADER_RE = re.compile(r"^=== (?:BLOCK|FOLLOW) (\S+) ===\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\s*STATUS:\s*(\w+)\s*$", re.MULTILINE)
_STATUSES = ("development", "quiet", "final")


@dataclass(frozen=True, slots=True)
class GroundedBlock:
    """One piece of grounded prose plus the sources behind it. Mirrors the
    fields render.py already knows how to draw a prose+sources block from
    (see render._grounded_html), with `queries`/`search_suggestions` added
    for Follow's own display requirements."""

    body: str
    markers: list[dict]  # {"start": int, "end": int, "outlet": str, "url": str}
    sources: list[dict]  # {"outlet": str, "url": str}, first-cited order
    queries: list[str]
    search_suggestions: str  # verbatim searchEntryPoint.rendered_content, never modified


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=key)


def _generate(
    prompt: str,
    system: str,
    label: str,
    *,
    search: bool = True,
    urls: list[str] | None = None,
    schema: dict | None = None,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> tuple[str, Any]:
    """One Gemini call. Returns (text, grounding_metadata|None). Waits out a
    429 via ratelimit.call_with_resume, same mechanism llm._generate uses;
    anything else propagates so the caller can decide whether to skip this
    story or fail the whole Follow run.

    `search` and `schema` are mutually exclusive at the API, not just by
    convention — passing both is a caller bug and raises here rather than
    earning a 400 from the server. `urls` switches the url_context tool on;
    the URLs themselves go in the prompt, the tool only grants permission to
    fetch them.
    """
    if search and schema is not None:
        raise ValueError("google_search and response_schema cannot be combined (400 INVALID_ARGUMENT)")

    global _CALLS
    client = _client()

    tools = []
    if search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    if urls:
        tools.append(types.Tool(url_context=types.UrlContext()))

    config = types.GenerateContentConfig(
        tools=tools or None,
        system_instruction=system,
        temperature=0.3,
        max_output_tokens=max_output_tokens,
        **({"response_mime_type": "application/json", "response_schema": schema} if schema else {}),
    )
    _CALLS += 1
    mode = f"search={search} urls={len(urls or [])} schema={schema is not None}"
    dbg(f"ground: call #{_CALLS} model={GROUND_MODEL} label={label} prompt={len(prompt)}ch {mode}")
    started = time.monotonic()
    resp = ratelimit.call_with_resume(
        lambda: client.models.generate_content(model=GROUND_MODEL, contents=prompt, config=config),
        label,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    candidates = resp.candidates or []
    finish = candidates[0].finish_reason if candidates else None
    dbg(f"ground: {label} finish_reason={finish}")
    metadata = candidates[0].grounding_metadata if candidates else None

    # An empty response with finish_reason=MAX_TOKENS is the specific failure
    # measured on 2026-07-27: thinking plus url_context overhead consumed the
    # whole output budget. Name it, because "empty" alone reads as "the model
    # had nothing to say" and sends debugging the wrong way.
    if not (resp.text or "").strip():
        dbg(f"ground: {label} -> EMPTY RESPONSE (finish_reason={finish}, cap={max_output_tokens})")

    # Grounding can't use response_schema, so the output is delimited prose
    # parsed in Python — which means a parse failure looks identical to a
    # quiet day unless the raw text is kept.
    stem = f"follow/ground-{_CALLS}-{tracer.slug(label)}"
    tracer.artifact(f"{stem}.system.txt", system)
    tracer.artifact(f"{stem}.prompt.txt", prompt)
    tracer.artifact(f"{stem}.response.txt", resp.text or "")
    tracer.event(
        "ground",
        call=_CALLS,
        label=label,
        model=GROUND_MODEL,
        latency_ms=latency_ms,
        prompt_chars=len(prompt),
        response_chars=len(resp.text or ""),
        finish_reason=str(finish),
        search=search,
        url_context=len(urls or []),
        schema=schema is not None,
        max_output_tokens=max_output_tokens,
        grounded=metadata is not None,
        chunks=len(getattr(metadata, "grounding_chunks", None) or []),
        supports=len(getattr(metadata, "grounding_supports", None) or []),
        queries=list(getattr(metadata, "web_search_queries", None) or []),
        url_status=_url_statuses(candidates[0] if candidates else None),
    )
    return resp.text or "", metadata


def _url_statuses(candidate: Any) -> dict[str, str]:
    """Per-URL retrieval outcome from a url_context call: SUCCESS, ERROR,
    PAYWALL or UNSAFE. dossier.md §13 forbids a silent cap, and a page the
    model could not read is exactly that — so the reason is recorded rather
    than the URL just quietly contributing nothing."""
    meta = getattr(candidate, "url_context_metadata", None) if candidate else None
    out: dict[str, str] = {}
    for um in getattr(meta, "url_metadata", None) or []:
        url = str(getattr(um, "retrieved_url", "") or "")
        if url:
            out[url] = str(getattr(um, "url_retrieval_status", "") or "")
    return out


def _char_offsets(text: str) -> list[int]:
    """prefix[i] = UTF-8 byte length of text[:i]. Gemini's Segment offsets are
    BYTE offsets into the response text, not character offsets — verified
    live: a support's start/end only slices the right substring once run
    through this conversion. Non-ASCII (an outlet name, a rupee sign, a
    Turkish letter) is exactly where a naive character-offset slice would go
    silently wrong."""
    out = [0]
    total = 0
    for ch in text:
        total += len(ch.encode("utf-8"))
        out.append(total)
    return out


def _byte_to_char(prefix: list[int], b: int) -> int:
    """Inverse of _char_offsets: the character index whose byte-prefix
    reaches `b`. Clamped so an out-of-range offset degrades to the nearest
    valid index rather than raising."""
    i = bisect.bisect_left(prefix, b)
    return max(0, min(i, len(prefix) - 1))


def _resolve(uri: str, cache: dict[str, str]) -> str:
    """Follow a vertexaisearch.cloud.google.com/grounding-api-redirect/... link
    to the real publisher URL, once, at generation time. These redirects
    expire in roughly 30 days; the archive is permanent, so the resolved URL
    is what gets stored. Falls back to the redirect URL itself on any
    failure — a working-for-now link beats no link."""
    if uri in cache:
        return cache[uri]
    resolved = uri
    error = ""
    try:
        resp = requests.head(uri, allow_redirects=True, timeout=HEAD_TIMEOUT, headers={"User-Agent": UA})
        if resp.url:
            resolved = resp.url
    except requests.RequestException as exc:
        error = repr(exc)[:200]
        dbg(f"ground: could not resolve {uri[:80]}... ({exc!r}); keeping redirect URL")
    cache[uri] = resolved
    # An unresolved redirect is a link that dies in ~30 days. Worth knowing
    # about before the archive quietly fills with them.
    tracer.event("ground", op="resolve", redirect=uri, resolved=resolved,
                 ok=resolved != uri, error=error)
    return resolved


def _outlet(title: str, url: str) -> str:
    """Google's grounding chunks give a bare domain as `title` (e.g.
    "cfr.org"); fall back to the resolved URL's own domain if title is
    empty."""
    title = (title or "").strip()
    if title:
        return title
    host = urlsplit(url).netloc
    return host[4:] if host.startswith("www.") else host


def _chunks(metadata: Any, cache: dict[str, str]) -> list[tuple[str, str]]:
    """[(outlet, resolved_url), ...] indexed exactly as
    metadata.grounding_chunks, so grounding_chunk_indices can index straight
    into it. A chunk with no `.web` (e.g. a Maps chunk) is kept as a
    placeholder so indices still line up, but never resolves to a marker."""
    out: list[tuple[str, str]] = []
    for chunk in getattr(metadata, "grounding_chunks", None) or []:
        web = getattr(chunk, "web", None)
        if web is None or not web.uri:
            out.append(("", ""))
            continue
        url = _resolve(web.uri, cache)
        out.append((_outlet(web.title, url), url))
    return out


def _markers_from_metadata(text: str, metadata: Any, cache: dict[str, str]) -> list[dict]:
    """Turn grounding_supports (byte-offset spans + chunk indices) into the
    same marker shape anchor.py produces for digest claims: one Marker per
    (support x chunk), all sharing the span, when a statement is corroborated
    by more than one source — never one merged marker covering several
    outlets."""
    if metadata is None:
        return []
    prefix = _char_offsets(text)
    chunks = _chunks(metadata, cache)

    markers: list[dict] = []
    for support in getattr(metadata, "grounding_supports", None) or []:
        segment = getattr(support, "segment", None)
        if segment is None or segment.end_index is None:
            continue
        start_b = segment.start_index or 0
        end_b = segment.end_index
        if end_b <= start_b:
            continue
        start = _byte_to_char(prefix, start_b)
        end = _byte_to_char(prefix, end_b)
        if end <= start:
            continue
        for idx in support.grounding_chunk_indices or []:
            if not (0 <= idx < len(chunks)):
                continue
            outlet, url = chunks[idx]
            if not url:
                continue
            markers.append({"start": start, "end": end, "outlet": outlet, "url": url})

    markers.sort(key=lambda m: (m["start"], m["end"]))
    return markers


def _sources_from_markers(markers: list[dict]) -> list[dict]:
    """{outlet, url} for markers actually present, deduped, first-cited
    order — identical in spirit to anchor.cited_sources, so numerals in the
    prose and rows in the source list use the same index."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for m in markers:
        key = (m["outlet"], m["url"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"outlet": m["outlet"], "url": m["url"]})
    return out


_TRIM_CHARS = " \t\n"


def _block(
    text: str,
    markers: list[dict],
    start: int,
    end: int,
    queries: list[str],
    suggestions: str,
) -> GroundedBlock:
    """Slice text[start:end], strip it, and re-base every marker fully inside
    that range by the same amount the leading whitespace was trimmed — the
    same trim-and-shift arithmetic anchor.parse_body uses for digest bodies,
    so offsets stay exact against the final, trimmed body."""
    raw = text[start:end]
    lead = len(raw) - len(raw.lstrip(_TRIM_CHARS))
    body = raw.strip(_TRIM_CHARS)

    shifted: list[dict] = []
    body_len = len(body)
    for m in markers:
        if m["start"] < start or m["end"] > end:
            continue
        s = m["start"] - start - lead
        e = m["end"] - start - lead
        s = max(0, min(body_len, s))
        e = max(0, min(body_len, e))
        if e > s:
            shifted.append({"start": s, "end": e, "outlet": m["outlet"], "url": m["url"]})

    return GroundedBlock(
        body=body,
        markers=shifted,
        sources=_sources_from_markers(shifted),
        queries=queries,
        search_suggestions=suggestions,
    )


def _entry_point_html(metadata: Any) -> str:
    sep = getattr(metadata, "search_entry_point", None)
    return (sep.rendered_content or "") if sep else ""


def research(prompt: str, system: str, label: str) -> GroundedBlock | None:
    """The single-block case: a backstory. Returns None when the model
    produced no text or the response carried no grounding at all — an
    ungrounded "backstory" has no sources and must not be published."""
    text, metadata = _generate(prompt, system, label)
    if not text.strip():
        dbg(f"ground: {label} -> empty response")
        return None

    markers = _markers_from_metadata(text, metadata, {})
    if not markers:
        dbg(f"ground: {label} -> no grounding citations; dropping")
        return None

    queries = list(getattr(metadata, "web_search_queries", None) or [])
    suggestions = _entry_point_html(metadata)
    block = _block(text, markers, 0, len(text), queries, suggestions)
    dbg(f"ground: {label} -> {len(block.body.split())} words, {len(block.sources)} source(s)")
    return block


def research_blocks(
    prompt: str,
    system: str,
    label: str,
    keys: list[str],
    *,
    require_status: bool = False,
) -> dict[str, tuple[str, GroundedBlock | None]]:
    """The multi-block case: one grounded call answering several keyed
    questions at once, so a round's searches stay batched rather than one call
    each. Returns {key: (status, block|None)}; a key the model never produced
    maps to ("quiet", None) — silence is always the safe default, never a
    fabricated entry.

    With `require_status` the model must emit a STATUS line per block and an
    unparseable one is coerced to quiet. Without it (dossier.py's Pass C, which
    only ever wants findings) a block is simply prose under a header and its
    status reads "ok".
    """
    text, metadata = _generate(prompt, system, label)
    result: dict[str, tuple[str, GroundedBlock | None]] = {k: ("quiet", None) for k in keys}
    if not text.strip():
        dbg(f"ground: {label} -> empty response")
        return result

    markers = _markers_from_metadata(text, metadata, {})
    queries = list(getattr(metadata, "web_search_queries", None) or [])
    suggestions = _entry_point_html(metadata)

    headers = list(_BLOCK_HEADER_RE.finditer(text))
    seen: set[str] = set()
    for i, m in enumerate(headers):
        key = m.group(1)
        if key not in keys or key in seen:
            if key not in keys:
                dbg(f"ground: {label} -> unrecognised key {key!r}, discarding")
            continue
        seen.add(key)

        block_start = m.end()
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        chunk = text[block_start:block_end]

        status_match = _STATUS_RE.search(chunk)
        if status_match is None and require_status:
            dbg(f"ground: {label} -> {key} has no parseable STATUS line, treating as quiet")
            continue

        status = "ok"
        body_start = block_start
        if status_match is not None:
            status = status_match.group(1).strip().lower()
            body_start = block_start + status_match.end()
            if require_status and status not in _STATUSES:
                dbg(f"ground: {label} -> {key} unknown STATUS {status!r}, coercing to quiet")
                status = "quiet"
            if status == "quiet":
                result[key] = ("quiet", None)
                continue

        if not text[body_start:block_end].strip():
            dbg(f"ground: {label} -> {key} STATUS={status} but no prose followed; treating as quiet")
            result[key] = ("quiet", None)
            continue

        block = _block(text, markers, body_start, block_end, queries, suggestions)
        if not block.body.strip():
            result[key] = ("quiet", None)
            continue
        result[key] = (status, block)

    missing = [k for k in keys if k not in seen]
    if missing:
        dbg(f"ground: {label} -> {len(missing)}/{len(keys)} key(s) not found in response: {missing}")

    return result


def read_urls(prompt: str, system: str, label: str, urls: list[str]) -> GroundedBlock | None:
    """A url_context call: the model reads whole pages rather than search
    snippets (dossier.md §4 Pass D). Search stays OFF — combining the two tools
    was measured to make the model skip searching entirely.

    Unlike research(), a missing citation is not fatal: the value here is the
    page text the model read, and dossier.py attributes findings to the URLs it
    was handed. Returns None only when nothing came back at all."""
    text, metadata = _generate(
        prompt, system, label, search=False, urls=urls, max_output_tokens=MAX_OUTPUT_TOKENS_LONG
    )
    if not text.strip():
        dbg(f"ground: {label} -> empty response")
        return None
    markers = _markers_from_metadata(text, metadata, {})
    queries = list(getattr(metadata, "web_search_queries", None) or [])
    return _block(text, markers, 0, len(text), queries, _entry_point_html(metadata))


def structured(
    prompt: str,
    system: str,
    label: str,
    schema: dict,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_LONG,
) -> Any:
    """A search-OFF call with response_schema, returning parsed JSON or None.

    This is the half of the API grounding cannot reach. dossier.py's ledger and
    write passes use it, so they get validated structure instead of a delimited
    text format that has to be re-parsed and re-validated by hand."""
    text, _ = _generate(
        prompt, system, label, search=False, schema=schema, max_output_tokens=max_output_tokens
    )
    if not text.strip():
        dbg(f"ground: {label} -> empty response")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        dbg(f"ground: {label} -> malformed JSON ({exc!r})")
        tracer.event("ground", label=label, verdict="malformed_json", chars=len(text))
        return None
