"""The public pages of a server that sells Valeria One (VO_MARKETING=on): home, Mexico, search pages, legal."""
import json
import re

import pytest

from valeria_one.web import marketing


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "on")


def test_off_by_default(client):
    for p in ("/mx", "/crm-for/hvac", "/terms", "/sitemap.xml"):
        assert client.get(p).status_code == 404, p


def test_every_page_renders_true_prices(client, mk):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()
    for p in marketing.pages():
        r = client.get(p, headers={"Accept-Language": "en"}, follow_redirects=False)
        assert r.status_code == 200, (p, r.status_code)
        t = r.text
        assert not re.search(r"\$29\b", t), p                                      # the old price never shows
        if p.startswith("/mx") and p not in ("/mx/terminos", "/mx/privacidad"):
            assert "$249 + IVA" in t and 'lang="es"' in t, p
        elif p in ("/", "/missed-call-text-back") or p.startswith(("/crm-for", "/alternatives")):
            assert "$39" in t and "whole team" in t.lower(), p
    ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', client.get("/crm-for/plumbers").text, re.S)
    assert [json.loads(x)["@type"] for x in ld] == ["SoftwareApplication", "FAQPage"]


def test_home_follows_the_browser_language(client, mk):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()
    assert client.get("/", headers={"Accept-Language": "es-MX,es;q=0.9"}, follow_redirects=False).headers["location"] == "/mx"
    home = client.get("/", headers={"Accept-Language": "en-US"})
    assert "The front office you can’t afford to hire." in home.text
    assert client.get("/start", follow_redirects=False).headers["location"] == "/signup"


def test_sitemap_robots_and_legal(client, mk):
    sm = client.get("/sitemap.xml").text
    assert sm.count("<loc>") == len(marketing.pages()) and "/crm-for/hvac" in sm and "/mx/privacidad" in sm
    assert "Disallow: /app" in client.get("/robots.txt").text
    t = client.get("/terms").text
    assert "draft, pending legal review" in t and "TCPA" in t and "API key" in t
    assert "Derechos ARCO" in client.get("/mx/privacidad").text
    assert "Reply STOP" in client.get("/texting-terms").text
    signup = client.get("/signup", headers={"Accept-Language": "en"}).text
    assert 'href="/terms"' in signup and 'href="/privacy"' in signup


# ---------------------------------------------------------------- Field Notes, check-up, promise, AI-readable pages
def test_new_pages_off_by_default(client):
    for p in ("/notes", "/mx/notas", "/score", "/promise", "/llms.txt", "/llms-full.txt", "/index.md", "/notes.md", "/feed.xml"):
        assert client.get(p).status_code == 404, p


def test_field_notes(client, mk):
    from valeria_one.web import notes
    idx = client.get("/notes").text
    assert all(n["title"].replace("'", "&#39;") in idx for n in notes.NOTES if n["lang"] == "en")
    page = client.get("/notes/google-reviews")
    assert page.status_code == 200 and "Key takeaways" in page.text and "every 90 days" in page.text
    ld = [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page.text, re.S)]
    assert [x["@type"] for x in ld] == ["Article", "FAQPage"] and ld[0]["inLanguage"] == "en"
    assert 'hreflang="es" href="http://testserver/mx/notas/resenas-google"' in page.text or "/mx/notas/resenas-google" in page.text
    es = client.get("/mx/notas/whatsapp-para-negocios")
    assert es.status_code == 200 and 'lang="es"' in es.text and "24 horas" in es.text and "hreflang" not in es.text
    assert client.get("/notes/nope").status_code == 404 and client.get("/mx/notas/google-reviews").status_code == 404
    feed = client.get("/feed.xml")
    assert feed.headers["content-type"].startswith("application/rss+xml") and feed.text.count("<item>") == len(notes.NOTES)
    for n in notes.NOTES:                                           # every pair points at a real note in the other language
        if n["pair"]:
            assert ("es" if n["lang"] == "en" else "en", n["pair"]) in notes.BY, n["slug"]


def test_score_is_computed_and_nothing_is_stored(client, mk):
    from valeria_one.web import notes
    form = client.get("/score").text
    assert form.count('type="radio"') == sum(len(q[4]) for q in notes.QUESTIONS) + 4
    best = {q[0]: str(q[1].index(max(q[1]))) for q in notes.QUESTIONS}
    assert notes.score(best) == {"score": 100, "band": "strong", "gaps": [], "missed_month": None}
    worst = notes.score({q[0]: "0" for q in notes.QUESTIONS} | {"n_calls": "40", "miss": "2"})
    assert worst["score"] == 0 and worst["band"] == "leaking" and len(worst["gaps"]) == 3
    assert worst["gaps"][0][0] == "Missed calls go nowhere" and worst["missed_month"] == 43          # 40 × ¼ × 52 ÷ 12
    assert notes.score({"calls": "9", "speed": "x", "n_calls": "-5", "miss": "7"})["score"] == 0   # junk in, nothing breaks
    r = client.post("/mx/diagnostico", data={q[0]: "0" for q in notes.QUESTIONS} | {"n_calls": "40", "miss": "2"})
    assert r.status_code == 200 and "0<small" in r.text and "≈ 43" in r.text and "Las llamadas perdidas se pierden" in r.text


def test_promise(client, mk):
    t = client.get("/promise").text
    assert "Our promise" in t and "No invented numbers" in t and "$39" in t
    assert "Sin números inventados" in client.get("/mx/promesa").text


def test_markdown_twins_and_llms(client, mk):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()
    home = client.get("/index.md")
    assert home.status_code == 200 and home.headers["content-type"].startswith("text/markdown")
    md = home.text
    assert md.startswith("# Valeria One") and "Canonical URL: http" in md and "<" not in md.split("---", 1)[1].replace("<small", "")
    assert "## What works today" in md and "**One inbox**" in md and "$39" in md
    note = client.get("/notes/missed-calls.md").text
    assert "## Ring first, then text" in note and "- Many callers" in note and "](http" in note
    alt = client.get("/alternatives/podium.md").text
    assert "| Price |" in alt and "|---|" in alt
    assert "$249 + IVA" in client.get("/mx.md").text
    for bad in ("/app.md", "/settings.md", "/nope.md", "/v1/contacts.md"):
        assert client.get(bad).status_code == 404, bad
    idx = client.get("/llms.txt").text
    assert idx.startswith("# Valeria One") and "USD 39" in idx and "MXN 249" in idx and "/index.md" in idx and "/notes/google-reviews" in idx
    full = client.get("/llms-full.txt").text
    assert full.count("Canonical URL:") == len(marketing.pages()) and "Sin números inventados" in full
    rb = client.get("/robots.txt").text
    assert "User-agent: GPTBot" in rb and "User-agent: ClaudeBot" in rb and rb.count("Disallow: /app") == 1 + rb.count("User-agent:") - 1
    assert "/llms.txt" in rb
