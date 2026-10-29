"""Pay by link from the chat: the business's OWN payment account; always asks; who owes me + a kind reminder."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from _va import NOW, Net, actions, customer, due_now, link_owner, refuse_network, shop, tg_in
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import owner as OW
from valeria_one.services import payments as PM
from valeria_one.web.i18n import t


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def bank(biz):
    with db.tx() as conn:
        PM.connect(conn, biz, "t", "bank_transfer", {"bank": "BBVA", "holder": "Sol SA", "account": "0123", "clabe": "012180001234567890"})


def test_no_payment_account_points_to_the_one_time_setup():
    biz, tg, gw = shop()
    link_owner(biz, tg, "900")
    customer(biz, gw, "Juan")
    net = Net()
    tg_in(tg, "900", "manda a Juan un link de pago por 500", net=net)
    assert "/app/settings#payments" in net.last()
    assert not actions(biz, kind="paylink")


def test_payment_link_always_asks_then_goes_from_the_business_account():
    biz, tg, gw = shop()
    bank(biz)
    link_owner(biz, tg, "900")
    juan = customer(biz, gw, "Juan", who="juan-1")
    net = Net()
    tg_in(tg, "900", "send Juan a payment link for 500 for the haircut", net=net)
    assert "$500.00" in net.last() and "haircut" in net.last()
    with db.tx() as conn:
        assert not PM.list_(conn, biz["id"])                                          # nothing made until the owner says yes
        with pytest.raises(Invalid):
            OW.grant(conn, biz, "t", "paylink", True)
    tg_in(tg, "900", "1", net=net)
    assert net.last() == t("va_queued", "es")
    due_now(biz)
    assert OW.run_due(http=net.http)["done"] == 1
    with db.tx() as conn:
        pays = PM.list_(conn, biz["id"])
    assert len(pays) == 1 and pays[0]["amount_minor"] == 50000 and pays[0]["contact_id"] == juan["id"]
    assert pays[0]["provider"] == "bank_transfer" and f"/p/{pays[0]['token']}" in net.gw[-1]["body"]


def test_who_owes_me_and_a_kind_reminder_at_most_weekly():
    biz, tg, gw = shop()
    bank(biz)
    link_owner(biz, tg, "900")
    juan = customer(biz, gw, "Juan", who="juan-1")
    PM.create(biz, "t", amount_minor=30000, description="Tinte", contact_id=juan["id"])
    PM.record_received(biz, "t", amount_minor=9900, description="Cash", contact_id=juan["id"])   # paid: never "owed"
    net = Net()
    tg_in(tg, "900", "¿quién me debe?", net=net)
    assert "Juan" in net.last() and "Tinte" in net.last() and "Cash" not in net.last()
    tg_in(tg, "900", "sí", net=net)
    due_now(biz)
    OW.run_due(http=net.http)
    assert "Tinte" in net.gw[-1]["body"] and "/p/" in net.gw[-1]["body"]
    n = len(net.gw)
    tg_in(tg, "900", "recuérdales a los que me deben", net=net)
    tg_in(tg, "900", "1", net=net)
    due_now(biz)
    OW.run_due(http=net.http)
    assert len(net.gw) == n                                                             # once a week per payment
    assert t("va_payrem_sent", "es").format(n=0) in [a["result"].get("say") for a in actions(biz, kind="pay_reminder")]


def test_staff_without_money_cannot_send_links():
    biz, tg, gw = shop()
    bank(biz)
    link_owner(biz, tg, "901", role="staff", perms=["view", "reply"])
    customer(biz, gw, "Juan")
    net = Net()
    tg_in(tg, "901", "send Juan a payment link for 500", net=net)
    assert net.last() == t("va_not_allowed", "es")


def test_unpaid_lists_only_pending_links():
    biz, tg, gw = shop()
    bank(biz)
    juan = customer(biz, gw, "Juan")
    p = PM.create(biz, "t", amount_minor=1000, description="A", contact_id=juan["id"])
    with db.tx() as conn:
        assert [r["id"] for r in PM.unpaid(conn, biz["id"])] == [p["id"]] and PM.can_link(conn, biz["id"])
        PM.cancel(conn, biz["id"], "t", p["id"])
        assert PM.unpaid(conn, biz["id"]) == []
        assert conn.execute(select(db.payments.c.status)).scalar() == "cancelled"
    assert NOW() - timedelta(days=1) < NOW()
