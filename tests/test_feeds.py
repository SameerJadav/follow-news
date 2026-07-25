from datetime import datetime, timedelta, timezone

import feedparser

from feeds import Article, article_window_start, canonical_url, gather, load_feeds


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
        return [a for a in same_story if a.outlet == outlet]

    monkeypatch.setattr("feeds.fetch_feed", fake_fetch_feed)
    result = gather(feeds_path, since=now - timedelta(hours=1))
    assert len(result) == 1
