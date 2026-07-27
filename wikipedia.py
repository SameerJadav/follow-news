"""Wikipedia Current Events Portal — a free, human-curated importance check.

Volume-based ranking over-weights whatever outlets happen to churn (a
celebrity story can out-publish a coup); this is the only free correction to
that bias available (research.md §2.5), and it is the best answer to
product.md's "be sure he didn't miss anything that matters." Never raises —
a Wikipedia outage must not cost a morning.

Fetch the RAW WIKITEXT (action=raw), not the REST HTML endpoint: raw wikitext
is ~17KB of cleanly nested bullets versus ~60KB of Parsoid HTML, and the
"cited bullet = real event" rule below only holds cleanly on wikitext.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import requests

import tracer
from tracer import dbg

WIKI_URL = "https://en.wikipedia.org/w/index.php?title=Portal:Current_events/{page}&action=raw"
WIKI_UA = "follow-news/1.0 (+https://github.com/SameerJadav/follow-news)"

MAX_EVENTS = 60  # cap on lines handed to the select prompt
EVENT_TEXT_CAP = 240  # chars per event line

_HEADING_RE = re.compile(r"^'''(.+?)'''\s*$")
_BULLET_RE = re.compile(r"^(\*+)\s*(.*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_CITE_RE = re.compile(r"\[https?://[^\s\]]+[^\]]*\]")
_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class WikiEvent:
    """One cited, human-curated event from the Current Events Portal."""

    category: str  # e.g. "Armed conflicts and attacks"
    topic: str  # depth-1 ancestor bullet, e.g. "Middle Eastern crisis"; "" if none
    text: str  # cleaned prose, wikilinks resolved, citations stripped


def page_title(d: date) -> str:
    """`date(2026, 7, 4)` -> "2026_July_4" — no zero-padding on the day."""
    return f"{d.year}_{d:%B}_{d.day}"


def _clean(raw: str) -> str:
    """Strip citations and wikilink/formatting markup down to plain prose."""
    text = _CITE_RE.sub("", raw)
    text = _LINK_RE.sub(lambda m: m.group(2) if m.group(2) else m.group(1), text)
    text = text.replace("'''", "").replace("''", "")
    text = _TAG_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def parse_wikitext(text: str) -> list[WikiEvent]:
    """Parse the Current Events Portal's wikitext into cited events.

    The page is a nested bullet list under category headings. A bullet is an
    actual event if and only if it carries an inline citation (`[http...]`) —
    every other bullet is topic-grouping scaffolding, not news itself.
    """
    text = _COMMENT_RE.sub("", text)
    events: list[WikiEvent] = []
    category = ""
    ancestors: dict[int, str] = {}

    for line in text.splitlines():
        heading = _HEADING_RE.match(line.strip())
        if heading:
            category = heading.group(1).strip()
            ancestors = {}
            continue

        bullet = _BULLET_RE.match(line.strip())
        if not bullet:
            continue

        depth = len(bullet.group(1))
        body = bullet.group(2)
        cleaned = _clean(body)

        ancestors = {k: v for k, v in ancestors.items() if k <= depth}
        ancestors[depth] = cleaned

        if "[http" in body and cleaned:
            topic = ancestors.get(1, "") if depth > 1 else ""
            events.append(WikiEvent(category=category, topic=topic, text=cleaned[:EVENT_TEXT_CAP]))

    return events


def fetch_day(d: date) -> list[WikiEvent]:
    """Fetch and parse one day's Current Events page. Returns [] on any
    failure — a dead or changed Wikipedia endpoint must not stop the run."""
    url = WIKI_URL.format(page=page_title(d))
    try:
        resp = requests.get(url, headers={"User-Agent": WIKI_UA}, timeout=20)
        if resp.status_code != 200:
            dbg(f"wiki: {page_title(d)} http={resp.status_code}")
            tracer.event("wikipedia", page=page_title(d), http=resp.status_code, verdict="http_error")
            return []
        events = parse_wikitext(resp.text)
        dbg(f"wiki: {page_title(d)} http={resp.status_code} events={len(events)}")
        # The raw wikitext alongside what we parsed out of it: if Wikipedia
        # changes its bullet markup, the parse silently returns fewer events
        # and only the raw source shows why.
        tracer.artifact(f"wikipedia/{page_title(d)}.wikitext", resp.text)
        tracer.event("wikipedia", page=page_title(d), http=resp.status_code,
                     bytes=len(resp.text), events=len(events), verdict="ok")
        return events
    except Exception as exc:  # noqa: BLE001 - a Wikipedia outage must not cost a morning
        dbg(f"wiki: {page_title(d)} FAILED ({exc!r})")
        tracer.event("wikipedia", page=page_title(d), error=repr(exc)[:200], verdict="exception")
        return []


def current_events(digest_day: date) -> list[WikiEvent]:
    """Today's and yesterday's curated events, combined and capped.

    Fetch both days because at 02:00 IST the digest day's own portal page is
    barely written yet — yesterday's page is the one that's actually full.
    """
    events = fetch_day(digest_day) + fetch_day(digest_day - timedelta(days=1))
    capped = events[:MAX_EVENTS]
    tracer.count(wiki_events_parsed=len(events), wiki_events_used=len(capped))
    tracer.artifact_json(
        "wikipedia/events.json",
        {
            "max_events": MAX_EVENTS,
            "parsed": len(events),
            "used": len(capped),
            "events": [{"category": e.category, "topic": e.topic, "text": e.text} for e in capped],
            "prompt_block": prompt_block(capped),
        },
    )
    return capped


def prompt_block(events: list[WikiEvent]) -> str:
    """Render events as a checklist block for the select prompt. "" when
    there are none, so callers can join it in unconditionally."""
    if not events:
        return ""
    lines = ["WIKIPEDIA CURRENT EVENTS (curated checklist — see rules above)"]
    for e in events:
        prefix = f"[{e.category}] "
        topic = f"{e.topic} — " if e.topic else ""
        lines.append(f"- {prefix}{topic}{e.text}")
    return "\n".join(lines)
