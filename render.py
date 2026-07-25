"""Renders docs/*.html from data/*.json. Reads only the filesystem — no
network, no API key — so `digest.py render` works standalone in render.yml.

data/ is the single source of truth; everything in docs/ is derived and
regenerated wholesale on every render. Never hand-edit docs/*.html and never
write style.css, app.js, robots.txt, or .nojekyll from here — those are
hand-written and untouched by the pipeline.

Phase 4 replaces this renderer's markup and CSS wholesale; keeping the
templates as small f-string functions is what keeps that swap local to this
file.
"""

from __future__ import annotations

import json
from html import escape as esc
from pathlib import Path

_HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<link rel="stylesheet" href="style.css">"""

# Order matters: World before India. A story with a missing/unrecognised
# section (only possible in data/ predating this phase) falls into the
# untitled trailing group so old files keep rendering.
SECTION_LABELS = {"world": "World", "india": "India"}


def _story_html(story: dict) -> str:
    paragraphs = "".join(f"<p>{esc(p)}</p>" for p in story["body"].split("\n\n") if p.strip())

    sources = "".join(
        f'<li><a href="{esc(s["url"], quote=True)}">{esc(s["outlet"])}</a></li>' for s in story["sources"]
    )
    sources_html = (
        f'<div class="sources"><strong>Sources</strong><ul>{sources}</ul></div>' if sources else ""
    )

    vocab_items = "".join(
        f'<li><strong>{esc(v["term"])}</strong> ({esc(v["say"])}) — {esc(v["meaning"])}</li>'
        for v in story.get("vocab", [])
    )
    vocab_html = (
        f'<div class="vocab"><strong>Words to know</strong><ul>{vocab_items}</ul></div>'
        if vocab_items
        else ""
    )

    return (
        f'<article class="story">'
        f"<h3>{esc(story['headline'])}</h3>"
        f"{paragraphs}"
        f"{sources_html}"
        f"{vocab_html}"
        f"</article>"
    )


def _sections_html(stories: list[dict]) -> str:
    grouped: dict[str, list[dict]] = {}
    for s in stories:
        grouped.setdefault(s.get("section") or "", []).append(s)

    parts = []
    for key in (*SECTION_LABELS, ""):
        group = grouped.get(key)
        if not group:
            continue
        label = SECTION_LABELS.get(key)
        heading = f"<h2>{esc(label)}</h2>" if label else ""
        stories_html = "".join(_story_html(s) for s in group)
        parts.append(f'<section class="section">{heading}{stories_html}</section>')
    return "".join(parts)


def _page(day: dict) -> str:
    stories_html = _sections_html(day["stories"]) or "<p>No stories today.</p>"
    title = f"Follow — {day['date_label']}"
    return f"""<!doctype html>
<html lang="en">
<head>
<title>{esc(title)}</title>
{_HEAD}
</head>
<body>
<h1>Follow</h1>
<p class="date-label">{esc(day['date_label'])}</p>
{stories_html}
<p class="hard-close">That's all for today.</p>
<a class="archive-link" href="archive.html">Browse the archive</a>
</body>
</html>
"""


def _archive_page(days: list[dict]) -> str:
    items = "".join(
        f'<li><a href="{esc(d["date"], quote=True)}.html">{esc(d["date_label"])}</a></li>' for d in days
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<title>Follow — Archive</title>
{_HEAD}
</head>
<body>
<h1>Archive</h1>
<ul class="archive-list">{items}</ul>
<a class="archive-link" href="index.html">Back to today</a>
</body>
</html>
"""


def render_all(data_dir: Path, docs_dir: Path) -> None:
    """Load every data/*.json, render one HTML page per day plus index.html
    (newest day) and archive.html (full list, newest first)."""
    days = []
    for path in sorted(data_dir.glob("*.json")):
        days.append(json.loads(path.read_text()))
    days.sort(key=lambda d: d["date"], reverse=True)

    docs_dir.mkdir(parents=True, exist_ok=True)

    for day in days:
        (docs_dir / f"{day['date']}.html").write_text(_page(day))

    if days:
        (docs_dir / "index.html").write_text(_page(days[0]))
    else:
        (docs_dir / "index.html").write_text(
            "<!doctype html><html><body><p>No digest yet.</p></body></html>\n"
        )

    (docs_dir / "archive.html").write_text(_archive_page(days))
