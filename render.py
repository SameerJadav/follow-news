"""Renders docs/*.html from data/*.json. Reads only the filesystem — no
network, no API key — so `digest.py render` works standalone in render.yml.

data/ is the single source of truth; everything in docs/ is derived and
regenerated wholesale on every render. Never hand-edit docs/*.html and never
write style.css, app.js, manifest.webmanifest, icon*, robots.txt or .nojekyll
from here — those are hand-written static assets and are untouched by the
pipeline.

Deliberately plain: black and white with blue only for links, one system sans
throughout, one column. Nothing decorative — the information is the design.

Two structures carry the product's promises:

  * `_prose_html` splices per-claim source markers into the body using the
    character offsets in `markers`. This is the component Follow reuses for
    grounded citations (see `_grounded_html`), so it takes plain arguments and
    no story dict.
  * `_sources_html` prints the numbered source list under each headline. Those
    numbers are the same numbers as the superscripts in the prose, so a marker is
    decoded where the reader first meets it.

Follow extends this module rather than replacing anything: `followed/*.json`
(written only by follow.py) is loaded alongside `data/*.json` and rendered into
`docs/follow-<issue>.html` plus a `docs/following.html` index. A followed
story's backstory and timeline entries are `ground.GroundedBlock`s serialised
to dicts — prose plus source markers with no claim id, since grounded prose
has no claim list — so `_grounded_html` calls `_prose_html`/`_sources_html`
with an empty `claims_by_id` rather than duplicating them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from html import escape as esc
from pathlib import Path
from urllib.parse import quote

import tracer

# Mirrors digest.IST. Kept local so render.py imports nothing from the pipeline
# and `digest.py render` stays a leaf call with no chance of an import cycle.
IST = timezone(timedelta(hours=5, minutes=30))

SECTION_ORDER = ("world", "india")
SECTION_LABELS = {"world": "World", "india": "India"}

_HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#ffffff">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="icon-192.png">
<link rel="stylesheet" href="style.css">
<script src="app.js" defer></script>"""


def _head(docs_dir: Path) -> str:
    """`_HEAD` with a content hash appended to style.css and app.js.

    Without this, a phone that has cached the old stylesheet renders new markup
    against it — which looks like a broken page rather than a stale one, and is
    impossible to diagnose from the other end. The hash changes only when the
    file changes, so the cache still does its job the rest of the time.
    """
    head = _HEAD
    for name in ("style.css", "app.js"):
        try:
            version = hashlib.sha256((docs_dir / name).read_bytes()).hexdigest()[:8]
        except OSError:
            continue  # asset missing; an unversioned href is still correct
        head = head.replace(f'"{name}"', f'"{name}?v={version}"')
    return head

# Inline so the speaker glyph needs no icon font and no emoji, which render
# inconsistently across Android versions.
_SPEAKER_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<path d="M4 9h3l5-4v14l-5-4H4z" fill="currentColor"/>'
    '<path d="M16 8.5a5 5 0 0 1 0 7M18.5 6a8 8 0 0 1 0 12" fill="none" '
    'stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>'
    "</svg>"
)

# One bottom sheet per page, emitted last inside <body>. app.js fills it via
# textContent, so it carries no data and needs no escaping here.
_SHEET_HTML = """<div class="sheet" id="sheet" hidden role="dialog" aria-modal="true" aria-labelledby="sheet-outlet">
<div class="sheet-card">
<div class="sheet-handle" aria-hidden="true"></div>
<p class="sheet-outlet" id="sheet-outlet"></p>
<blockquote class="sheet-claim" id="sheet-claim"></blockquote>
<div class="sheet-actions">
<a class="sheet-open" id="sheet-open" href="#" target="_blank" rel="noopener noreferrer">Open article</a>
<button type="button" class="sheet-close" id="sheet-close">Close</button>
</div>
</div>
</div>"""


def _short_date(iso: str) -> str:
    """"2026-07-25" -> "Sat 25 Jul". Returns `iso` unchanged if unparseable."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %-d %b")
    except ValueError:
        return iso


def _time_label(generated_at: str) -> str:
    """"2026-07-25T13:10:19Z" -> "6:40pm" in IST. Empty string if unparseable,
    in which case the stale banner simply omits the time clause."""
    try:
        stamp = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    return f"{stamp.replace(tzinfo=timezone.utc).astimezone(IST):%-I:%M%p}".lower()


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular}" if n == 1 else f"{n} {plural or singular + 's'}"


def _stories(n: int) -> str:
    return _plural(n, "story", "stories")


# 180 words per minute, not the 240 a reading-time widget usually assumes.
# product.md's reader is non-native and reads this at breakfast; a number that
# flatters the digest is the same kind of lie as one that undersells it.
_WPM = 180


def _read_minutes(day: dict) -> int:
    """Whole minutes to read the day, measured from the prose actually on the
    page. Returns 0 for a day with no stories, so the masthead can leave the
    clause out rather than promise a minute of nothing."""
    words = sum(len(str(s.get("body") or "").split()) for s in day.get("stories") or [])
    if not words:
        return 0
    return max(1, round(words / _WPM))


def _masthead_html(day: dict, back: str) -> str:
    """The digest masthead: nameplate, dateline, then a folio line carrying the
    day's size. Reading it top to bottom answers "what is this", "when is it"
    and "how much is there" before a single headline is met — and the size is
    measured from the stories, never estimated."""
    minutes = _read_minutes(day)
    count = len(day.get("stories") or [])
    n, word = _stories(count).split(" ", 1)

    right = f"{minutes} min read" if minutes else "&nbsp;"
    return (
        '<header class="plate">'
        '<h1 class="plate-name">Follow</h1>'
        f'<p class="plate-date">{esc(str(day.get("date_label") or day.get("date") or ""))}</p>'
        f"{back}"
        "</header>"
        f'<div class="folio"><span><b>{esc(n)}</b> {esc(word)}</span>'
        f"<span>{right}</span></div>"
    )


def _accepted_markers(body: str, markers: list[dict]) -> list[dict]:
    """Markers that can be spliced into `body` without corrupting it.

    LLM output is only ever syntactically validated, so the offsets are never
    trusted: anything out of range, inverted, or overlapping an already-accepted
    marker is dropped. A dropped marker costs a numeral and nothing else — the
    body text must always come through whole.
    """
    candidates = []
    for m in markers or []:
        try:
            start, end = int(m["start"]), int(m["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start < end <= len(body):
            candidates.append({**m, "start": start, "end": end})

    candidates.sort(key=lambda m: (m["start"], m["end"]))

    accepted: list[dict] = []
    for m in candidates:
        if accepted and m["start"] < accepted[-1]["end"]:
            continue
        accepted.append(m)
    return accepted


def _marker_html(marker: dict, src_index: dict[str, int], claims_by_id: dict[int, str]) -> str:
    """One tappable source marker: a superscript numeral keyed to the story's
    provenance list. Returns "" when the marker has no resolvable source, so the
    prose loses a numeral rather than gaining a dead link."""
    url = str(marker.get("url") or "")
    n = src_index.get(url)
    if not url or n is None:
        return ""
    outlet = str(marker.get("outlet") or "")
    try:
        claim = claims_by_id.get(int(marker["claim_id"]), "")
    except (KeyError, TypeError, ValueError):
        claim = ""
    return (
        f'<a class="src" href="{esc(url, quote=True)}" target="_blank" rel="noopener noreferrer"'
        f' data-n="{n}" data-outlet="{esc(outlet, quote=True)}"'
        f' data-claim="{esc(claim, quote=True)}"'
        f' aria-label="Source {n}: {esc(outlet, quote=True)}"><sup>{n}</sup></a>'
    )


def _prose_html(
    body: str,
    markers: list[dict],
    src_index: dict[str, int],
    claims_by_id: dict[int, str],
) -> str:
    """Body prose with a tappable source marker after each anchored span.

    Takes plain arguments rather than a story dict: Phase 5's followed-story
    pages render grounded backstory and timeline prose through this same
    function, and Gemini's grounding annotations carry the same
    start/end-offset shape.

    Markers are not coalesced. A run of sentences from one outlet gets one
    numeral each, because the bottom sheet promises the exact claim behind the
    sentence that was tapped — a merged numeral would show one claim for
    several sentences. Repeated sources produce the same repeated numeral,
    which the eye learns to skip.
    """
    accepted = _accepted_markers(body, markers)

    parts = []
    cursor = 0  # offset in `body` of the start of the current paragraph
    for para in body.split("\n\n"):
        para_start, para_end = cursor, cursor + len(para)
        cursor = para_end + 2  # advance past the "\n\n" separator

        if not para.strip():
            continue

        inner = []
        at = para_start
        for m in accepted:
            if m["start"] < para_start or m["end"] > para_end:
                continue
            inner.append(esc(body[at : m["end"]]))
            inner.append(_marker_html(m, src_index, claims_by_id))
            at = m["end"]
        inner.append(esc(body[at:para_end]))

        parts.append(f"<p>{''.join(inner)}</p>")

    return "".join(parts)


def _sources_html(story: dict, src_index: dict[str, int]) -> str:
    """The story's numbered source list: a single wrapping row after the prose.

    The numerals here are the same numerals as the superscripts in the body, so
    this doubles as their key. Items are separated by space alone — each numeral
    already marks where an item begins, so punctuation between them would only
    add noise.
    """
    sources = story.get("sources") or []
    if not sources:
        return ""

    links = []
    for i, source in enumerate(sources):
        url = str(source.get("url") or "")
        outlet = str(source.get("outlet") or "")
        links.append(
            f'<a class="source" href="{esc(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
            f'<span class="s-n">{src_index.get(url, i + 1)}</span>{esc(outlet)}</a>'
        )

    return (
        '<div class="sources">'
        '<span class="sources-label">Sources</span>'
        f'{"".join(links)}'
        "</div>"
    )


def _thin_html(story: dict) -> str:
    """The thin-sourcing badge, at the top of the story. `thin_sourced` is a
    measured outlet count from anchor.py, never a model assessment."""
    if not story.get("thin_sourced"):
        return ""

    count = story.get("signals", {}).get("claim_outlets")
    if not isinstance(count, int) or count <= 0:
        count = len(story.get("sources") or [])

    detail = (
        f"Only {_plural(count, 'outlet')} reported the facts in this story."
        if count
        else "Fewer outlets than usual back this story."
    )
    return (
        '<p class="thin" role="note">'
        '<span class="thin-tag">Thinly sourced</span> '
        f"{esc(detail)}</p>"
    )


def _vocab_html(story: dict) -> str:
    """Words to Know. `data-term` carries the real word, never the respelling —
    text-to-speech must read `ceasefire`, not `SEES-fy-er`. The respelling stays
    visible as text because it is useful without audio."""
    items = story.get("vocab") or []
    if not items:
        return ""

    rows = []
    for item in items:
        term = str(item.get("term") or "")
        if not term:
            continue
        say = str(item.get("say") or "")
        say_html = f' <span class="say">{esc(say)}</span>' if say else ""
        rows.append(
            '<div class="term-row">'
            f'<dt><span class="term">{esc(term)}</span>{say_html} '
            f'<button type="button" class="say-btn" data-term="{esc(term, quote=True)}"'
            f' aria-label="Hear {esc(term, quote=True)}">{_SPEAKER_SVG}</button></dt>'
            f'<dd>{esc(str(item.get("meaning") or ""))}</dd>'
            "</div>"
        )

    if not rows:
        return ""
    return (
        '<section class="vocab" aria-label="Words to know">'
        '<h4 class="kicker">Words to know</h4>'
        f'<dl>{"".join(rows)}</dl>'
        "</section>"
    )


# Follow: the prefilled issue URL a Follow button opens. Mirrors
# follow.issue_url exactly. render.py cannot import follow.py — follow.py
# already imports render.py to publish followed-story pages, and a module
# cycle would follow — so this shape is duplicated rather than shared.
# Any change to one must be mirrored in the other.
_FOLLOW_LABEL = "follow"
_FOLLOW_REPO = "SameerJadav/follow-news"


def _follow_url(day_date: str, section: str, position: int, headline: str) -> str:
    title = f"Follow: {headline}"[:120]
    body = (
        "Follow this story.\n\n"
        f"digest: {day_date}\n"
        f"section: {section}\n"
        f"story: {position}\n"
        f"headline: {headline}"
    )
    params = (
        f"labels={quote(_FOLLOW_LABEL, safe='')}"
        f"&title={quote(title, safe='')}"
        f"&body={quote(body, safe='')}"
    )
    return f"https://github.com/{_FOLLOW_REPO}/issues/new?{params}"


def _actions_html(
    day_date: str,
    section: str,
    position: int,
    headline: str,
    followed_index: dict[tuple[str, str, int], tuple[int, str]],
) -> str:
    """Fills the <footer class="story-actions"> seam: a Follow button for a
    story with no record yet, a quiet link to the followed-story page for one
    being actively followed, and BOTH for a story whose follow has closed.

    That third case matters: a closed follow is a story the reader chose to
    stop following, or one that ran its course — and either way, wanting it
    again later is a real thing to want. Showing only "Following →" for a
    closed record made the story permanently un-followable, since the button
    is the only way a follow is ever created.

    No slug or id is computed in JavaScript — everything the button needs is
    already on the <article> as data attributes and passed straight through
    here."""
    entry = followed_index.get((day_date, section, position))
    if entry is not None and entry[1] == "active":
        return f'<a class="follow-btn is-on" href="follow-{entry[0]}.html">Following →</a>'

    url = _follow_url(day_date, section, position, headline)
    button = (
        f'<a class="follow-btn" href="{esc(url, quote=True)}" target="_blank"'
        ' rel="noopener noreferrer">Follow this story</a>'
    )
    if entry is None:
        return button
    return (
        f'{button}<a class="follow-prev" href="follow-{entry[0]}.html">Previously followed →</a>'
    )


def _suggestions_list_html(raws: list[str]) -> str:
    """The deduplicated set of Search Suggestions blobs for a whole followed
    story. Each blob is still emitted byte-for-byte unmodified — that is what
    the grounding Terms require, and it is why _suggestions_html below is the
    one function in this codebase that does not escape its input."""
    seen: list[str] = []
    for raw in raws:
        text = str(raw or "")
        if text and text not in seen:
            seen.append(text)
    if not seen:
        return ""
    cards = "".join(f'<div class="chip-card">{raw}</div>' for raw in seen)
    return (
        '<section class="suggest" aria-label="Search suggestions from Google">'
        '<h4 class="kicker">Search these on Google</h4>'
        f'<div class="chips">{cards}</div>'
        "</section>"
    )


def _researching_html(record: dict) -> str:
    """The honest in-progress state for a followed story whose dossier has not
    finished. Same posture render._stale_html takes toward a stale digest: say
    plainly what is true rather than showing an empty or half-built page."""
    since = _short_date(str(record.get("origin", {}).get("date") or ""))
    detail = (
        "Research hasn't started yet."
        if record.get("research_state") == "pending"
        else "Reading the story's full history now. This page fills in when it's done."
    )
    return (
        '<div class="researching" role="status">'
        f"<p>Still researching this story, followed since {esc(since)}.</p>"
        f"<p>{esc(detail)}</p>"
        "</div>"
    )


def _capped_html(record: dict) -> str:
    """A capped dossier still has real prose — it just stopped short of the
    whole story. dossier.md §13: a truncated dossier must never present as a
    complete one."""
    if record.get("research_state") != "capped":
        return ""
    return (
        '<p class="capped" role="note"><span class="capped-tag">Research paused</span> '
        "This story hit its research budget, so some threads may still be missing.</p>"
    )


def _suggestions_html(raw: str) -> str:
    """The Search Suggestions chips grounding's Terms of Service require be
    displayed, unmodified, with the Grounded Results. `raw` is
    `searchEntryPoint.rendered_content` as Google's API returned it, stored
    verbatim in followed/*.json — never user input, never touched here. This
    is the one place in the codebase that emits HTML that isn't escaped or
    built from `esc()`; modifying it would violate the Terms this feature
    depends on.

    The widget arrives as a self-contained card carrying its own stylesheet, so
    it will always be a foreign object on this page. What it does not arrive
    with is any statement of what it is — just a Google mark and a row of
    pills. The standing head below is ours, sits outside the widget, and leaves
    Google's markup byte-for-byte untouched; it gives the card a reason to be
    there instead of leaving it stranded under the source list."""
    if not raw:
        return ""
    return (
        '<section class="suggest" aria-label="Search suggestions from Google">'
        '<h4 class="kicker">Search these on Google</h4>'
        f'<div class="chips">{raw}</div>'
        "</section>"
    )


def _grounded_html(block: dict) -> str:
    """A grounded prose block (a followed story's backstory or one timeline
    entry): body with tappable source markers, then the source list. Reuses
    _prose_html/_sources_html exactly as the digest does; `claims_by_id` is
    passed empty because grounded prose has no claim list — _marker_html and
    the bottom sheet already tolerate a missing claim.

    Chips are NOT emitted here. A dossier accumulates a searchEntryPoint blob
    per grounded call — dozens of them — and repeating the same widget after
    every block would bury the page. _follow_page renders the deduplicated set
    once instead."""
    sources = block.get("sources") or []
    src_index = {str(s.get("url") or ""): i + 1 for i, s in enumerate(sources)}
    prose = _prose_html(str(block.get("body") or ""), block.get("markers") or [], src_index, {})
    return f'<div class="prose">{prose}</div>{_sources_html(block, src_index)}'


def _story_html(
    story: dict,
    index: int,
    day_date: str,
    followed_index: dict[tuple[str, str, int], tuple[int, str]] | None = None,
) -> str:
    sources = story.get("sources") or []
    src_index = {str(s.get("url") or ""): i + 1 for i, s in enumerate(sources)}
    claims_by_id: dict[int, str] = {}
    for claim in story.get("claims") or []:
        try:
            claims_by_id[int(claim["id"])] = str(claim.get("text") or "")
        except (KeyError, TypeError, ValueError):
            continue

    headline = str(story.get("headline") or "")
    section = str(story.get("section") or "")
    tier = str(story.get("signals", {}).get("tier") or "secondary")

    prose = _prose_html(str(story.get("body") or ""), story.get("markers") or [], src_index, claims_by_id)
    actions = _actions_html(day_date, section, index, headline, followed_index or {})

    return (
        # Reading order: the headline is the entry point, the thin-sourcing
        # notice conditions how the story is read so it comes before the prose,
        # Words to Know explains what was just read, and sources close it out as
        # attribution. Tapping a superscript is the primary way to check a
        # source; the list at the foot is the summary.
        f'<article class="story" id="story-{index}"'
        f' data-tier="{esc(tier, quote=True)}"'
        f' data-section="{esc(section, quote=True)}"'
        f' data-date="{esc(day_date, quote=True)}"'
        f' data-headline="{esc(headline, quote=True)}">'
        f'<h3 class="story-hd">{esc(headline)}</h3>'
        f"{_thin_html(story)}"
        f'<div class="prose">{prose}</div>'
        f"{_vocab_html(story)}"
        f"{_sources_html(story, src_index)}"
        f'<footer class="story-actions">{actions}</footer>'
        "</article>"
    )


def _section_html(
    key: str,
    stories: list[dict],
    day: dict,
    followed_index: dict[tuple[str, str, int], tuple[int, str]] | None = None,
) -> str:
    """One switchable view. The section id is the bare section key so the tab
    bar's href="#world" works as a plain anchor with no JavaScript.

    Deliberately never emits `hidden`: both sections render visible and app.js
    hides the inactive one, so a page with broken JavaScript is still fully
    readable rather than half invisible.
    """
    label = SECTION_LABELS[key]

    if stories:
        body = "".join(
            _story_html(s, i, str(day.get("date") or ""), followed_index)
            for i, s in enumerate(stories, start=1)
        )
        close = f"That's all for {label} — {_stories(len(stories))}."
    else:
        # No padding and no quota stories: an empty section says so plainly, and
        # the message is itself the section's close.
        body = ""
        close = f"Nothing big enough in {label} today."

    return (
        f'<section class="section" id="{key}" data-section="{key}" aria-labelledby="tab-{key}">'
        f'<h2 class="sr-only">{esc(label)}</h2>'
        f"{body}"
        f'<div class="close"><span>{esc(close)}</span></div>'
        "</section>"
    )


def _tabs_html(counts: dict[str, int]) -> str:
    """Both tabs always render, even at zero — "India 0" is the honest
    inventory and serves the reader's "did I miss anything" question. app.js
    corrects `is-on`/`aria-selected` from the URL hash on load."""
    tabs = []
    for i, key in enumerate(SECTION_ORDER):
        on = i == 0
        tabs.append(
            f'<a class="tab{" is-on" if on else ""}" id="tab-{key}" href="#{key}" role="tab"'
            f' aria-selected="{"true" if on else "false"}" aria-controls="{key}"'
            f' data-section="{key}">{esc(SECTION_LABELS[key])}'
            f' <span class="tab-n">{counts.get(key, 0)}</span></a>'
        )
    return f'<nav class="tabs" role="tablist">{"".join(tabs)}</nav>'


def _stale_html(day: dict, today: date) -> str:
    """Yesterday's digest, honestly labelled. The markup is always emitted; the
    `hidden` attribute is what varies. That lets app.js reveal the banner from
    the phone's own clock when a morning's run never fired and therefore never
    triggered a re-render — Phase 6's run-side staleness logic works alongside
    it unchanged."""
    day_date = str(day.get("date") or "")
    is_today = day_date == today.isoformat()
    when = _time_label(str(day.get("generated_at") or ""))
    tail = f", last updated {esc(when)}" if when else ""
    return (
        f'<div class="stale" id="stale"{" hidden" if is_today else ""}'
        f' data-digest-date="{esc(day_date, quote=True)}">'
        "<p>Today's digest isn't ready yet. This is "
        f'<b>{esc(str(day.get("date_label") or day_date))}</b>{tail}.</p>'
        "</div>"
    )


def _following_row(records: list[dict], today: date) -> str:
    """The masthead's entry point into Follow — only on index.html, and only
    when at least one follow is active. Not a third tab: product.md is
    explicit that there are two sections you switch between."""
    active = [r for r in records if r.get("status") == "active"]
    if not active:
        return ""

    new_count = sum(
        1
        for r in active
        if any(e.get("date") == today.isoformat() for e in (r.get("timeline") or []))
    )
    new_html = f'<span class="new">{new_count} new today</span>' if new_count else ""
    label = "story" if len(active) == 1 else "stories"
    # Exactly two children. Bare text beside the <b> would each become its own
    # anonymous flex item, and space-between would then spread "Following", "1"
    # and "story" across the whole row.
    return (
        '<a class="following-row" href="following.html">'
        "<span>Following</span>"
        f'<span class="following-count"><b>{len(active)}</b> {label}{new_html}</span>'
        "</a>"
    )


def _follow_status_html(record: dict) -> str:
    updates = len(record.get("timeline") or [])
    since = _short_date(str(record.get("origin", {}).get("date") or ""))
    if record.get("status") == "closed":
        entries = record.get("timeline") or []
        closed_on = entries[-1].get("date") if entries and entries[-1].get("date") else since
        return f'<p class="follow-status">Closed {esc(_short_date(str(closed_on)))} · {esc(_plural(updates, "update"))}</p>'
    return f'<p class="follow-status">Following since {esc(since)} · {esc(_plural(updates, "update"))}</p>'


def _follow_close_html(record: dict) -> str:
    """The followed-story equivalent of the digest's hard close: a clear,
    finite statement of where things stand, never an open-ended scroll."""
    entries = record.get("timeline") or []
    if record.get("status") == "closed":
        closed_on = entries[-1].get("date") if entries and entries[-1].get("date") else record.get("origin", {}).get("date", "")
        text = f"This story closed on {_short_date(str(closed_on))}. That's the end of it."
    elif entries:
        text = f"Still following — last update {_short_date(str(entries[-1].get('date') or ''))}."
    else:
        text = "Still following — no updates yet."
    return f'<div class="close"><span>{esc(text)}</span></div>'


def _follow_page(record: dict, head: str = _HEAD) -> str:
    """A followed story: the full-picture backstory, then the timeline
    oldest-first so it grows the picture downward, then a hard close.

    The masthead says what the page is ("Followed story") rather than repeating
    the headline, and carries the way back to today on the same row — the same
    shape as the archive and Following pages. The story's own title then opens
    the page as its heading. Same <head> and bottom sheet as a digest day page,
    so typography, source markers and pronunciation behave identically; Follow
    only adds content, never a second design."""
    title = str(record.get("title") or "")
    backstory = record.get("backstory") or {}
    entries = record.get("timeline") or []
    state = str(record.get("research_state") or "complete")

    timeline_html = "".join(
        f'<section class="entry{" is-final" if e.get("kind") == "final" else ""}">'
        f'<h4 class="entry-date">{esc(str(e.get("date_label") or e.get("date") or ""))}</h4>'
        f"{_grounded_html(e)}"
        "</section>"
        for e in entries
    )

    if state in ("pending", "researching") or not backstory.get("body"):
        picture_html = _researching_html(record)
    else:
        picture_html = f"{_capped_html(record)}{_grounded_html(backstory)}"

    return f"""<!doctype html>
<html lang="en">
<head>
<title>{esc(title)}</title>
{head}
</head>
<body>
<header class="mast"><h1 class="day">Followed story</h1><a class="back" href="index.html">Today</a></header>
<h2 class="follow-title">{esc(title)}</h2>
{_follow_status_html(record)}
<h3 class="kicker">The full picture</h3>
{picture_html}
{timeline_html}
{_suggestions_list_html(list(record.get("chips") or []) + [str(backstory.get("search_suggestions") or "")] + [str(e.get("search_suggestions") or "") for e in entries])}
{_follow_close_html(record)}
<a class="archive-link" href="following.html">All followed stories</a>
{_SHEET_HTML}
</body>
</html>
"""


def _following_page(records: list[dict], head: str = _HEAD) -> str:
    """Every followed story, active first then closed — a separate,
    unpromoted surface reached from the masthead row, the same relationship
    the archive has to the digest."""

    def _row(r: dict) -> str:
        entries = r.get("timeline") or []
        since = _short_date(str(r.get("origin", {}).get("date") or ""))
        state = str(r.get("research_state") or "complete")
        if state in ("pending", "researching"):
            # "0 updates" would read as a story where nothing has happened,
            # rather than one whose research hasn't landed yet.
            return (
                f'<li><a href="follow-{esc(str(r.get("issue") or ""), quote=True)}.html">'
                f'<span class="d">{esc(str(r.get("title") or ""))}</span>'
                f'<span class="c">{esc(f"Since {since} · still researching")}</span>'
                "</a></li>"
            )
        detail = f"Since {since} · {_plural(len(entries), 'update')}"
        latest = entries[-1].get("date") if entries else None
        if latest:
            detail += f" · latest {_short_date(str(latest))}"
        if state == "capped":
            detail += " · research paused"
        return (
            f'<li><a href="follow-{esc(str(r.get("issue") or ""), quote=True)}.html">'
            f'<span class="d">{esc(str(r.get("title") or ""))}</span>'
            f'<span class="c">{esc(detail)}</span>'
            "</a></li>"
        )

    active = sorted((r for r in records if r.get("status") == "active"), key=lambda r: r.get("issue", 0), reverse=True)
    closed = sorted((r for r in records if r.get("status") != "active"), key=lambda r: r.get("issue", 0), reverse=True)

    parts = []
    if active:
        parts.append('<h2 class="month">Following</h2><ol class="days">')
        parts += [_row(r) for r in active]
        parts.append("</ol>")
    if closed:
        parts.append('<h2 class="month">Closed</h2><ol class="days">')
        parts += [_row(r) for r in closed]
        parts.append("</ol>")

    listing = "".join(parts) or '<p class="empty">Nothing followed yet.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<title>Following</title>
{head}
</head>
<body>
<header class="mast"><h1 class="day">Following</h1><a class="back" href="index.html">Today</a></header>
{listing}
</body>
</html>
"""


def _page(
    day: dict,
    today: date,
    *,
    is_index: bool,
    head: str = _HEAD,
    followed_index: dict[tuple[str, str, int], tuple[int, str]] | None = None,
    followed_records: list[dict] | None = None,
) -> str:
    grouped: dict[str, list[dict]] = {key: [] for key in SECTION_ORDER}
    for story in day.get("stories") or []:
        # Anything unrecognised (only possible in data/ predating Phase 2)
        # falls into World so old files keep rendering.
        key = story.get("section")
        grouped[key if key in grouped else "world"].append(story)

    counts = {key: len(group) for key, group in grouped.items()}
    date_label = str(day.get("date_label") or day.get("date") or "")

    back = '<a class="back" href="index.html">Today</a>' if not is_index else ""
    stale = _stale_html(day, today) if is_index else ""
    following_row = _following_row(followed_records or [], today) if is_index else ""
    sections = "".join(
        _section_html(key, grouped[key], day, followed_index) for key in SECTION_ORDER
    )

    # Masthead order is deliberate: what this is, when, how much, then the
    # sections to switch between, then Follow. The stale notice sits directly
    # under the folio because it corrects the folio's claim about the day.
    return f"""<!doctype html>
<html lang="en">
<head>
<title>{esc(date_label)}</title>
{head}
</head>
<body>
{_masthead_html(day, back)}
{stale}
{_tabs_html(counts)}
{following_row}
{sections}
<a class="archive-link" href="archive.html">Past days</a>
{_SHEET_HTML}
</body>
</html>
"""


def _archive_page(days: list[dict], head: str = _HEAD) -> str:
    """The full archive as a date list, newest first, grouped by month. A
    separate, unpromoted surface — reached only from the small link below each
    day's hard close."""
    parts = []
    month = None
    for day in days:
        iso = str(day.get("date") or "")
        key = iso[:7]
        if key != month:
            if month is not None:
                parts.append("</ol>")
            try:
                label = datetime.strptime(iso, "%Y-%m-%d").strftime("%B %Y")
            except ValueError:
                label = key
            parts.append(f'<h2 class="month">{esc(label)}</h2><ol class="days">')
            month = key
        count = len(day.get("stories") or [])
        parts.append(
            f'<li><a href="{esc(iso, quote=True)}.html">'
            f'<span class="d">{esc(_short_date(iso))}</span>'
            f'<span class="c">{esc(_stories(count))}</span>'
            "</a></li>"
        )
    if month is not None:
        parts.append("</ol>")

    listing = "".join(parts) or '<p class="empty">No digests yet.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
<title>Archive</title>
{head}
</head>
<body>
<header class="mast"><h1 class="day">Archive</h1><a class="back" href="index.html">Today</a></header>
{listing}
</body>
</html>
"""


def _empty_page(head: str = _HEAD) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<title>Follow</title>
{head}
</head>
<body>
<p class="empty">No digest yet.</p>
</body>
</html>
"""


def _load_followed(
    followed_dir: Path | None,
) -> tuple[list[dict], dict[tuple[str, str, int], tuple[int, str]]]:
    """Every followed record (oldest issue first) plus an index from
    (origin date, origin section, origin position) -> (issue, status), built
    from records of ANY status so a closed follow's page stays reachable from
    the story it came from.

    Reads followed/<issue>/record.json, falling back to legacy flat
    followed/<issue>.json — mirroring follow.load_all(), which render.py
    cannot call: follow.py already imports render.py to publish followed-story
    pages, and a module cycle would follow. Any change to one must be
    mirrored in the other, the same rule _follow_url/issue_url already live
    under.

    `research_state` and `chips` are attached here as DERIVED fields, read
    from the sibling dossier.json — they are never persisted into
    record.json, whose shape is unchanged. A record with no dossier.json is a
    legacy one-shot follow whose prose is already finished, so it defaults to
    "complete"; defaulting to "researching" would hide working prose behind a
    placeholder."""
    if followed_dir is None or not followed_dir.exists():
        return [], {}

    records: list[dict] = []
    index: dict[tuple[str, str, int], tuple[int, str]] = {}
    seen: set[int] = set()

    def _add(record: dict, issue_dir: Path | None) -> None:
        issue = record.get("issue")
        if not isinstance(issue, int) or issue in seen:
            return
        seen.add(issue)

        state, chips = "complete", []
        if issue_dir is not None:
            try:
                meta = json.loads((issue_dir / "dossier.json").read_text())
                state = str(meta.get("research_state") or "complete")
                chips = list(meta.get("chips") or [])
            except (OSError, ValueError):
                pass  # a corrupt dossier degrades to "complete", never a broken render
        record["research_state"] = state
        record["chips"] = chips
        records.append(record)

        origin = record.get("origin") or {}
        d = str(origin.get("date") or "")
        section = str(origin.get("section") or "")
        position = origin.get("position")
        if d and section and isinstance(position, int):
            key = (d, section, position)
            # Highest issue number wins: a story followed a second time must
            # point at the NEW follow. Sorting paths as strings would put "10"
            # before "2" and silently hand the story back to the older, closed
            # follow.
            if key not in index or issue > index[key][0]:
                index[key] = (issue, str(record.get("status") or "active"))

    for path in sorted(followed_dir.glob("*/record.json"), key=lambda p: p.parent.name):
        try:
            _add(json.loads(path.read_text()), path.parent)
        except (OSError, ValueError):
            continue

    for path in sorted(followed_dir.glob("*.json")):
        try:
            _add(json.loads(path.read_text()), None)
        except (OSError, ValueError):
            continue

    records.sort(key=lambda r: r.get("issue", 0))
    return records, index


def render_all(
    data_dir: Path,
    docs_dir: Path,
    today: date | None = None,
    followed_dir: Path | None = None,
) -> None:
    """Load every data/*.json, render one HTML page per day plus index.html
    (newest day) and archive.html (full list, newest first). When
    `followed_dir` holds followed/*.json records (Follow's own source of
    truth, written only by follow.py — never here), also renders
    docs/follow-<issue>.html per record and docs/following.html when any
    exist.

    `today` decides whether index.html carries the stale banner; digest.py
    passes digest_date(). The default exists only so this stays callable from a
    test or a REPL.
    """
    if today is None:
        today = datetime.now(timezone.utc).astimezone(IST).date()

    days = [json.loads(path.read_text()) for path in sorted(data_dir.glob("*.json"))]
    days.sort(key=lambda d: d["date"], reverse=True)

    records, followed_index = _load_followed(followed_dir)

    docs_dir.mkdir(parents=True, exist_ok=True)
    head = _head(docs_dir)

    for day in days:
        (docs_dir / f"{day['date']}.html").write_text(
            _page(day, today, is_index=False, head=head, followed_index=followed_index)
        )

    if days:
        (docs_dir / "index.html").write_text(
            _page(
                days[0],
                today,
                is_index=True,
                head=head,
                followed_index=followed_index,
                followed_records=records,
            )
        )
    else:
        (docs_dir / "index.html").write_text(_empty_page(head))

    (docs_dir / "archive.html").write_text(_archive_page(days, head))

    for record in records:
        issue = record.get("issue")
        if not isinstance(issue, int):
            continue
        (docs_dir / f"follow-{issue}.html").write_text(_follow_page(record, head))

    if records:
        (docs_dir / "following.html").write_text(_following_page(records, head))

    # The one stage with no diagnostics of its own. A page that renders at a
    # fraction of its usual size is the cheapest possible signal that a
    # template or a data file went wrong, and it costs a stat() to see.
    if tracer.enabled():
        pages = sorted(p for p in docs_dir.glob("*.html"))
        tracer.count(pages_rendered=len(pages), days_rendered=len(days),
                     follow_records_rendered=len(records))
        tracer.artifact_json(
            "render.json",
            {
                "today": f"{today:%Y-%m-%d}",
                "newest_data_day": days[0]["date"] if days else None,
                "stale_banner_expected": bool(days) and days[0]["date"] != f"{today:%Y-%m-%d}",
                "days": len(days),
                "followed_records": len(records),
                "pages": [{"name": p.name, "bytes": p.stat().st_size} for p in pages],
            },
        )
