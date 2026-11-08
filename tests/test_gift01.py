"""gift01 (v0.55.0): gift cards — sold (paid now, or by the business's own pay link → active when paid), sent to the recipient
from the business's own account, the public card page, redeemed in parts atomically (never below zero), expiry, void."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from _va import csrf, owner_session
from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid, log
from valeria_one.services import giftcards as GC


def shop():
    biz, _ = make_business("MX", "salon", name="Estética Sol")
    return biz


def test_sell_paid_now_use_in_parts_never_below_zero():
    biz = shop()
    card = GC.create(biz, "t", amount_minor=50000, recipient_name="Ana Ruiz", recipient_phone="999 111 2233", message="¡Feliz cumple!")
    assert card["status"] == "active" and card["balance_minor"] == 50000 and len(card["code"]) == 10
    assert all(ch not in card["code"] for ch in "0O1I")                                   # easy to read aloud
    with db.tx() as conn:
        r = GC.redeem(conn, biz, "t", GC.pretty(card["code"]).lower(), 20000, "Corte")   # typed with a dash, lowercase: fine
    assert r["balance_minor"] == 30000
    with db.tx() as conn:
        with pytest.raises(Invalid) as e:
            GC.redeem(conn, biz, "t", card["code"], 30001)
    assert str(e.value) == "gc_not_enough"
    with db.tx() as conn:
        assert GC.redeem(conn, biz, "t", card["code"], 30000)["balance_minor"] == 0
        assert [u["amount_minor"] for u in GC.uses(conn, biz["id"], card["id"])] == [20000, 30000]


def test_paid_by_link_activates_when_paid_and_other_rules(monkeypatch):
    biz = shop()
    from valeria_one.services import payments as PM
    monkeypatch.setattr(PM, "create", lambda *a, **k: {"id": _payment(biz, k["amount_minor"])})
    card = GC.create(biz, "t", amount_minor=30000, paid_now=False)
    assert card["status"] == "pending"
    with db.tx() as conn:
        with pytest.raises(Invalid) as e:
            GC.redeem(conn, biz, "t", card["code"], 100)
        assert str(e.value) == "gc_not_paid"
        log(conn, biz["id"], "payment.paid", "webhook", card["payment_id"], {})              # their pay link was paid
        assert GC.get(conn, biz["id"], card["id"])["status"] == "active"
    other = shop()
    with db.tx() as conn:
        with pytest.raises(Invalid) as e:
            GC.redeem(conn, other, "t", card["code"], 100)                                 # another business's card: unknown here
        assert str(e.value) == "gc_not_found"
    old = GC.create(biz, "t", amount_minor=1000, expires_months=3)
    with db.tx() as conn:
        with pytest.raises(Invalid) as e:
            GC.redeem(conn, biz, "t", old["code"], 100, today=date.today() + timedelta(days=200))
        assert str(e.value) == "gc_expired"
        GC.void(conn, biz, "t", old["id"])
        with pytest.raises(Invalid) as e:
            GC.redeem(conn, biz, "t", old["code"], 100)
        assert str(e.value) == "gc_void"


def _payment(biz, amount):
    with db.tx() as conn:
        return conn.execute(db.payments.insert().values(business_id=biz["id"], provider="stripe", amount_minor=amount, currency="MXN",
                                                         description="x", status="pending", token="tok" + str(amount))
                            .returning(db.payments.c.id)).scalar()


def test_send_to_the_recipient_from_their_own_account(monkeypatch):
    biz = shop()
    sent = []
    from valeria_one.services import inbox as X
    monkeypatch.setattr(X, "send", lambda biz_, actor, **k: sent.append(k) or {"status": "sent"})
    card = GC.create(biz, "t", amount_minor=50000, recipient_name="Ana Ruiz", recipient_phone="999 111 2233", message="¡Feliz cumple!")
    GC.send(biz, "t", card["id"])
    assert "Ana" in sent[-1]["body"] and f"/g/{card['code']}" in sent[-1]["body"] and "¡Feliz cumple!" in sent[-1]["body"]
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.id == sent[-1]["contact_id"])).mappings().one()
        assert c["phone"] == "+529991112233" and GC.get(conn, biz["id"], card["id"])["sent_at"]
    GC.send(biz, "t", card["id"])                                                          # again: the same person, no duplicate
    with db.tx() as conn:
        assert conn.execute(select(db.contacts.c.id).where(db.contacts.c.business_id == biz["id"])).all().__len__() == 1


def test_screens_and_the_public_card(client):
    biz = shop()
    owner_session(client, biz)
    assert "/app/gift-cards" in client.get("/app/invoices").text
    tok = csrf(client, "/app/gift-cards")
    r = client.post("/app/gift-cards", data={"csrf": tok, "act": "sell", "amount": "500", "recipient_name": "Ana", "paid": "now"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/app/gift-cards/" in r.headers["location"]
    with db.tx() as conn:
        card = conn.execute(select(db.gift_cards)).mappings().one()
    assert card["amount_minor"] == 50000
    page = client.get(r.headers["location"]).text
    assert GC.pretty(card["code"]) in page and f"/g/{card['code']}" in page
    client.post("/app/gift-cards", data={"csrf": tok, "act": "redeem", "code": card["code"], "use": "120.50"})
    with db.tx() as conn:
        assert conn.execute(select(db.gift_cards.c.balance_minor)).scalar() == 50000 - 12050
    pub = client.get(f"/g/{card['code']}").text
    assert "Estética Sol" in pub and ("379.50" in pub or "379,50" in pub) and 'name="robots" content="noindex"' in pub
    assert client.get("/g/NOPE000000").status_code == 404
