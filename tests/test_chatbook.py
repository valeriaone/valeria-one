import json
from datetime import datetime, timedelta, timezone

import httpx

from valeria_one import channels, db
from valeria_one.channels import Outbound
from valeria_one.services import chatbook as CB

ALL_WEEK = {d: [["08:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def biz_with_booking(client, monkeypatch, services=(("Corte", 60, 35000), ("Tinte", 120, 90000))):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Salón Chat", "country": "MX", "industry": "salon"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    client.patch("/v1/ai", headers=h, json={"mode": "off"})
    client.patch("/v1/booking", headers=h, json={"enabled": True, "hours": ALL_WEEK, "notice_minutes": 0})
    for n, m, p in services:
        client.post("/v1/booking/services", headers=h, json={"name": n, "minutes": m, "price_minor": p})
    return h


def say(client, h, body, who="c1"):
    return client.post("/v1/sandbox/simulate", headers=h, json={"from": who, "name": "Rosa", "body": body}).json()["ai"][0]


def last_out(client, h):
    conv = client.get("/v1/conversations", headers=h).json()[0]
    msgs = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"]
    return [m for m in msgs if m["direction"] == "out"][-1]["body"]


def test_full_booking_in_chat(client, monkeypatch):
    h = biz_with_booking(client, monkeypatch)
    assert say(client, h, "Hola, quiero una cita") == "booking:asked"
    assert "¿Qué te gustaría agendar?" in last_out(client, h) and "1) Corte" in last_out(client, h) and "$350.00" in last_out(client, h)
    assert say(client, h, "2") == "booking:asked"                                 # Tinte
    assert "Tinte — ¿Qué día?" in last_out(client, h)
    assert say(client, h, "2") == "booking:asked"                                 # second day offered
    assert "¿A qué hora?" in last_out(client, h)
    assert say(client, h, "1") == "booking:asked"                                 # no phone on a sandbox chat -> ask
    assert "teléfono" in last_out(client, h)
    assert say(client, h, "123") == "booking:asked" and "no parece correcto" in last_out(client, h)
    assert say(client, h, "999 123 4567") == "booking:booked"
    assert last_out(client, h).startswith("¡Listo! Tinte — ")
    appts = client.get("/v1/appointments", headers=h).json()
    assert len(appts) == 1 and appts[0]["title"] == "Tinte"
    contact = client.get("/v1/contacts", headers=h).json()[0]
    assert contact["phone"] == "+529991234567"
    assert say(client, h, "gracias!") in ("off", "failed")                         # flow is over: back to normal (AI off)


def test_english_cancel_and_bad_choice(client, monkeypatch):
    h = biz_with_booking(client, monkeypatch)
    assert say(client, h, "Hi, can I book an appointment?") == "booking:asked"
    assert "What would you like to book?" in last_out(client, h)
    assert say(client, h, "banana") == "booking:asked" and "reply with one of the numbers" in last_out(client, h)
    assert say(client, h, "cancel") == "booking:cancelled" and "cancelled" in last_out(client, h)
    with db.tx() as conn:
        assert conn.execute(db.conversations.select()).mappings().one()["flow"] is None


def test_single_service_goes_straight_to_days_and_flow_expires(client, monkeypatch):
    h = biz_with_booking(client, monkeypatch, services=(("Consulta", 30, None),))
    say(client, h, "予約したいです")
    assert last_out(client, h).startswith("Consulta — ご希望の日")
    with db.tx() as conn:
        conn.execute(db.conversations.update().values(flow={"step": "day", "lang": "ja", "service": "x", "options": [],
                                                            "expires": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}))
    assert say(client, h, "1") in ("off", "failed")                               # an expired flow doesn't trap anyone


def test_off_when_booking_disabled(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Sin agenda", "country": "MX"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    client.patch("/v1/ai", headers=h, json={"mode": "off"})
    assert say(client, h, "quiero una cita") == "off"


def test_triggers_many_languages():
    for t in ("Quiero agendar", "book please", "Quero marcar um horário", "Je voudrais réserver", "Termin buchen",
              "Pwede mag-book?", "Mau pesan", "予約できますか", "我想预约", "أريد حجز موعد", "बुक करना है"):
        assert CB.wants_booking(t), t
    assert not CB.wants_booking("¿Cuánto cuesta?")


def test_whatsapp_gets_a_tap_to_pick_list():
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})
    wa = channels.adapter("whatsapp", httpx.Client(transport=httpx.MockTransport(handler)))
    opts = [{"id": f"o{i}", "title": f"Opción número {i} con nombre largo", "description": "$350.00 · 60 min", "button": "Elegir"} for i in range(3)]
    wa.send({"phone_number_id": "1", "token": "t"}, "+529991234567",
            Outbound(to="+529991234567", body="¿Qué te gustaría agendar?\n\n1) …", options=opts))
    i = sent["interactive"]
    assert sent["type"] == "interactive" and i["type"] == "list" and i["body"]["text"] == "¿Qué te gustaría agendar?"
    rows = i["action"]["sections"][0]["rows"]
    assert len(rows) == 3 and all(len(r["title"]) <= 24 for r in rows) and i["action"]["button"] == "Elegir"
