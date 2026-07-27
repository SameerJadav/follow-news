import extract
from extract import PARA_MIN, from_jsonld, from_paragraphs

# A fixture shaped like the real Times of India failure measured in
# research.md §2.4: JSON-LD carries the full article; the visible <p> tags
# carry only short author-bio boilerplate. The extraction cascade must pick
# the JSON-LD candidate, since paragraphs alone falls well under 1,000 chars.
_TOI_LIKE_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "NewsArticle", "headline": "Big Story",
   "articleBody": "%s"}
]}
</script>
</head>
<body>
<nav><p>Home | India | World | Sports | this paragraph is definitely long enough to pass the sixty character minimum but must never appear</p></nav>
<script>var x = "this is definitely long enough to pass the sixty character minimum but lives in a script tag";</script>
<p>Staff Reporter is a senior correspondent covering national affairs for over a decade now with many bylines.</p>
</body></html>
""" % (
    "The government today announced a major policy shift affecting millions of citizens nationwide. " * 14
)


def test_from_jsonld_finds_nested_article_body():
    body = from_jsonld(_TOI_LIKE_HTML)
    assert len(body) > 1000
    assert "policy shift" in body


def test_from_paragraphs_skips_script_and_nav():
    paras = from_paragraphs(_TOI_LIKE_HTML)
    assert "Home | India" not in paras
    assert "script tag" not in paras


def test_from_paragraphs_drops_short_fragments():
    html = "<p>too short</p><p>" + "x" * (PARA_MIN + 5) + "</p>"
    result = from_paragraphs(html)
    assert "too short" not in result
    assert "x" * (PARA_MIN + 5) in result


def test_times_of_india_acceptance_case():
    """The acceptance test named in the phase spec: paragraph extraction
    alone must fail (under ~700 chars of boilerplate) while JSON-LD succeeds
    (over 1,000 chars), and the cascade's longest-wins rule must pick it."""
    jsonld_text = from_jsonld(_TOI_LIKE_HTML)
    para_text = from_paragraphs(_TOI_LIKE_HTML)

    assert len(para_text) < 700
    assert len(jsonld_text) > 1000
    assert max(jsonld_text, para_text, key=len) == jsonld_text


def test_from_jsonld_unescapes_entities():
    html = """<script type="application/ld+json">
{"articleBody": "%s"}
</script>""" % (
        "Tension &amp; conflict rise as talks &quot;collapse&quot; entirely today in a long sentence."
    )
    body = from_jsonld(html)
    assert "&amp;" not in body
    assert "&" in body
    assert '"collapse"' in body


def test_via_jina_strips_markdown_header(monkeypatch):
    from extract import via_jina

    class FakeResp:
        status_code = 200
        text = "Title: X\nURL Source: https://x\nMarkdown Content:\nActual article text here."

    monkeypatch.setattr("extract.requests.get", lambda *a, **k: FakeResp())
    monkeypatch.setattr("extract.time.sleep", lambda *_: None)
    result = via_jina("https://example.com/a")
    assert result == "Actual article text here."


# ---------- the URL-keyed cache (dossier.md §10) ----------


def test_a_cache_hit_costs_no_fetch(tmp_path, monkeypatch):
    """Fourteen days of updates on one followed story would otherwise re-fetch
    the same background articles every morning, each costing a request and up
    to JINA_PAUSE seconds of enforced sleep."""
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return "<html><p>" + ("x" * 900) + "</p></html>", 200, 5, ""

    monkeypatch.setattr(extract, "_fetch", fake_fetch)
    extract.enable_cache(tmp_path / "extract.json")

    first = extract.article_text("https://e.example/1")
    second = extract.article_text("https://e.example/1")

    assert first == second
    assert len(calls) == 1


def test_the_cache_survives_a_restart(tmp_path, monkeypatch):
    def fake_fetch(url):
        return "<html><p>" + ("y" * 900) + "</p></html>", 200, 5, ""

    monkeypatch.setattr(extract, "_fetch", fake_fetch)
    extract.enable_cache(tmp_path / "extract.json")
    first = extract.article_text("https://e.example/2")

    def explode(url):
        raise AssertionError("a cached URL must not be fetched again in a later run")

    monkeypatch.setattr(extract, "_fetch", explode)
    extract.enable_cache(tmp_path / "extract.json")  # a fresh process would do this
    assert extract.article_text("https://e.example/2") == first


def test_caching_is_off_unless_enabled(monkeypatch, tmp_path):
    """The digest fetches today's articles once and would never see a hit, so
    it stays uncached and unaffected."""
    monkeypatch.setattr(extract, "_CACHE_PATH", None)
    monkeypatch.setattr(extract, "_CACHE", {})
    assert extract._cache_get("https://e.example/3") is None
    extract._cache_put("https://e.example/3", "text")
    assert extract._CACHE == {}
