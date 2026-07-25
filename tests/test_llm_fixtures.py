"""Schema-validation tests against saved fixtures — real Gemini responses
from a real run, committed to tests/fixtures/. This is the test that proves
Phase 3 actually works: real model output, run through the real validators,
with no network call.

If tests/fixtures/*.json is missing (e.g. a fresh clone before anyone has
generated a real digest), every test here is skipped rather than failed —
generating fixtures requires GEMINI_API_KEY and a real run
(DIGEST_DUMP_DIR=tests/fixtures uv run digest.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

import anchor
import llm
from feeds import Article
from rank import RankedCluster

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "write.json").exists(),
    reason="tests/fixtures/*.json not present; run DIGEST_DUMP_DIR=tests/fixtures uv run digest.py first",
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_select_response_matches_schema():
    jsonschema.validate(_load("select"), llm._SELECT_SCHEMA)


def test_claims_response_matches_schema():
    jsonschema.validate(_load("claims"), llm._CLAIMS_SCHEMA)


def test_write_response_matches_schema():
    jsonschema.validate(_load("write"), llm._WRITE_SCHEMA)


def _rebuild_clusters() -> list[RankedCluster]:
    clusters_data = _load("clusters")
    clusters = []
    for c in clusters_data:
        articles = [
            Article(outlet=a["outlet"], title="", url=a["url"], summary="", published=None)
            for a in c["articles"]
        ]
        clusters.append(
            RankedCluster(
                headline_hint=c["headline_hint"],
                section=c["section"],
                category=c["category"],
                tier=c["tier"],
                articles=articles,
                distinct_outlets=c["distinct_outlets"],
                wiki_backed=c["wiki_backed"],
                weight=c["weight"],
            )
        )
    return clusters


def _rebuild_claims_by_cluster() -> dict[int, list[anchor.Claim]]:
    assigned = _load("claims_assigned")
    return {int(cid): [anchor.Claim(**c) for c in cs] for cid, cs in assigned.items()}


def test_real_response_survives_validation():
    """Rebuild the exact clusters/claims that produced the saved write.json,
    monkeypatch nothing (no network involved at all — we call the same
    merge logic write_stories uses, directly against the fixture), and
    assert every trust property the phase promises actually holds."""
    clusters = _rebuild_clusters()
    claims_by_cluster = _rebuild_claims_by_cluster()
    write_result = _load("write")

    by_cluster_id: dict[int, dict] = {}
    for raw in write_result.get("stories", []):
        cid = raw.get("cluster_id")
        if not isinstance(cid, int) or cid not in claims_by_cluster or cid in by_cluster_id:
            continue
        story = anchor.build_story(clusters[cid], cid, raw, claims_by_cluster[cid])
        if story is not None:
            by_cluster_id[cid] = story
    stories = [by_cluster_id[cid] for cid in sorted(by_cluster_id)]

    assert len(stories) >= 1

    for story in stories:
        assert "[c" not in story["body"]

        assert len(story["markers"]) >= anchor.MIN_MARKERS
        assert story["signals"]["unanchored_share"] <= anchor.MAX_UNANCHORED_SHARE

        claim_ids = {c["id"] for c in story["claims"]}
        for m in story["markers"]:
            assert m["claim_id"] in claim_ids
            assert story["body"][m["start"] : m["end"]].strip() == story["body"][m["start"] : m["end"]]
            assert story["body"][m["start"] : m["end"]] != ""

        for claim in story["claims"]:
            assert claim["url"]
            assert claim["outlet"]

        assert isinstance(story["thin_sourced"], bool)


def test_claims_only_reference_their_own_articles():
    """Every claim's outlet/url must trace back to an article actually in
    its cluster — the never-invent-a-source guarantee, checked directly
    against the real assigned-claims fixture."""
    clusters = _rebuild_clusters()
    claims_by_cluster = _rebuild_claims_by_cluster()
    for cid, claims in claims_by_cluster.items():
        cluster_urls = {a.url for a in clusters[cid].articles}
        for c in claims:
            assert c.url in cluster_urls
            assert c.cluster_id == cid
