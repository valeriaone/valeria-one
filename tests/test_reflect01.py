"""reflect01 (Tad 2026-11-09): Reflect Aesthetics (Angeles City, PH) — the sandbox demo behind the copy of their website:
their printed prices, their hours, their website chat on (served by script tag on reflectaesthetics.valai.mx), its AI notes."""
from sqlalchemy import select

from valeria_one import db


def test_reflect_demo_has_their_prices_and_its_website_chat(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    seed.seed()
    with db.tx() as conn:
        biz = conn.execute(select(db.businesses).where(db.businesses.c.slug == "reflect-aesthetics")).mappings().one()
        svc = {r["name"]: r["price_minor"] for r in conn.execute(select(db.booking_services).where(db.booking_services.c.business_id == biz["id"])).mappings()}
        ai = conn.execute(select(db.ai_settings).where(db.ai_settings.c.business_id == biz["id"])).mappings().one()
    assert biz["country"] == "PH" and biz["industry"] == "clinic" and biz["timezone"] == "Asia/Manila" and biz["sandbox"]
    assert len(svc) == 42
    assert svc["The Reflect Signature"] == 189900 and svc["Reflect Clarity Shot (per pimple)"] == 20000
    assert svc["HIFU V-lift (from)"] == 499900 and svc["Diode underarm laser (package)"] == 800000
    assert svc["Ultimate Collection · 100 tokens"] == 2000000 and svc["Consultation with Dr. Ken Romero"] is None
    assert biz["site"]["assistant"] and ai["provider"] == "demo" and ai["mode"] == "auto"
    assert "4+1 sessions at 9,999" in ai["instructions"] and "Monday to Sunday, 11:00 AM to 8:00 PM" in ai["instructions"]
    js = client.get("/w/reflect-aesthetics/assistant.js")
    assert js.status_code == 200 and "Reflect Aesthetics" in js.text and "/w/reflect-aesthetics" in js.text
    assert client.get("/try/reflect-aesthetics", follow_redirects=False).headers["location"] == "/app"
    assert "Camille Santos" in client.get("/app/inbox").text
    b = client.get("/b/reflect-aesthetics")
    assert b.status_code == 200 and "The Reflect Signature" in b.text
