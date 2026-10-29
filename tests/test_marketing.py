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
    assert sm.count("<loc>") == len(marketing.pages()) + 1 and "/crm-for/hvac" in sm and "/mx/privacidad" in sm
    assert "/directory</loc>" in sm                                         # the directory (and each business listed in it)
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


# ---------------------------------------------------------------- a server that doesn't charge (VO_BILLING off): free, everywhere
OUR_PRICE = re.compile(r"\$39 a month|\$39 al mes|US\$39|USD 39|\$249|MXN 249|\+ IVA|12 months free|12 meses gratis|30 days free|30 días gratis"
                       r"|months free|meses gratis|launch offer|oferta de lanzamiento|flat price|one price|un solo precio|precio fijo"
                       r"|stop paying|dejas de pagar|free plan|plan gratis|pricing page|página de precios|then \$")


@pytest.fixture
def free_mk(monkeypatch):
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "off")


def test_free_server_says_free_on_every_page(client, free_mk):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()
    for p in marketing.pages():
        r = client.get(p, headers={"Accept-Language": "es" if p.startswith("/mx") else "en"}, follow_redirects=False)
        assert r.status_code == 200, (p, r.status_code)
        t = r.text
        assert not OUR_PRICE.search(t), (p, OUR_PRICE.search(t).group(0))
        if p in ("/terms", "/privacy", "/texting-terms", "/pricing", "/mx/terminos", "/mx/privacidad"):
            continue
        assert ("Gratis" in t and 'lang="es"' in t) if p.startswith("/mx") else "Free" in t, p
    for p in ("/", "/crm-for/plumbers", "/alternatives/podium", "/mx", "/mx/crm-para/esteticas"):
        t = client.get(p, headers={"Accept-Language": "en"}).text
        ld = [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', t, re.S)]
        assert ld[0]["@type"] == "SoftwareApplication" and ld[0]["offers"]["price"] == "0", p
        for q in ld[1]["mainEntity"]:                                           # the FAQ data says what the page says
            assert q["name"].replace("'", "&#39;") in t and q["acceptedAnswer"]["text"].replace("'", "&#39;") in t, (p, q["name"])
    home = client.get("/", headers={"Accept-Language": "en"}).text
    assert '<div class="amt">Free</div>' in home and "Is it really free?" in home and 'href="/pricing">Free</a>' in home
    assert '<div class="amt">Gratis</div>' in client.get("/mx").text
    alt = client.get("/alternatives/keap").text
    assert "Free, whole team" in alt and "Free for every user" in alt and "$299 a month" in alt      # their price stays theirs
    assert "Free for the whole team" in client.get("/alternatives/hubspot").text


def test_free_pricing_page(client, free_mk):
    en = client.get("/pricing", headers={"Accept-Language": "en"})
    assert en.status_code == 200 and "Valeria One is free." in en.text and "No card." in en.text and 'href="/signup"' in en.text
    es = client.get("/pricing", headers={"Accept-Language": "es"}).text
    assert "Valeria One es gratis." in es and "Regístrate gratis" in es
    for t in (en.text, es):
        assert not re.search(r"\$39|39\.00|249|IVA|12 months|12 meses", t)


def test_free_terms_promise_llms_and_signup(client, free_mk):
    from valeria_one.web import aisearch
    t = client.get("/terms").text
    assert "5. Price" in t and "Valeria One is free." in t and "version 2026-10-29" in t
    assert "stop paying" not in t and "free plan" not in t and "12 months" not in t and "/pricing" not in t
    es = client.get("/mx/terminos").text
    assert "5. Precio" in es and "Valeria One es gratis." in es and "dejas de pagar" not in es and "12 meses" not in es
    pr = client.get("/promise").text
    assert "Free, in the open" in pr and "free plan" not in pr and "stop paying" not in pr
    assert "Gratis, a la vista" in client.get("/mx/promesa").text
    idx = client.get("/llms.txt").text
    assert "Free for everyone" in idx and not OUR_PRICE.search(idx) and "USD 39" not in idx
    aisearch._FULL.clear()                                       # the 10-minute cache may hold another test's server
    full = client.get("/llms-full.txt").text
    m = OUR_PRICE.search(full)
    assert "Valeria One is free." in full and not m, full[m.start() - 300:m.end() + 50] if m else ""
    aisearch._FULL.clear()
    signup = client.get("/signup", headers={"Accept-Language": "en"}).text
    assert 'id="offer"' not in signup and "Launch offer" not in signup and 'href="/terms"' in signup


def test_charging_server_keeps_its_prices_on_terms_and_promise(client, mk):
    t = client.get("/terms").text
    assert "5. Price and payment" in t and "pricing page" in t and "version 2026-10-24" in t
    assert "One price, in the open" in client.get("/promise").text
    assert "Valeria One is free" not in client.get("/").text


def test_every_free_line_replaces_a_real_line():
    seen = set()

    def walk(x):
        if isinstance(x, str):
            seen.add(x)
        elif isinstance(x, (list, tuple, dict)):
            for v in (x.values() if isinstance(x, dict) else x):
                walk(v)
    walk(marketing.US)
    walk(marketing.ALT)
    assert set(marketing.FREE_SAYS) <= seen
