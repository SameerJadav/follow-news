"""Feed loading, article ingestion, and the time window that keeps a run from
digesting the same article twice.

RSS gives headlines and snippets only — article bodies are fetched separately
in extract.py, only for the articles the LLM selection pass actually chooses.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

import tracer
from tracer import dbg

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Article window: never shorter than this (so a manual same-day rerun still
# has a real window to look at) and never longer than this (so a multi-day
# gap doesn't flood a single run).
WINDOW_FLOOR_H = 12
WINDOW_CAP_H = 48

SUMMARY_CAP = 400

# Query params that vary per link but don't change what the link points at.
# Stripping these is what makes e.g. BBC's "?at_medium=RSS&at_campaign=rss"
# and NDTV's "#publisher=newsstand" dedupe against the same canonical URL.
TRACKING_PARAMS = {
    "at_medium",
    "at_campaign",
    "traffic_source",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "publisher",
}

# Feed health / quorum thresholds (Phase 6). research.md SS2.3/SS7.1: The Wire
# and The Print return HTTP 200 with zero items, and 6 of 22 feeds tested were
# already dead or blocked on day one -- a status code alone cannot catch
# either failure mode, and a year of no maintenance will only rot more feeds.
# "Degrade, don't fail" (decisions.md) means a bad morning still ships a
# digest as long as there's real news to build one from; below these floors
# there isn't enough left to call it a digest.
MIN_LIVE_FEEDS = 3  # below this it's one or two outlets' opinion, not a digest
MIN_ARTICLES = 20  # after window + dedupe
DEGRADED_LIVE_SHARE = 0.5  # under half the configured feeds live still ships, loudly

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


@dataclass(frozen=True, slots=True)
class Article:
    """The hand-off contract to the selection/write passes and to later
    phases. Do not rename these fields — Phase 2 builds on them directly."""

    outlet: str
    title: str
    url: str
    summary: str
    published: datetime | None  # tz-aware UTC; None when the feed omitted it


@dataclass(frozen=True, slots=True)
class FeedHealth:
    """One feed's condition on one run. `error` is "" for a healthy feed;
    otherwise a short, greppable reason -- including the HTTP-200-zero-items
    trap, which a status code alone can't distinguish from a feed with
    nothing to report today."""

    outlet: str
    url: str
    http: int | None  # None when the request itself failed (DNS, timeout, ...)
    raw_items: int  # entries feedparser saw, before the link/title filter
    usable: int  # entries that became an Article
    error: str


@dataclass(frozen=True, slots=True)
class GatherResult:
    """gather()'s full return: the articles for the pipeline plus enough feed
    health to decide (a) whether this run has quorum to build a digest from,
    and (b) whether report.py's cross-day check should flag a feed as dying."""

    articles: list[Article]
    health: list[FeedHealth]
    configured: int
    live: int  # feeds with usable > 0
    degraded: bool  # live < configured * DEGRADED_LIVE_SHARE


def load_feeds(path: Path) -> list[tuple[str, str]]:
    """Parse `feeds.txt`: one "Name URL" pair per line, `#` for comments.

    Uses rsplit(None, 1) rather than split() because outlet names contain
    spaces ("BBC World", "Channel News Asia") — the URL is always the last
    whitespace-separated token on the line.
    """
    feeds: list[tuple[str, str]] = []
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2 or not parts[1].startswith("http"):
            dbg(f"feeds.txt:{lineno}: skipping malformed line: {raw_line!r}")
            continue
        name, url = parts
        feeds.append((name.strip(), url.strip()))
    return feeds


def canonical_url(url: str) -> str:
    """Strip the fragment and any tracking query params so links that differ
    only by campaign/referrer noise dedupe to the same article."""
    parts = urlsplit(url)
    kept_query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept_query), ""))


def _normalized_title(title: str) -> str:
    """Casefold, strip punctuation, collapse whitespace — used to catch
    duplicate stories across outlets whose URLs differ but titles match."""
    folded = unicodedata.normalize("NFKC", title).casefold()
    no_punct = _PUNCT_RE.sub(" ", folded)
    return _WS_RE.sub(" ", no_punct).strip()


def _clean_summary(raw: str) -> str:
    """Strip HTML tags out of an RSS summary/description and collapse
    whitespace, then truncate to a sane length."""
    text = _TAG_RE.sub(" ", raw or "")
    text = _WS_RE.sub(" ", text).strip()
    return text[:SUMMARY_CAP]


def _entry_published(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t is not None:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(outlet: str, url: str) -> tuple[list[Article], FeedHealth]:
    """Fetch and parse one feed. Never raises — a single dead or malformed
    feed must not stop the whole run. Returns ([], health) on any failure,
    including the HTTP-200-with-zero-items trap (The Wire, The Print) —
    `health.error` is what makes that trap visible instead of silently
    looking like "no news today"."""
    started = time.monotonic()
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            dbg(f"feed {outlet!r}: http={resp.status_code}")
            tracer.event("feeds", outlet=outlet, url=url, http=resp.status_code, elapsed_ms=elapsed_ms, verdict="http_error")
            return [], FeedHealth(outlet, url, resp.status_code, 0, 0, f"http {resp.status_code}")

        # The raw bytes, exactly as parsed. A feed that changes shape a year
        # from now is diagnosable only against what it actually served.
        tracer.artifact(f"feeds/{tracer.slug(outlet)}.xml", resp.content)

        parsed = feedparser.parse(resp.content)
        raw_count = len(parsed.entries)

        articles: list[Article] = []
        for entry in parsed.entries:
            link = getattr(entry, "link", None)
            title = getattr(entry, "title", None)
            if not link or not title:
                continue
            articles.append(
                Article(
                    outlet=outlet,
                    title=title.strip(),
                    url=link.strip(),
                    summary=_clean_summary(getattr(entry, "summary", "")),
                    published=_entry_published(entry),
                )
            )

        error = ""
        if raw_count == 0:
            error = "zero items (HTTP 200)"
            dbg(f"feed {outlet!r}: ZERO ITEMS at HTTP 200 — the Wire/Print failure mode")

        dbg(
            f"feed {outlet!r}: http={resp.status_code} raw_items={raw_count} "
            f"usable={len(articles)}"
        )
        tracer.event(
            "feeds",
            outlet=outlet,
            url=url,
            http=resp.status_code,
            elapsed_ms=elapsed_ms,
            bytes=len(resp.content),
            raw_items=raw_count,
            usable=len(articles),
            unusable=raw_count - len(articles),
            error=error,
            verdict="zero_items" if raw_count == 0 else "ok",
        )
        return articles, FeedHealth(outlet, url, resp.status_code, raw_count, len(articles), error)
    except Exception as exc:  # noqa: BLE001 - a dead feed must not stop the run
        dbg(f"feed {outlet!r}: FAILED ({exc!r})")
        tracer.event(
            "feeds",
            outlet=outlet,
            url=url,
            http=None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=repr(exc)[:200],
            verdict="exception",
        )
        return [], FeedHealth(outlet, url, None, 0, 0, repr(exc)[:120])


def article_window_start(now: datetime, prev_generated_at: datetime | None) -> datetime:
    """Only consider articles published since the previous digest, clamped
    to a floor (a same-day manual rerun still gets a real window) and a cap
    (a multi-day gap doesn't flood a single run)."""
    if prev_generated_at is None:
        return now - timedelta(hours=24)
    floor = now - timedelta(hours=WINDOW_FLOOR_H)
    cap = now - timedelta(hours=WINDOW_CAP_H)
    return min(max(prev_generated_at, cap), floor)


def gather(feeds_path: Path, since: datetime) -> GatherResult:
    """Fetch every feed, filter to the window, dedupe, sort newest first, and
    report feed health alongside the articles. Degradation (some feeds dead)
    is logged here but never returns early — "degrade, don't fail" means
    whatever real news survives still becomes a digest; quorum_ok() is the
    caller's decision about whether there's enough of it."""
    feeds = load_feeds(feeds_path)
    all_articles: list[Article] = []
    health: list[FeedHealth] = []
    for name, url in feeds:
        articles, feed_health = fetch_feed(name, url)
        all_articles.extend(articles)
        health.append(feed_health)

    undated = sum(1 for a in all_articles if a.published is None)
    if undated:
        dbg(f"gather: {undated} article(s) had no publish date; keeping them")

    # Every article that doesn't make it, and why. These are the two silent
    # filters in the pipeline — an article dropped here never reaches
    # selection, so a feed that looks live can still contribute nothing.
    drops: list[dict] = []

    in_window = []
    for a in all_articles:
        if a.published is not None and a.published < since:
            drops.append({"reason": "out_of_window", "outlet": a.outlet, "title": a.title,
                          "url": a.url, "published": a.published, "since": since})
            continue
        in_window.append(a)

    seen_urls: dict[str, str] = {}
    seen_titles: dict[str, str] = {}
    deduped: list[Article] = []
    for a in in_window:
        curl = canonical_url(a.url)
        ntitle = _normalized_title(a.title)
        if curl in seen_urls:
            drops.append({"reason": "dup_url", "outlet": a.outlet, "title": a.title,
                          "url": a.url, "collided_with": seen_urls[curl]})
            continue
        if ntitle in seen_titles:
            drops.append({"reason": "dup_title", "outlet": a.outlet, "title": a.title,
                          "url": a.url, "collided_with": seen_titles[ntitle]})
            continue
        seen_urls[curl] = a.url
        seen_titles[ntitle] = a.url
        deduped.append(a)

    deduped.sort(key=lambda a: (a.published is None, a.published and -a.published.timestamp()))

    per_outlet: dict[str, int] = {}
    for a in deduped:
        per_outlet[a.outlet] = per_outlet.get(a.outlet, 0) + 1
    dbg(f"gather: {len(deduped)} article(s) after window+dedupe; by outlet: {per_outlet}")

    configured = len(feeds)
    live = sum(1 for h in health if h.usable > 0)
    degraded = configured > 0 and live < configured * DEGRADED_LIVE_SHARE
    dbg(f"gather: {live}/{configured} feed(s) live, {len(deduped)} article(s)")
    if degraded:
        dead = [h.outlet for h in health if h.usable == 0]
        dbg(f"gather: DEGRADED — only {live}/{configured} feeds live; dead: {dead}")

    tracer.count(
        feeds_configured=configured,
        feeds_live=live,
        articles_fetched=len(all_articles),
        articles_undated=undated,
        articles_in_window=len(in_window),
        articles_after_dedupe=len(deduped),
        articles_dropped_window=sum(1 for d in drops if d["reason"] == "out_of_window"),
        articles_dropped_dup=sum(1 for d in drops if d["reason"].startswith("dup")),
    )
    tracer.artifact_json(
        "feeds/index.json",
        {
            "since": since,
            "configured": configured,
            "live": live,
            "degraded": degraded,
            "by_outlet_after_dedupe": per_outlet,
            "feeds": [
                {"outlet": h.outlet, "url": h.url, "http": h.http, "raw_items": h.raw_items,
                 "usable": h.usable, "error": h.error}
                for h in health
            ],
            "dropped_articles": drops,
        },
    )

    return GatherResult(articles=deduped, health=health, configured=configured, live=live, degraded=degraded)


def quorum_ok(result: GatherResult) -> bool:
    """"Degrade, don't fail" (decisions.md): a digest built from 6 of 14
    sources still ships. This is the floor below which there genuinely isn't
    enough left to call it a digest."""
    ok = result.live >= MIN_LIVE_FEEDS and len(result.articles) >= MIN_ARTICLES
    dbg(
        f"gather: QUORUM {'OK' if ok else 'FAILED'} — live={result.live}/{MIN_LIVE_FEEDS} "
        f"min, articles={len(result.articles)}/{MIN_ARTICLES} min"
    )
    return ok


def health_payload(result: GatherResult) -> dict:
    """The health key written into data/YYYY-MM-DD.json — plain JSON types
    only, so report.py can read committed data/ files across many days
    without importing this module's dataclasses."""
    return {
        "configured": result.configured,
        "live": result.live,
        "degraded": result.degraded,
        "articles": len(result.articles),
        "feeds": [
            {
                "outlet": h.outlet,
                "http": h.http,
                "raw_items": h.raw_items,
                "usable": h.usable,
                "error": h.error,
            }
            for h in result.health
        ],
    }
