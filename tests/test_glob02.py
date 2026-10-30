"""glob02 (v0.17.0): the website speaks all 11 languages — the 🌐 picker, a home page per language, landing by
the visitor's choice or browser (never for crawlers), hreflang + sitemap, /countries, worldwide wording."""
import json
import re

import pytest

from valeria_one.web import day, marketing, world
from valeria_one.web.i18n import NATIVE, UI_LANGS

NEW = {"fil": "/fil", "hi": "/hi", "ar": "/ar", "id": "/id", "pt": "/pt", "fr": "/fr", "de": "/de", "ja": "/ja", "zh": "/zh"}
CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


@pytest.fixture
def free_mk(monkeypatch):
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "off")


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "on")


def _ready(client):
    from test_selfhost import do_setup
    do_setup(client)
    client.cookies.clear()


def test_the_scheme_is_the_apps_language_codes():
    assert set(world.HOMES) == set(UI_LANGS) and world.HOMES["en"] == "/" and world.HOMES["es"] == "/mx"
    assert {c: world.HOMES[c] for c in NEW} == NEW
    assert world.page_dir("ar") == "rtl" and all(world.page_dir(c) == "ltr" for c in UI_LANGS if c != "ar")


def test_every_language_page_renders_with_its_lang_dir_hreflang_and_canonical(client, free_mk):
    _ready(client)
    for code, path in world.HOMES.items():
        r = client.get(path, headers={"Accept-Language": "en", "User-Agent": CHROME}, follow_redirects=False)
        assert r.status_code == 200, (path, r.status_code)
        t = r.text
        hl = world.html_lang(code)
        assert re.search(rf'<html lang="{hl}"( dir="rtl")?>', t), path
        assert (' dir="rtl"' in t.split(">", 2)[1]) == (code == "ar"), path
        assert f'<link rel="canonical" href="https://one.test{path}">' in t, path
        for c2, p2 in world.HOMES.items():                                       # every language + x-default
            assert f'<link rel="alternate" hreflang="{world.html_lang(c2)}" href="https://one.test{p2}">' in t, (path, c2)
        assert '<link rel="alternate" hreflang="x-default" href="https://one.test/">' in t, path
        assert f"<h1>{world.copy(code)['h1']}</h1>" in t.replace("&#39;", "'"), path
        assert "data-day" in t and f'data-lang="{world.day_lang(code)}"' in t, path       # the live phone, in its language
        ld = [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', t, re.S)]
        assert ld[0]["offers"]["price"] == "0" and "aggregateRating" not in json.dumps(ld) and "review" not in json.dumps(ld).lower()


def test_scripts_get_their_own_faces_only_where_needed(client, free_mk):
    _ready(client)
    for code, face in (("ja", "Noto+Sans+JP"), ("zh", "Noto+Sans+SC"), ("hi", "Noto+Sans+Devanagari"), ("ar", "Noto+Sans+Arabic")):
        assert f"family={face}:" in client.get(world.HOMES[code]).text, code
    for path in ("/", "/mx", "/fr", "/crm-for/hvac"):
        assert "Noto+Sans" not in client.get(path, headers={"Accept-Language": "en"}).text, path


def test_the_picker_is_on_every_public_page_and_the_old_link_is_gone(client, free_mk):
    _ready(client)
    for p in marketing.pages() + ["/login", "/signup", "/pricing", "/directory", "/helpers"]:
        r = client.get(p, headers={"Accept-Language": "es" if p.startswith("/mx") else "en"}, follow_redirects=False)
        assert r.status_code == 200, p
        t = r.text
        assert t.count("<details class=\"wp\" data-wp>") == 1, p
        menu = t.split('<details class="wp" data-wp>')[1].split("</details>")[0]
        for c in world.ORDER:                                                    # all 11, each in its own name
            name = f'<bdi dir="rtl">{NATIVE[c]}</bdi>' if world.page_dir(c) == "rtl" else NATIVE[c]
            assert re.search(rf'<a href="[^"]*\?hl={c}" hreflang="{world.html_lang(c)}" lang="{world.html_lang(c)}"( aria-current="true")?>{name}</a>', menu), (p, c)
        assert menu.count('aria-current="true"') == 1, p
        assert "México · Español" not in t and "USA · English" not in t and 'class="lg"' not in t, p


def test_picking_a_language_is_remembered_and_beats_the_browser(client, free_mk):
    _ready(client)
    fr = {"Accept-Language": "fr-FR,fr;q=0.9", "User-Agent": CHROME}
    assert client.get("/", headers=fr, follow_redirects=False).headers["location"] == "/fr"
    r = client.get("/de?hl=de", headers=fr, follow_redirects=False)              # the 🌐 picker: remembered, then the clean address
    assert r.status_code == 303 and r.headers["location"] == "/de"
    assert client.get("/", headers=fr, follow_redirects=False).headers["location"] == "/de"
    assert client.get("/fr", headers=fr, follow_redirects=False).status_code == 200      # an address asked for is always shown
    r = client.get("/?hl=en", headers=fr, follow_redirects=False)                  # back to English: "/" stays English for them
    assert r.headers["location"] == "/"
    home = client.get("/", headers=fr, follow_redirects=False)
    assert home.status_code == 200 and 'lang="en"' in home.text and "Accept-Language" in home.headers.get("vary", "")
    assert client.get("/?hl=nope", headers=fr, follow_redirects=False).status_code == 200   # junk is ignored


def test_the_browser_picks_the_home_page_for_each_language(client, free_mk):
    _ready(client)
    browsers = {"es": "es-MX,es;q=0.9", "fil": "fil-PH", "hi": "hi-IN,hi;q=0.9,en;q=0.5", "ar": "ar-AE,ar;q=0.9", "id": "id-ID",
                "pt": "pt-BR,pt;q=0.9", "fr": "fr-CA", "de": "de-DE,de;q=0.9,en;q=0.8", "ja": "ja-JP", "zh": "zh-CN,zh;q=0.9"}
    for code, al in browsers.items():
        client.cookies.clear()
        r = client.get("/", headers={"Accept-Language": al, "User-Agent": CHROME}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == world.HOMES[code], (code, r.headers.get("location"))
    client.cookies.clear()
    assert client.get("/", headers={"Accept-Language": "tl-PH", "User-Agent": CHROME}, follow_redirects=False).headers["location"] == "/fil"
    for al in ("en-US", "ko-KR", "", "sw-KE,en;q=0.5"):                         # English, or a language we don't have: English
        r = client.get("/", headers={"Accept-Language": al, "User-Agent": CHROME}, follow_redirects=False)
        assert r.status_code == 200, al


def test_the_apps_own_language_choice_carries_to_the_website(client, free_mk):
    from test_selfhost import csrf
    _ready(client)
    tok = csrf(client, "/login")
    client.post("/lang", data={"csrf": tok, "code": "ja"}, follow_redirects=False)
    assert client.get("/", headers={"Accept-Language": "en", "User-Agent": CHROME}, follow_redirects=False).headers["location"] == "/ja"


def test_the_website_choice_carries_into_sign_up_and_the_app(client, free_mk):
    from valeria_one.web.i18n import t
    _ready(client)
    de = client.get("/de").text
    assert 'href="/signup?hl=de"' in de                                           # the page's own sign-up link carries it
    client.get("/signup?hl=de", headers={"Accept-Language": "en"})
    for p in ("/signup", "/login"):                                               # then every next page, in the browser's spite
        assert t("sign_in", "de") in client.get(p, headers={"Accept-Language": "en"}).text, p
    client.cookies.clear()
    page = client.get("/login?hl=pt", headers={"Accept-Language": "en"}).text   # the picker on a base.html page reopens it in pt
    assert t("sign_in", "pt") in page and 'href="/login?hl=fr"' in page


def test_crawlers_and_previews_are_never_redirected(client, free_mk):
    _ready(client)
    for ua in ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "facebookexternalhit/1.1",
               "Mozilla/5.0 (compatible; bingbot/2.0)", "WhatsApp/2.23.20.0", "Mozilla/5.0 AppleWebKit/537.36 (compatible; GPTBot/1.2)",
               "Twitterbot/1.0", "Slackbot-LinkExpanding 1.0", ""):
        r = client.get("/", headers={"Accept-Language": "fr-FR,fr", "User-Agent": ua}, follow_redirects=False)
        assert r.status_code == 200 and 'lang="en"' in r.text, ua
    assert not world.is_bot(type("R", (), {"headers": {"user-agent": CHROME}})())


def test_the_sitemap_and_llms_list_every_language_and_countries(client, free_mk):
    _ready(client)
    sm = client.get("/sitemap.xml").text
    for p in list(world.HOMES.values()) + ["/countries"]:
        assert f"<loc>https://one.test{p}</loc>" in sm, p
    idx = client.get("/llms.txt").text
    assert all(f"https://one.test{p}" in idx for p in NEW.values()) and "United States, English" not in idx
    assert client.get("/fr.md").status_code == 200 and "# Valeria One" in client.get("/ja.md").text


def test_en_and_es_aliases(client, free_mk):
    assert client.get("/en", follow_redirects=False).headers["location"] == "/"
    assert client.get("/es", follow_redirects=False).headers["location"] == "/mx"


def test_new_pages_are_off_without_marketing(client):
    for p in list(NEW.values()) + ["/countries", "/en", "/es"]:
        assert client.get(p, follow_redirects=False).status_code == 404, p


def _flat(routes):
    """Every route in the order the app tries them (FastAPI keeps included routers as one entry each)."""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _flat(inner.routes)
        elif hasattr(r, "path"):
            yield r


def test_no_route_collisions():
    """The new addresses reach their own page first, and no other page lives at or under them."""
    from starlette.routing import Match
    from valeria_one.api import app
    routes = list(_flat(app.routes))
    assert len(routes) > 200
    mine = set(NEW.values()) | {"/en", "/es", "/countries", "/mx"}
    for path in mine:
        scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
        first = next(r for r in routes if r.matches(scope)[0] == Match.FULL)
        assert first.path == path and first.endpoint.__module__ == "valeria_one.web.marketing", (path, first.path)
    for r in routes:
        for m in mine - {"/mx"}:                                                   # /mx/... (Mexico pages) predate glob02
            assert not r.path.startswith(m + "/"), (m, r.path)
    paths = [r.path for r in routes]
    for m in mine:
        assert paths.count(m) == 1, (m, paths.count(m))


def test_every_animation_language_has_the_full_key_set_and_local_money():
    d = day.data()
    en = set(d["strings"]["en"])
    assert set(d["strings"]) == set(day.LANGS) == set(d["meta"]) >= {"ar", "id", "pt", "fr", "de", "ja", "zh"}
    for lang, w in d["strings"].items():
        assert set(w) == en and all(v.strip() for v in w.values()), lang
        assert w["clockmon"].endswith("9:00"), lang
        for tag in ("<br>", '<span class="quote">', "<b>"):                     # the same markup as English
            assert "".join(d["strings"]["en"].values()).count(tag) == "".join(w.values()).count(tag), (lang, tag)
    for lang, money in (("ar", "درهم"), ("id", "Rp"), ("pt", "R$"), ("fr", "€"), ("de", "€"), ("ja", "円"), ("zh", "¥")):
        assert money in d["strings"][lang]["s2"], lang
    assert d["meta"]["ar"]["dir"] == "rtl" and day.still("ar")["dir"] == "rtl"
    for lang in ("ar", "id", "pt", "fr", "de", "ja", "zh"):
        assert d["meta"][lang]["video"] is False and not day.still(lang)["video"], lang      # no video yet: the link hides


def test_every_language_file_has_the_same_words():
    en = world.copy("en")
    for c in UI_LANGS:
        w = world.copy(c)
        assert list(w) == list(en) and w["name"] == NATIVE[c], c
        for k, v in en.items():
            if isinstance(v, list):
                assert len(w[k]) == len(v), (c, k)
            elif isinstance(v, str):
                for ph in ("{price}", "{free}", "{n}"):
                    assert v.count(ph) == w[k].count(ph), (c, k, ph)
        assert [r[0] for r in w["bento"]] == [r[0] for r in en["bento"]] and [r[0] for r in w["hub"]] == [r[0] for r in en["hub"]], c


def test_free_and_paying_servers_say_it_in_each_language(client, monkeypatch):
    _ready(client)
    monkeypatch.setenv("VO_MARKETING", "on")
    monkeypatch.setenv("VO_BILLING", "off")
    for code, path in NEW.items():
        t = client.get(path).text
        assert f'<div class="amt">{world.copy(code)["price_free"]}</div>' in t and "US$" not in t and "{" + "price}" not in t, code
        assert "Soon" not in t or code == "en", code                              # their own word for "Soon"
    monkeypatch.setenv("VO_BILLING", "on")
    for code, path in NEW.items():
        t = client.get(path).text
        assert '<div class="amt">US$39</div>' in t and "30" in t and "{free}" not in t, code


def test_countries_page_lists_every_pack(client, free_mk):
    from valeria_one import packs
    _ready(client)
    t = client.get("/countries").text
    for code, cp in packs.countries().items():
        assert cp.label("en") in t and cp.currency in t, code
    assert "Any other country can sign up too" in t and "comprehensive sanctions" in t
    assert 'href="/countries"' in client.get("/").text


def test_worldwide_wording_on_the_english_pages(client, free_mk):
    for p in [f"/crm-for/{s}" for s in marketing.US] + [f"/alternatives/{s}" for s in marketing.ALT] + ["/missed-call-text-back", "/"]:
        t = client.get(p, headers={"Accept-Language": "en"}).text
        assert not re.search(r"\bUS only\b|United States only|FTC wants|the US and Mexico|USA · English", t), p
    assert "In the US" in client.get("/notes/texting-rules").text or "in the US" in client.get("/notes/texting-rules").text
