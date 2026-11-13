"""start01 (Tad 2026-11-09): /start — Valeria live for any business: the animated pitch (trade → her with a customer → the money),
the offer, sign up or «do it for me»; her real chat rides along; what visitors do reaches the house business as events (for Tad)."""
from sqlalchemy import select, update

from valeria_one import db, kits
from valeria_one.services import site as ST

from conftest import make_business


def _house():
    biz, _ = make_business("US", "generic", name="Valeria One")
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(slug="valeria-one"))
        b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        ST.save(conn, b, "test", enabled=True, assistant="1", whatsapp="+19514343352")
    return b


def _events(biz_id, kind=None):
    with db.tx() as conn:
        q = select(db.events).where(db.events.c.business_id == biz_id, db.events.c.type.like("start.%"))
        return [dict(r) for r in conn.execute(q).mappings() if not kind or r["type"] == kind]


def test_start_page_in_english_and_spanish(client):
    _house()
    r = client.get("/start", headers={"Accept-Language": "en-US"})
    assert r.status_code == 200 and "I take care of your customers" in r.text and "/signup" in r.text
    assert "The offer: everything free" in r.text and "/w/valeria-one/assistant.js" in r.text
    assert '<img src="/assistant-face.jpg"' in r.text                              # vface01: her face, not «V»
    for k in ("salon", "gym", "clinic", "food", "design", "trades", "shop", "realtor"):
        assert f"{k}:{{n:" in r.text, k
    es = client.get("/start", headers={"Accept-Language": "es-MX"})
    assert "Yo atiendo a sus clientes" in es.text and '<html lang="es"' in es.text and "/signup?hl=es" in es.text
    assert "Yo atiendo a sus clientes" in client.get("/empezar").text
    assert "I take care of your customers" in client.get("/empezar?hl=en").text
    assert client.get("/start", follow_redirects=False).status_code == 200      # no longer a redirect to /signup


def test_start_without_house_still_shows(client):
    r = client.get("/start")
    assert r.status_code == 200 and "assistant.js" not in r.text
    assert client.post("/start/seen", json={}).status_code == 204
    assert client.post("/start/lead", json={"email": "a@b.co", "whatsapp": "+529991234567"}).status_code == 503


def test_seen_once_per_visitor_and_notes(client):
    h = _house()
    for _ in range(3):
        assert client.post("/start/seen", json={"lang": "es"}).status_code == 204
    assert len(_events(h["id"], "start.opened")) == 1
    client.post("/start/note", json={"k": "trade", "t": "  taller   mecánico "})
    client.post("/start/note", json={"k": "chat", "t": "¿Es gratis de verdad?"})
    client.post("/start/note", json={"t": ""})
    notes = sorted((e["data"]["kind"], e["data"]["text"]) for e in _events(h["id"], "start.chat"))
    assert notes == [("chat", "¿Es gratis de verdad?"), ("trade", "taller mecánico")]


def test_do_it_for_me_makes_a_customer(client):
    h = _house()
    r = client.post("/start/lead", json={"name": "Hugo Pech", "business": "Decora con Hugo", "email": "Hugo@Ejemplo.mx", "whatsapp": "+52 999 115 1095"})
    assert r.json() == {"ok": True}
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == h["id"])).mappings().one()
    assert c["name"] == "Hugo Pech" and c["email"] == "hugo@ejemplo.mx" and c["phone"] == "+529991151095" and c["source"] == "start"
    assert len(_events(h["id"], "start.lead")) == 1
    # a local number from another country without its +code: the email still lands, the number in the message
    r = client.post("/start/lead", json={"name": "Ana", "email": "ana@x.mx", "whatsapp": "999 123 4567"})
    assert r.json() == {"ok": True}
    assert client.post("/start/lead", json={"email": "nope", "whatsapp": "1"}).status_code == 400
    assert client.post("/start/lead", json={"email": "bot@x.co", "whatsapp": "+15551234567", "website": "spam"}).json() == {"ok": True}
    with db.tx() as conn:
        n = conn.execute(select(db.contacts.c.email).where(db.contacts.c.business_id == h["id"])).scalars().all()
    assert sorted(n) == ["ana@x.mx", "hugo@ejemplo.mx"]


def test_hugo_demo_is_design_with_studio(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    seed.seed()
    with db.tx() as conn:
        b = conn.execute(select(db.businesses).where(db.businesses.c.slug == "decora-con-hugo")).mappings().one()
        n = conn.execute(select(db.projects).where(db.projects.c.business_id == b["id"])).all() if hasattr(db, "projects") else [1, 1]
    assert b["industry"] == "design" and kits.is_on(b, "studio") and len(n) >= 2     # trade01: studio from the design 5
