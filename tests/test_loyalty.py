"""Loyalty without cards: visits counted once a day, every Nth earns the reward, the customer is told only with
consent and after the owner says yes (unless granted), never with a review."""
from datetime import timedelta

import pytest

from _va import NOW, Net, actions, appt, customer, due_now, link_owner, owner_session, csrf, refuse_network, shop, tg_in
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import deals as D
from valeria_one.services import loyalty as LY
from valeria_one.services import owner as OW
from valeria_one.web.i18n import T, t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def done_visit(biz, c, days_ago):
    a = appt(biz, c, NOW() - timedelta(days=days_ago))
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", a["id"], "done")


def visit_on(biz, c, days_ago):
    with db.tx() as conn:
        return LY.visit(conn, biz, c["id"], "manual", now=NOW() - timedelta(days=days_ago))


def test_off_by_default_visits_still_counted_once_a_day():
    biz, tg, gw = shop()
    ana = customer(biz, gw, "Ana")
    done_visit(biz, ana, 0)
    from valeria_one.services import payments as PM
    PM.record_received(biz, "t", amount_minor=100, description="x", contact_id=ana["id"])     # same day: one visit
    with db.tx() as conn:
        assert LY.count(conn, biz["id"], ana["id"]) == 1 and LY.rule(conn, biz["id"])["on"] is False
        assert LY.rewards(conn, biz["id"]) == []


def test_every_nth_visit_earns_and_asks_before_telling_the_customer():
    biz, tg, gw = shop()
    ana = customer(biz, gw, "Ana", who="ana-1")
    with db.tx() as conn:
        LY.set_rule(conn, biz, "t", 3, "10% off")
        with pytest.raises(Invalid):
            LY.set_rule(conn, biz, "t", 1, "x")
        with pytest.raises(Invalid):
            LY.set_rule(conn, biz, "t", 5, "")
    for d in (3, 2):
        assert visit_on(biz, ana, d) is None
    rw = visit_on(biz, ana, 0)
    assert rw and rw["visit_number"] == 3 and rw["reward"] == "10% off"
    q = actions(biz, kind="loyalty")[-1]
    assert q["status"] == "asked" and "10% off" in q["payload"]["text"] and "3" in q["payload"]["text"]
    assert "/r/" not in q["payload"]["text"]                                             # never with a review link
    with db.tx() as conn:
        used = LY.mark_used(conn, biz["id"], "t", rw["id"])
        assert used["used_at"] and LY.rewards(conn, biz["id"], open_only=True) == []


def test_no_consent_no_message_but_the_reward_stands():
    biz, tg, gw = shop()
    ana = customer(biz, gw, "Ana", consent=False)
    with db.tx() as conn:
        LY.set_rule(conn, biz, "t", 2, "café gratis")
    visit_on(biz, ana, 1)
    assert visit_on(biz, ana, 0)["reward"] == "café gratis"
    assert actions(biz, kind="loyalty") == []


def test_granted_loyalty_goes_alone_and_the_message_is_a_promotion():
    biz, tg, gw = shop()
    ana = customer(biz, gw, "Ana", who="ana-1")
    with db.tx() as conn:
        LY.set_rule(conn, biz, "t", 2, "10% off")
        OW.grant(conn, biz, "t", "loyalty", True)
    visit_on(biz, ana, 1)
    visit_on(biz, ana, 0)
    a = actions(biz, kind="loyalty")[-1]
    assert a["status"] == "queued" and a["payload"]["auto"] is True
    due_now(biz)
    net = Net()
    OW.run_due(http=net.http)
    assert "10% off" in net.gw[-1]["body"]
    with db.tx() as conn:
        m = conn.execute(db.messages.select().where(db.messages.c.direction == "out")).mappings().all()[-1]
    assert m["kind"] == "marketing"


def test_owner_marks_a_visit_by_chat_and_on_the_customer_page(client):
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    ana = customer(biz, gw, "Ana")
    net = Net()
    tg_in(tg, "900", "Ana vino hoy, márcale una visita", net=net)
    assert net.last() == t("va_visit_marked", "es").format(name="Ana", n=1)
    owner_session(client, biz)
    page = client.get(f"/app/contacts/{ana['id']}").text
    assert t("ly_card", "es") in page
    client.post(f"/app/contacts/{ana['id']}/visit", data={"csrf": csrf(client, f"/app/contacts/{ana['id']}")})
    with db.tx() as conn:
        assert LY.count(conn, biz["id"], ana["id"]) == 1                                  # same day: still one


def test_loyalty_words_never_mention_reviews():
    for lang, words in T["va_loyalty_msg"].items():
        assert "{review" not in words
    assert "{reward" not in "".join(T["review_ask"].values())
