from dataclasses import replace

from anchor import (
    Claim,
    Marker,
    build_story,
    clean_vocab,
    is_thin_sourced,
    parse_body,
    unanchored_share,
    unsourced_figures,
)
from feeds import Article
from rank import RankedCluster


def _article(outlet: str, idx: int = 0) -> Article:
    return Article(
        outlet=outlet,
        title=f"{outlet} headline {idx}",
        url=f"https://example.com/{outlet}/{idx}",
        summary="summary text",
        published=None,
    )


def _claim(id_: int, cluster_id: int = 0, outlet: str = "BBC World", text: str = "Something happened.") -> Claim:
    return Claim(
        id=id_,
        cluster_id=cluster_id,
        text=text,
        kind="event",
        outlet=outlet,
        url=f"https://example.com/{outlet}/{id_}",
        source_kind="fulltext",
    )


_DEFAULT_CLUSTER = RankedCluster(
    headline_hint="Test story",
    section="world",
    category="politics",
    tier="major",
    articles=[_article("BBC World"), _article("Guardian World")],
    distinct_outlets=2,
    wiki_backed=False,
    weight=6,
)


def _cluster(**overrides) -> RankedCluster:
    return replace(_DEFAULT_CLUSTER, **overrides)


def test_markers_stripped_and_spans_exact():
    raw = "Houthi forces blockaded the Red Sea [c1]. Oil passed $100 a barrel [c2]."
    claims = [_claim(1), _claim(2, outlet="Livemint")]
    body, markers, dropped = parse_body(raw, claims)

    assert dropped == 0
    assert "[c" not in body
    assert " ." not in body
    assert len(markers) == 2
    assert body[markers[0].start : markers[0].end] == "Houthi forces blockaded the Red Sea"
    assert body[markers[1].start : markers[1].end] == "Oil passed $100 a barrel"


def test_span_does_not_cross_paragraph_break():
    raw = "First paragraph statement [c1].\n\nSecond paragraph statement [c2]."
    claims = [_claim(1), _claim(2)]
    body, markers, _ = parse_body(raw, claims)

    para_break = body.index("\n\n")
    assert markers[0].end <= para_break
    assert markers[1].start > para_break
    assert body[markers[1].start : markers[1].end] == "Second paragraph statement"


def test_unknown_claim_id_marker_is_dropped():
    raw = "A real claim here [c1]. An unknown one here [c999]."
    claims = [_claim(1)]
    body, markers, dropped = parse_body(raw, claims)

    assert dropped == 1
    assert "[c999]" not in body
    assert "An unknown one here" in body
    assert len(markers) == 1


def test_cross_story_claim_id_is_dropped():
    # Claim 2 belongs to a different story's claim list; parse_body is
    # given only story 0's claims, so a marker citing it is unresolved.
    raw = "This story's fact [c1]. Another story's fact [c2]."
    claims = [_claim(1, cluster_id=0)]  # claim 2 deliberately absent
    _, markers, dropped = parse_body(raw, claims)

    assert dropped == 1
    assert len(markers) == 1
    assert markers[0].claim_id == 1


def test_unanchored_story_is_dropped_but_well_anchored_one_survives():
    cluster = _cluster()
    claims = [_claim(1), _claim(2, outlet="Guardian World")]

    # >= MIN_BODY_WORDS and >= MIN_MARKERS, but mostly unattributed filler —
    # dropped for exceeding MAX_UNANCHORED_SHARE, not for being too short.
    sparse = (
        "Lots of unattributed filler text goes here with no anchoring at all "
        "and it just keeps going on and on without ever citing anything. " * 8
        + "One fact is anchored here [c1]. Another fact is anchored here too [c2]."
    )
    assert build_story(cluster, 0, {"headline": "H", "body": sparse, "vocab": []}, claims) is None

    anchored = "First fact stated plainly [c1]. Second fact stated plainly [c2]. " * 12
    story = build_story(cluster, 0, {"headline": "H", "body": anchored, "vocab": []}, claims)
    assert story is not None
    assert story["section"] == "world"
    assert story["headline"] == "H"


def test_too_few_markers_dropped():
    cluster = _cluster()
    claims = [_claim(1)]
    # >= MIN_BODY_WORDS via unmarked filler, but only ONE marker total.
    body = "Only one anchored fact here [c1]. " + (
        "Filler unattributed sentence padding the word count here today. " * 15
    )
    assert build_story(cluster, 0, {"headline": "H", "body": body, "vocab": []}, claims) is None


def test_short_body_dropped():
    cluster = _cluster()
    claims = [_claim(1), _claim(2, outlet="Guardian World")]
    body = "Short fact one [c1]. Short fact two [c2]."
    assert build_story(cluster, 0, {"headline": "H", "body": body, "vocab": []}, claims) is None


def test_thin_sourced_single_outlet():
    markers = [Marker(0, 5, 1, "BBC World", "u1"), Marker(6, 10, 2, "BBC World", "u1")]
    thin, outlets = is_thin_sourced(markers)
    assert thin is True
    assert outlets == 1


def test_thin_sourced_dominant_outlet():
    markers = [Marker(i, i + 1, i, "BBC World", "u1") for i in range(9)]
    markers.append(Marker(100, 101, 100, "Guardian World", "u2"))
    thin, outlets = is_thin_sourced(markers)
    assert thin is True  # 9/10 = 90% > 80% threshold
    assert outlets == 2


def test_not_thin_when_balanced():
    markers = [Marker(i, i + 1, i, "BBC World", "u1") for i in range(6)]
    markers += [Marker(100 + i, 101 + i, 100 + i, "Guardian World", "u2") for i in range(4)]
    thin, outlets = is_thin_sourced(markers)
    assert thin is False
    assert outlets == 2


def test_vocab_term_not_in_body_is_dropped():
    body = "The blockade caused a quagmire for shipping."
    vocab = [
        {"term": "quagmire", "say": "KWAG-my-er", "meaning": "a difficult situation"},
        {"term": "obfuscation", "say": "ob-fuh-SKAY-shun", "meaning": "making something unclear"},
    ]
    cleaned = clean_vocab(vocab, body)
    assert [v["term"] for v in cleaned] == ["quagmire"]


def test_vocab_capped_at_six():
    body = " ".join(f"term{i}" for i in range(10))
    vocab = [{"term": f"term{i}", "say": "x", "meaning": "y"} for i in range(10)]
    cleaned = clean_vocab(vocab, body)
    assert len(cleaned) == 6


def test_unsourced_figure_detected():
    body = "Officials confirmed 40 deaths [c1], though the toll may reach 500."
    claims = [_claim(1, text="Officials confirmed 40 deaths in the incident.")]
    figures = unsourced_figures(body, claims)
    assert "500" in figures
    assert "40" not in figures


def test_sources_and_claims_cover_cited_only():
    cluster = _cluster()
    claims = [
        _claim(1, outlet="BBC World"),
        _claim(2, outlet="Guardian World"),
        _claim(3, outlet="NPR World"),  # never cited
    ]
    body = "First fact stated plainly [c1]. Second fact stated plainly [c2]. " * 12
    story = build_story(cluster, 0, {"headline": "H", "body": body, "vocab": []}, claims)

    assert story is not None
    cited_ids = {c["id"] for c in story["claims"]}
    assert cited_ids == {1, 2}
    urls = {s["url"] for s in story["sources"]}
    assert all(m["url"] in urls for m in story["markers"])
    assert claims[2].url not in urls


def test_span_does_not_swallow_unbounded_preceding_filler():
    # A huge unmarked block followed by one marker must NOT have the whole
    # block attributed to that marker's span — only the text immediately
    # supporting it (bounded by MAX_SPAN_CHARS), so unanchored_share still
    # catches the rest as uncovered.
    filler = "unattributed word " * 100  # ~1800 chars, well past MAX_SPAN_CHARS
    raw = filler + "The actual claim statement [c1]."
    body, markers, _ = parse_body(raw, [_claim(1)])

    assert len(markers) == 1
    span_len = markers[0].end - markers[0].start
    assert span_len < len(filler)
    share = unanchored_share(body, markers)
    assert share > 0.5


def test_unanchored_share_ignores_whitespace():
    markers = [Marker(0, 4, 1, "BBC World", "u1")]
    body = "Fact\n\nmore filler text that is not anchored at all here"
    share = unanchored_share(body, markers)
    assert 0.0 < share < 1.0
