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
from html.parser import HTMLParser

import requests

from feeds import UA, dbg

MIN_CHARS = 600  # below this, escalate to Jina
JINA_PAUSE = 3.0  # seconds; r.jina.ai keyless is rate-limited to 20 RPM
PARA_MIN = 60  # a <p> shorter than this is furniture (nav/share links), not prose
ARTICLE_CAP = 5000  # chars kept per article for the write pass

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "figcaption"}
_JINA_MARKDOWN_MARKER = re.compile(r"^Markdown Content:\s*\n", re.M)


def fetch_html(url: str) -> str | None:
    """Fetch a page's raw HTML. Returns None on any failure so callers can
    fall back to Jina rather than raising."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if resp.status_code != 200:
            dbg(f"extract: fetch_html {url}: http={resp.status_code}")
            return None
        return resp.text
    except Exception as exc:  # noqa: BLE001
        dbg(f"extract: fetch_html {url}: FAILED ({exc!r})")
        return None


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
    proxy. Used only when the normal fetch failed or returned too little."""
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", headers={"User-Agent": UA}, timeout=60)
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


def article_text(url: str) -> str:
    """The extraction cascade: JSON-LD and paragraphs from a single fetch,
    longest wins; escalate to Jina only if that still falls short."""
    page_html = fetch_html(url)
    best = ""
    if page_html:
        best = max(from_jsonld(page_html), from_paragraphs(page_html), key=len)
    if len(best) < MIN_CHARS:
        best = max(best, via_jina(url), key=len)
    dbg(f"extract: {len(best)}ch {url}")
    return best[:ARTICLE_CAP]
