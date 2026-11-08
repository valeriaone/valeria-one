"""nps01 (v0.53.0): «del 0 al 10» after each visit — one tap to switch on, their number reply is their score, a 6 or less
reaches the owner at once (push + «Llámale» on Today), the NPS on Today. It never decides who gets a review request."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from _va import csrf, owner_session
from test_calls_reviews import _customer, call, connect, twilio  # noqa: F401  (fixture)
from valeria_one import db
from valeria_one.services import businesses as B
from valeria_one.services import deals as D
from valeria_one.services import followups as FU
from valeria_one.services import nps as NPS


def _visit_done_and_send(biz, cid):
    st = datetime.now(timezone.utc) - timedelta(hours=5)
    with db.tx() as conn:
        a = D.book(conn, biz, "t", {"title": "Visit", "contact_id": cid, "starts_at": st, "ends_at": st + timedelta(hours=1)})
        D.set_appointment_status(conn, biz["id"], "t", a["id"], "done")
    ny = (datetime.now(timezone.utc) + timedelta(hours=4)).astimezone(ZoneInfo(biz["timezone"]))
    if not 10 <= ny.hour < 18:
        ny = (ny + timedelta(days=1)).replace(hour=12, minute=0)
    return FU.send_due(now=ny.astimezone(timezone.utc))


def test_one_tap_survey_is_asked_after_a_visit_and_a_number_is_their_score(client, us, twilio, monkeypatch):
    bz, h = us
    path = connect(client, h, register=False, monkeypatch=monkeypatch)
    cid = _customer(client, h)
    owner_session(client, bz)
    tok = csrf(client, "/app/followups")
    assert "app/followups/survey-preset" in client.get("/app/followups").text
    client.post("/app/followups/survey-preset", data={"csrf": tok})
    page = client.get("/app/followups").text
    assert "app/followups/survey-preset" not in page                                     # switched on once
    with db.tx() as conn:
        rule = [r for r in FU.automations(conn, bz["id"]) if "{survey}" in r["message"]][0]
        biz = B.get(conn, bz["id"])
    assert rule["trigger"] == "appointment.done" and rule["minutes"] == 180 and rule["kind"] == "service"
    res = _visit_done_and_send(biz, cid)
    assert res["sent"] == 1
    body = twilio.sent[-1]["Body"][0]
    assert "0 to 10" in body and "{survey}" not in body and "{name}" not in body
    pushed = []
    from valeria_one.services import push
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushed.append(a) or 1)
    call(client, path, {"From": "+19514343352", "To": "+15550001111", "Body": "4", "MessageSid": "SM-in-1"})
    with db.tx() as conn:
        row = conn.execute(select(db.nps_responses).where(db.nps_responses.c.contact_id == cid)).mappings().one()
    assert row["score"] == 4 and row["answered_at"]
    assert pushed and "4/10" in pushed[-1][2]                                            # a detractor reaches the owner at once
    call(client, path, {"From": "+19514343352", "To": "+15550001111", "Body": "10", "MessageSid": "SM-in-2"})
    with db.tx() as conn:
        assert conn.execute(select(db.nps_responses.c.score).where(db.nps_responses.c.contact_id == cid)).scalar() == 4   # answered once


def test_only_a_plain_number_soon_after_being_asked_counts(us):
    bz, _ = us
    with db.tx() as conn:
        from valeria_one.services import contacts as C
        c = C.create(conn, bz, "t", {"name": "Ana", "phone": "+19514343399"})
        now = datetime.now(timezone.utc)
        assert NPS.on_inbound(conn, bz, c, "9", now) is None                                # never asked: nothing
        NPS.asked(conn, bz, c["id"], now - timedelta(days=4))
        assert NPS.on_inbound(conn, bz, c, "9", now) is None                                # asked too long ago
        NPS.asked(conn, bz, c["id"], now - timedelta(hours=2))
        assert NPS.on_inbound(conn, bz, c, "hola, a las 9 está bien?", now) is None         # not a score
        assert NPS.on_inbound(conn, bz, c, "9/10", now) == 9


def test_the_nps_number_and_today(client, us):
    bz, _ = us
    from valeria_one.services import contacts as C
    now = datetime.now(timezone.utc)
    with db.tx() as conn:
        for i, s in enumerate((10, 9, 9, 7, 3)):
            c = C.create(conn, bz, "t", {"name": f"P{i}", "phone": f"+1951434{3300 + i}"})
            NPS.asked(conn, bz, c["id"], now - timedelta(hours=1))
            conn.execute(db.nps_responses.update().where(db.nps_responses.c.contact_id == c["id"]).values(score=s, answered_at=now))
        s = NPS.summary(conn, bz["id"])
    assert (s["n"], s["promoters"], s["detractors"], s["nps"]) == (5, 3, 1, 40) and s["recent_low"][0]["name"] == "P4"
    owner_session(client, bz)
    h = client.get("/app").text
    assert 'id="nps"' in h and ">40<" in h and "P4" in h
