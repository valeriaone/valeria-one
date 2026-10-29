import json
import re

import httpx
import pytest
from sqlalchemy import select, update

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import booking as BK
from valeria_one.services import businesses as B
from valeria_one.services import directory as DR
from valeria_one.services import site as SI

ALL_WEEK = {d: [["09:00", "18:00"]] for d in BK.DAYS}
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in these tests may reach the internet: only in-process mock transports answer."""
    def refuse(self, request):
        raise RuntimeError(f"real network call in a test: {request.url}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    from valeria_one.web import directory as WD
    WD._contact_hits.clear()


def owner_session(client, biz):
    from valeria_one.services import users as U
    email = f"owner-{str(biz['id'])[:6]}@example.com"
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email=email, name="Dueña", password_hash=U._ph.hash("una-clave-larga"))
                           .returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid)
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    assert client.post("/login", data={"csrf": tok, "email": email, "password": "una-clave-larga"},
                       follow_redirects=False).headers["location"] == "/app"
    return tok


def shop(name="Estética Sol", country="MX", industry="salon", city="Mérida", sandbox=False, listed=True, **kw):
    biz, _ = make_business(country, industry, sandbox=sandbox, name=name)
    with db.tx() as conn:
        if listed:
            DR.save(conn, biz, "t", listed=True, city=city, summary=kw.pop("summary", "Cortes y color en el centro"), **kw)
        return B.get(conn, biz["id"])


def ld_of(html: str) -> dict:
    return json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))


# ---------------------------------------------------------------- opt-in, off by default
def test_listing_is_off_by_default(client):
    biz = shop(listed=False)
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", enabled=True, slug="estetica-sol", hours=ALL_WEEK)
        assert DR.settings(conn, biz["id"])["listed"] is False
    assert "Estética Sol" not in client.get("/directory").text
    assert client.get("/directory/estetica-sol").status_code == 404           # a booking page alone is not a listing


def test_listing_needs_a_city_and_gets_an_address(client):
    biz, _ = make_business("MX", "salon", name="Barbería Don Pepe")
    assert not biz["slug"]
    with db.tx() as conn:
        with pytest.raises(Invalid):
            DR.save(conn, biz, "t", listed=True)                              # no city: people nearby can't find it
        with pytest.raises(Invalid):
            DR.save(conn, biz, "t", listed=True, city="<b>Mérida</b>")
        DR.save(conn, biz, "t", listed=True, city="Mérida")
        biz = B.get(conn, biz["id"])
    assert biz["slug"] and client.get(f"/directory/{biz['slug']}").status_code == 200
    with db.tx() as conn:
        DR.save(conn, biz, "t", listed=False)                                 # off again: the page is gone
    assert client.get(f"/directory/{biz['slug']}").status_code == 404


# ---------------------------------------------------------------- search and filters
def test_search_by_words_type_city_and_country(client):
    a = shop("Estética Sol", city="Mérida", category="Salón de belleza")
    b = shop("Plomería Rápida", industry="contractor", city="Mérida", summary="Fugas y calentadores")
    c = shop("Austin Roofing", country="US", industry="contractor", city="Austin", summary="Roof repair")
    with db.tx() as conn:
        assert {r["name"] for r in DR.search(conn)["rows"]} == {a["name"], b["name"], c["name"]}
        assert [r["name"] for r in DR.search(conn, q="fugas")["rows"]] == [b["name"]]
        assert [r["name"] for r in DR.search(conn, q="belleza")["rows"]] == [a["name"]]          # its own category word
        assert {r["name"] for r in DR.search(conn, kind="contractor")["rows"]} == {b["name"], c["name"]}
        assert {r["name"] for r in DR.search(conn, city="mérida")["rows"]} == {a["name"], b["name"]}
        assert [r["name"] for r in DR.search(conn, country="us")["rows"]] == [c["name"]]
        assert DR.search(conn, q="%")["count"] == 0                                              # no LIKE wildcards from visitors
        fac = DR.facets(conn)
    assert fac["countries"] == ["MX", "US"] and set(fac["cities"]) == {"Mérida", "Austin"}
    page = client.get("/directory", params={"type": "contractor", "city": "Mérida"})
    assert page.status_code == 200 and "Plomería Rápida" in page.text and "Estética Sol" not in page.text
    assert '<meta name="robots" content="noindex,follow">' in page.text                         # filtered views aren't indexed
    home = client.get("/directory").text
    assert '<link rel="canonical" href="https://one.test/directory">' in home and "noindex" not in home
    ld = ld_of(home)
    assert ld["@type"] == "ItemList" and len(ld["itemListElement"]) == 3
    assert client.get("/directory?page=99").status_code == 200


def test_listing_page_seo_and_only_what_the_owner_chose(client):
    biz = shop("Estética Sol", city="Mérida", area="Centro", summary="Cortes y color")
    with db.tx() as conn:
        biz = SI.save(conn, biz, "t", enabled=True, phone="9991234567", whatsapp="9997654321", address="Calle 60 #123")
        biz = BK.save_settings(conn, biz, "t", enabled=True, hours=ALL_WEEK)
    page = client.get(f"/directory/{biz['slug']}")
    h = page.text
    assert page.status_code == 200 and "<title>Estética Sol" in h and "Mérida" in h and "Centro" in h
    assert f'<link rel="canonical" href="https://one.test/directory/{biz["slug"]}">' in h
    assert 'name="description"' in h and page.headers.get("cache-control", "").startswith("public")
    ld = ld_of(h)
    assert ld["@type"] == "LocalBusiness" and ld["address"]["addressLocality"] == "Mérida" and ld["address"]["addressCountry"] == "MX"
    assert "telephone" not in ld and "streetAddress" not in ld["address"] and "aggregateRating" not in ld
    assert "9991234567" not in h and "Calle 60" not in h and "wa.me" not in h       # nothing personal unless ticked
    assert f"/s/{biz['slug']}" in h and f"/b/{biz['slug']}" in h and "09:00" in h  # site, booking and hours (on)
    with db.tx() as conn:
        DR.save(conn, biz, "t", show_phone=True, show_whatsapp=True, show_address=True, show_hours=False)
    h = client.get(f"/directory/{biz['slug']}").text
    assert "+529991234567" in h and "wa.me/529997654321" in h and "Calle 60 #123" in h and "09:00" not in h
    assert ld_of(h)["telephone"] == "+529991234567"


def test_rating_comes_from_the_businesss_reviews(client):
    biz = shop()
    with db.tx() as conn:
        for i, stars in enumerate((5, 4, 5)):
            conn.execute(db.reviews.insert().values(business_id=biz["id"], source="google", external_id=f"r{i}", rating=stars))
    h = client.get(f"/directory/{biz['slug']}").text
    agg = ld_of(h)["aggregateRating"]
    assert agg["ratingValue"] == 4.7 and agg["reviewCount"] == 3 and "★" in h
    assert "4.7" in client.get("/directory").text


# ---------------------------------------------------------------- never listed: sandbox, paused, sandbox servers
def test_sandbox_businesses_never_appear(client, monkeypatch):
    real = shop("Real Shop")
    demo = shop("Demo Shop", sandbox=True)
    with db.tx() as conn:
        assert DR.settings(conn, demo["id"])["listed"] is True                # it may tick the box…
        assert [r["name"] for r in DR.search(conn)["rows"]] == ["Real Shop"]   # …but it is never shown
        assert all(demo["slug"] not in p for p in DR.sitemap_paths(conn))
    assert client.get(f"/directory/{demo['slug']}").status_code == 404
    assert "Demo Shop" not in client.get("/directory").text
    monkeypatch.setenv("VO_SANDBOX", "1")                                      # a sandbox server lists nobody at all
    with db.tx() as conn:
        assert DR.search(conn)["rows"] == [] and DR.sitemap_paths(conn) == []
    assert client.get(f"/directory/{real['slug']}").status_code == 404
    assert client.get("/directory").headers.get("x-robots-tag") == "noindex"


def test_a_paused_business_leaves_the_directory(client):
    biz = shop()
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(suspended_at=db.func.now()))
        assert DR.search(conn)["count"] == 0
    assert client.get(f"/directory/{biz['slug']}").status_code == 404


def test_sitemap_lists_the_directory(client):
    biz = shop()
    sm = client.get("/directory/sitemap.xml")
    assert sm.status_code == 200 and "https://one.test/directory</loc>" in sm.text and f"/directory/{biz['slug']}</loc>" in sm.text
    assert "Sitemap: https://one.test/directory/sitemap.xml" in client.get("/robots.txt").text


# ---------------------------------------------------------------- contact -> a lead in the inbox
def test_contact_from_the_listing_lands_as_a_directory_lead(client):
    biz = shop()
    form = client.get(f"/directory/{biz['slug']}").text
    tok = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    r = client.post(f"/directory/{biz['slug']}", data={"csrf": tok, "name": "Rosa", "reach": "9991112233", "message": "¿Tienen cita el sábado?"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?sent=1#contact")
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz["id"])).mappings().one()
        assert c["source"] == "directory" and c["phone"] == "+529991112233" and c["name"] == "Rosa"
        msg = conn.execute(select(db.messages.c.body, db.messages.c.direction)).one()
        assert msg.body == "¿Tienen cita el sábado?" and msg.direction == "in"
        assert conn.execute(select(db.events.c.id).where(db.events.c.type == "lead.directory")).first()
    from valeria_one.web.i18n import t
    from markupsafe import escape
    assert str(escape(t("site_sent", "en"))) in client.get(f"/directory/{biz['slug']}?sent=1", headers={"Accept-Language": "en"}).text
    client.post(f"/directory/{biz['slug']}", data={"csrf": tok, "name": "Bot", "reach": "b@example.com", "message": "x",
                                                   "website": "http://spam"})       # honeypot: dropped quietly
    with db.tx() as conn:
        assert conn.execute(select(db.func.count()).select_from(db.contacts)).scalar() == 1


def test_contact_form_is_rate_limited(client):
    biz = shop()
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get(f"/directory/{biz['slug']}").text).group(1)
    for i in range(11):
        client.post(f"/directory/{biz['slug']}", data={"csrf": tok, "name": f"P{i}", "reach": f"p{i}@example.com", "message": "hola"})
    with db.tx() as conn:
        assert conn.execute(select(db.func.count()).select_from(db.contacts)).scalar() == 10       # the 11th was refused


# ---------------------------------------------------------------- the owner's screen, every language
def test_owner_lists_the_business_from_settings(client):
    biz, _ = make_business("MX", "salon", name="Salón Lupita")
    owner_session(client, biz)
    s = client.get("/app/settings").text
    assert 'href="/app/directory"' in s and ("Not listed" in s or "No aparece" in s)
    page = client.get("/app/directory")
    assert page.status_code == 200 and 'name="listed"' in page.text and "checked" not in page.text.split('name="listed"')[1][:30]
    tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/app/directory", data={"csrf": tok, "listed": "1", "city": "Mérida", "category": "Estética", "summary": "Uñas y cejas",
                                            "show_hours": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/app/directory"
    with db.tx() as conn:
        s = DR.settings(conn, biz["id"])
        slug = B.get(conn, biz["id"])["slug"]
    assert s["listed"] and s["category"] == "Estética" and s["show_hours"] and not s["show_phone"]
    assert "Uñas y cejas" in client.get(f"/directory/{slug}").text
    assert 'href="/app/directory"' in client.get("/app/connect").text and "/app/directory" in client.get("/app/start").text


def test_every_language_renders_the_directory_pages(client):
    biz = shop()
    owner_session(client, biz)
    for lang in LANGS:
        tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)
        client.post(f"/lang/{lang}", data={"csrf": tok})
        for path in ("/directory", f"/directory/{biz['slug']}", "/app/directory", "/app/settings"):
            r = client.get(path)
            assert r.status_code == 200 and not re.search(r"\{(n|avg|city|business)\}", r.text), (lang, path)
            assert f'lang="{lang}"' in r.text, (lang, path)


def test_new_words_are_in_all_11_languages_and_translated():
    import pathlib
    files = {f.stem: json.loads(f.read_text()) for f in pathlib.Path("valeria_one/web/locales").glob("*.json")}
    assert set(LANGS) <= set(files)
    keys = [k for k in files["en"] if k.startswith(("dir_", "gb_", "po_", "tt_", "al_")) or k in (
        "posts", "google", "directory", "cx_google", "cx_directory_d", "nx_directory", "nx_google", "nx_posts", "blk_posts_d",
        "r_customer_first", "r_window_closed")]
    assert len(keys) > 150
    same_ok = {"po_net_instagram", "dir_site"}                                 # a brand name, or the same word in some languages
    for lang in LANGS:
        for k in keys:
            assert files[lang].get(k, "").strip(), (lang, k)
            if lang != "en" and k not in same_ok and len(files["en"][k]) > 12:
                assert files[lang][k] != files["en"][k], ("copied English", lang, k)
