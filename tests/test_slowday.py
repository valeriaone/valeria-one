"""Slow-day rescue: tomorrow vs a usual day like it; asks (never sends alone); regulars only, with Grow's rules."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from _va import NOW, Net, actions, appt, customer, due_now, link_owner, refuse_network, shop, tg_in
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import followups as FU
from valeria_one.services import owner as OW
from valeria_one.services import slowday as SD
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


@pytest.fixture(autouse=True)
def afternoon(monkeypatch):
    monkeypatch.setattr(SD, "FROM_HOUR", 0)
    monkeypatch.setattr(OW, "OPEN_FROM", 0)
    monkeypatch.setattr(OW, "OPEN_UNTIL", 24)


def busy_history(biz, gw, per_day=4, weeks=4):
    tz = ZoneInfo(biz["timezone"])
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    regulars = [customer(biz, gw, f"Reg{i}", who=f"reg-{i}") for i in range(per_day)]
    for w in range(1, weeks + 1):
        for i, c in enumerate(regulars):
            appt(biz, c, datetime.combine(tomorrow - timedelta(weeks=w), time(9 + i), tz))
    return regulars, tomorrow


def test_quiet_tomorrow_asks_once_and_never_sends_alone():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    regulars, tomorrow = busy_history(biz, gw)
    with db.tx() as conn:
        lk = SD.look(conn, biz, NOW())
        with pytest.raises(Invalid):
            OW.grant(conn, biz, "t", "slowday", True)
    assert lk["quiet"] and lk["usual"] == 4 and lk["tomorrow"] == 0
    assert SD.round_()["asked"] == 1 and SD.round_()["asked"] == 0                    # once per day
    q = actions(biz, kind="slowday")[-1]
    assert q["status"] == "asked" and "4" in q["summary"]
    with db.tx() as conn:
        assert conn.execute(select(db.scheduled_messages.c.id)).first() is None        # nothing queued before the owner says yes


def test_a_normal_tomorrow_asks_nothing():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    regulars, tomorrow = busy_history(biz, gw)
    tz = ZoneInfo(biz["timezone"])
    for i in range(3):
        appt(biz, regulars[i], datetime.combine(tomorrow, time(10 + i), tz))
    assert SD.round_()["asked"] == 0


def test_yes_sends_only_to_regulars_who_agreed_and_were_not_offered_lately():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    regulars, tomorrow = busy_history(biz, gw)
    with db.tx() as conn:
        C.unsubscribe(conn, biz["id"], regulars[0]["id"], "t")                          # said STOP
        C.patch(conn, biz, "t", regulars[1]["id"], {"marketing_consent": False})        # no promotions
    once = customer(biz, gw, "Nueva", who="new-1")                                     # not a regular (no visits)
    SD.round_()
    q = actions(biz, kind="slowday")[-1]
    with db.tx() as conn:
        conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.id == q["id"]).values(prompted_at=NOW()))
    net = Net()
    tg_in(tg, "900", "sí", net=net)
    due_now(biz)
    OW.run_due(http=net.http)
    with db.tx() as conn:
        to = {r[0] for r in conn.execute(select(db.scheduled_messages.c.contact_id))}
    assert to == {regulars[2]["id"], regulars[3]["id"]} and once["id"] not in to
    FU.send_due(http=net.http)
    body = net.gw[-1]["body"]
    assert t("va_slow_msg", "es").split("{")[0].strip() in body or "Estética Sol" in body
    assert "%" not in body                                                               # no offer set: none invented
    with db.tx() as conn:                                                               # a second offer within 30 days: nobody
        assert SD.regulars(conn, biz, NOW()) == []
