"""trade01 (v0.65.0): each business sees only its trade's tools. Every kit is the trade's 5 (CAD/Studio only for design);
the menu keeps core + those 5 in view and folds what the owner added under «More»; Today asks once — «¿A qué se dedica?»
when the trade is unknown, «Lo dejé listo para … ¿Es correcto?» when it is known — and the answer sets the menu; the
migration keeps every tool that holds a business's data; a transport trade (chauffeurs, shuttles) exists."""
import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy import text, update

from _va import owner_session, refuse_network
from conftest import make_business
from valeria_one import db, kits, packs
from valeria_one.services import businesses as B
from valeria_one.services import social as SO

ROOT = Path(__file__).resolve().parent.parent
FIVE = {"fitness": ["members", "calendar", "invoices", "followups", "website"],
        "salon": ["calendar", "followups", "grow", "pos", "website"],
        "clinic": ["calendar", "followups", "forms", "invoices", "website"],
        "design": ["studio", "pipeline", "invoices", "calendar", "website"],
        "contractor": ["pipeline", "projects", "calendar", "invoices", "website"],
        "restaurant": ["pos", "inventory", "calendar", "grow", "website"],
        "shop": ["pos", "inventory", "invoices", "grow", "website"],
        "florist": ["pipeline", "calendar", "pos", "grow", "website"],
        "realtor": ["pipeline", "calendar", "followups", "campaigns", "website"],
        "professional": ["calendar", "pipeline", "invoices", "forms", "website"],
        "transport": ["calendar", "pipeline", "invoices", "followups", "website"],
        "generic": ["calendar", "pipeline", "invoices", "followups", "website"]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def row(bid) -> dict:
    with db.tx() as conn:
        return B.get(conn, bid)


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def menu_parts(client) -> tuple[list[str], list[str]]:
    """(hrefs in view, hrefs folded under «More»)."""
    nav = client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
    top, _, more = nav.partition('<details class="nav-more"')
    return re.findall(r'<a href="([^"]+)"', top), re.findall(r'<a href="([^"]+)"', more)


def office(client, industry, lang="es"):
    biz, _ = make_business("MX", industry, name=f"Negocio {industry}")
    owner_session(client, biz)
    client.post(f"/lang/{lang}", data={"csrf": tok(client)})
    return biz


# ---------------------------------------------------------------- the kits
def test_every_trade_starts_with_its_own_five_and_only_design_gets_the_studio():
    assert kits.check_kits() == []
    assert set(packs.industries()) == set(FIVE)                              # transport is a trade now
    for code, tools in FIVE.items():
        assert kits.kit(code).tools == tools, code
    assert [c for c, ip in packs.industries().items() if "studio" in ip.kit.tools] == ["design"]
    assert packs.industry("design").studio and not any(ip.studio for c, ip in packs.industries().items() if c != "design")


@pytest.mark.parametrize("industry", sorted(FIVE))
def test_the_menu_shows_core_and_the_trades_five_and_every_link_opens(client, industry):
    biz = office(client, industry)
    top, more = menu_parts(client)
    want = {kits.TOOLS[k].href for k in FIVE[industry]} - ({"/app/projects"} if industry == "design" else set())
    assert want <= set(top), (industry, want - set(top))
    assert set(top) - want <= {"/app", "/app/valeria", "/app/connect", "/app/inbox", "/app/contacts"}, industry
    assert "/app/studio" not in top + more or industry == "design"           # no CAD for a salon, a gym, a groomer
    for h in top + more:
        assert client.get(h, follow_redirects=False).status_code in (200, 302, 303), (industry, h)


def test_a_tool_the_owner_adds_folds_under_more_and_stays_reachable(client):
    biz = office(client, "salon")
    assert "/app/members" not in sum(menu_parts(client), [])
    client.post("/app/add", data={"csrf": tok(client, "/app/add"), "tool": "members", "on": "1"})
    top, more = menu_parts(client)
    assert "/app/members" in more and "/app/members" not in top
    assert "/app/calendar" in top and "/app/pos" in top


# ---------------------------------------------------------------- Today asks once
def test_an_unknown_trade_is_asked_and_the_answer_sets_the_menu(client):
    biz = office(client, "generic")
    page = client.get("/app").text
    assert 'id="trade"' in page and "¿A qué se dedica su negocio?" in page
    assert 'value="transport"' in page and 'value="generic"' not in page.split('id="trade"', 1)[1].split("</div>\n", 1)[0]
    r = client.post("/app/trade", data={"csrf": tok(client), "industry": "fitness"}, follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/app"
    b = row(biz["id"])
    assert b["industry"] == "fitness" and "trade" in b["tips_dismissed"]
    assert 'id="trade"' not in client.get("/app").text                         # asked once
    assert "/app/members" in menu_parts(client)[0]


def test_a_known_trade_is_confirmed_in_her_words_and_can_be_changed(client):
    biz = office(client, "salon")
    page = client.get("/app").text
    card = page.split('id="trade"', 1)[1].split("</details>", 1)[0]
    assert "Lo dejé listo para Salón, spa y barbería:" in card and "¿Es correcto?" in card and "Sí, correcto" in card
    assert "Agregar más" in card and "Cambiar" in card
    client.post("/app/trade", data={"csrf": tok(client)})                     # «Sí, correcto»
    b = row(biz["id"])
    assert b["industry"] == "salon" and b["tips_dismissed"] == ["trade"]
    assert 'id="trade"' not in client.get("/app").text


def test_change_from_the_card_keeps_what_holds_data(client):
    biz = office(client, "salon")
    with db.tx() as conn:
        conn.execute(db.suppliers.insert().values(business_id=biz["id"], name="Telas SA"))
    client.post("/app/trade", data={"csrf": tok(client), "industry": "clinic"})
    b = row(biz["id"])
    assert b["industry"] == "clinic" and b["tools"].get("suppliers") is True    # not in the clinic's 5; her data keeps it
    assert "/app/suppliers" in sum(menu_parts(client), [])


def test_english_card_and_no_card_for_staff_or_a_bad_trade(client):
    biz = office(client, "transport", lang="en")
    assert "I set it up for Transport, chauffeur &amp; shuttle:" in client.get("/app").text
    r = client.post("/app/trade", data={"csrf": tok(client), "industry": "nope"}, follow_redirects=False)
    assert r.status_code in (302, 303) and row(biz["id"])["industry"] == "transport"      # refused, nothing changed
    staff_biz, _ = make_business("MX", "salon", name="Estética del staff")
    owner_session(client, staff_biz, role="staff")
    assert 'id="trade"' not in client.get("/app").text


def test_a_public_demo_never_shows_the_card(client):
    biz = office(client, "fitness")
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(demo=True))
    assert 'id="trade"' not in client.get("/app").text


# ---------------------------------------------------------------- nothing used is lost
def _migration():
    f = next((ROOT / "alembic" / "versions").glob("*_trade01_*.py"))
    spec = importlib.util.spec_from_file_location("trade01_mig", f)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_migration_keeps_every_used_tool_and_respects_what_the_owner_hid():
    mig = _migration()
    assert set(mig.USES) == {t.key for t in kits.REGISTRY if t.uses}       # the frozen copy matches the registry
    biz, _ = make_business("MX", "fitness", name="Gym con caja")
    hid, _ = make_business("MX", "fitness", name="Gym que la ocultó")
    idle, _ = make_business("MX", "fitness", name="Gym sin datos")
    with db.tx() as conn:
        for b in (biz, hid):
            conn.execute(db.suppliers.insert().values(business_id=b["id"], name="Proteínas SA"))
        conn.execute(update(db.businesses).where(db.businesses.c.id.in_([biz["id"], idle["id"]])).values(tools={}))
        conn.execute(update(db.businesses).where(db.businesses.c.id == hid["id"]).values(tools={"suppliers": False}))
        for sql in mig.pin_statements():
            conn.execute(text(sql))
    pinned = row(biz["id"])["tools"]
    assert pinned.get("suppliers") is True and kits.is_on(row(biz["id"]), "suppliers")
    assert row(hid["id"])["tools"].get("suppliers") is False                 # hidden on purpose stays hidden
    assert "suppliers" not in row(idle["id"])["tools"]                       # nothing used: the 5 decide
    with db.tx() as conn:
        for sql in mig.pin_statements():
            conn.execute(text(sql))
    assert row(biz["id"])["tools"] == pinned                                 # twice = once


# ---------------------------------------------------------------- transport
def test_transport_speaks_its_trade_and_is_guessed_from_a_page():
    ip = packs.industry("transport")
    assert packs.text(ip.words["appointment"], "es") == "Viaje" and packs.text(ip.words["contact"], "en") == "Passenger"
    assert SO.industry_guess("Limo Service") == SO.industry_guess("Chauffeur") == "transport"
    assert SO.industry_guess("Pet Groomer") == "salon" and SO.industry_guess("Dentist & Dental Office") == "clinic"
    from valeria_one.sandbox import seed
    assert next(b for b in seed.DEMO if b["name"] == "Malik Hasnain")["industry"] == "transport"
    assert not [b["name"] for b in seed.DEMO if b.get("tools")]              # every demo shows its trade's 5, nothing more
