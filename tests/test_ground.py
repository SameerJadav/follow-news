"""Tests for the fragile edges in ground.py: byte-to-character offset
conversion (Gemini's Segment offsets are bytes, not characters — silently
wrong on any non-ASCII text) and splitting a batched grounded response into
per-story blocks. Nothing here makes a network call; requests.head and
ground._generate are always monkeypatched out.
"""

from __future__ import annotations

from types import SimpleNamespace

import ground
from ground import _byte_to_char, _char_offsets


def _segment(start, end, text=""):
    return SimpleNamespace(start_index=start, end_index=end, text=text)


def _support(start, end, chunk_indices):
    return SimpleNamespace(segment=_segment(start, end), grounding_chunk_indices=chunk_indices)


def _chunk(uri, title=""):
    return SimpleNamespace(web=SimpleNamespace(uri=uri, title=title))


def _metadata(chunks, supports, queries=None, entry_point_html=""):
    return SimpleNamespace(
        grounding_chunks=chunks,
        grounding_supports=supports,
        web_search_queries=queries or [],
        search_entry_point=SimpleNamespace(rendered_content=entry_point_html) if entry_point_html else None,
    )


# ---------- byte <-> character offsets ----------


def test_byte_offsets_survive_non_ascii():
    # "Türkiye" and "₹" are exactly where a naive character-offset slice
    # would go wrong: 'ü' is 2 bytes, '₹' is 3 bytes in UTF-8.
    text = "Türkiye pledged ₹500 crore in aid to the region today."
    target = "₹500 crore"
    start_char = text.index(target)
    end_char = start_char + len(target)

    prefix = _char_offsets(text)
    start_byte = len(text[:start_char].encode("utf-8"))
    end_byte = len(text[:end_char].encode("utf-8"))
    # Byte offsets diverge from character offsets once the multi-byte
    # characters are involved.
    assert start_byte != start_char or end_byte != end_char

    assert text[_byte_to_char(prefix, start_byte) : _byte_to_char(prefix, end_byte)] == target


def test_markers_from_metadata_converts_byte_offsets(monkeypatch):
    monkeypatch.setattr(ground, "_resolve", lambda uri, cache: uri)

    text = "Türkiye closed its border. ₹500 crore was pledged in aid."
    target = "₹500 crore was pledged in aid."
    start_char = text.index(target)
    end_char = start_char + len(target)
    start_byte = len(text[:start_char].encode("utf-8"))
    end_byte = len(text[:end_char].encode("utf-8"))

    metadata = _metadata(
        chunks=[_chunk("https://example.com/a", "example.com")],
        supports=[_support(start_byte, end_byte, [0])],
    )
    markers = ground._markers_from_metadata(text, metadata, {})
    assert len(markers) == 1
    m = markers[0]
    assert text[m["start"] : m["end"]] == target
    assert m["outlet"] == "example.com"
    assert m["url"] == "https://example.com/a"


def test_start_index_none_means_zero(monkeypatch):
    monkeypatch.setattr(ground, "_resolve", lambda uri, cache: uri)

    text = "The story opens here. Then it continues."
    end_byte = len("The story opens here.".encode("utf-8"))
    metadata = _metadata(
        chunks=[_chunk("https://example.com/a", "example.com")],
        supports=[_support(None, end_byte, [0])],
    )
    markers = ground._markers_from_metadata(text, metadata, {})
    assert len(markers) == 1
    assert markers[0]["start"] == 0


# ---------- batched multi-story splitting ----------


def test_split_blocks_rebases_offsets_and_drops_boundary_straddle(monkeypatch):
    text = (
        "=== FOLLOW 1 ===\n"
        "STATUS: development\n"
        "Story A had a major development today.\n\n"
        "=== FOLLOW 2 ===\n"
        "STATUS: development\n"
        "Story B also moved forward today.\n"
    )
    sentence_a = "Story A had a major development today."
    sentence_b = "Story B also moved forward today."
    boundary_text = "today.\n\n=== FOLLOW 2"  # spans across the block boundary

    def byte_span(needle):
        start = text.index(needle)
        end = start + len(needle)
        return len(text[:start].encode()), len(text[:end].encode())

    a_start, a_end = byte_span(sentence_a)
    b_start, b_end = byte_span(sentence_b)
    x_start, x_end = byte_span(boundary_text)

    metadata = _metadata(
        chunks=[_chunk("https://a.example/1", "a.example"), _chunk("https://b.example/2", "b.example")],
        supports=[
            _support(a_start, a_end, [0]),
            _support(b_start, b_end, [1]),
            _support(x_start, x_end, [0]),  # straddles the block boundary; must be dropped entirely
        ],
        entry_point_html="<div>chips</div>",
    )

    monkeypatch.setattr(ground, "_resolve", lambda uri, cache: uri)
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: (text, metadata))

    result = ground.research_blocks("prompt", "system", "label", ["1", "2"], require_status=True)

    status_a, block_a = result["1"]
    status_b, block_b = result["2"]
    assert status_a == "development"
    assert status_b == "development"
    assert block_a is not None
    assert block_b is not None

    assert sentence_a in block_a.body
    assert "FOLLOW 1" not in block_a.body and "STATUS" not in block_a.body
    assert block_a.body[block_a.markers[0]["start"] : block_a.markers[0]["end"]] == sentence_a

    assert sentence_b in block_b.body
    assert block_b.body[block_b.markers[0]["start"] : block_b.markers[0]["end"]] == sentence_b

    # The boundary-straddling support anchored nothing in either block.
    assert len(block_a.markers) == 1
    assert len(block_b.markers) == 1

    assert block_a.search_suggestions == "<div>chips</div>"


def test_unknown_status_becomes_quiet(monkeypatch):
    text = "=== FOLLOW 1 ===\nSTATUS: banana\nSome prose that should never surface.\n"
    metadata = _metadata(chunks=[], supports=[])
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: (text, metadata))

    result = ground.research_blocks("prompt", "system", "label", ["1"], require_status=True)
    assert result["1"] == ("quiet", None)


def test_missing_key_maps_to_quiet(monkeypatch):
    text = "=== FOLLOW 1 ===\nSTATUS: development\nSomething happened.\n"
    metadata = _metadata(chunks=[_chunk("https://a.example/1", "a.example")], supports=[])
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: (text, metadata))

    result = ground.research_blocks("prompt", "system", "label", ["1", "2"], require_status=True)
    assert result["2"] == ("quiet", None)


def test_quiet_status_never_produces_a_block(monkeypatch):
    text = "=== FOLLOW 1 ===\nSTATUS: quiet\n"
    metadata = _metadata(chunks=[], supports=[])
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: (text, metadata))

    result = ground.research_blocks("prompt", "system", "label", ["1"], require_status=True)
    assert result["1"] == ("quiet", None)


def test_blocks_without_status_are_kept_when_status_is_not_required(monkeypatch):
    """dossier.py's Pass C batches several questions into one grounded call and
    only ever wants findings back — there is no quiet/development/final axis to
    report, so a bare header followed by prose is the whole contract."""
    text = "=== BLOCK q1 ===\nThe hunger strike began on 14 June.\n\n=== BLOCK q2 ===\nPolice used pellet guns.\n"
    metadata = _metadata(chunks=[], supports=[])
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: (text, metadata))

    result = ground.research_blocks("prompt", "system", "label", ["q1", "q2"])

    assert result["q1"][0] == "ok"
    assert "hunger strike" in result["q1"][1].body
    assert "pellet guns" in result["q2"][1].body
    assert "BLOCK" not in result["q1"][1].body


def test_legacy_follow_header_spelling_still_parses(monkeypatch):
    text = "=== FOLLOW q1 ===\nStill parses.\n"
    metadata = _metadata(chunks=[], supports=[])
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: (text, metadata))

    result = ground.research_blocks("prompt", "system", "label", ["q1"])
    assert result["q1"][1] is not None


def test_search_and_schema_together_is_rejected_before_the_api_sees_it():
    """Verified live 2026-07-25: response_mime_type=json alongside google_search
    is a 400. Catching it here makes it a caller bug with a stack trace rather
    than a wasted call and an opaque server error."""
    import pytest

    with pytest.raises(ValueError, match="cannot be combined"):
        ground._generate("p", "s", "l", search=True, schema={"type": "object"})


def test_structured_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: ("not json{", None))
    assert ground.structured("p", "s", "l", {"type": "object"}) is None


def test_structured_parses_a_valid_payload(monkeypatch):
    monkeypatch.setattr(ground, "_generate", lambda prompt, system, label, **kw: ('{"entries":[1,2]}', None))
    assert ground.structured("p", "s", "l", {"type": "object"}) == {"entries": [1, 2]}


def test_url_statuses_reports_every_outcome():
    """dossier.md §13: a page the model could not read is a cap, and a cap is
    never silent. PAYWALL and ERROR must survive into the record."""
    cand = SimpleNamespace(
        url_context_metadata=SimpleNamespace(
            url_metadata=[
                SimpleNamespace(retrieved_url="https://a.example/1", url_retrieval_status="URL_RETRIEVAL_STATUS_SUCCESS"),
                SimpleNamespace(retrieved_url="https://b.example/2", url_retrieval_status="URL_RETRIEVAL_STATUS_PAYWALL"),
            ]
        )
    )
    assert ground._url_statuses(cand) == {
        "https://a.example/1": "URL_RETRIEVAL_STATUS_SUCCESS",
        "https://b.example/2": "URL_RETRIEVAL_STATUS_PAYWALL",
    }
    assert ground._url_statuses(None) == {}


# ---------- redirect resolution ----------


def test_resolve_falls_back_to_redirect_uri_on_failure(monkeypatch):
    import requests

    def raise_head(*args, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(ground.requests, "head", raise_head)
    cache: dict[str, str] = {}
    uri = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
    assert ground._resolve(uri, cache) == uri
    assert cache[uri] == uri


def test_resolve_caches(monkeypatch):
    calls = []

    def fake_head(url, allow_redirects=True, timeout=10, headers=None):
        calls.append(url)
        return SimpleNamespace(url="https://real-outlet.example/story")

    monkeypatch.setattr(ground.requests, "head", fake_head)
    cache: dict[str, str] = {}
    uri = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/xyz"
    first = ground._resolve(uri, cache)
    second = ground._resolve(uri, cache)
    assert first == second == "https://real-outlet.example/story"
    assert len(calls) == 1


def test_outlet_falls_back_to_domain_when_title_empty():
    assert ground._outlet("", "https://www.cfr.org/timeline/x") == "cfr.org"
    assert ground._outlet("cfr.org", "https://www.cfr.org/timeline/x") == "cfr.org"
