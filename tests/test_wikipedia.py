from datetime import date

from wikipedia import page_title, parse_wikitext

# A miniature fixture shaped like the real Current Events Portal wikitext
# (research.md §2.5): a category heading, uncited scaffolding bullets that
# only exist to nest a topic, and one cited leaf bullet that is the actual
# event. Only the cited leaf should turn into a WikiEvent.
_FIXTURE_WIKITEXT = """{{Current events|year=2026|month=07|day=24|content=
<!-- All news items below this line -->
'''Armed conflicts and attacks'''
*[[Middle Eastern crisis (2023–present)|Middle Eastern crisis]]
**[[2026 Iran war]]
***[[Bahrain]]'s [[Bahrain Defence Force|military]] says that they have intercepted a number of [[Ballistic missile program of Iran|missiles]] and drones from [[Iran]]. [https://www.arabnews.com/node/2652109/middle-east (''Arab News'')]
}}"""


def test_page_title_has_no_zero_padding():
    assert page_title(date(2026, 7, 4)) == "2026_July_4"
    assert page_title(date(2026, 7, 24)) == "2026_July_24"


def test_parse_wikitext_keeps_only_cited_leaf_bullets():
    events = parse_wikitext(_FIXTURE_WIKITEXT)
    assert len(events) == 1
    event = events[0]
    assert event.category == "Armed conflicts and attacks"
    assert event.topic == "Middle Eastern crisis"


def test_parse_wikitext_strips_links_and_citations():
    events = parse_wikitext(_FIXTURE_WIKITEXT)
    text = events[0].text
    assert "Bahrain's military says" in text
    assert "[[" not in text
    assert "http" not in text
