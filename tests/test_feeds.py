from datetime import datetime, timedelta, timezone

import feedparser

from feeds import (
    Article,
    FeedHealth,
    GatherResult,
    article_window_start,
    canonical_url,
    fetch_feed,
    gather,
    health_payload,
    load_feeds,
    quorum_ok,
)


def test_load_feeds_parses_multiword_names(tmp_path):
    path = tmp_path / "feeds.txt"
    path.write_text(
        "# a comment\n"
        "\n"
        "BBC World          https://feeds.bbci.co.uk/news/world/rss.xml\n"
        "Channel News Asia  https://www.channelnewsasia.com/rss\n"
    )
    feeds = load_feeds(path)
    assert feeds == [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Channel News Asia", "https://www.channelnewsasia.com/rss"),
    ]


def test_load_feeds_skips_malformed_lines(tmp_path):
    path = tmp_path / "feeds.txt"
    path.write_text("just-a-name-no-url\nBBC World https://feeds.bbci.co.uk/news/world/rss.xml\n")
    feeds = load_feeds(path)
    assert feeds == [("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml")]


def test_canonical_url_strips_tracking_params():
    assert (
        canonical_url("https://bbc.co.uk/news/articles/abc?at_medium=RSS&at_campaign=rss")
        == "https://bbc.co.uk/news/articles/abc"
    )
    assert canonical_url("https://ndtv.com/india-news/foo#publisher=newsstand") == "https://ndtv.com/india-news/foo"


def test_canonical_url_preserves_meaningful_query():
    assert canonical_url("https://example.com/a?id=123") == "https://example.com/a?id=123"


def test_article_window_start_no_previous_digest():
    now = datetime(2026, 7, 25, 20, 30, tzinfo=timezone.utc)
    start = article_window_start(now, None)
    assert start == now - timedelta(hours=24)


def test_article_window_start_clamps_to_floor():
    now = datetime(2026, 7, 25, 20, 30, tzinfo=timezone.utc)
    prev = now - timedelta(hours=3)  # a manual rerun soon after the last run
    start = article_window_start(now, prev)
    assert start == now - timedelta(hours=12)


def test_article_window_start_clamps_to_cap():
    now = datetime(2026, 7, 25, 20, 30, tzinfo=timezone.utc)
    prev = now - timedelta(days=5)  # a multi-day gap
    start = article_window_start(now, prev)
    assert start == now - timedelta(hours=48)


def test_feed_with_zero_items_returns_no_entries():
    """The trap The Wire and The Print already set: HTTP 200, well-formed
    RSS, but no <item> elements. feedparser must not raise, and the feed
    must yield zero usable articles rather than erroring the whole run."""
    empty_rss = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Empty Feed</title></channel></rss>"""
    parsed = feedparser.parse(empty_rss)
    assert parsed.bozo == 0
    assert parsed.entries == []


def test_gather_dedupes_articles_differing_only_by_tracking_params(monkeypatch, tmp_path):
    feeds_path = tmp_path / "feeds.txt"
    feeds_path.write_text("Outlet A https://a.example.com/rss\nOutlet B https://b.example.com/rss\n")

    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    same_story = [
        Article(
            outlet="Outlet A",
            title="Big Story Happens",
            url="https://news.example.com/big-story?at_medium=RSS",
            summary="s",
            published=now,
        ),
        Article(
            outlet="Outlet B",
            title="Big story happens",
            url="https://news.example.com/big-story?traffic_source=rss",
            summary="s",
            published=now,
        ),
    ]

    def fake_fetch_feed(outlet, url):
        articles = [a for a in same_story if a.outlet == outlet]
        health = FeedHealth(outlet, url, 200, len(articles), len(articles), "")
        return articles, health

    monkeypatch.setattr("feeds.fetch_feed", fake_fetch_feed)
    result = gather(feeds_path, since=now - timedelta(hours=1))
    assert len(result.articles) == 1


def test_zero_items_at_http_200_is_unhealthy(monkeypatch):
    """The trap this whole mechanism exists for: The Wire and The Print
    return HTTP 200 with a well-formed but empty feed. A status code alone
    would call this feed healthy; fetch_feed must not."""
    empty_rss = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'

    class FakeResp:
        status_code = 200
        content = empty_rss

    monkeypatch.setattr("feeds.requests.get", lambda *_a, **_k: FakeResp())
    articles, health = fetch_feed("The Wire", "https://thewire.in/rss")
    assert articles == []
    assert health.http == 200
    assert health.usable == 0
    assert "zero items" in health.error


def _dummy_articles(n: int) -> list[Article]:
    return [Article(outlet="X", title=f"t{i}", url=f"https://x/{i}", summary="", published=None) for i in range(n)]


def test_quorum_fails_below_min_live_feeds():
    result = GatherResult(
        articles=_dummy_articles(30),  # plenty of articles, but from too few feeds
        health=[FeedHealth("A", "u", 200, 5, 5, "")],
        configured=14,
        live=1,
        degraded=True,
    )
    assert not quorum_ok(result)


def test_quorum_ok_at_half_the_feeds():
    """Half the feeds dead is exactly the "degrade, don't fail" scenario:
    still enough for a real digest, and still flagged as degraded so the
    health report can act on it."""
    result = GatherResult(
        articles=_dummy_articles(30),
        health=[FeedHealth(f"Feed{i}", "u", 200, 3, 3, "") for i in range(7)]
        + [FeedHealth(f"Dead{i}", "u", 200, 0, 0, "zero items (HTTP 200)") for i in range(7)],
        configured=14,
        live=7,
        degraded=True,
    )
    assert quorum_ok(result)
    assert result.degraded


def test_health_payload_is_plain_json_types():
    result = GatherResult(
        articles=[],
        health=[FeedHealth("A", "https://a", 200, 3, 3, "")],
        configured=1,
        live=1,
        degraded=False,
    )
    payload = health_payload(result)
    assert payload == {
        "configured": 1,
        "live": 1,
        "degraded": False,
        "articles": 0,
        "feeds": [{"outlet": "A", "http": 200, "raw_items": 3, "usable": 3, "error": ""}],
    }
