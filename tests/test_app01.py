"""app01 (v0.15.0): the home page always shows the website (signed in: "Open my app"), the office wears Valeria One's look
for every business, and a business's own look stays on its own customer-facing pages and its own address."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from test_web import signup
from valeria_one import db
from valeria_one.api import app


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "on")


def _studio_business(client, host="studio.av.test"):
    """A design studio that asked for its own look everywhere (Website › its own look), with its own studio address."""
    from valeria_one.services import site as SI
    from valeria_one.web import studio as WS
    signup(client, industry="design", email="av@example.com")
    with db.tx() as conn:
        conn.execute(update(db.businesses).values(name="AV Interiorismo", slug="av-interiorismo", studio_host=host))
        b = dict(conn.execute(select(db.businesses)).mappings().one())
        SI.save(conn, b, "test", enabled=True, brand="1", color="#2a2623")
    WS._hosts["at"] = 0
    return b


# ---------------------------------------------------------------- the home page
def test_signed_in_home_shows_the_website_with_open_my_app(client, mk):
    signup(client)
    r = client.get("/", headers={"Accept-Language": "en-US"}, follow_redirects=False)
    assert r.status_code == 200, r.headers.get("location")                      # no more straight to /app
    t = r.text
    assert "/static/mk.css" in t and 'class="mk-nav"' in t
    assert re.search(r'<a class="btn primary" href="/app">Open my app</a>', t)  # the nav and the hero
    assert t.count('href="/app">Open my app') >= 2
    assert '<a class="si" href="/login">' not in t and '<a class="btn primary" href="/signup">Start free' not in t


def test_signed_in_mx_home_shows_the_website_with_abrir_mi_app(client, mk):
    signup(client)
    assert client.get("/", headers={"Accept-Language": "es-MX,es;q=0.9"}, follow_redirects=False).headers["location"] == "/mx"
    t = client.get("/mx").text
    assert t.count('href="/app">Abrir mi app') >= 2 and '<a class="si" href="/login">Entrar' not in t


def test_signed_out_home_keeps_sign_in_and_start_free(client, mk):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()
    t = client.get("/", headers={"Accept-Language": "en-US"}).text
    assert '<a class="si" href="/login">Sign in</a>' in t and 'href="/signup">Start free' in t and "Open my app" not in t


def test_public_pages_on_base_offer_the_app_when_signed_in(client, mk):
    signup(client)
    t = client.get("/pricing").text
    assert 'href="/app">' in t and ("Open my app" in t or "Abrir mi app" in t)


def test_sandbox_landing_stays_and_offers_the_app(client, monkeypatch):
    signup(client)
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "Try a demo business" in r.text and 'href="/app"' in r.text and "Open my app" in r.text


def test_server_without_a_website_still_opens_the_app(client):
    signup(client)                                                              # self-hosted, no marketing pages: its home is sign-in
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"


# ---------------------------------------------------------------- the business's own address is unchanged
def test_studio_address_home_is_unchanged(client):
    _studio_business(client)
    st = TestClient(app, raise_server_exceptions=False, base_url="https://studio.av.test")
    r = st.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/app"            # her address: her office first, never marketing
    assert st.get("/pricing", follow_redirects=False).headers["location"] == "https://one.test/pricing"
    login = st.get("/login").text
    assert 'class="branded"' in login and "AV Interiorismo" in login and "Cormorant Garamond" in login and "/static/vo.css" not in login


# ---------------------------------------------------------------- the office: always Valeria One
APP_PAGES = ["/app", "/app/inbox", "/app/contacts", "/app/calendar", "/app/pipeline", "/app/settings", "/app/connect", "/app/start",
             "/app/channels", "/app/valeria", "/app/grow", "/app/posts", "/app/google", "/app/tiktok", "/app/helpers",
             "/app/directory", "/app/website", "/app/projects", "/app/tasks", "/app/suppliers", "/app/library"]


def test_app_pages_load_the_valeria_one_stylesheet(client):
    signup(client)
    for p in APP_PAGES:
        r = client.get(p)
        if r.status_code != 200:                                               # a block this business doesn't use
            continue
        assert "/static/vo.css" in r.text and '<body class="vo">' in r.text, p
        assert "data-def','dark'" in r.text and 'class="vsw" data-mode' in r.text, p
        assert "Space+Grotesk" in r.text and "Geist" not in r.text, p


def test_business_branding_stays_out_of_the_office(client):
    _studio_business(client)
    for p in ("/app", "/app/inbox", "/app/contacts", "/app/settings", "/app/projects", "/app/library", "/studio"):
        t = client.get(p).text
        assert "/static/vo.css" in t and '<body class="vo">' in t, p
        assert 'class="branded"' not in t and "Cormorant" not in t and "sbrand" not in t and "--gold:#b08d57" not in t, p
        assert '<div class="brand"><img src="/static/icon.svg" alt="">Valeria One</div>' in t, p
        assert '<div class="biz"><b>AV Interiorismo</b>' in t, p               # the business: its name in the workspace header


def test_business_branding_stays_on_its_own_pages(client):
    from valeria_one.services import projects as PR
    b = _studio_business(client)
    site = client.get("/s/av-interiorismo").text
    assert 'class="branded"' in site and "#2a2623" in site and "/static/vo.css" not in site
    with db.tx() as conn:
        p = PR.create(conn, b, "test", {"name": "Casa Valle"})
    portal = client.get(f"/portal/{p['token']}").text
    assert 'class="branded"' in portal and "Cormorant Garamond" in portal and "/static/vo.css" not in portal
    st = TestClient(app, raise_server_exceptions=False, base_url="https://studio.av.test")
    tok = re.search(r'name="csrf" value="([^"]+)"', st.get("/login").text).group(1)
    st.post("/login", data={"csrf": tok, "email": "av@example.com", "password": "una-clave-larga"})
    studio = st.get("/studio").text                                             # her studio, at her own address: her look
    assert 'class="studio branded"' in studio and "Cormorant Garamond" in studio and "AV Interiorismo" in studio
    assert 'class="studio branded"' in st.get("/app/projects").text


def test_customer_pages_of_unbranded_businesses_are_unchanged(client):
    from valeria_one.services import site as SI
    signup(client)
    with db.tx() as conn:
        b = dict(conn.execute(select(db.businesses)).mappings().one())
        SI.save(conn, b, "test", enabled=True)
        slug = conn.execute(select(db.businesses.c.slug)).scalar()
    t = client.get(f"/s/{slug}").text
    assert "/static/vo.css" not in t and "data-def" not in t and "Geist" in t   # the business's own site: as before


def test_tokens_match_the_website():
    """The office and the website share one palette: the accent and the backgrounds are the same values."""
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "static"
    mk, vo = (static / "mk.css").read_text(), (static / "vo.css").read_text()
    for tok in ("--bg", "--panel", "--ink", "--muted", "--c1", "--c2", "--brand", "--btn-ink", "--glow", "--glow2"):
        a = re.findall(rf"{tok}:([^;]+);", mk)[:2]
        b = re.findall(rf"{tok}:([^;]+);", vo)[:2]
        assert a == b, (tok, a, b)
    assert "prefers-reduced-motion" in vo


@pytest.mark.parametrize("industry", ["design", "generic", "salon"])
def test_every_menu_link_opens(client, industry):
    """fix01: the menu built /app/ai_team while the page lives at /app/ai-team. Every link in the menu must open."""
    signup(client, industry=industry, email=f"menu-{industry}@example.com")
    nav = client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
    links = sorted(set(re.findall(r'href="(/app[^"]*)"', nav)))
    assert "/app/ai-team" in links or industry != "design"
    for href in links:
        r = client.get(href, follow_redirects=False)
        assert r.status_code in (200, 302, 303), (href, r.status_code)


def test_the_theme_switch_is_one_button_with_one_icon(client, mk):
    """mon01 (v0.16, Tad): one round button whose icon flips (a sun in dark mode, a moon in light mode), not a two-icon pill.
    Same vo-theme key; an accessible name, aria-pressed (= dark), a visible focus ring; nothing direction-dependent (RTL)."""
    from pathlib import Path
    signup(client)
    for path in ("/app", "/app/pipeline", "/", "/pricing", "/login"):
        t = client.get(path, headers={"Accept-Language": "en-US"}).text
        btns = re.findall(r'<button type="button" class="vsw" data-mode aria-pressed="true" aria-label="[^"]+"[^>]*>(.*?)</button>', t, re.S)
        assert btns, path
        assert all(b.count("<svg") == 2 and 'class="sun"' in b and 'class="moon"' in b for b in btns), path
    static = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "static"
    for name in ("mk.css", "vo.css"):
        css = (static / name).read_text()
        assert re.search(r':root\[data-theme="light"\] (\.vo )?\.vsw \.sun,:root:not\(\[data-theme="light"\]\) (\.vo )?\.vsw \.moon\{display:none\}', css), name
        assert ".vsw:focus-visible{outline:2px" in css and ".vsw::after" not in css and "direction:ltr" not in css.split(".vsw{")[1][:200], name
    js = (static / "app.js").read_text()
    assert "aria-pressed" in js and "localStorage.setItem(KEY,next)" in js and "var KEY='vo-theme'" in js
