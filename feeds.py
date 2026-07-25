"""Feed loading, article ingestion, and the time window that keeps a run from
digesting the same article twice.

RSS gives headlines and snippets only — article bodies are fetched separately
in extract.py, only for the articles the LLM selection pass actually chooses.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

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

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def dbg(msg: str) -> None:
    """Diagnostics to stderr only — never into the site. A scheduled run at
    02:00 IST can only be debugged afterwards from the Actions log."""
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class Article:
    """The hand-off contract to the selection/write passes and to later
    phases. Do not rename these fields — Phase 2 builds on them directly."""

    outlet: str
    title: str
    url: str
    summary: str
    published: datetime | None  # tz-aware UTC; None when the feed omitted it


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


def fetch_feed(outlet: str, url: str) -> list[Article]:
    """Fetch and parse one feed. Never raises — a single dead or malformed
    feed must not stop the whole run. Returns [] on any failure, including
    the HTTP-200-with-zero-items trap (The Wire, The Print)."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=25)
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

        dbg(
            f"feed {outlet!r}: http={resp.status_code} raw_items={raw_count} "
            f"usable={len(articles)}"
        )
        return articles
    except Exception as exc:  # noqa: BLE001 - a dead feed must not stop the run
        dbg(f"feed {outlet!r}: FAILED ({exc!r})")
        return []


def article_window_start(now: datetime, prev_generated_at: datetime | None) -> datetime:
    """Only consider articles published since the previous digest, clamped
    to a floor (a same-day manual rerun still gets a real window) and a cap
    (a multi-day gap doesn't flood a single run)."""
    if prev_generated_at is None:
        return now - timedelta(hours=24)
    floor = now - timedelta(hours=WINDOW_FLOOR_H)
    cap = now - timedelta(hours=WINDOW_CAP_H)
    return min(max(prev_generated_at, cap), floor)


def gather(feeds_path: Path, since: datetime) -> list[Article]:
    """Fetch every feed, filter to the window, dedupe, and sort newest first."""
    feeds = load_feeds(feeds_path)
    all_articles: list[Article] = []
    for name, url in feeds:
        all_articles.extend(fetch_feed(name, url))

    undated = sum(1 for a in all_articles if a.published is None)
    if undated:
        dbg(f"gather: {undated} article(s) had no publish date; keeping them")

    in_window = [a for a in all_articles if a.published is None or a.published >= since]

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[Article] = []
    for a in in_window:
        curl = canonical_url(a.url)
        ntitle = _normalized_title(a.title)
        if curl in seen_urls or ntitle in seen_titles:
            continue
        seen_urls.add(curl)
        seen_titles.add(ntitle)
        deduped.append(a)

    deduped.sort(key=lambda a: (a.published is None, a.published and -a.published.timestamp()))

    per_outlet: dict[str, int] = {}
    for a in deduped:
        per_outlet[a.outlet] = per_outlet.get(a.outlet, 0) + 1
    dbg(f"gather: {len(deduped)} article(s) after window+dedupe; by outlet: {per_outlet}")

    return deduped
