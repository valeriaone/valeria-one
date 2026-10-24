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
