"""Weekly proof: one honest number Valeria caused, a few lines, once a week; nothing = said kindly + one idea."""
from datetime import datetime, timedelta

import pytest

from _va import NOW, Net, appt, customer, link_owner, refuse_network, shop
from valeria_one import db
from valeria_one.services import briefing as BR
from valeria_one.services import growth as GR
from valeria_one.services import log
from valeria_one.services import owner as OW
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


@pytest.fixture
def monday_now(monkeypatch):
    monkeypatch.setattr(OW, "OPEN_FROM", 0)
    monkeypatch.setattr(OW, "OPEN_UNTIL", 24)
    monkeypatch.setattr(BR, "WEEKLY_FROM", 0)
    monkeypatch.setattr(BR, "WEEKLY_UNTIL", 24)
    monkeypatch.setattr(BR, "MORNING_UNTIL", 0)                    # only the weekly one in these tests


def test_nothing_yet_is_said_kindly_with_one_idea():
    biz, tg, gw = shop()
    with db.tx() as conn:
        text = BR.weekly(conn, biz, "es", NOW())
    assert text.startswith(t("va_week_nothing", "es")) and t("va_try_reminders", "es") in text


def test_only_attributable_results_and_the_biggest_is_the_headline():
    biz, tg, gw = shop()
    with db.tx() as conn:
        rule = GR.set_winback(conn, biz, "t", True)
    back, ignored = customer(biz, gw, "Ana"), customer(biz, gw, "Beto")
    with db.tx() as conn:
        conn.execute(db.scheduled_messages.insert().values(business_id=biz["id"], automation_id=rule["id"], contact_id=back["id"],
                                                           send_at=NOW() - timedelta(days=10), status="sent"))
    appt(biz, back, NOW() + timedelta(days=2))                                        # booked after the win-back: came back
    appt(biz, ignored, NOW() + timedelta(days=2))                                     # booked, but never got one: not counted
    with db.tx() as conn:
        for _ in range(3):
            log(conn, biz["id"], "booking.chat", "booking_chat", None)
        p = BR.proof(conn, biz, NOW())
        text = BR.weekly(conn, biz, "es", NOW())
    assert p["came_back"] == 1 and p["booked_chat"] == 3 and p["kept"] == 0
    assert text.splitlines()[0] == t("va_week_booked_chat", "es").format(n=3)
    assert t("va_wline_came_back", "es").format(n=1) in text


def test_old_results_dont_count():
    biz, tg, gw = shop()
    with db.tx() as conn:
        log(conn, biz["id"], "booking.chat", "booking_chat", None)
        conn.execute(db.events.update().where(db.events.c.type == "booking.chat").values(created_at=NOW() - timedelta(days=9)))
        assert BR.proof(conn, biz, NOW())["booked_chat"] == 0


def test_sent_once_a_week_on_monday(monday_now, monkeypatch):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(BR, "WEEKLY_DAY", datetime.now(ZoneInfo(biz["timezone"])).weekday())
    net = Net()
    assert BR.round_(http=net.http)["weekly"] == 1
    assert t("va_week_nothing", "es") in net.last()
    assert BR.round_(http=net.http)["weekly"] == 0
    monkeypatch.setattr(BR, "WEEKLY_DAY", (datetime.now(ZoneInfo(biz["timezone"])).weekday() + 1) % 7)
    other, tg2, _ = shop(name="Otra")
    link_owner(other, tg2, "901")
    assert BR.round_(http=net.http)["weekly"] == 0                                    # not its day


def test_weekly_off_means_no_message(monday_now, monkeypatch):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(BR, "WEEKLY_DAY", datetime.now(ZoneInfo(biz["timezone"])).weekday())
    with db.tx() as conn:
        OW.save_settings(conn, biz, "t", weekly=False)
    assert BR.round_(http=Net().http)["weekly"] == 0
