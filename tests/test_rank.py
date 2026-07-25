from feeds import Article
from rank import (
    MAX_ARTICLES_PER_OUTLET,
    MIN_STORIES_IF_ANY,
    WEIGHT_FLOOR,
    SelectedCluster,
    build_select_pool,
    rank_clusters,
)
from wikipedia import WikiEvent


def _article(outlet: str, idx: int, title: str = "") -> Article:
    return Article(
        outlet=outlet,
        title=title or f"{outlet} headline {idx}",
        url=f"https://example.com/{outlet}/{idx}",
        summary="summary text",
        published=None,
    )


def test_build_select_pool_caps_per_outlet():
    articles = [_article("HT", i) for i in range(60)] + [_article("BBC", i) for i in range(5)]
    pool = build_select_pool(articles)
    counts: dict[str, int] = {}
    for a in pool:
        counts[a.outlet] = counts.get(a.outlet, 0) + 1
    assert counts["HT"] == MAX_ARTICLES_PER_OUTLET
    assert counts["BBC"] == 5


def test_out_of_scope_category_is_dropped():
    pool = [_article(f"O{i}", i) for i in range(6)]
    selected = [
        SelectedCluster(
            headline_hint="Awards show dazzles fans",
            section="world",
            category="entertainment",
            tier="lead",
            article_ids=list(range(6)),
        )
    ]
    assert rank_clusters(selected, pool, []) == []


def test_weight_arithmetic_and_floor():
    events = [WikiEvent(category="Test", topic="", text="Iran Bahrain military tension continues")]
    pool = [_article(f"O{i}", i, title="Iran Bahrain crisis story") for i in range(5)]
    pool += [_article(f"P{i}", i, title="Local council votes on budget") for i in range(2)]

    selected = [
        SelectedCluster(
            headline_hint="Iran Bahrain crisis deepens",
            section="world",
            category="conflict",
            tier="major",
            article_ids=[0, 1, 2, 3, 4],
        ),
        SelectedCluster(
            headline_hint="Local council votes on budget",
            section="world",
            category="politics",
            tier="notable",
            article_ids=[5, 6],
        ),
    ]

    ranked = rank_clusters(selected, pool, events)
    headlines = {c.headline_hint: c for c in ranked}

    assert "Iran Bahrain crisis deepens" in headlines
    kept = headlines["Iran Bahrain crisis deepens"]
    assert kept.distinct_outlets == 5
    assert kept.wiki_backed is True
    assert kept.weight == 5 + 2 + 2  # outlets + major + wiki bonus

    assert "Local council votes on budget" not in headlines  # weight 2, below floor


def test_wiki_backed_single_outlet_lead_survives():
    events = [WikiEvent(category="Test", topic="", text="Iran Bahrain military tension continues")]
    pool = [_article("SoloOutlet", 0, title="Iran Bahrain scoop breaks first")]
    selected = [
        SelectedCluster(
            headline_hint="Iran Bahrain scoop breaks first",
            section="world",
            category="conflict",
            tier="lead",
            article_ids=[0],
        )
    ]
    ranked = rank_clusters(selected, pool, events)
    assert len(ranked) == 1
    assert ranked[0].distinct_outlets == 1
    assert ranked[0].wiki_backed is True
    assert ranked[0].weight == 1 + 4 + 2  # outlets + lead + wiki bonus
    assert ranked[0].weight >= WEIGHT_FLOOR


def test_article_id_claimed_by_first_cluster_only():
    pool = [_article("O0", 0), _article("O1", 1), _article("O2", 2)]
    selected = [
        SelectedCluster(
            headline_hint="A",
            section="world",
            category="politics",
            tier="major",
            article_ids=[0, 1],
        ),
        SelectedCluster(
            headline_hint="B",
            section="world",
            category="politics",
            tier="major",
            article_ids=[0, 2],  # id 0 already claimed by "A"
        ),
    ]
    ranked = rank_clusters(selected, pool, [])
    by_headline = {c.headline_hint: c for c in ranked}
    assert {a.outlet for a in by_headline["A"].articles} == {"O0", "O1"}
    assert {a.outlet for a in by_headline["B"].articles} == {"O2"}
    assert by_headline["B"].distinct_outlets == 1


def test_invalid_ids_rejected():
    pool = [_article("O0", 0), _article("O1", 1)]
    selected = [
        SelectedCluster(
            headline_hint="A",
            section="world",
            category="politics",
            tier="major",
            article_ids=[-1, 100, "abc", 0, 1],  # type: ignore[list-item]
        )
    ]
    ranked = rank_clusters(selected, pool, [])
    assert len(ranked) == 1
    assert ranked[0].distinct_outlets == 2


def test_floor_relaxation_keeps_top_three():
    # 4 clusters, all scored below WEIGHT_FLOOR (notable tier, no wiki),
    # with distinct weights 4, 3, 2, 1 (== their distinct-outlet counts).
    # None clear the floor, so the MIN_STORIES_IF_ANY rail should keep only
    # the top 3 by weight.
    pool = []
    selected = []
    next_id = 0
    for weight, name in [(4, "d4"), (3, "d3"), (2, "d2"), (1, "d1")]:
        ids = []
        for _ in range(weight):
            pool.append(_article(f"{name}-{next_id}", next_id))
            ids.append(next_id)
            next_id += 1
        selected.append(
            SelectedCluster(
                headline_hint=name,
                section="world",
                category="politics",
                tier="notable",
                article_ids=ids,
            )
        )

    ranked = rank_clusters(selected, pool, [])
    assert len(ranked) == MIN_STORIES_IF_ANY
    assert [c.headline_hint for c in ranked] == ["d4", "d3", "d2"]
    assert all(c.weight < WEIGHT_FLOOR for c in ranked)


def test_second_lead_in_a_section_is_demoted():
    pool = [_article(f"B{i}", i) for i in range(5)]  # BIG: 5 outlets
    pool += [_article(f"S{i}", i) for i in range(3)]  # SMALL: 3 outlets
    selected = [
        SelectedCluster(
            headline_hint="BIG",
            section="world",
            category="politics",
            tier="lead",
            article_ids=[0, 1, 2, 3, 4],
        ),
        SelectedCluster(
            headline_hint="SMALL",
            section="world",
            category="politics",
            tier="lead",
            article_ids=[5, 6, 7],
        ),
    ]
    ranked = rank_clusters(selected, pool, [])
    by_headline = {c.headline_hint: c for c in ranked}
    assert by_headline["BIG"].tier == "lead"
    assert by_headline["SMALL"].tier == "major"
    assert by_headline["SMALL"].weight == 3 + 2  # demoted to major, no wiki bonus


def test_order_is_world_block_then_india_block_by_weight():
    pool = []
    selected = []
    next_id = 0
    for section, outlets, name in [
        ("world", 6, "world-low"),
        ("world", 8, "world-high"),
        ("india", 10, "india-high"),
        ("india", 5, "india-floor"),
    ]:
        ids = []
        for _ in range(outlets):
            pool.append(_article(f"{name}-{next_id}", next_id))
            ids.append(next_id)
            next_id += 1
        selected.append(
            SelectedCluster(
                headline_hint=name,
                section=section,
                category="politics",
                tier="notable",  # weight == distinct_outlets exactly
                article_ids=ids,
            )
        )

    ranked = rank_clusters(selected, pool, [])
    assert [c.headline_hint for c in ranked] == ["world-high", "world-low", "india-high", "india-floor"]
