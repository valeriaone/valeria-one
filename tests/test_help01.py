"""help01 (Tad 2026-11-09): Valeria inside the office helps without an AI key — built-in answers, step-by-step wizards that point at
the real spot on each page, «do it for me» (→ valAI), and a visible «Questions? Ask me» button. EN + ES."""
import re

from sqlalchemy import select

from _va import owner_session
from conftest import make_business
from valeria_one import db
from valeria_one.services import helpdesk as HD


def _tok(client):
    page = client.get("/app").text
    return re.search(r'data-csrf="([^"]+)"', page).group(1)


def _owner(client, lang="es"):
    biz, _ = make_business("MX", "salon", name="Salón Help")
    owner_session(client, biz)
    client.post("/lang/" + lang, data={"csrf": _tok(client)})
    return biz


def test_questions_find_their_wizard():
    assert HD.match("¿Cómo conecto mi WhatsApp?")[0] == "whatsapp"
    assert HD.match("quiero cambiar mis precios")[0] == "prices"
    assert HD.match("How do I upload photos of my work?")[0] == "photos"
    assert HD.match("cobrar con mercado pago")[0] == "payments"
    assert HD.match("editar mi página web")[0] == "page"
    assert HD.match("asdf qwerty") == []
    assert HD.for_page("/app/channels") == "whatsapp" and HD.for_page("/app/nowhere") is None


def test_catalog_both_languages_complete():
    for L in ("en", "es"):
        c = HD.catalog(L)
        assert set(c["guides"]) == set(HD.GUIDES) and all(g["title"] and g["answer"] and g["steps"] for g in c["guides"].values())
        assert {"go", "forme", "next", "done", "later", "stuck", "errors", "asked"} <= set(c["text"])
    assert HD.catalog("es")["guides"]["whatsapp"]["title"] == "Conectar su WhatsApp"
    assert HD.catalog("fr")["text"]["go"] == "Let's do it together"                  # other languages: English


def _found(html, sel):
    for q in [s.strip() for s in sel.split(",")]:
        m = re.fullmatch(r"#([\w-]+)", q)
        if m and f'id="{m.group(1)}"' in html:
            return True
        m = re.fullmatch(r"(\w+)\[(\w+)='([^']+)'\]", q)
        if m and re.search(rf'<{m.group(1)}\b[^>]*\b{m.group(2)}="{re.escape(m.group(3))}"', html):
            return True
    return False


def test_every_wizard_step_points_at_something_real(client):
    """Each step's page opens for an owner and has the spot the wizard points at — a wizard never points at nothing."""
    _owner(client, "en")
    for key, g in HD.GUIDES.items():
        for path, sel, _ in g["en"][2]:
            r = client.get(path.split("#")[0])
            assert r.status_code == 200, (key, path)
            assert _found(r.text, sel), (key, path, sel)


def test_no_key_gets_help_not_a_dead_end(client):
    _owner(client, "es")
    tok = _tok(client)
    j = client.post("/app/assistant", json={"turns": [{"r": "user", "t": "¿cómo pongo mis precios?"}]}, headers={"X-CSRF": tok}).json()
    assert j["guides"][0] == "prices" and "Sus precios van en sus servicios" in j["say"] and "reason" not in j
    j = client.post("/app/assistant", json={"turns": [{"r": "user", "t": "hola"}]}, headers={"X-CSRF": tok}).json()
    assert j["say"].startswith("Le ayudo con") and len(j["guides"]) == 6
    c = client.get("/app/help/catalog").json()
    assert c["guides"]["prices"]["steps"][0]["path"] == "/app/calendar" and c["text"]["forme"] == "Hágalo por mí"


def test_do_it_for_me_reaches_valai(client):
    biz = _owner(client, "es")
    tok = _tok(client)
    assert client.post("/app/help/forme", json={"task": "whatsapp", "page": "/app/channels"}, headers={"X-CSRF": "x"}).status_code == 403
    j = client.post("/app/help/forme", json={"task": "whatsapp", "page": "/app/channels"}, headers={"X-CSRF": tok}).json()
    assert j["ok"] and j["say"].startswith("Listo ✓")
    with db.tx() as conn:
        a = conn.execute(select(db.owner_asks).where(db.owner_asks.c.business_id == biz["id"])).mappings().one()
    assert a["text"] == "[Hágalo por mí] Conectar su WhatsApp · /app/channels" and a["status"] == "new" and a["industry"] == "salon"


def test_the_button_is_visible_and_the_wizard_loads(client):
    _owner(client, "es")
    page = client.get("/app/website").text
    assert "/static/guide.js" in page and "¿Dudas? Pregúntame" in page
    js = client.get("/static/guide.js")
    assert js.status_code == 200 and "VOGuide" in js.text and "vo-help-quiet" in js.text
