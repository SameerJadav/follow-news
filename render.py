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
    character offsets in `markers`. This is the component Phase 5 reuses for
    grounded citations, so it takes plain arguments and no story dict.
  * `_sources_html` prints the numbered source list under each headline. Those
    numbers are the same numbers as the superscripts in the prose, so a marker is
    decoded where the reader first meets it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from html import escape as esc
from pathlib import Path

# Mirrors digest.IST. Kept local so render.py imports nothing from the pipeline
# and `digest.py render` stays a leaf call with no chance of an import cycle.
IST = timezone(timedelta(hours=5, minutes=30))

SECTION_ORDER = ("world", "india")
SECTION_LABELS = {"world": "World", "india": "India"}

_HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#121212" media="(prefers-color-scheme: dark)">
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


def _story_html(story: dict, index: int, day_date: str) -> str:
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

    # Phase 5 inserts the Follow button inside <footer class="story-actions">.
    # Everything it needs (date, headline, section) is already on the <article>
    # as data attributes, so no slug function is computed here or in app.js —
    # a slug duplicated across Python and JavaScript breaks silently.
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
        '<footer class="story-actions"></footer>'
        "</article>"
    )


def _section_html(key: str, stories: list[dict], day: dict) -> str:
    """One switchable view. The section id is the bare section key so the tab
    bar's href="#world" works as a plain anchor with no JavaScript.

    Deliberately never emits `hidden`: both sections render visible and app.js
    hides the inactive one, so a page with broken JavaScript is still fully
    readable rather than half invisible.
    """
    label = SECTION_LABELS[key]

    if stories:
        body = "".join(
            _story_html(s, i, str(day.get("date") or "")) for i, s in enumerate(stories, start=1)
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


def _page(day: dict, today: date, *, is_index: bool, head: str = _HEAD) -> str:
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
    sections = "".join(_section_html(key, grouped[key], day) for key in SECTION_ORDER)

    return f"""<!doctype html>
<html lang="en">
<head>
<title>{esc(date_label)}</title>
{head}
</head>
<body>
<header class="mast"><h1 class="day">{esc(date_label)}</h1>{back}</header>
{stale}
{_tabs_html(counts)}
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


def render_all(data_dir: Path, docs_dir: Path, today: date | None = None) -> None:
    """Load every data/*.json, render one HTML page per day plus index.html
    (newest day) and archive.html (full list, newest first).

    `today` decides whether index.html carries the stale banner; digest.py
    passes digest_date(). The default exists only so this stays callable from a
    test or a REPL.
    """
    if today is None:
        today = datetime.now(timezone.utc).astimezone(IST).date()

    days = [json.loads(path.read_text()) for path in sorted(data_dir.glob("*.json"))]
    days.sort(key=lambda d: d["date"], reverse=True)

    docs_dir.mkdir(parents=True, exist_ok=True)
    head = _head(docs_dir)

    for day in days:
        (docs_dir / f"{day['date']}.html").write_text(_page(day, today, is_index=False, head=head))

    if days:
        (docs_dir / "index.html").write_text(_page(days[0], today, is_index=True, head=head))
    else:
        (docs_dir / "index.html").write_text(_empty_page(head))

    (docs_dir / "archive.html").write_text(_archive_page(days, head))
