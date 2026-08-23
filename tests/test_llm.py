"""llm.py's own logic, with no network and no fixtures: the write prompt's
Subject line, and the per-section claims split. Both are 2026-08-23 fixes with
a measured failure behind them, so both get a regression guard."""

from __future__ import annotations

import llm
from feeds import Article
from rank import RankedCluster


def _cluster(section: str, hint: str, outlet: str = "BBC World") -> RankedCluster:
    return RankedCluster(
        headline_hint=hint,
        section=section,
        category="politics",
        tier="major",
        articles=[Article(outlet=outlet, title=hint, url=f"https://e.com/{outlet}/{hint[:8]}",
                          summary="s" * 40, published=None)],
        distinct_outlets=1,
        wiki_backed=False,
        weight=6,
    )


def test_subject_line_masks_every_figure():
    """H1(a): headline_hint comes from the select pass, which reads RSS titles
    and never claims — so a figure in it has no claim behind it. data/2026-07-30
    published "leaves 18 dead" in a headline whose only source was this line."""
    assert llm.subject_line(
        "Powerful earthquake in Japan kills at least 18 and triggers massive search"
    ) == "Powerful earthquake in Japan kills at least … and triggers massive search"
    assert llm.subject_line("PM CARES Fund audit report shows ₹8,452 crore corpus") == (
        "PM CARES Fund audit report shows ₹… crore corpus"
    )
    # A hint with no figures is untouched — the line still says which story it is.
    assert llm.subject_line("Ebola outbreak in DR Congo spreads") == "Ebola outbreak in DR Congo spreads"


def test_write_prompt_carries_no_hint_figure(monkeypatch):
    seen = {}

    def fake_generate(prompt, schema, system, label):
        seen[label] = prompt
        return {"stories": []}

    monkeypatch.setattr(llm, "_generate", fake_generate)
    clusters = [_cluster("world", "Quake kills at least 18 people")]
    claims = {0: [llm.Claim(id=1, cluster_id=0, text="A quake struck Kyushu on Saturday morning.",
                            kind="event", outlet="BBC World", url="https://e.com/1",
                            source_kind="fulltext")]}
    llm.write_stories(clusters, claims)
    assert "18" not in seen["write"]
    assert "Subject: Quake kills at least … people" in seen["write"]


def test_claims_runs_one_call_per_section(monkeypatch):
    """H3: the batched pass decays with prompt position and India was always
    last. Each section now gets its own call, its own position 0, and its own
    prompt — which must carry ONLY that section's stories."""
    prompts: dict[str, str] = {}

    def fake_generate(prompt, schema, system, label):
        prompts[label] = prompt
        return {"stories": []}

    monkeypatch.setattr(llm, "_generate", fake_generate)
    clusters = [
        _cluster("world", "World story one"),
        _cluster("world", "World story two"),
        _cluster("india", "India story one"),
    ]
    llm.extract_claims(clusters, {})

    assert sorted(prompts) == ["claims-india", "claims-world"]
    assert "World story one" in prompts["claims-world"]
    assert "India story one" not in prompts["claims-world"]
    assert "World story one" not in prompts["claims-india"]
    # Global cluster ids, so a returned cluster_id needs no remapping.
    assert "=== STORY 2 [india]" in prompts["claims-india"]


def test_claims_ids_stay_unique_and_sectioned(monkeypatch):
    """Claim ids must not restart per call (a marker cites [cN] across the whole
    day), and a section's response naming another section's story is rejected
    rather than misfiled."""
    responses = {
        "claims-world": {"stories": [
            {"cluster_id": 0, "claims": [
                {"article_id": 0, "kind": "event", "text": "A world thing happened today in full."},
                {"article_id": 0, "kind": "event", "text": "Another world thing happened today too."},
            ]},
            # Not this call's story: india's cluster 1, returned by the world call.
            {"cluster_id": 1, "claims": [
                {"article_id": 0, "kind": "event", "text": "Misfiled claim that must be rejected."},
            ]},
        ]},
        "claims-india": {"stories": [
            {"cluster_id": 1, "claims": [
                {"article_id": 0, "kind": "event", "text": "An india thing happened today in full."},
            ]},
        ]},
    }
    monkeypatch.setattr(llm, "_generate", lambda prompt, schema, system, label: responses[label])
    clusters = [_cluster("world", "World story"), _cluster("india", "India story")]

    out = llm.extract_claims(clusters, {})
    assert sorted(out) == [0, 1]
    assert [c.id for c in out[0]] == [1, 2]
    assert [c.id for c in out[1]] == [3]  # continues, never restarts
    assert out[1][0].text.startswith("An india thing")
