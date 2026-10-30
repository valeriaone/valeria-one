"""ghl01 (v0.18): invoices (numbered per business, tax from the country pack, from an accepted quote, a pay link from the
business's own account, paid when that link is paid, overdue reminders that ask first) and subscriptions (one invoice per
period, never two; pause/cancel; MRR). No money ever flows through Valeria One; the network is refused."""
import json
import re
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from _va import refuse_network
from conftest import make_business
from test_web import signup
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import inbox as X
from valeria_one.services import invoices as IVS
from valeria_one.services import payments as PM
from valeria_one.services import quotes as QS

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
LINES = [{"description": "Corte", "qty": 2, "unit_minor": 5000}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def gateway(log):
    def handler(request):
        log.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"gw{len(log)}"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def setup(country="MX"):
    biz, _ = make_business(country, "salon", name="Salón Sol")
    with db.tx() as conn:
        X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/send", "secret": "s"})
        ana = C.create(conn, biz, "t", {"name": "Ana López", "phone": "+525511110001"})
        C.link(conn, biz["id"], ana["id"], "webhook", "ana-1")
    return biz, ana


def test_numbered_per_business_with_the_country_tax():
    mx, ana = setup("MX")
    us, _ = setup("US")
    with db.tx() as conn:
        a = IVS.create(conn, mx, "t", title="Visita", lines=LINES, contact_id=ana["id"])
        b = IVS.create(conn, mx, "t", title="Visita 2", lines=LINES)
        c = IVS.create(conn, us, "t", title="Visit", lines=LINES)
        with pytest.raises(Invalid):
            IVS.create(conn, mx, "t", title="x", lines=[{"description": "a", "qty": 0, "unit_minor": 1}])
    assert (a["number"], b["number"], c["number"]) == (1, 2, 1)
    assert (a["subtotal_minor"], a["tax_minor"], a["total_minor"], a["tax_name"]) == (10000, 1600, 11600, "IVA")
    assert c["tax_minor"] is None and c["total_minor"] == 10000                                  # US sales tax: left to the provider
    assert a["status"] == "draft" and a["due_on"] == a["issued_on"] + timedelta(days=15)


def test_an_accepted_quote_becomes_its_invoice_once():
    biz, ana = setup()
    with db.tx() as conn:
        q = QS.create(conn, biz, "t", title="Boda", lines=[{"description": "Peinado", "qty": 1, "unit_minor": 80000}], contact_id=ana["id"])
        with pytest.raises(Invalid):
            IVS.from_quote(conn, biz, "t", q["id"])                                           # not accepted yet
        QS.accept(conn, q["token"], name="Ana López", agree=True, ip="1.1.1.1", ua="t")
        inv = IVS.from_quote(conn, biz, "t", q["id"])
        again = IVS.from_quote(conn, biz, "t", q["id"])
    assert inv["id"] == again["id"] and inv["quote_id"] == q["id"]
    assert (inv["total_minor"], inv["tax_minor"], inv["lines"]) == (q["total_minor"], q["tax_minor"], q["lines"])


def test_send_with_a_pay_link_from_the_businesss_own_account_and_paid_when_paid(client):
    biz, ana = setup()
    with db.tx() as conn:
        PM.connect(conn, biz, "t", "bank_transfer", {"bank": "BBVA", "holder": "Salón Sol", "account": "0123", "clabe": "012180001234567891"})
        inv = IVS.create(conn, biz, "t", title="Visita", lines=LINES, contact_id=ana["id"])
    assert client.get(f"/inv/{inv['token']}").status_code == 404                             # a draft isn't the customer's yet
    log = []
    sent = IVS.send(biz, "user:x", inv["id"], http=gateway(log))
    assert sent["status"] == "sent" and sent["payment_id"] and sent["message_problem"] is None
    assert f"https://one.test/inv/{inv['token']}" in log[0]["body"] and "#1" in log[0]["body"] and "Ana" in log[0]["body"]
    page = client.get(f"/inv/{inv['token']}")
    assert page.status_code == 200 and "Salón Sol" in page.text and "window.print()" in page.text and "/p/" in page.text
    with db.tx() as conn:
        PM.mark_paid(conn, sent["payment_id"], "owner")
        assert IVS.get(conn, biz["id"], inv["id"])["status"] == "paid"
        with pytest.raises(Invalid):
            IVS.void(conn, biz, "t", inv["id"])
    assert client.get("/inv/nope").status_code == 404


def test_without_a_payment_account_it_still_goes_and_can_be_marked_paid():
    biz, ana = setup()
    with db.tx() as conn:
        inv = IVS.create(conn, biz, "t", title="Visita", lines=LINES, contact_id=ana["id"])
    log = []
    sent = IVS.send(biz, "user:x", inv["id"], http=gateway(log))
    assert sent["status"] == "sent" and sent["payment_id"] is None and len(log) == 1
    with db.tx() as conn:
        assert IVS.mark_paid(conn, biz, "t", inv["id"])["status"] == "paid"


def test_overdue_and_the_reminder_asks_first_once_a_week():
    biz, ana = setup()
    with db.tx() as conn:
        inv = IVS.create(conn, biz, "t", title="Visita", lines=LINES, contact_id=ana["id"], due_days=0)
    IVS.send(biz, "user:x", inv["id"], http=gateway([]))
    with db.tx() as conn:
        conn.execute(update(db.invoices).values(due_on=date.today() - timedelta(days=3)))
    st = IVS.round_()
    assert st["overdue"] == 1 and st["asked"] == 1
    with db.tx() as conn:
        a = [dict(r) for r in conn.execute(select(db.assistant_actions)).mappings()]
        assert IVS.get(conn, biz["id"], inv["id"])["status"] == "overdue"
    assert a[0]["kind"] == "pay_reminder" and a[0]["status"] == "asked" and a[0]["payload"]["invoice_ids"] == [str(inv["id"])]
    assert IVS.round_()["asked"] == 0                                                         # not again this week
    from valeria_one.services import actions
    from valeria_one.services import owner as OW
    log = []
    with db.tx() as conn:
        OW.decide(conn, biz, a[0]["id"], "yes", "user:x")
    ok = actions.run({**a[0], "status": "running"}, http=gateway(log))
    assert ok and f"/inv/{inv['token']}" in log[-1]["body"]


def test_subscription_makes_one_invoice_per_period_and_pauses():
    biz, ana = setup()
    with db.tx() as conn:
        plan = IVS.create_plan(conn, biz, "t", name="Membresía", amount_minor=50000, interval="month")
        week = IVS.create_plan(conn, biz, "t", name="Semanal", amount_minor=1200, interval="week")
        sub = IVS.subscribe(conn, biz, "t", plan_id=plan["id"], contact_id=ana["id"], start_on=date.today() - timedelta(days=1),
                            auto_send=False)
        IVS.subscribe(conn, biz, "t", plan_id=week["id"], contact_id=ana["id"], auto_send=False)
        assert IVS.mrr(conn, biz["id"]) == 50000 + round(1200 * 52 / 12)
    st = IVS.round_()
    assert st["made"] == 2 and IVS.round_()["made"] == 0                                     # never two for one period
    with db.tx() as conn:
        s = dict(conn.execute(select(db.subscriptions).where(db.subscriptions.c.id == sub["id"])).mappings().one())
        assert s["next_on"] == IVS.add_period(sub["started_on"], "month", 1)
        p = dict(conn.execute(select(db.plans).where(db.plans.c.id == plan["id"])).mappings().one())
        assert IVS._period_invoice(conn, biz, {**s, "next_on": sub["started_on"]}, p) is None   # the same period again: no
        invs = IVS.list_(conn, biz["id"])
        assert {i["period_start"] for i in invs if i["subscription_id"] == sub["id"]} == {sub["started_on"]}
        IVS.set_subscription(conn, biz, "t", sub["id"], "pause")
        conn.execute(update(db.subscriptions).where(db.subscriptions.c.id == sub["id"]).values(next_on=date.today() - timedelta(days=1)))
    assert IVS.round_()["made"] == 0
    with db.tx() as conn:
        IVS.set_subscription(conn, biz, "t", sub["id"], "resume")
        IVS.set_subscription(conn, biz, "t", sub["id"], "cancel")
        with pytest.raises(Invalid):
            IVS.set_subscription(conn, biz, "t", sub["id"], "pause")
    assert IVS.add_period(date(2027, 1, 31), "month", 1) == date(2027, 2, 28)
    assert IVS.add_period(date(2027, 2, 28), "month", 1, anchor_day=31) == date(2027, 3, 31)


def test_the_screens(client):
    signup(client, email="iv@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        ana = C.create(conn, biz, "t", {"name": "Ana", "phone": "+525511110001"})
    page = client.get("/app/invoices")
    assert page.status_code == 200
    t = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/app/invoices", data={"csrf": t, "contact_id": str(ana["id"]), "title": "Visita", "line_desc": ["Corte", ""],
                                           "line_qty": ["1", "1"], "line_unit": ["250", ""], "due_days": "10"}, follow_redirects=False)
    url = r.headers["location"]
    assert "IVA" in client.get(url).text
    client.post(url, data={"csrf": t, "act": "send", "account_id": "none"})
    with db.tx() as conn:
        inv = dict(conn.execute(select(db.invoices)).mappings().one())
    assert inv["status"] == "sent" and inv["total_minor"] == 29000
    assert client.get(f"/inv/{inv['token']}").status_code == 200
    assert client.get("/app/invoices?tab=subscriptions").status_code == 200
    client.post("/app/plans", data={"csrf": t, "name": "Mensual", "amount": "499", "interval": "month", "every": "1"})
    assert "Mensual" in client.get("/app/invoices?tab=subscriptions").text
    assert f'/app/invoices/{inv["id"]}' in client.get(f"/app/contacts/{ana['id']}").text


def test_new_words_are_in_all_11_languages():
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.startswith(("iv_", "sb_"))] + ["invoices", "today_mrr", "copy"]
    assert len(new) > 80
    for lg in LANGS:
        for k in new:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    _ = datetime, timezone
