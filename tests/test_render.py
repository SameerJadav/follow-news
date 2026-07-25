"""Tests for the one fragile edge in rendering: character-offset arithmetic.

meta-plan.md keeps tests to fragile edges only and explicitly excludes
rendering. This file is a deliberate, narrow exception: `_prose_html` splices
source markers into prose using offsets that come from LLM output, and getting
that wrong garbles or silently drops body text rather than raising. Nothing here
tests typography, copy or markup shape.

The stale banner is covered too, because it is the one render decision that
depends on state outside the data file.
"""

from __future__ import annotations

import json
import re
from datetime import date
from html import unescape
from pathlib import Path

import render

URL_A = "https://example.com/a"
URL_B = "https://example.com/b?x=1&y=2"

SRC_INDEX = {URL_A: 1, URL_B: 2}
CLAIMS = {1: "Claim one text.", 2: "Claim two text."}


def _strip(html: str) -> str:
    """The rendered prose with every marker element and tag removed, so it can
    be compared against the body it came from."""
    without_markers = re.sub(r'<a class="src".*?</a>', "", html, flags=re.S)
    return unescape(re.sub(r"<[^>]+>", "", without_markers.replace("</p><p>", "\n\n")))


def _day(stories: list[dict], day_date: str = "2026-07-25") -> dict:
    return {
        "date": day_date,
        "date_label": "Saturday, 25 July 2026",
        "generated_at": f"{day_date}T13:10:19Z",
        "stories": stories,
    }


def _story(**over: object) -> dict:
    story = {
        "section": "world",
        "headline": "A headline",
        "body": "AAA. BBB.",
        "markers": [
            {"start": 0, "end": 4, "claim_id": 1, "outlet": "Outlet A", "url": URL_A},
            {"start": 5, "end": 9, "claim_id": 2, "outlet": "Outlet B", "url": URL_B},
        ],
        "claims": [
            {"id": 1, "text": "Claim one text.", "outlet": "Outlet A", "url": URL_A},
            {"id": 2, "text": "Claim two text.", "outlet": "Outlet B", "url": URL_B},
        ],
        "thin_sourced": False,
        "sources": [
            {"outlet": "Outlet A", "url": URL_A},
            {"outlet": "Outlet B", "url": URL_B},
        ],
        "vocab": [{"term": "ceasefire", "say": "SEES-fy-er", "meaning": "a pause in fighting"}],
        "signals": {"tier": "lead", "claim_outlets": 2},
    }
    story.update(over)
    return story


# ---------- marker splicing ----------


def test_prose_splices_markers_in_document_order():
    body = "AAA. BBB."
    markers = [
        {"start": 0, "end": 4, "claim_id": 1, "outlet": "Outlet A", "url": URL_A},
        {"start": 5, "end": 9, "claim_id": 2, "outlet": "Outlet B", "url": URL_B},
    ]
    html = render._prose_html(body, markers, SRC_INDEX, CLAIMS)

    assert html.index("<sup>1</sup>") < html.index("<sup>2</sup>")
    assert "AAA.<a class=\"src\"" in html
    assert _strip(html) == body


def test_prose_carries_claim_text_and_outlet_for_the_tapped_span():
    html = render._prose_html(
        "AAA. BBB.",
        [{"start": 5, "end": 9, "claim_id": 2, "outlet": "Outlet B", "url": URL_B}],
        SRC_INDEX,
        CLAIMS,
    )
    assert 'data-claim="Claim two text."' in html
    assert 'data-outlet="Outlet B"' in html
    # The ampersand in URL_B must be escaped inside the attribute.
    assert "y=2" in html and "&amp;y=2" in html


def test_prose_escapes_body_text():
    body = 'A <b>bold</b> claim & "quoted".'
    html = render._prose_html(body, [], SRC_INDEX, CLAIMS)

    assert "<b>" not in html
    assert "&lt;b&gt;" in html
    assert "&amp;" in html
    assert _strip(html) == body


def test_prose_preserves_paragraph_breaks_and_offsets():
    body = "First para one. Second sentence.\n\nSecond para here."
    # The second paragraph starts at 34: 32 characters, then the two-character
    # "\n\n" separator.
    assert body[34:51] == "Second para here."
    markers = [
        {"start": 0, "end": 15, "claim_id": 1, "outlet": "Outlet A", "url": URL_A},
        {"start": 34, "end": 51, "claim_id": 2, "outlet": "Outlet B", "url": URL_B},
    ]
    html = render._prose_html(body, markers, SRC_INDEX, CLAIMS)

    assert html.count("<p>") == 2
    assert html.count('class="src"') == 2
    assert _strip(html) == body


def test_prose_survives_bad_markers_without_corrupting_text():
    body = "AAA. BBB. CCC."
    markers = [
        {"start": 0, "end": 4, "claim_id": 1, "outlet": "Outlet A", "url": URL_A},
        {"start": 2, "end": 8, "claim_id": 2, "outlet": "Outlet B", "url": URL_B},  # overlaps
        {"start": 9, "end": 9, "claim_id": 1, "outlet": "Outlet A", "url": URL_A},  # empty
        {"start": 10, "end": 999, "claim_id": 2, "outlet": "Outlet B", "url": URL_B},  # past end
        {"start": "x", "end": None, "claim_id": 1, "outlet": "Outlet A", "url": URL_A},  # junk
    ]
    html = render._prose_html(body, markers, SRC_INDEX, CLAIMS)

    # Only the first marker is usable; the body must still come through whole.
    assert html.count('class="src"') == 1
    assert _strip(html) == body


def test_prose_skips_markers_with_no_resolvable_source():
    body = "AAA. BBB."
    markers = [{"start": 0, "end": 4, "claim_id": 1, "outlet": "Nowhere", "url": "https://unknown/"}]
    html = render._prose_html(body, markers, SRC_INDEX, CLAIMS)

    assert 'class="src"' not in html
    assert _strip(html) == body


def test_marker_numeral_matches_position_in_sources():
    html = render._story_html(_story(), 1, "2026-07-25")
    a_pos, b_pos = html.index("<sup>1</sup>"), html.index("<sup>2</sup>")

    assert a_pos < b_pos
    # Numeral 2 belongs to the second entry of `sources`.
    marker_b = re.search(r'<a class="src"[^>]*data-outlet="Outlet B"[^>]*>(<sup>\d+</sup>)', html)
    assert marker_b is not None
    assert marker_b.group(1) == "<sup>2</sup>"


# ---------- story chrome ----------


def test_thin_sourced_notice_sits_between_headline_and_prose():
    """Top of the story, but after the headline — it conditions how the prose is
    read, so it must come before the prose and not before the headline."""
    html = render._story_html(
        _story(thin_sourced=True, signals={"tier": "secondary", "claim_outlets": 1}), 1, "2026-07-25"
    )
    assert "Only 1 outlet reported" in html
    assert html.index('class="story-hd"') < html.index('class="thin"') < html.index('class="prose"')


def test_no_thin_badge_when_not_flagged():
    assert 'class="thin"' not in render._story_html(_story(), 1, "2026-07-25")


def test_vocab_button_speaks_the_term_not_the_respelling():
    html = render._story_html(_story(), 1, "2026-07-25")
    assert 'data-term="ceasefire"' in html
    assert 'data-term="SEES-fy-er"' not in html
    assert "SEES-fy-er" in html  # still shown as text


def test_story_parts_are_in_reading_order():
    html = render._story_html(_story(), 1, "2026-07-25")
    order = ["story-hd", "prose", "vocab", "sources", "story-actions"]
    positions = [html.index(f'class="{name}"') for name in order]
    assert positions == sorted(positions), dict(zip(order, positions))


def test_sources_are_one_row_with_no_punctuation_separators():
    html = render._story_html(_story(), 1, "2026-07-25")
    row = html[html.index('class="sources"') : html.index('class="story-actions"')]
    assert row.count('class="source"') == 2
    assert "·" not in row and ", " not in row


def test_story_footer_carries_a_follow_button_when_not_followed():
    html = render._story_html(_story(), 1, "2026-07-25")
    assert '<footer class="story-actions">' in html
    assert 'class="follow-btn"' in html
    assert "issues/new?" in html
    assert 'data-date="2026-07-25"' in html
    assert 'data-headline="A headline"' in html


def test_story_footer_links_to_the_followed_page_when_already_followed():
    followed_index = {("2026-07-25", "world", 1): 12}
    html = render._story_html(_story(), 1, "2026-07-25", followed_index)
    assert 'class="follow-btn is-on" href="follow-12.html"' in html
    assert "issues/new?" not in html


# ---------- sections and hard close ----------


def test_sections_are_separate_and_each_closes():
    html = render._page(
        _day([_story(), _story(section="india", headline="India headline")]),
        date(2026, 7, 25),
        is_index=True,
    )
    assert '<section class="section" id="world"' in html
    assert '<section class="section" id="india"' in html
    # Neither section is pre-hidden, so a page without JavaScript stays readable.
    assert "hidden" not in html[html.index('id="world"') : html.index('id="world"') + 80]
    assert html.count('class="close"') == 2
    # unescape because esc() turns the apostrophe into &#x27;
    assert "That's all for World — 1 story." in unescape(html)
    assert "That's all for India — 1 story." in unescape(html)


def test_empty_section_says_so_rather_than_padding():
    html = render._page(_day([_story()]), date(2026, 7, 25), is_index=True)
    # The honest message is itself the section's close — no filler story, and no
    # second terminal line to read past.
    assert "Nothing big enough in India today." in html
    assert '<span class="tab-n">0</span>' in html
    assert html.count('class="close"') == 2


def test_unknown_section_falls_back_to_world():
    """data/ predating Phase 2 has no `section`; those files must still render."""
    html = render._page(_day([_story(section="")]), date(2026, 7, 25), is_index=True)
    assert "A headline" in html
    assert "That's all for World — 1 story." in unescape(html)


# ---------- stale banner ----------


def _render(tmp_path: Path, day: dict, today: date) -> dict[str, str]:
    data_dir, docs_dir = tmp_path / "data", tmp_path / "docs"
    data_dir.mkdir()
    (data_dir / f"{day['date']}.json").write_text(json.dumps(day))
    render.render_all(data_dir, docs_dir, today)
    return {p.name: p.read_text() for p in docs_dir.iterdir()}


def _stale_tag(html: str) -> str:
    match = re.search(r"<div class=\"stale\" id=\"stale\"[^>]*>", html)
    assert match is not None, "stale banner markup missing"
    return match.group(0)


def test_stale_banner_visible_when_newest_data_is_not_today(tmp_path):
    pages = _render(tmp_path, _day([_story()]), date(2026, 7, 26))
    tag = _stale_tag(pages["index.html"])

    assert "hidden" not in tag
    assert 'data-digest-date="2026-07-25"' in tag
    assert "Today's digest isn't ready yet" in pages["index.html"]
    assert "last updated 6:40pm" in pages["index.html"]


def test_stale_banner_hidden_when_data_is_today(tmp_path):
    pages = _render(tmp_path, _day([_story()]), date(2026, 7, 25))
    # Markup is always present so app.js can reveal it later from the phone's
    # own clock without needing a re-render.
    assert "hidden" in _stale_tag(pages["index.html"])


def test_dated_permalink_has_no_stale_banner(tmp_path):
    pages = _render(tmp_path, _day([_story()]), date(2026, 7, 26))
    assert 'id="stale"' not in pages["2026-07-25.html"]


def test_unparseable_generated_at_drops_only_the_time_clause(tmp_path):
    day = _day([_story()])
    day["generated_at"] = "not a timestamp"
    pages = _render(tmp_path, day, date(2026, 7, 26))

    assert "Today's digest isn't ready yet" in pages["index.html"]
    assert "last updated" not in pages["index.html"]


# ---------- archive ----------


def test_archive_lists_days_newest_first(tmp_path):
    data_dir, docs_dir = tmp_path / "data", tmp_path / "docs"
    data_dir.mkdir()
    for day_date in ("2026-06-30", "2026-07-24", "2026-07-25"):
        (data_dir / f"{day_date}.json").write_text(json.dumps(_day([_story()], day_date)))
    render.render_all(data_dir, docs_dir, date(2026, 7, 25))

    archive = (docs_dir / "archive.html").read_text()
    order = [m.group(1) for m in re.finditer(r'<a href="(\d{4}-\d{2}-\d{2})\.html"', archive)]

    assert order == ["2026-07-25", "2026-07-24", "2026-06-30"]
    assert archive.index("July 2026") < archive.index("June 2026")
    assert archive.count('class="month"') == 2
    # index.html is the newest day.
    assert "Saturday, 25 July 2026" in (docs_dir / "index.html").read_text()


def test_empty_data_dir_still_writes_a_page(tmp_path):
    data_dir, docs_dir = tmp_path / "data", tmp_path / "docs"
    data_dir.mkdir()
    render.render_all(data_dir, docs_dir, date(2026, 7, 25))

    assert "No digest yet." in (docs_dir / "index.html").read_text()
    assert (docs_dir / "archive.html").exists()


def test_asset_hrefs_are_versioned_by_content(tmp_path):
    """A phone holding a cached stylesheet must not render new markup against it."""
    data_dir, docs_dir = tmp_path / "data", tmp_path / "docs"
    data_dir.mkdir()
    (data_dir / "2026-07-25.json").write_text(json.dumps(_day([_story()])))
    docs_dir.mkdir()
    css = docs_dir / "style.css"
    css.write_text("body{}")

    render.render_all(data_dir, docs_dir, date(2026, 7, 25))
    first = re.search(r'href="style\.css\?v=(\w+)"', (docs_dir / "index.html").read_text())
    assert first is not None

    css.write_text("body{color:red}")
    render.render_all(data_dir, docs_dir, date(2026, 7, 25))
    second = re.search(r'href="style\.css\?v=(\w+)"', (docs_dir / "index.html").read_text())
    assert second is not None and second.group(1) != first.group(1)


def test_missing_asset_falls_back_to_a_plain_href(tmp_path):
    pages = _render(tmp_path, _day([_story()]), date(2026, 7, 25))
    # No style.css exists in this tmp docs dir, so the href stays unversioned
    # rather than the render failing.
    assert 'href="style.css"' in pages["index.html"]


def test_render_never_writes_hand_written_assets(tmp_path):
    """style.css, app.js, the manifest and the icons are hand-written statics;
    the pipeline must never emit or overwrite them."""
    pages = _render(tmp_path, _day([_story()]), date(2026, 7, 25))
    assert set(pages) == {"index.html", "archive.html", "2026-07-25.html"}


# ---------- noindex (Phase 6: product.md promises the site asks search
# engines not to index it; GitHub Pages does not do this on its own) ----------

_NOINDEX = 'name="robots" content="noindex, nofollow"'
_FOLLOW_RECORD = {
    "issue": 1,
    "status": "active",
    "title": "A Followed Story",
    "section": "world",
    "origin": {"date": "2026-07-25", "section": "world", "position": 1, "headline": "A Followed Story"},
    "started_at": "2026-07-25T12:00:00Z",
    "last_development": "2026-07-25",
    "backstory": {
        "body": "Backstory prose.",
        "markers": [],
        "sources": [],
        "queries": [],
        "search_suggestions": "",
    },
    "timeline": [],
}


def test_every_page_type_is_noindex(tmp_path):
    """Every generated page type carries the noindex meta tag -- robots.txt
    alone only covers well-behaved crawlers; this is the tag GitHub Pages
    gives no other way to set (no header config, no X-Robots-Tag)."""
    data_dir, docs_dir, followed_dir = tmp_path / "data", tmp_path / "docs", tmp_path / "followed"
    data_dir.mkdir()
    followed_dir.mkdir()
    (data_dir / "2026-07-25.json").write_text(json.dumps(_day([_story()])))
    (followed_dir / "1.json").write_text(json.dumps(_FOLLOW_RECORD))

    render.render_all(data_dir, docs_dir, date(2026, 7, 25), followed_dir)

    for name in ("index.html", "archive.html", "2026-07-25.html", "follow-1.html", "following.html"):
        assert _NOINDEX in (docs_dir / name).read_text(), f"{name} missing noindex"


def test_empty_page_is_noindex():
    assert _NOINDEX in render._empty_page()


def test_robots_txt_present_and_disallows_all():
    robots = (Path(__file__).parent.parent / "docs" / "robots.txt").read_text()
    assert "User-agent: *" in robots
    assert "Disallow: /" in robots
