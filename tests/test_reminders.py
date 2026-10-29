"""Reminders that cut no-shows: "reply 1 to confirm, 2 to change", read in every language; unconfirmed in the summary."""
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from _va import NOW, Net, actions, appt, customer, gw_in, link_owner, refuse_network, shop, tg_in
from valeria_one import blocks, db
from valeria_one.services import businesses as B
from valeria_one.services import followups as FU
from valeria_one.services import reminders as RM
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def outbox(biz):
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.scheduled_messages).where(db.scheduled_messages.c.business_id == biz["id"])).mappings()]


def sent_reminder(biz, gw, name="Ana", who="ana-1", in_hours=20):
    c = customer(biz, gw, name, who=who)
    with db.tx() as conn:
        RM.set_on(conn, biz, "t", True, hours=24)
    a = appt(biz, c, NOW() + timedelta(hours=in_hours))                    # booked inside the 24 h: the reminder is due now
    with db.tx() as conn:
        conn.execute(db.scheduled_messages.insert().values(business_id=biz["id"], automation_id=RM.rule(conn, biz["id"])["id"],
                                                           contact_id=c["id"], subject_id=a["id"], send_at=NOW()))
    net = Net()
    assert FU.send_due(http=net.http)["sent"] == 1
    return c, a, net


def test_switching_on_makes_one_rule_and_backfills_bookings_already_made():
    biz, tg, gw = shop()
    c = customer(biz, gw, "Ana", who="ana-1")
    a = appt(biz, c, NOW() + timedelta(days=3))
    with db.tx() as conn:
        r = RM.set_on(conn, biz, "t", True, hours=24)
        assert RM.set_on(conn, biz, "t", True, hours=24)["id"] == r["id"]        # idempotent
    assert r["preset"] == "reminder" and r["minutes"] == 1440 and FU.needs(r) == ("calendar",)
    rows = outbox(biz)
    assert len(rows) == 1 and rows[0]["subject_id"] == a["id"]
    assert abs((rows[0]["send_at"] - (a["starts_at"] - timedelta(hours=24))).total_seconds()) < 1
    with db.tx() as conn:
        r2 = RM.set_on(conn, biz, "t", True, hours=48)
    assert r2["minutes"] == 2880 and [x["status"] for x in outbox(biz)].count("pending") == 1
    with db.tx() as conn:
        RM.set_on(conn, biz, "t", False)
        assert RM.rule(conn, biz["id"]) is None


def test_the_reminder_says_reply_1_or_2_and_follows_the_style():
    biz, tg, gw = shop()
    with db.tx() as conn:
        from valeria_one.services import style
        style.save(conn, biz, "t", {"signoff": "— Sol"})
    c, a, net = sent_reminder(biz, gw)
    body = net.gw[-1]["body"]
    assert "1" in body and "2" in body and "Ana" in body and body.endswith("— Sol") and "{" not in body


@pytest.mark.parametrize("answer", ["1", "sí", "Si!", "confirm", "ok", "はい", "是", "oo", "ya", "haan", "نعم"])
def test_1_confirms_in_any_language(answer):
    biz, tg, gw = shop()
    c, a, net = sent_reminder(biz, gw)
    _, res = gw_in(gw, "ana-1", answer, net=net)
    assert res == ["reminder:confirmed"]
    with db.tx() as conn:
        assert conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar() == "confirmed"
        assert RM.kept(conn, biz["id"], NOW() - timedelta(days=1)) == 1


@pytest.mark.parametrize("answer", ["2", "cambiar", "reagendar", "reschedule", "変更", "改期", "palitan", "ganti", "बदलें", "تغيير"])
def test_2_asks_the_owner_and_tells_the_customer(answer):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    c, a, net = sent_reminder(biz, gw)
    _, res = gw_in(gw, "ana-1", answer, net=net)
    assert res == ["reminder:change"]
    q = actions(biz, kind="reschedule")[-1]
    assert q["status"] == "asked" and q["payload"]["appointment_id"] == str(a["id"])
    with db.tx() as conn:
        assert conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar() == "booked"   # never alone


def test_owner_cancels_and_the_customer_gets_the_booking_link():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    c, a, net = sent_reminder(biz, gw)
    gw_in(gw, "ana-1", "2", net=net)
    with db.tx() as conn:
        from valeria_one.services import owner as OW
        q = actions(biz, kind="reschedule")[-1]
        conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.id == q["id"]).values(prompted_at=NOW()))
    tg_in(tg, "900", "1", net=net)                                          # 1 = cancel and send the booking link
    from _va import due_now
    due_now(biz)
    OW.run_due(http=net.http)
    with db.tx() as conn:
        assert conn.execute(select(db.appointments.c.status).where(db.appointments.c.id == a["id"])).scalar() == "cancelled"
    assert f"/b/{biz['slug']}" in net.gw[-1]["body"]


def test_a_1_without_a_recent_reminder_is_just_a_message():
    biz, tg, gw = shop(booking=False)
    customer(biz, gw, "Ana", who="ana-1")
    _, res = gw_in(gw, "ana-1", "1")
    assert res == ["off"]


def test_unconfirmed_bookings_show_in_the_summary():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    tz = ZoneInfo(biz["timezone"])
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    c = customer(biz, gw, "Ana", who="ana-1")
    a = appt(biz, c, datetime.combine(tomorrow, time(10), tz))
    with db.tx() as conn:
        RM.set_on(conn, biz, "t", True, hours=1)
        conn.execute(db.scheduled_messages.update().where(db.scheduled_messages.c.subject_id == a["id"]).values(status="sent"))
        assert [u["id"] for u in RM.unconfirmed(conn, biz, tomorrow)] == [a["id"]]
    net = Net()
    tg_in(tg, "900", "mañana", net=net)
    assert "Ana" in net.last() and t("va_unconfirmed", "es").split("(")[0] in net.last()


def test_calendar_off_skips_reminders_and_grow_off_leaves_them_alone():
    biz, tg, gw = shop()
    c = customer(biz, gw, "Ana", who="ana-1")
    appt(biz, c, NOW() + timedelta(days=3))
    with db.tx() as conn:
        RM.set_on(conn, biz, "t", True)
        B.set_blocks(conn, biz["id"], "t", [b for b in blocks.BLOCKS if b != "grow"])
    assert [r["status"] for r in outbox(biz)] == ["pending"]                     # Grow off: the reminder stays
    with db.tx() as conn:
        conn.execute(db.scheduled_messages.update().values(send_at=NOW()))
        B.set_blocks(conn, biz["id"], "t", [b for b in blocks.BLOCKS if b != "calendar"])
    assert FU.send_due(http=Net().http)["skipped"] == 1
    assert outbox(biz)[0]["error"] == "calendar switched off"
