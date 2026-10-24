import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import deals as D
from valeria_one.services import followups as FU


def sandbox(client, monkeypatch, country="MX"):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Seguimiento", "country": country, "industry": "salon"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    client.patch("/v1/ai", headers=h, json={"mode": "off"})
    client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Rosa María", "body": "hola"})
    contact = client.get("/v1/contacts", headers=h).json()[0]
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == r["business"]["id"])).mappings().one())
    return biz, h, contact


def rows():
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(db.scheduled_messages.select().order_by(db.scheduled_messages.c.send_at)).mappings()]


def last_out(client, h):
    conv = client.get("/v1/conversations", headers=h).json()[0]
    return [m for m in client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"] if m["direction"] == "out"]


def test_reminder_uses_template_outside_the_window_and_skips_cancelled(client, monkeypatch):
    biz, h, c = sandbox(client, monkeypatch)
    client.post("/v1/automations", headers=h, json={"trigger": "appointment.reminder", "minutes": 1440,
                                                    "message": "Hola {name}, te esperamos {when}", "template": "recordatorio_cita"})
    st = datetime.now(timezone.utc) + timedelta(days=3)
    with db.tx() as conn:
        a = D.book(conn, biz, "t", {"title": "Corte", "contact_id": c["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})
        b = D.book(conn, biz, "t", {"title": "Tinte", "contact_id": c["id"], "starts_at": st + timedelta(hours=2),
                                    "ends_at": st + timedelta(hours=3)})
    sched = rows()
    assert len(sched) == 2 and sched[0]["send_at"] == st - timedelta(minutes=1440)
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", b["id"], "cancelled")
    assert FU.send_due(now=st - timedelta(minutes=1441)) == {"sent": 0, "skipped": 0, "failed": 0, "later": 0}   # not yet
    res = FU.send_due(now=st)
    assert res["sent"] == 1 and res["skipped"] == 1
    out = last_out(client, h)
    assert out[-1]["template"]["name"] == "recordatorio_cita" and out[-1]["template"]["params"][0] == "Rosa"
    done = {r["subject_id"]: r for r in rows()}
    assert done[b["id"]]["error"] == "appointment changed or cancelled" and done[a["id"]]["status"] == "sent"


def test_after_appointment_inside_the_window_fills_the_text(client, monkeypatch):
    biz, h, c = sandbox(client, monkeypatch)
    client.patch("/v1/booking", headers=h, json={"enabled": True})
    client.post("/v1/automations", headers=h, json={"trigger": "appointment.done", "minutes": 0,
                                                    "message": "¡Gracias {name}! Vuelve pronto a {business}: {link}"})
    st = datetime.now(timezone.utc) - timedelta(hours=2)
    with db.tx() as conn:
        a = D.book(conn, biz, "t", {"title": "Corte", "contact_id": c["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})
        D.set_appointment_status(conn, biz["id"], "t", a["id"], "done")
    assert FU.send_due()["sent"] == 1
    body = last_out(client, h)[-1]["body"]
    assert body.startswith("¡Gracias Rosa! Vuelve pronto a Seguimiento: https://one.test/b/")


def test_no_template_outside_window_is_skipped_and_deal_won_only(client, monkeypatch):
    biz, h, c = sandbox(client, monkeypatch)
    client.post("/v1/automations", headers=h, json={"trigger": "deal.won", "minutes": 60, "message": "Gracias {name}"})
    d = client.post("/v1/deals", headers=h, json={"title": "Paquete", "contact_id": c["id"]}).json()
    client.patch(f"/v1/deals/{d['id']}", headers=h, json={"stage": "booked"})
    assert rows() == []                                                             # not a won stage
    client.patch(f"/v1/deals/{d['id']}", headers=h, json={"stage": "won"})
    assert len(rows()) == 1
    later = datetime.now(timezone.utc) + timedelta(days=2)                            # past the 24 h window, no template
    assert FU.send_due(now=later)["skipped"] == 1 and rows()[0]["error"] == "outside_reply_window"


def test_campaign_only_reaches_consent_and_respects_quiet_hours(client, monkeypatch):
    biz, h, c = sandbox(client, monkeypatch, country="US")
    client.post("/v1/sandbox/simulate", headers=h, json={"from": "c2", "name": "Bob", "body": "hi"})
    contacts = {x["name"]: x for x in client.get("/v1/contacts", headers=h).json()}
    client.patch(f"/v1/contacts/{contacts['Rosa María']['id']}", headers=h,
                 json={"marketing_consent": True, "marketing_consent_source": "booking checkbox", "tags": ["vip"]})
    client.patch(f"/v1/contacts/{contacts['Bob']['id']}", headers=h, json={"tags": ["vip"]})            # no consent
    night = datetime(2026, 11, 4, 3, 30, tzinfo=timezone.utc)                                          # 22:30 New York
    cp = client.post("/v1/campaigns", headers=h, json={"name": "Black Friday", "message": "20% off, {name}!", "tag": "vip",
                                                       "send_at": "2026-11-04T03:00:00+00:00"}).json()
    assert FU.dispatch_campaigns(now=night) == 1
    assert [str(r["contact_id"]) for r in rows()] == [contacts["Rosa María"]["id"]]
    assert FU.send_due(now=night)["later"] == 1
    r = rows()[0]
    assert r["status"] == "pending" and r["send_at"] == datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc)   # 08:00 New York
    listed = client.get("/v1/campaigns", headers=h).json()[0]
    assert listed["id"] == cp["id"] and listed["counts"]["audience"] == 1


def test_rules_validation_and_screen(client):
    biz, key = make_business("MX")
    with db.tx() as conn:
        with pytest.raises(Invalid):
            FU.create_automation(conn, biz, "t", trigger="birthday", minutes=0, message="x")
        with pytest.raises(Invalid):
            FU.create_automation(conn, biz, "t", trigger="contact.created", minutes=-1, message="x")
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Clínica F", "country": "MX", "industry": "clinic",
                                 "email": "f@example.com", "password": "una-clave-larga"})
    page = client.get("/app/followups").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post("/app/followups/rules", data={"csrf": tok, "trigger": "contact.created", "minutes": "5",
                                              "message": "Bienvenida {name}"})
    client.post("/app/followups/campaigns", data={"csrf": tok, "name": "Promo", "message": "Hola", "send_at": "2030-01-01T10:00"})
    page = client.get("/app/followups").text
    assert "Bienvenida {name}" in page and "Promo" in page
    client.post("/app/contacts", data={"csrf": tok, "name": "Nueva"})
    assert any(r["status"] == "pending" for r in rows())                                        # welcome scheduled
