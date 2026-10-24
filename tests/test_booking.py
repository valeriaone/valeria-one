import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid, NotFound
from valeria_one.services import booking as BK

TZ = ZoneInfo("America/Mexico_City")
ALL_WEEK = {d: [["08:00", "20:00"]] for d in BK.DAYS}


def setup_biz(hours=None, notice=0, ahead=30, enabled=True):
    biz, key = make_business("MX", "salon", name="Salón Prueba")
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", enabled=enabled, slug="salon-prueba", hours=hours or ALL_WEEK,
                               notice_minutes=notice, days_ahead=ahead)
        svc = BK.add_service(conn, biz, "t", name="Corte", minutes=60, price_minor=35000)
    return biz, key, svc


def test_hours_validation():
    assert BK.clean_hours({"mon": [["16:00", "19:00"], ["09:00", "14:00"]], "xyz": [["1", "2"]]}) == \
        {"mon": [["09:00", "14:00"], ["16:00", "19:00"]]}
    for bad in ({"mon": [["14:00", "09:00"]]}, {"mon": [["9", "10"]]}, {"mon": [["09:00", "12:00"], ["11:00", "13:00"]]},
                {"mon": [["09:00"]]}):
        with pytest.raises(Invalid):
            BK.clean_hours(bad)


def test_slugs():
    assert BK.slugify("Salón Lupita & Spa!") == "salon-lupita-spa"
    assert BK.slugify("佐藤") == "negocio"


def test_slots_respect_hours_notice_and_appointments():
    monday = date(2026, 11, 2)
    biz, _, svc = setup_biz(hours={"mon": [["09:00", "11:00"]]}, notice=60)
    now = datetime(2026, 11, 2, 8, 30, tzinfo=TZ)                               # Monday 8:30 local, 1 h notice
    with db.tx() as conn:
        got = [t.strftime("%H:%M") for t in BK.slots(conn, biz, svc["id"], monday, now)]
    assert got == ["09:30", "09:45", "10:00"]                                  # 60-min service must end by 11:00
    with db.tx() as conn:
        assert BK.slots(conn, biz, svc["id"], monday + timedelta(days=1), now) == []   # Tuesday: closed
        assert BK.slots(conn, biz, svc["id"], monday - timedelta(days=1), now) == []   # the past
        BK.book(conn, biz, service_id=svc["id"], starts_at=datetime(2026, 11, 2, 9, 30, tzinfo=TZ), name="Ana",
                phone="9991234567", now=now)
        assert BK.slots(conn, biz, svc["id"], monday, now) == []               # 9:30–10:30 taken → nothing fits


def test_same_slot_cannot_be_booked_twice_and_phone_reuses_customer():
    biz, _, svc = setup_biz()
    now = datetime.now(timezone.utc)
    with db.tx() as conn:
        d = BK.open_days(conn, biz, svc["id"], now)[1]
        t = BK.slots(conn, biz, svc["id"], d, now)[0]
        a = BK.book(conn, biz, service_id=svc["id"], starts_at=t, name="Ana", phone="999 123 4567", email="ANA@x.mx", now=now)
    with db.tx() as conn, pytest.raises(Invalid) as e:
        BK.book(conn, biz, service_id=svc["id"], starts_at=t, name="Otra", phone="9992223333", now=now)
    assert e.value.field == "starts_at"
    with db.tx() as conn:
        t2 = BK.slots(conn, biz, svc["id"], d, now)[0]
        b = BK.book(conn, biz, service_id=svc["id"], starts_at=t2, name="Ana López", phone="+52 999 123 4567",
                    marketing_consent=True, now=now)
    assert b["contact"]["id"] == a["contact"]["id"] and b["contact"]["marketing_consent"]
    assert a["contact"]["email"] == "ana@x.mx" and a["appointment"]["title"] == "Corte"


def test_bad_inputs():
    biz, _, svc = setup_biz()
    now = datetime.now(timezone.utc)
    with db.tx() as conn:
        t = BK.slots(conn, biz, svc["id"], BK.open_days(conn, biz, svc["id"], now)[1], now)[0]
        for kw in ({"name": "", "phone": "9991234567"}, {"name": "A", "phone": "123"}):
            with pytest.raises(Invalid):
                BK.book(conn, biz, service_id=svc["id"], starts_at=t, now=now, **kw)
        with pytest.raises(Invalid):                                            # not on the 15-minute grid
            BK.book(conn, biz, service_id=svc["id"], starts_at=t + timedelta(minutes=7), name="A", phone="9991234567", now=now)
        with pytest.raises(Invalid):
            BK.add_service(conn, biz, "t", name="x", minutes=2)


def test_disabled_page_is_not_found(client):
    setup_biz(enabled=False)
    assert client.get("/b/salon-prueba").status_code == 404
    assert client.get("/v1/public/salon-prueba").status_code == 404


def test_public_api(client):
    biz, key, svc = setup_biz()
    info = client.get("/v1/public/salon-prueba").json()
    assert info["name"] == "Salón Prueba" and info["services"][0]["price_minor"] == 35000
    days = client.get("/v1/public/salon-prueba/slots", params={"service_id": svc["id"]}).json()["days"]
    slots = client.get("/v1/public/salon-prueba/slots", params={"service_id": svc["id"], "day": days[1]}).json()["slots"]
    r = client.post("/v1/public/salon-prueba/book", json={"service_id": str(svc["id"]), "starts_at": slots[0], "name": "Bea",
                                                          "phone": "9997776655"})
    assert r.status_code == 201
    again = client.post("/v1/public/salon-prueba/book", json={"service_id": str(svc["id"]), "starts_at": slots[0], "name": "Cy",
                                                              "phone": "9997776656"})
    assert again.status_code == 422
    owner = client.get("/v1/appointments", headers={"Authorization": f"Bearer {key}"}).json()
    assert len(owner) == 1 and owner[0]["title"] == "Corte"
    s = client.get("/v1/booking", headers={"Authorization": f"Bearer {key}"}).json()
    assert s["url"].endswith("/b/salon-prueba") and s["enabled"]


def test_public_page_flow(client):
    biz, key, svc = setup_biz()
    home = client.get("/b/salon-prueba")
    assert home.status_code == 200 and "Corte" in home.text and "$350.00" in home.text
    page = client.get(f"/b/salon-prueba/{svc['id']}").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    t = re.search(r'name="starts_at" value="([^"]+)"', page).group(1)
    done = client.post(f"/b/salon-prueba/{svc['id']}", data={"csrf": tok, "starts_at": t, "name": "Dora", "phone": "9991112233",
                                                             "email": "", "notes": "primera vez"})
    assert "¡Listo, tienes cita!" in done.text
    ics_url = re.search(r'href="(/b/salon-prueba/ics/[^"]+)"', done.text).group(1)
    ics = client.get(ics_url)
    assert ics.status_code == 200 and "BEGIN:VEVENT" in ics.text and "Corte" in ics.text
    assert client.get(ics_url[:-3] + "xyz").status_code == 404                    # signature required
    bot = client.post(f"/b/salon-prueba/{svc['id']}", data={"csrf": tok, "starts_at": t, "name": "Bot", "phone": "9990000000",
                                                            "website": "http://spam"}, follow_redirects=False)
    assert bot.status_code == 303
    with db.tx() as conn:
        assert conn.execute(db.appointments.select()).mappings().all()[0]["notes"] == "primera vez"
        assert len(conn.execute(db.appointments.select()).mappings().all()) == 1


def test_owner_settings_screen(client):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Barbería Don Beto", "country": "MX", "industry": "salon",
                                 "email": "beto@example.com", "password": "una-clave-larga"})
    cal = client.get("/app/calendar").text
    tok = re.search(r'name="csrf" value="([^"]+)"', cal).group(1)
    client.post("/app/booking", data={"csrf": tok, "enabled": "1", "slug": "", "mon_a1": "10:00", "mon_b1": "19:00",
                                      "notice": "60", "ahead": "14"})
    client.post("/app/booking/services", data={"csrf": tok, "name": "Corte clásico", "minutes": "30", "price": "180"})
    cal = client.get("/app/calendar").text
    assert "/b/barberia-don-beto" in cal and "Corte clásico" in cal and "$180.00" in cal
    bad = client.post("/app/booking", data={"csrf": tok, "enabled": "1", "mon_a1": "19:00", "mon_b1": "10:00"}, follow_redirects=True)
    assert "not a valid opening" in bad.text
    assert client.get("/b/barberia-don-beto").status_code == 200


def test_slug_taken():
    setup_biz()
    other, _ = make_business("MX")
    with db.tx() as conn, pytest.raises(Invalid):
        BK.save_settings(conn, other, "t", slug="Salon Prueba")
    with db.tx() as conn, pytest.raises(NotFound):
        BK.public_business(conn, "nope")
