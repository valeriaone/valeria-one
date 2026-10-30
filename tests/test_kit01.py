"""kit01 (v0.19): one toolbox, a kit per trade. The kit picked at sign-up shapes the menu (its tools, its order, its
words); a business that already uses a tool never loses it (the migration pins it; a new trade in Settings pins it);
Add more shows or hides any tool with one tap (hiding never deletes, the page still opens); every menu link of every kit
opens; the registry holds every menu entry there was; every new word is in all 11 languages; Valeria's one suggestion
comes from the business's own numbers and never comes back once dismissed."""
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from _va import refuse_network
from conftest import make_business
from test_web import signup
from valeria_one import blocks, db, kits, packs
from valeria_one.services import businesses as B
from valeria_one.services import projects as PR
from valeria_one.services import quotes as Q

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
# the office menu of v0.18.2 (base.html), before kits: every one of these must be in the registry, at the same address
OLD_MENU = ["today", "valeria", "connect", "inbox", "contacts", "projects", "tasks", "pipeline", "calendar", "invoices",
            "suppliers", "library", "followups", "campaigns", "recipes", "workflows", "grow", "posts", "google", "website",
            "forms", "ai_team", "channels", "settings"]
OLD_EXTRA = {"start": "/app/start", "billing": "/app/billing", "server": "/app/server", "studio": "/app/studio"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def nav(client) -> str:
    return client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]


def nav_hrefs(client) -> list[str]:
    return re.findall(r'<a href="([^"]+)"', nav(client))


def join(client, industry, email):
    """Sign up (the wizard's "what do you sell" = the industry), reading the screens in English."""
    signup(client, industry=industry, email=email)
    client.post("/lang/en", data={"csrf": tok(client)})


def gets(routes) -> set[str]:
    out = set()
    for r in routes:
        if hasattr(r, "original_router"):
            out |= gets(r.original_router.routes)
        elif "GET" in (getattr(r, "methods", None) or ()):
            out.add(r.path)
    return out


def row(bid) -> dict:
    with db.tx() as conn:
        return B.get(conn, bid)


def me(client) -> dict:
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses).order_by(db.businesses.c.created_at.desc()).limit(1)).mappings().one())


# ---------------------------------------------------------------- kits are data, every trade has one
def test_every_trade_has_a_valid_kit_and_unknown_trades_get_the_generic_one():
    assert kits.check_kits() == []
    for code, ip in packs.industries().items():
        assert ip.kit and ip.kit.tools, code                      # the shipped trades each say what they start with
    assert {"florist", "fitness", "contractor", "shop", "generic"} <= set(packs.industries())
    assert kits.kit("nope") == kits.kit(None) == packs.industry("generic").kit
    covered = {i for d in __import__("valeria_one.services.snapshots", fromlist=["x"]).shipped().values() for i in d["industries"]}
    assert covered == set(packs.industries())                     # every kit's trade has its ready setup too


def test_registry_holds_every_menu_entry_there_was_and_every_address_is_a_page():
    for key in OLD_MENU:
        assert key in kits.TOOLS, key
        assert kits.TOOLS[key].href == "/app" + ("" if key == "today" else "/" + key.replace("_", "-")), key
    for key, href in OLD_EXTRA.items():
        assert kits.TOOLS[key].href == href
    from valeria_one.api import app
    pages = gets(app.routes)
    for t in kits.REGISTRY:
        assert t.href in pages, t.key
        assert t.core or t.group in kits.GROUPS, t.key


# ---------------------------------------------------------------- sign-up picks the kit
def test_the_kit_picked_at_sign_up_shapes_the_menu(client):
    join(client, "fitness", "gym@example.com")
    hrefs = nav_hrefs(client)
    menu = nav(client)
    assert "Members" in menu and "Classes" in menu and "Customers" not in menu        # the trade's own words
    assert "/app/calendar" in hrefs and "/app/invoices" in hrefs
    assert "/app/projects" not in hrefs and "/app/suppliers" not in hrefs and "/app/tiktok" not in hrefs
    kit = [x for x in packs.industry("fitness").kit.tools]
    shown = [k for k in kit if kits.TOOLS[k].href in hrefs]
    assert [hrefs.index(kits.TOOLS[k].href) for k in shown] == sorted(hrefs.index(kits.TOOLS[k].href) for k in shown)   # its order
    for core in ("/app", "/app/inbox", "/app/contacts", "/app/settings", "/app/add", "/app/start"):
        assert core in hrefs, core


def test_a_flower_shop_says_orders_and_deliveries(client):
    join(client, "florist", "flores@example.com")
    menu = nav(client)
    assert "Orders" in menu and "Deliveries" in menu and 'href="/app/pipeline"' in menu
    client.post("/lang/es", data={"csrf": tok(client)})
    assert "Pedidos" in nav(client) and "Entregas" in nav(client)


def test_staff_never_see_owner_tools(client):
    from _va import owner_session
    biz, _ = make_business("MX", "salon")
    owner_session(client, biz, role="staff")
    hrefs = nav_hrefs(client)
    assert "/app/settings" not in hrefs and "/app/add" not in hrefs and "/app/forms" not in hrefs
    assert "/app/inbox" in hrefs and "/app/calendar" in hrefs
    assert client.get("/app/add", follow_redirects=False).status_code == 303          # owners only


# ---------------------------------------------------------------- every link of every kit opens
@pytest.mark.parametrize("industry", sorted(packs.industries()))
def test_every_menu_link_of_every_kit_opens(client, industry):
    join(client, industry, f"{industry}@example.com")
    for href in nav_hrefs(client):
        r = client.get(href, follow_redirects=False)
        assert r.status_code in (200, 303), (industry, href, r.status_code)
        if r.status_code == 303:
            assert client.get(r.headers["location"]).status_code < 400, (industry, href)
    page = client.get("/app/add")
    assert page.status_code == 200
    for t in kits.REGISTRY:                                          # and every other tool, straight by its address
        if t.only:
            continue
        r = client.get(t.href, follow_redirects=False)
        assert r.status_code in (200, 303), (industry, t.key, r.status_code)


# ---------------------------------------------------------------- Add more: one tap on, one tap off, nothing lost
def test_add_more_shows_and_hides_a_tool_and_never_deletes(client):
    join(client, "salon", "s@example.com")
    biz = me(client)
    assert "/app/projects" not in nav_hrefs(client)
    page = client.get("/app/add").text
    assert 'id="t-projects"' in page and "Sell and get paid" in page and "Projects and team" in page
    r = client.post("/app/add", data={"csrf": tok(client), "tool": "projects", "on": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/add#t-projects"
    assert "/app/projects" in nav_hrefs(client) and row(biz["id"])["tools"] == {"projects": True}
    client.post("/app/projects", data={"csrf": tok(client, "/app/projects"), "name": "Boda Ana"})
    with db.tx() as conn:
        n = conn.execute(text("SELECT count(*) FROM projects WHERE business_id = :b"), {"b": biz["id"]}).scalar()
    assert n == 1
    client.post("/app/add", data={"csrf": tok(client), "tool": "projects", "on": "0"})
    assert "/app/projects" not in nav_hrefs(client) and row(biz["id"])["tools"] == {"projects": False}
    with db.tx() as conn:                                            # hidden, not deleted
        assert conn.execute(text("SELECT count(*) FROM projects WHERE business_id = :b"), {"b": biz["id"]}).scalar() == n
    page = client.get("/app/projects")                               # its address still opens, and offers to show it again
    assert page.status_code == 200 and "hidden from your menu" in page.text and 'name="tool" value="projects"' in page.text
    r = client.post("/app/add", data={"csrf": tok(client, "/app/projects"), "tool": "projects", "on": "1", "back": "1"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/projects" and "/app/projects" in nav_hrefs(client)
    assert client.post("/app/add", data={"csrf": tok(client), "tool": "inbox", "on": "0"}).status_code == 404   # core stays
    assert client.post("/app/add", data={"csrf": tok(client), "tool": "nope", "on": "1"}).status_code == 404


def test_showing_a_tool_switches_on_the_block_it_needs(client):
    join(client, "generic", "g@example.com")
    biz = me(client)
    assert not blocks.on(biz, "studio") and "/app/studio" not in nav_hrefs(client)
    client.post("/app/add", data={"csrf": tok(client), "tool": "studio", "on": "1"})
    biz = row(biz["id"])
    assert blocks.on(biz, "studio") and biz["features"] == {"studio": True}
    assert "/app/studio" in nav_hrefs(client) and "/app/projects" not in nav_hrefs(client)   # the door replaces them, as before
    client.post("/app/add", data={"csrf": tok(client), "tool": "studio", "on": "0"})
    assert blocks.on(row(biz["id"]), "studio")                       # hiding never touches what it does


def test_a_block_switched_off_still_hides_its_tool(client):
    join(client, "salon", "b@example.com")
    client.post("/app/start/blocks", data={"csrf": tok(client, "/app/start"), "block": [b for b in blocks.BLOCKS if b != "calendar"]})
    assert "/app/calendar" not in nav_hrefs(client)
    assert 'id="t-calendar"' in client.get("/app/add").text
    client.post("/app/add", data={"csrf": tok(client), "tool": "calendar", "on": "1"})
    assert "/app/calendar" in nav_hrefs(client) and blocks.on(me(client), "calendar")


def test_the_add_more_page_works_on_a_phone_and_in_every_language(client):
    join(client, "shop", "p@example.com")
    for lang in LANGS:
        client.post(f"/lang/{lang}", data={"csrf": tok(client)})
        page = client.get("/app/add", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
        assert page.status_code == 200 and not re.search(r"\{\w+\}", page.text.split("<main", 1)[1]), lang
        assert 'name="viewport"' in page.text and ('dir="rtl"' in page.text) == (lang == "ar")


# ---------------------------------------------------------------- a business that already uses a tool keeps it
def _migration(pattern="*_kit01_kits_tools.py"):
    path = next((ROOT / "alembic" / "versions").glob(pattern))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_migration_pins_every_tool_a_business_already_uses():
    mig = _migration()
    later = _migration("*_ars01_*.py")                                      # v0.20 froze its four new tools' copy
    newer = _migration("*_ars02_*.py")                                      # v0.21 froze its eight new tools' copy
    mkt = _migration("*_mkt01_*.py")                                        # v0.22 froze the Marketplace's copy
    assert not set(mig.USES) & set(later.USES) and not (set(mig.USES) | set(later.USES)) & set(newer.USES)
    assert not (set(mig.USES) | set(later.USES) | set(newer.USES)) & set(mkt.USES)
    assert set(mig.USES) | set(later.USES) | set(newer.USES) | set(mkt.USES) == {t.key for t in kits.REGISTRY if t.uses}   # the copies match the registry
    biz, _ = make_business("MX", "salon", name="Estética Mar")              # a salon kit has no projects, suppliers, comments
    other, _ = make_business("MX", "salon", name="Otra")
    with db.tx() as conn:
        PR.create(conn, biz, "t", {"name": "Boda"})
        conn.execute(db.suppliers.insert().values(business_id=biz["id"], name="Telas SA"))
        conn.execute(db.comment_rules.insert().values(business_id=biz["id"], keywords=["precio"], reply="Te escribimos"))
        conn.execute(update(db.businesses).values(tools={}))                # as every business was before v0.19
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert kits.in_use(conn, biz["id"]) >= {"projects", "suppliers", "comments"}
    b = row(biz["id"])
    assert b["tools"] == {"projects": True, "suppliers": True, "comments": True}
    assert kits.is_on(b, "projects") and kits.is_on(b, "suppliers") and kits.is_on(b, "comments")
    assert row(other["id"])["tools"] == {}                                   # nothing used, nothing pinned: the kit decides
    with db.tx() as conn:                                                    # and running it again changes nothing
        for sql in mig.pin_statements():
            conn.execute(text(sql))
    assert row(biz["id"])["tools"] == b["tools"]


def test_a_new_trade_keeps_what_holds_data_but_not_what_the_owner_hid(client):
    join(client, "contractor", "c@example.com")
    biz = me(client)
    with db.tx() as conn:
        conn.execute(db.suppliers.insert().values(business_id=biz["id"], name="Ferretería"))
        PR.create(conn, biz, "t", {"name": "Baño"})
        kits.set_tool(conn, biz, "t", "projects", False)                     # the owner hid this one on purpose
    t = tok(client, "/app/settings")
    client.post("/app/settings", data={"csrf": t, "name": biz["name"], "industry": "salon", "timezone": biz["timezone"]})
    b = row(biz["id"])
    assert b["industry"] == "salon" and b["tools"] == {"projects": False, "suppliers": True}
    hrefs = nav_hrefs(client)
    assert "/app/suppliers" in hrefs and "/app/projects" not in hrefs and "Clients" in nav(client)
    with db.tx() as conn:                                                    # saving the same trade again pins nothing new
        B.patch(conn, biz["id"], "t", industry="salon")
    assert row(biz["id"])["tools"] == b["tools"]


# ---------------------------------------------------------------- Valeria suggests, once
def _quotes(biz, n):
    with db.tx() as conn:
        for i in range(n):
            Q.create(conn, biz, "t", title=f"Fiesta {i}", lines=[{"description": "Mesa", "qty": 1, "unit_minor": 10000}])


def test_many_quotes_suggest_invoices_and_one_tap_turns_them_on(client):
    join(client, "restaurant", "r@example.com")             # its kit has no invoices
    biz = me(client)
    assert "tip_" not in client.get("/app").text and 'id="tip"' not in client.get("/app").text
    _quotes(biz, kits.TIP_QUOTES)
    today = client.get("/app").text
    assert 'id="tip"' in today and "Send invoices too" in today and "3 quotes" in today
    assert 'id="tip"' in client.get("/app/add").text
    client.post("/app/add", data={"csrf": tok(client), "tool": "invoices", "on": "1"})
    assert "/app/invoices" in nav_hrefs(client) and 'id="tip"' not in client.get("/app").text


def test_no_shows_suggest_reminders_and_no_thanks_is_forever(client):
    join(client, "salon", "n@example.com")
    biz = me(client)
    now = datetime.now(timezone.utc)
    with db.tx() as conn:
        for d in (3, 10):
            conn.execute(db.appointments.insert().values(business_id=biz["id"], title="Corte", status="no_show",
                                                         starts_at=now - timedelta(days=d), ends_at=now - timedelta(days=d, hours=-1)))
    today = client.get("/app").text
    assert 'id="tip"' in today and "turn on reminders" in today and 'href="/app/valeria#reminders"' in today
    r = client.post("/app/tips/dismiss", data={"csrf": tok(client), "tip": "reminders"}, follow_redirects=False)
    assert r.headers["location"] == "/app" and row(biz["id"])["tips_dismissed"] == ["reminders"]
    with db.tx() as conn:                                                    # even with more no-shows, never again
        conn.execute(db.appointments.insert().values(business_id=biz["id"], title="Uñas", status="no_show",
                                                     starts_at=now - timedelta(days=1), ends_at=now - timedelta(hours=23)))
    assert 'id="tip"' not in client.get("/app").text and 'id="tip"' not in client.get("/app/add").text
    client.post("/app/tips/dismiss", data={"csrf": tok(client), "tip": "reminders"})
    assert row(biz["id"])["tips_dismissed"] == ["reminders"]                 # twice = once
    assert client.post("/app/tips/dismiss", data={"csrf": tok(client), "tip": "nope"}).status_code in (303, 200)
    assert row(biz["id"])["tips_dismissed"] == ["reminders"]


def test_regulars_suggest_loyalty_one_at_a_time():
    biz, _ = make_business("MX", "salon")
    now = datetime.now(timezone.utc)
    with db.tx() as conn:
        for i in range(kits.TIP_REPEAT):
            cid = conn.execute(db.contacts.insert().values(business_id=biz["id"], name=f"Cliente {i}").returning(db.contacts.c.id)).scalar()
            for d in (5, 20):
                conn.execute(db.appointments.insert().values(business_id=biz["id"], contact_id=cid, title="Corte", status="done",
                                                             starts_at=now - timedelta(days=d), ends_at=now - timedelta(days=d, hours=-1)))
        tip = kits.suggestion(conn, B.get(conn, biz["id"]))
        assert tip["key"] == "loyalty" and tip["href"] == "/app/valeria#loyalty" and tip["n"] == kits.TIP_REPEAT
        _quotes(biz, kits.TIP_QUOTES)
        kits.set_tool(conn, biz, "t", "invoices", False)
        assert kits.suggestion(conn, B.get(conn, biz["id"]))["key"] == "invoices"     # still one at a time
        kits.dismiss(conn, biz, "t", "invoices")
        assert kits.suggestion(conn, B.get(conn, biz["id"]))["key"] == "loyalty"
        kits.dismiss(conn, biz, "t", "loyalty")
        assert kits.suggestion(conn, B.get(conn, biz["id"])) is None


# ---------------------------------------------------------------- every new word, in every language
def test_every_kit_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {"add_more", "ad_intro", "ad_kit", "ad_in_menu", "ad_open", "ad_hide", "ad_turn_on", "ad_show", "ad_hidden_here",
            "ad_shown", "ad_hidden", "ad_note", "kit_hint", "tip_yes", "tip_no"}
    keys |= {t.word for t in kits.REGISTRY} | {t.about for t in kits.REGISTRY if not t.core}
    keys |= {f"ftg_{g}" for g in kits.GROUPS} | {f"tip_{k}" for k in kits.TIPS} | {f"tip_{k}_d" for k in kits.TIPS}
    keys |= {v for ip in packs.industries().values() for v in (ip.kit.labels.values() if ip.kit else ())}
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    for k in keys - {"ft_studio", "ad_open"}:                                # translated, not English copied over
        if k.startswith(("ft_", "ad_", "tip_", "kw_", "ftg_")) and len(files["en"][k]) > 12:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
