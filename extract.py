"""Article text extraction — the highest-risk piece of the pipeline.

RSS gives headlines and snippets only; article bodies must be fetched from
the publisher's page. No single strategy covers every outlet:

- JSON-LD `articleBody` (Strategy A) works for the Indian outlets and is
  entirely absent on BBC, Al Jazeera, Guardian, The Hindu.
- Paragraph extraction (Strategy B) works for the Western outlets and The
  Hindu, but fails on Times of India — it returns ~635 chars of author-bio
  boilerplate instead of the article.

So: fetch the HTML once, compute both candidates, keep whichever is longer.
Times of India is the acceptance test for this cascade — if it yields under
~1,000 chars, extraction is broken.

NDTV returns 403 to a normal User-Agent. r.jina.ai is a free, keyless reader
proxy (20 RPM) used as a last-resort escalation only, never the default path.
"""

from __future__ import annotations

import html as html_module
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

import tracer
from feeds import UA
from tracer import dbg

MIN_CHARS = 600  # below this, escalate to Jina
JINA_PAUSE = 3.0  # seconds; r.jina.ai keyless is rate-limited to 20 RPM
# r.jina.ai REJECTS browser-like User-Agents. Measured 2026-07-31 on the same
# NDTV URL: `feeds.UA` (a Chrome 126 string) -> 403; curl's own UA, no UA at
# all, `python-requests/2.32.3` and the string below -> 200. The regression
# started around 2026-07-30 and cost the digest every escalation it made (13
# fired, 13 empty, 29% of that day's articles with no usable body) while
# looking exactly like an outlet blocking us. `UA` is still what publisher
# pages are fetched with — this string is for the proxy alone.
JINA_UA = "follow-news/1.0 (+https://sameerjadav.github.io/follow-news/)"
PARA_MIN = 60  # a <p> shorter than this is furniture (nav/share links), not prose
ARTICLE_CAP = 5000  # chars kept per article for the write pass

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "figcaption"}
_JINA_MARKDOWN_MARKER = re.compile(r"^Markdown Content:\s*\n", re.M)


def _fetch(url: str) -> tuple[str | None, int | None, int, str]:
    """The fetch, with the diagnostics attached: (html, http, elapsed_ms,
    error). fetch_html() is the plain-string view of this for callers that
    only want the page."""
    started = time.monotonic()
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            dbg(f"extract: fetch_html {url}: http={resp.status_code}")
            return None, resp.status_code, elapsed, f"http {resp.status_code}"
        return resp.text, resp.status_code, elapsed, ""
    except Exception as exc:  # noqa: BLE001
        dbg(f"extract: fetch_html {url}: FAILED ({exc!r})")
        return None, None, int((time.monotonic() - started) * 1000), repr(exc)[:200]


def fetch_html(url: str) -> str | None:
    """Fetch a page's raw HTML. Returns None on any failure so callers can
    fall back to Jina rather than raising."""
    return _fetch(url)[0]


def _longest_article_body(node) -> str:
    """Walk a parsed JSON-LD value (dicts/lists, possibly nested under
    @graph) and return the longest string found under an `articleBody` key."""
    best = ""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            body = current.get("articleBody")
            if isinstance(body, str) and len(body) > len(best):
                best = body
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return best


def from_jsonld(page_html: str) -> str:
    """Strategy A: pull `articleBody` out of `<script type="application/ld+json">`.
    This is the branch that saves Times of India."""
    best = ""
    for match in _JSONLD_RE.finditer(page_html):
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        body = _longest_article_body(data)
        if len(body) > len(best):
            best = body
    return html_module.unescape(best).strip()


class _ParagraphParser(HTMLParser):
    """Collects text inside top-level <p> tags, ignoring anything nested
    inside script/style/nav/header/footer/aside/form/figcaption."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_p = False
        self._buf: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "p" and self._skip_depth == 0:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "p" and self._in_p:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) > PARA_MIN:
                self.paragraphs.append(text)
            self._in_p = False

    def handle_data(self, data: str) -> None:
        if self._in_p and self._skip_depth == 0:
            self._buf.append(data)


def from_paragraphs(page_html: str) -> str:
    """Strategy B: strip script/style/nav, take <p> text over PARA_MIN chars.
    This is the branch that carries BBC, Guardian, Al Jazeera, The Hindu."""
    parser = _ParagraphParser()
    parser.feed(page_html)
    return "\n\n".join(parser.paragraphs)


def via_jina(url: str) -> str:
    """Last-resort escalation through the free, keyless r.jina.ai reader
    proxy. Used only when the normal fetch failed or returned too little.

    Sends JINA_UA, never UA — see the note there; a browser UA is a 403."""
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", headers={"User-Agent": JINA_UA}, timeout=60)
        if resp.status_code != 200:
            dbg(f"extract: via_jina {url}: http={resp.status_code}")
            return ""
        text = resp.text
        match = _JINA_MARKDOWN_MARKER.search(text)
        body = text[match.end() :] if match else text
        return body.strip()
    except Exception as exc:  # noqa: BLE001
        dbg(f"extract: via_jina {url}: FAILED ({exc!r})")
        return ""
    finally:
        time.sleep(JINA_PAUSE)


# ---------- the URL-keyed cache (dossier.md §10) ----------
#
# OFF by default, so the digest's own morning run is untouched: it fetches
# today's articles once and would never see a hit anyway. Follow is the case
# this exists for — fourteen days of updates on one story, or a second follow
# on a related one, otherwise re-fetch the same background articles every
# single day, each one costing a real HTTP request and up to JINA_PAUSE
# seconds of enforced sleep.
_CACHE_PATH: Path | None = None
_CACHE: dict[str, dict] = {}


def enable_cache(path: Path) -> None:
    """Point extraction at a URL-keyed cache on disk. Idempotent; a missing or
    unreadable file starts an empty cache rather than failing, because a cold
    cache is only ever a performance question, never a correctness one."""
    global _CACHE_PATH, _CACHE
    _CACHE_PATH = path
    try:
        _CACHE = json.loads(path.read_text())
        dbg(f"extract: cache enabled at {path} ({len(_CACHE)} url(s))")
    except (OSError, ValueError):
        _CACHE = {}
        dbg(f"extract: cache enabled at {path} (empty)")


def _cache_get(url: str) -> str | None:
    entry = _CACHE.get(url) if _CACHE_PATH is not None else None
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    return text if isinstance(text, str) and text else None


def _cache_put(url: str, text: str) -> None:
    """Written through after every extraction, not once at the end: a research
    run that dies mid-round must not throw away the pages it already paid for
    — that is the same reasoning as the extraction index above, and it is what
    makes a resumed run cheap."""
    if _CACHE_PATH is None or not text:
        return
    _CACHE[url] = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chars": len(text),
        "text": text,
    }
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_CACHE, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        dbg(f"extract: could not write cache {_CACHE_PATH} ({exc!r})")


def article_text(url: str) -> str:
    """The extraction cascade: JSON-LD and paragraphs from a single fetch,
    longest wins; escalate to Jina only if that still falls short. A cache hit
    (see enable_cache) short-circuits the whole thing."""
    cached = _cache_get(url)
    if cached is not None:
        dbg(f"extract: {len(cached)}ch {url} (cached)")
        tracer.count(articles_extract_cached=1)
        return cached

    page_html, http, elapsed_ms, error = _fetch(url)

    jsonld = from_jsonld(page_html) if page_html else ""
    paras = from_paragraphs(page_html) if page_html else ""
    best = max(jsonld, paras, key=len)
    winner = "" if not best else ("jsonld" if len(jsonld) >= len(paras) else "paragraphs")

    jina = ""
    jina_fired = len(best) < MIN_CHARS
    if jina_fired:
        jina = via_jina(url)
        if len(jina) > len(best):
            best = jina
            winner = "jina"

    dbg(f"extract: {len(best)}ch {url}")
    final = best[:ARTICLE_CAP]
    _cache_put(url, final)
    _capture(url, page_html, jsonld, paras, jina, jina_fired, winner, final, http, elapsed_ms, error)
    return final


# Sequence number and accumulated rows for the extraction index. The index
# is rewritten after every article rather than once at the end, so a run
# that dies mid-loop still leaves a readable record of how far it got.
_SEQ = 0
_ROWS: list[dict] = []


def _capture(
    url: str,
    page_html: str | None,
    jsonld: str,
    paras: str,
    jina: str,
    jina_fired: bool,
    winner: str,
    final: str,
    http: int | None,
    elapsed_ms: int,
    error: str,
) -> None:
    """Persist what the scraper actually saw for one article.

    This is the stage with the least visibility in the whole pipeline: a
    page that extracts to 200 characters of author-bio boilerplate produces
    a thin story three stages later, with nothing left to explain why. So
    all three of the raw HTML, the Jina body, and the exact text handed to
    the claims pass are kept, alongside how each strategy scored."""
    global _SEQ
    if not tracer.enabled():
        return
    _SEQ += 1
    stem = f"extract/{_SEQ:03d}-{tracer.slug(url, 70)}"

    if page_html:
        tracer.artifact(f"{stem}.html", page_html)
    if jina:
        tracer.artifact(f"{stem}.jina.txt", jina)
    tracer.artifact(f"{stem}.txt", final)

    _ROWS.append(
        {
            "seq": _SEQ,
            "url": url,
            "http": http,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "html_chars": len(page_html or ""),
            "jsonld_chars": len(jsonld),
            "paragraph_chars": len(paras),
            "jina_fired": jina_fired,
            "jina_chars": len(jina),
            "winner": winner,
            "final_chars": len(final),
            "truncated_at_cap": len(final) >= ARTICLE_CAP,
            # source_kind in the claims prompt turns on this same threshold:
            # under it, the model gets the RSS summary instead of the body.
            "below_min_chars": len(final) < MIN_CHARS,
            "files": {"html": bool(page_html), "jina": bool(jina), "text": True},
        }
    )
    tracer.artifact_json(
        "extract/index.json",
        {"min_chars": MIN_CHARS, "article_cap": ARTICLE_CAP, "articles": _ROWS},
    )
    tracer.count(
        articles_extracted=len(_ROWS),
        articles_extract_weak=sum(1 for r in _ROWS if r["below_min_chars"]),
        articles_extract_jina=sum(1 for r in _ROWS if r["jina_fired"]),
        articles_extract_failed=sum(1 for r in _ROWS if not r["final_chars"]),
    )
