"""ars01 (v0.20): point of sale, inventory, staff and books. Money is exact integers in the smallest unit (tax, discount,
change, refunds, pay, profit); selling takes stock off the shelf and a void or refund puts it back; nothing is deleted
(void / refund / voided hours / voided book lines are marks); a pay link is the business's own; nothing is sent by itself;
every query stays inside its business; staff sell and count, the owner handles money going back, prices, pay and books;
Books counts every paid invoice, sale and pay link exactly once; every new word is in all 11 languages; the kits switch
the new tools on where they fit; a business that already holds data in a tool keeps it."""
import csv
import hashlib
import importlib.util
import io
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from _va import owner_session, refuse_network
from conftest import make_business
from test_web import signup
from valeria_one import db, kits, money, packs, qr
from valeria_one.api import app
from valeria_one.services import Invalid, NotFound
from valeria_one.services import books as BK
from valeria_one.services import businesses as B
from valeria_one.services import contacts as C
from valeria_one.services import inventory as INV
from valeria_one.services import invoices as IVS
from valeria_one.services import mailer
from valeria_one.services import payments as PM
from valeria_one.services import pos as POS
from valeria_one.services import staff as SF

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
NEW = ("pos", "inventory", "staff", "books")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def signed_in(client, country="MX", industry="shop", role="owner", name=None):
    biz, _ = make_business(country, industry, name=name or f"Tienda {country}")
    uid = owner_session(client, biz, role=role)
    client.post("/lang/en", data={"csrf": tok(client)})
    return biz, uid


def other_client():
    return TestClient(app, raise_server_exceptions=False, base_url="https://testserver")


def item(biz, name="Rosa", price=11600, stock=10, low_at=None, **kw):
    with db.tx() as conn:
        return INV.add(conn, biz, "t", {"name": name, "price_minor": price, "stock": stock, "low_at": low_at, **kw})


def sale(conn, sid):
    return conn.execute(select(db.pos_sales).where(db.pos_sales.c.id == sid)).mappings().one()


def stock(iid):
    with db.tx() as conn:
        return conn.execute(select(db.catalog_items.c.stock).where(db.catalog_items.c.id == iid)).scalar()


def nav_hrefs(client):
    return re.findall(r'<a href="([^"]+)"', client.get("/app").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0])


# ---------------------------------------------------------------- money: exact integers
def test_the_money_rules_are_exact_integers():
    assert money.split(20880, 1600, True) == (18000, 2880, 20880)          # IVA inside a Mexican shelf price
    assert money.split(5900, 825, False) == (5900, 487, 6387)              # 486.75 -> 487: half up, on top
    assert money.split(1, 1600, True) == (1, 0, 1)
    assert money.split(3, 5000, False) == (3, 2, 5)                       # 1.5 -> 2
    assert money.part(5, 1000) == 1 and money.part(4, 1000) == 0 and money.part(36850, 1000) == 3685
    mx = packs.country("MX")                                               # price() is the same rule as before
    assert money.price(11600, mx) == money.Price(10000, 1600, 11600, "MXN", "IVA")


def test_register_totals_tax_discount_and_change(client):
    biz, _ = signed_in(client)
    rosa = item(biz, stock=10)
    r = client.post("/app/pos", data={"csrf": tok(client, "/app/pos"), "line_item": [str(rosa["id"]), ""], "line_desc": ["", "Envoltura"],
                                      "line_qty": ["3", "1"], "line_unit": ["", "20.50"], "discount": "10", "discount_kind": "pct",
                                      "method": "cash", "cash_given": "500"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/app/pos/sales/")
    sid = r.headers["location"].rsplit("/", 1)[1]
    with db.tx() as conn:
        s = sale(conn, sid)
    # 3 × 116.00 + 20.50 = 368.50; −10 % = 36.85; IVA 16 % inside 331.65 = 45.74; change from 500 = 168.35
    assert (s["subtotal_minor"], s["discount_minor"], s["tax_minor"], s["total_minor"]) == (36850, 3685, 4574, 33165)
    assert (s["cash_given_minor"], s["change_minor"], s["status"], s["number"], s["tax_name"], s["tax_rate_bp"]) == (50000, 16835, "paid", 1, "IVA", 1600)
    assert s["lines"][0] == {"item_id": str(rosa["id"]), "description": "Rosa", "qty": 3, "unit_minor": 11600}
    assert stock(rosa["id"]) == 7                                           # off the shelf
    page = client.get(f"/app/pos/sales/{sid}").text
    assert "$331.65" in page and "$168.35" in page and "wa.me/" in page and f"/receipt/{s['token']}" in page
    rec = client.get(f"/receipt/{s['token']}")
    assert rec.status_code == 200 and "$331.65" in rec.text and "Rosa" in rec.text and "noindex" in rec.text
    assert mailer.OUTBOX == []                                              # nothing is sent by itself
    with db.tx() as conn:
        assert conn.execute(text("SELECT count(*) FROM messages WHERE business_id = :b"), {"b": biz["id"]}).scalar() == 0
    # the screen's prices are never trusted: an item is priced from its own record
    r = client.post("/app/pos", data={"csrf": tok(client, "/app/pos"), "line_item": [str(rosa["id"])], "line_desc": [""], "line_qty": ["1"],
                                      "line_unit": ["0.01"], "method": "cash"}, follow_redirects=False)
    with db.tx() as conn:
        assert sale(conn, r.headers["location"].rsplit("/", 1)[1])["total_minor"] == 11600
    # short cash is refused; nothing is saved
    client.post("/app/pos", data={"csrf": tok(client, "/app/pos"), "line_item": [""], "line_desc": ["x"], "line_qty": ["1"],
                                  "line_unit": ["100"], "method": "cash", "cash_given": "50"})
    with db.tx() as conn:
        assert conn.execute(text("SELECT count(*) FROM pos_sales WHERE business_id = :b"), {"b": biz["id"]}).scalar() == 2


def test_a_tax_the_owner_sets_on_top_and_a_currency_without_cents(client):
    biz, _ = signed_in(client, "US", "shop")
    assert POS.tax(biz) == {"rate_bp": 0, "included": False, "name": "Sales tax", "own": False}     # external: none until set
    client.post("/app/pos/settings", data={"csrf": tok(client, "/app/pos"), "tax": "8.25"})
    with db.tx() as conn:
        biz = B.get(conn, biz["id"])
    assert POS.tax(biz)["rate_bp"] == 825 and not POS.tax(biz)["included"]
    s = POS.sell(biz, "t", lines=[{"description": "Cap", "qty": 3, "unit_minor": 1999}], method="card", discount_minor=97)
    assert (s["subtotal_minor"], s["discount_minor"], s["tax_minor"], s["total_minor"], s["change_minor"]) == (5997, 97, 487, 6387, None)
    jp, _ = make_business("JP", "shop")
    s = POS.sell(jp, "t", lines=[{"description": "茶", "qty": 1, "unit_minor": 1100}], method="cash", cash_given_minor=2000)
    assert (s["tax_minor"], s["total_minor"], s["change_minor"], s["currency"]) == (100, 1100, 900, "JPY")
    with pytest.raises(Invalid):
        POS.sell(jp, "t", lines=[{"description": "x", "qty": 1, "unit_minor": 100}], method="cash", discount_bp=10_001)
    with pytest.raises(Invalid):
        POS.sell(jp, "t", lines=[], method="cash")


# ---------------------------------------------------------------- void and refund: never deleted, back on the shelf
def test_void_and_refund_put_stock_back_and_keep_the_sale(client):
    biz, _ = signed_in(client)
    rosa = item(biz, stock=5)
    a = POS.sell(biz, "t", lines=[{"item_id": rosa["id"], "qty": 2}], method="cash")
    b = POS.sell(biz, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="card")
    assert stock(rosa["id"]) == 2
    t = tok(client, f"/app/pos/sales/{a['id']}")
    client.post(f"/app/pos/sales/{a['id']}", data={"csrf": t, "act": "void", "reason": ""})        # a reason is needed
    with db.tx() as conn:
        assert sale(conn, a["id"])["status"] == "paid"
    client.post(f"/app/pos/sales/{a['id']}", data={"csrf": t, "act": "void", "reason": "wrong button"})
    client.post(f"/app/pos/sales/{b['id']}", data={"csrf": t, "act": "refund", "reason": "came back"})
    with db.tx() as conn:
        sa, sb = sale(conn, a["id"]), sale(conn, b["id"])
        assert (sa["status"], sa["undo_reason"], sb["status"], sb["refund_day"]) == ("void", "wrong button", "refunded", POS.today_of(biz))
        reasons = [m["reason"] for m in INV.moves(conn, biz["id"], rosa["id"])]
        with pytest.raises(Invalid):
            POS.refund(conn, biz, "t", a["id"], "again")                    # a void can't be refunded
        with pytest.raises(Invalid):
            POS.void(conn, biz, "t", b["id"], "again")                      # nor a refund voided
    assert stock(rosa["id"]) == 5 and sorted(reasons) == ["refund", "sale", "sale", "start", "void"]
    c = POS.sell(biz, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="cash")
    with db.tx() as conn:                                                   # yesterday's sale: a refund, not a void
        conn.execute(update(db.pos_sales).where(db.pos_sales.c.id == c["id"]).values(day=POS.today_of(biz) - timedelta(days=1)))
        with pytest.raises(Invalid):
            POS.void(conn, biz, "t", c["id"], "late")
        assert POS.refund(conn, biz, "t", c["id"], "late")["status"] == "refunded"
        assert conn.execute(text("SELECT count(*) FROM pos_sales WHERE business_id = :b"), {"b": biz["id"]}).scalar() == 3


def test_a_pay_link_from_the_businesss_own_account_waits_then_is_paid(client):
    biz, _ = signed_in(client)
    with pytest.raises(Invalid):                                            # no account of their own: no link
        POS.sell(biz, "t", lines=[{"description": "Ramo", "qty": 1, "unit_minor": 50000}], method="link")
    with db.tx() as conn:
        PM.connect(conn, biz, "t", "bank_transfer", {"bank": "BBVA", "holder": "Tienda", "account": "0123"})
        assert conn.execute(text("SELECT count(*) FROM pos_sales WHERE business_id = :b"), {"b": biz["id"]}).scalar() == 0
    rosa = item(biz, stock=3)
    s = POS.sell(biz, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="link")
    assert s["status"] == "pending" and s["payment_id"] and s["paid_at"] is None and stock(rosa["id"]) == 2
    page = client.get(f"/app/pos/sales/{s['id']}").text
    assert "ps-qr-img\"><svg" in page and "/p/" in page and "Scan to pay" in page
    with db.tx() as conn:
        pay = conn.execute(select(db.payments).where(db.payments.c.id == s["payment_id"])).mappings().one()
        assert pay["amount_minor"] == 11600 and pay["status"] == "pending"
        with pytest.raises(Invalid):
            POS.refund(conn, biz, "t", s["id"], "not paid yet")
    client.post(f"/app/pos/sales/{s['id']}", data={"csrf": tok(client, f"/app/pos/sales/{s['id']}"), "act": "paid"})
    with db.tx() as conn:
        assert sale(conn, s["id"])["status"] == "paid" and sale(conn, s["id"])["paid_at"]
        with pytest.raises(Invalid):
            POS.void(conn, biz, "t", s["id"], "paid by link")               # money that arrived: refund, not void
    assert "ps-qr" not in client.get(f"/app/pos/sales/{s['id']}").text
    s2 = POS.sell(biz, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="link")
    with db.tx() as conn:                                                   # an unpaid link voided: the link is cancelled too
        POS.void(conn, biz, "t", s2["id"], "they left")
        assert conn.execute(select(db.payments.c.status).where(db.payments.c.id == s2["payment_id"])).scalar() == "cancelled"
    assert stock(rosa["id"]) == 2


def test_end_of_day_cash_count(client):
    biz, _ = signed_in(client)
    POS.sell(biz, "t", lines=[{"description": "a", "qty": 1, "unit_minor": 10000}], method="cash", cash_given_minor=20000)
    POS.sell(biz, "t", lines=[{"description": "b", "qty": 2, "unit_minor": 2500}], method="cash")
    POS.sell(biz, "t", lines=[{"description": "c", "qty": 1, "unit_minor": 7000}], method="card")
    r = POS.sell(biz, "t", lines=[{"description": "d", "qty": 1, "unit_minor": 3000}], method="cash")
    with db.tx() as conn:
        POS.refund(conn, biz, "t", r["id"], "broken")
        s = POS.day_summary(conn, biz, POS.today_of(biz))
    assert s["cash_minor"] == 15000 and s["net_minor"] == 22000 and s["by"]["card"]["sales_minor"] == 7000 and s["count"] == 4
    client.post("/app/pos/close", data={"csrf": tok(client, "/app/pos/sales"), "day": POS.today_of(biz).isoformat(), "float": "500",
                                        "counted": "640"})
    with db.tx() as conn:
        c = POS.closes(conn, biz["id"])[0]
    assert (c["float_minor"], c["expected_minor"], c["counted_minor"]) == (50000, 65000, 64000)
    page = client.get("/app/pos/sales").text
    assert "less than expected" in page and "$10.00" in page


# ---------------------------------------------------------------- inventory
def test_inventory_counts_with_reasons_low_stock_on_today_and_valeria_suggests(client):
    biz, _ = signed_in(client, industry="florist")
    r = client.post("/app/inventory", data={"csrf": tok(client, "/app/inventory"), "name": "Tulipán", "sku": "TUL-1", "price": "45.50",
                                            "cost": "20", "stock": "12", "low_at": "5"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        tul = INV.items(conn, biz["id"])[0]
    assert (tul["price_minor"], tul["cost_minor"], tul["stock"], tul["in_inventory"], tul["category"]) == (4550, 2000, 12, True, "other")
    t = tok(client, f"/app/inventory/{tul['id']}")
    for reason, qty in (("received", "6"), ("damaged", "2"), ("count", "4")):
        client.post(f"/app/inventory/{tul['id']}", data={"csrf": t, "act": "adjust", "reason": reason, "qty": qty, "note": "x"})
    assert stock(tul["id"]) == 4
    with db.tx() as conn:
        assert [(m["reason"], m["delta"], m["stock_after"]) for m in reversed(INV.moves(conn, biz["id"], tul["id"]))] == \
            [("start", 12, 12), ("received", 6, 18), ("damaged", -2, 16), ("count", -12, 4)]
    today = client.get("/app").text
    assert 'id="low"' in today and "Tulipán" in today and "4 left" in today
    assert 'id="tip"' in today and "running low" in today and 'href="/app/inventory?low=1"' in today
    assert "Tulipán" in client.get("/app/inventory?low=1").text
    from valeria_one.services import briefing
    with db.tx() as conn:
        msg, _ = briefing.morning(conn, B.get(conn, biz["id"]), "en", datetime.now(timezone.utc))
    assert "Running low (1): Tulipán" in msg
    client.post("/app/tips/dismiss", data={"csrf": tok(client), "tip": "restock"})
    assert 'id="tip"' not in client.get("/app").text and 'id="low"' in client.get("/app").text   # the card stays; the tip doesn't


def test_variants_are_one_level_and_csv_round_trips(client):
    biz, _ = signed_in(client)
    shirt = item(biz, name="Playera", price=25000, stock=None)
    red = item(biz, name="Playera", price=25000, stock=3, sku="PL-R", variant="Roja / M", parent_id=shirt["id"])
    with pytest.raises(Invalid):
        item(biz, name="x", parent_id=red["id"])                            # a variant of a variant
    with db.tx() as conn:
        rows = INV.items(conn, biz["id"])
        assert [r["id"] for r in rows] == [shirt["id"], red["id"]] and rows[1]["parent_name"] == "Playera"
        PR_sup = conn.execute(db.suppliers.insert().values(business_id=biz["id"], name="Telas SA").returning(db.suppliers.c.id)).scalar()
        INV.patch(conn, biz, "t", red["id"], {"supplier_id": PR_sup, "name": "=SUM(A1)"})
    r = client.get("/app/inventory.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[1] == {"name": "'=SUM(A1)", "variant": "Roja / M", "sku": "PL-R", "price": "250", "cost": "", "stock": "3", "low_at": "",
                       "supplier": "Telas SA"}
    body = "name,sku,price,stock,low_at,supplier\nAnything,PL-R,260.50,7,2,Telas SA\nGorra,,99,4,,Nadie\n,,,,,\n"
    client.post("/app/inventory/import", data={"csrf": tok(client, "/app/inventory")}, files={"file": ("i.csv", body.encode(), "text/csv")})
    with db.tx() as conn:
        red2 = INV.item(conn, biz["id"], red["id"])
        cap = [x for x in INV.items(conn, biz["id"]) if x["name"] == "Gorra"][0]
        assert (red2["price_minor"], red2["stock"], red2["low_at"], red2["name"]) == (26050, 7, 2, "Anything")
        assert INV.moves(conn, biz["id"], red["id"])[0]["reason"] == "import" and (cap["stock"], cap["price_minor"]) == (4, 9900)
        assert conn.execute(text("SELECT count(*) FROM catalog_items WHERE business_id = :b"), {"b": biz["id"]}).scalar() == 3


# ---------------------------------------------------------------- staff
def test_staff_clock_in_from_their_own_phone_and_the_owners_fix_keeps_history(client):
    biz, owner_uid = signed_in(client, industry="salon")
    phone = other_client()
    staff_uid = owner_session(phone, biz, role="staff")
    phone.post("/lang/en", data={"csrf": tok(phone)})
    assert "not on the schedule yet" in phone.get("/app/staff").text
    client.post("/app/staff/members", data={"csrf": tok(client, "/app/staff?tab=team"), "name": "Lupe Ruiz", "phone": "55 1111 2222",
                                            "job": "Estilista", "hourly": "150", "user_id": str(staff_uid)})
    with db.tx() as conn:
        lupe = SF.members(conn, biz["id"])[0]
    assert lupe["phone"] == "+525511112222" and lupe["hourly_minor"] == 15000 and lupe["user_id"] == staff_uid
    assert "Clock in" in phone.get("/app/staff").text
    phone.post("/app/staff/clock", data={"csrf": tok(phone, "/app/staff")})
    assert "Clock out" in phone.get("/app/staff").text
    phone.post("/app/staff/clock", data={"csrf": tok(phone, "/app/staff")})
    with db.tx() as conn:
        es = conn.execute(select(db.clock_entries).where(db.clock_entries.c.business_id == biz["id"])).mappings().all()
    assert len(es) == 1 and es[0]["out_at"] > es[0]["in_at"] and es[0]["source"] == "self"
    yesterday = SF.today_of(biz) - timedelta(days=1)
    t = tok(client, "/app/staff?tab=hours")
    client.post(f"/app/staff/hours/{es[0]['id']}", data={"csrf": t, "in_at": f"{yesterday}T09:00", "out_at": f"{yesterday}T17:00", "note": ""})
    with db.tx() as conn:
        assert SF.entry(conn, biz["id"], es[0]["id"])["history"] == []      # no reason: nothing changes
    client.post(f"/app/staff/hours/{es[0]['id']}", data={"csrf": t, "in_at": f"{yesterday}T09:00", "out_at": f"{yesterday}T17:00",
                                                         "note": "forgot to clock", "act": "fix"})
    with db.tx() as conn:
        e = SF.entry(conn, biz["id"], es[0]["id"])
    tz = ZoneInfo(biz["timezone"])
    assert e["in_at"] == datetime.combine(yesterday, time(9), tz) and SF.minutes(e) == 480
    assert e["history"][0]["note"] == "forgot to clock" and e["history"][0]["in_at"] == es[0]["in_at"].isoformat()
    page = client.get(f"/app/staff?tab=hours&from={yesterday}&to={SF.today_of(biz)}").text
    assert "forgot to clock" in page and "Was " in page and "for your accountant" in page
    client.post(f"/app/staff/hours/{es[0]['id']}", data={"csrf": t, "act": "void", "note": "duplicate"})
    with db.tx() as conn:
        e = SF.entry(conn, biz["id"], es[0]["id"])
        assert e["voided"] and len(e["history"]) == 2 and SF.minutes(e) == 0
        assert conn.execute(text("SELECT count(*) FROM clock_entries WHERE business_id = :b"), {"b": biz["id"]}).scalar() == 1
    staff_page = phone.get("/app/staff").text                               # staff never see the team or the pay
    assert "?tab=team" not in staff_page and "Download for payroll" not in staff_page
    assert phone.post("/app/staff/members", data={"csrf": tok(phone, "/app/staff"), "name": "Nope"}, follow_redirects=False).status_code == 303
    with db.tx() as conn:
        assert len(SF.members(conn, biz["id"])) == 1


def test_timesheet_pay_is_hours_times_rate_plus_days_plus_commission(client):
    biz, _ = signed_in(client, industry="salon")
    staff_uid = owner_session(other_client(), biz, role="staff")
    tz = ZoneInfo(biz["timezone"])
    today = SF.today_of(biz)
    with db.tx() as conn:
        m = SF.add_member(conn, biz, "t", {"name": "Lupe", "hourly_minor": 15000, "per_shift_minor": 5000, "commission_bp": 1000,
                                           "user_id": staff_uid})
        d1, d2 = today - timedelta(days=2), today - timedelta(days=1)
        for d, a, b, void in ((d1, time(9), time(16, 30), False), (d2, time(10), time(14), False), (d2, time(15), time(17), True)):
            conn.execute(db.clock_entries.insert().values(business_id=biz["id"], staff_id=m["id"], in_at=datetime.combine(d, a, tz),
                                                          out_at=datetime.combine(d, b, tz), voided=void))
        conn.execute(db.clock_entries.insert().values(business_id=biz["id"], staff_id=m["id"], in_at=datetime.now(timezone.utc)))  # still in
    POS.sell(biz, "t", lines=[{"description": "Tinte", "qty": 1, "unit_minor": 11600}], method="cash", user_id=staff_uid)   # net 100.00
    back = POS.sell(biz, "t", lines=[{"description": "Corte", "qty": 1, "unit_minor": 23200}], method="cash", user_id=staff_uid)
    with db.tx() as conn:
        POS.refund(conn, biz, "t", back["id"], "unhappy")                   # a refunded sale earns no commission
        r = SF.timesheet(conn, biz, today - timedelta(days=6), today)[0]
    assert (r["minutes"], r["days"], r["open"], r["sales_minor"]) == (690, 2, True, 10000)
    assert (r["hours_pay_minor"], r["day_pay_minor"], r["commission_minor"], r["total_minor"]) == (172500, 10000, 1000, 183500)
    c = client.get(f"/app/staff/timesheet.csv?from={today - timedelta(days=6)}&to={today}")
    row = list(csv.DictReader(io.StringIO(c.text.lstrip("﻿"))))[0]
    assert (row["name"], row["hours"], row["days"], row["hours_pay"], row["days_pay"], row["commission_pct"], row["commission"], row["total"],
            row["currency"]) == ("Lupe", "11:30", "2", "1725", "100", "10", "10", "1835", "MXN")


def test_the_schedule_week_copy_and_share_by_whatsapp(client):
    biz, _ = signed_in(client, industry="restaurant")
    with db.tx() as conn:
        m = SF.add_member(conn, biz, "t", {"name": "Toño Pérez", "phone": "+525522223333"})
    wk = SF.monday(SF.today_of(biz))
    t = tok(client, "/app/staff")
    client.post("/app/staff/shifts", data={"csrf": t, "staff_id": str(m["id"]), "starts": "18:00", "ends": "02:00",
                                           "day": [str(wk), str(wk + timedelta(days=2))]})
    with db.tx() as conn:
        week = SF.week(conn, biz["id"], wk)
    assert len(week) == 2 and SF.shift_minutes(week[0]["starts"], week[0]["ends"]) == 480     # a night shift
    page = client.get(f"/app/staff?week={wk}").text
    assert "18:00–02:00" in page and "https://wa.me/525522223333?text=" in page and "%2A" not in page
    nxt = wk + timedelta(days=7)
    for _ in range(2):                                                      # twice = once
        client.post("/app/staff/copy-week", data={"csrf": t, "week": str(nxt)})
    with db.tx() as conn:
        assert len(SF.week(conn, biz["id"], nxt)) == 2
    assert mailer.OUTBOX == []


# ---------------------------------------------------------------- books: everything once
def test_books_count_each_payment_once_and_the_month_adds_up(client):
    biz, _ = signed_in(client)
    with db.tx() as conn:
        PM.connect(conn, biz, "t", "bank_transfer", {"bank": "BBVA", "holder": "Tienda", "account": "0123"})
        ana = C.create(conn, biz, "t", {"name": "Ana", "phone": "+525511110001"})
        inv = IVS.create(conn, biz, "t", title="Arreglo", lines=[{"description": "Arreglo", "qty": 1, "unit_minor": 100000}], contact_id=ana["id"])
    sent = IVS.send(biz, "t", inv["id"], message=False)                     # a pay link for the invoice
    with db.tx() as conn:
        PM.mark_paid(conn, sent["payment_id"], "t")                         # paid: the invoice AND its payment
        assert IVS.get(conn, biz["id"], inv["id"])["status"] == "paid"
    PM.record_received(biz, "t", amount_minor=5000, description="Cash for a pot", contact_id=ana["id"])   # neither: counted
    cash = POS.sell(biz, "t", lines=[{"description": "Ramo", "qty": 1, "unit_minor": 20000}], method="cash")
    link = POS.sell(biz, "t", lines=[{"description": "Caja", "qty": 1, "unit_minor": 30000}], method="link")
    POS.sell(biz, "t", lines=[{"description": "Never paid", "qty": 1, "unit_minor": 99900}], method="link")
    ret = POS.sell(biz, "t", lines=[{"description": "Florero", "qty": 1, "unit_minor": 7000}], method="card")
    with db.tx() as conn:
        PM.owner_confirms(conn, biz["id"], "t", link["payment_id"])
        POS.refund(conn, biz, "t", ret["id"], "broken")
        today = BK.today_of(biz)
        rent = BK.add(conn, biz, "t", direction="out", on_date=today, amount_minor=12345, category="rent", description="October")
        oops = BK.add(conn, biz, "t", direction="out", on_date=today, amount_minor=99999, category="other")
        BK.void(conn, biz, "t", oops["id"], "typed twice")
        BK.add(conn, biz, "t", direction="in", on_date=today, amount_minor=1500, category="other_in")
        conn.execute(db.book_entries.insert().values(business_id=biz["id"], direction="in", on_date=today, amount_minor=777, currency="USD",
                                                     category="other_in", created_by="t"))       # another currency: not in these sums
        with pytest.raises(Invalid):
            BK.add(conn, biz, "t", direction="out", on_date=today, amount_minor=0, category="rent")
        with pytest.raises(Invalid):
            BK.add(conn, biz, "t", direction="out", on_date=today, amount_minor=5, category="sales")
        mo = BK.month(conn, biz, today.year, today.month)
    assert mo["sources"] == {"invoice": 116000, "payment": 5000, "sale": 20000 + 30000 + 7000, "manual": 1500}   # the invoice: + IVA
    assert mo["out"] == {"refunds": 7000, "rent": 12345}
    assert (mo["in_minor"], mo["out_minor"], mo["profit_minor"]) == (179500, 19345, 160155)
    page = client.get(f"/app/books?m={today.year}-{today.month:02d}")
    assert page.status_code == 200 and "$1,601.55" in page.text and "typed twice" in page.text
    c = client.get(f"/app/books.csv?from={today}&to={today}")
    rows = list(csv.DictReader(io.StringIO(c.text.lstrip("﻿"))))
    assert len(rows) == 9 and sum(1 for r in rows if r["voided"] == "yes") == 1
    assert {(r["source"], r["amount"]) for r in rows if r["in_out"] == "in"} >= {("invoice", "1160"), ("payment", "50"), ("sale", "300")}
    with db.tx() as conn:
        assert rent["id"] in {e["id"] for e in BK.ledger(conn, biz, today, today)}


def test_a_receipt_photo_goes_with_an_expense(client):
    biz, _ = signed_in(client)
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    with db.tx() as conn:
        sup = conn.execute(db.suppliers.insert().values(business_id=biz["id"], name="Flores del Sur").returning(db.suppliers.c.id)).scalar()
    r = client.post("/app/books", data={"csrf": tok(client, "/app/books"), "direction": "out", "amount": "1,250.50", "category": "stock",
                                        "on_date": str(BK.today_of(biz)), "description": "Rosas", "supplier_id": str(sup)},
                    files={"receipt": ("r.png", png, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        e = conn.execute(select(db.book_entries).where(db.book_entries.c.business_id == biz["id"])).mappings().one()
    assert (e["amount_minor"], e["category"], e["supplier_id"]) == (125050, "stock", sup) and e["file_id"]
    assert client.get(f"/app/files/{e['file_id']}").status_code == 200 and "Flores del Sur" in client.get("/app/books").text


# ---------------------------------------------------------------- one business never sees another's
def test_every_new_tool_stays_inside_its_business(client):
    a, _ = make_business("MX", "shop", name="A")
    rosa = item(a)
    s = POS.sell(a, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="cash")
    with db.tx() as conn:
        m = SF.add_member(conn, a, "t", {"name": "Ana"})
        e = conn.execute(db.clock_entries.insert().values(business_id=a["id"], staff_id=m["id"], in_at=datetime.now(timezone.utc) - timedelta(hours=2),
                                                          out_at=datetime.now(timezone.utc)).returning(db.clock_entries.c.id)).scalar()
        sh = SF.add_shift(conn, a, "t", m["id"], SF.today_of(a), "09:00", "10:00")
        be = BK.add(conn, a, "t", direction="out", on_date=BK.today_of(a), amount_minor=100, category="rent")
    a_user = owner_session(other_client(), a)                              # someone who signs in to A, not to B
    b, _ = signed_in(client, name="B")
    t = tok(client)
    for path in (f"/app/pos/sales/{s['id']}", f"/app/inventory/{rosa['id']}"):
        assert client.get(path).status_code == 404, path
    for path, data in ((f"/app/pos/sales/{s['id']}", {"act": "paid"}), (f"/app/inventory/{rosa['id']}", {"act": "adjust", "reason": "received", "qty": "5"}),
                       (f"/app/books/{be['id']}/void", {"reason": "x"}), (f"/app/staff/members/{m['id']}", {"name": "Hacked"}),
                       (f"/app/staff/hours/{e}", {"note": "x", "act": "void"}), (f"/app/staff/shifts/{sh['id']}/remove", {})):
        assert client.post(path, data={"csrf": t, **data}).status_code == 404, path
    with pytest.raises(NotFound):
        POS.sell(b, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="cash")
    with db.tx() as conn:
        assert POS.list_(conn, b["id"]) == [] and INV.items(conn, b["id"]) == [] and SF.members(conn, b["id"]) == []
        assert BK.ledger(conn, b, date(2000, 1, 1), BK.today_of(b)) == [] and SF.week(conn, b["id"], SF.monday(SF.today_of(b))) == []
        with pytest.raises(NotFound):
            POS.void(conn, b, "t", s["id"], "x")
        with pytest.raises(NotFound):
            SF.add_member(conn, b, "t", {"name": "x", "user_id": a_user})
    with db.tx() as conn:
        assert stock(rosa["id"]) == 9 and SF.member(conn, a["id"], m["id"])["name"] == "Ana"
    assert client.get(f"/receipt/{s['token']}").status_code == 200 and client.get("/receipt/nope").status_code == 404


def test_staff_sell_and_count_but_money_back_prices_and_books_are_the_owners(client):
    biz, _ = signed_in(client, industry="salon", role="staff")
    rosa = item(biz, stock=4)
    s = POS.sell(biz, "t", lines=[{"item_id": rosa["id"], "qty": 1}], method="cash")
    assert client.get("/app/pos").status_code == 200 and client.get("/app/inventory").status_code == 200
    assert "Cost" not in client.get(f"/app/inventory/{rosa['id']}").text
    t = tok(client, "/app/pos")
    r = client.post("/app/pos", data={"csrf": t, "line_item": [str(rosa["id"])], "line_desc": [""], "line_qty": ["1"], "line_unit": [""],
                                      "method": "card"}, follow_redirects=False)
    assert r.headers["location"].startswith("/app/pos/sales/")
    client.post(f"/app/inventory/{rosa['id']}", data={"csrf": t, "act": "adjust", "reason": "received", "qty": "3"})
    assert stock(rosa["id"]) == 5
    for path in ("/app/books", "/app/books.csv", "/app/inventory.csv"):
        assert client.get(path, follow_redirects=False).status_code == 303, path
    for path, data in (("/app/inventory", {"name": "Nope", "price": "1"}), (f"/app/inventory/{rosa['id']}", {"act": "save", "name": "x", "price": "0"}),
                       (f"/app/pos/sales/{s['id']}", {"act": "refund", "reason": "x"}), ("/app/pos/settings", {"tax": "0"}),
                       ("/app/books", {"direction": "out", "amount": "1", "category": "rent"})):
        assert client.post(path, data={"csrf": t, **data}, follow_redirects=False).status_code == 303, path
    with db.tx() as conn:
        assert [i["name"] for i in INV.items(conn, biz["id"])] == ["Rosa"] and sale(conn, s["id"])["status"] == "paid"
        assert B.get(conn, biz["id"])["pos_settings"] == {} and conn.execute(text("SELECT count(*) FROM book_entries")).scalar() == 0
    assert "/app/books" not in nav_hrefs(client) and "/app/pos" in nav_hrefs(client)


# ---------------------------------------------------------------- kits, menus, pages
def test_the_kits_switch_the_new_tools_on_where_they_fit():
    assert kits.check_kits() == []
    for k in NEW:
        t = kits.TOOLS[k]
        assert t.href == f"/app/{k}" and t.group in kits.GROUPS and t.uses and k in kits.OPTIONAL
    assert kits.TOOLS["books"].owner and not kits.TOOLS["pos"].owner and not kits.TOOLS["staff"].owner
    want = {"pos": {"shop", "florist", "restaurant", "salon"}, "inventory": {"shop", "florist", "restaurant", "salon"},
            "staff": {"salon", "restaurant", "clinic", "fitness", "contractor"}, "books": set(packs.industries())}
    for tool, trades in want.items():
        assert {c for c, ip in packs.industries().items() if tool in ip.kit.tools} == trades, tool


@pytest.mark.parametrize("industry", ["shop", "salon", "realtor"])
def test_every_new_page_opens_from_every_kit_in_every_language_on_a_phone(client, industry):
    signup(client, industry=industry, email=f"{industry}-ars@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).order_by(db.businesses.c.created_at.desc()).limit(1)).mappings().one())
    it = item(biz, stock=1, low_at=2)
    s = POS.sell(biz, "t", lines=[{"item_id": it["id"], "qty": 1}], method="cash")
    pages = ["/app/pos", "/app/pos/sales", f"/app/pos/sales/{s['id']}", "/app/inventory", f"/app/inventory/{it['id']}", "/app/staff",
             "/app/staff?tab=hours", "/app/staff?tab=team", "/app/books", "/app/add", "/app"]
    for lang in LANGS:
        client.post(f"/lang/{lang}", data={"csrf": tok(client)})
        for p in pages:
            r = client.get(p, headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
            assert r.status_code == 200, (industry, lang, p)
            main = r.text.split("<main", 1)[1]
            assert not re.search(r"\{[a-z_]+\}", main), (lang, p, re.search(r"\{[a-z_]+\}", main))
            assert ('dir="rtl"' in r.text) == (lang == "ar")
        assert client.get(f"/receipt/{s['token']}").status_code == 200
    client.post("/lang/en", data={"csrf": tok(client)})
    hrefs = nav_hrefs(client)
    for k in NEW:
        assert (kits.TOOLS[k].href in hrefs) == (k in packs.industry(industry).kit.tools), (industry, k)
    page = client.get("/app/add").text
    for k in NEW:
        assert f'id="t-{k}"' in page


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith(("pos_", "inv_", "sf_", "bk_"))} | set(NEW) | {f"ft_{k}_d" for k in NEW}
    keys |= {"tip_restock", "tip_restock_d", "va_low_stock"}
    assert len(keys) >= 240
    for lg in LANGS:
        same = 0
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)   # same order too
            same += lg != "en" and files[lg][k] == files["en"][k]
        assert same <= 6, (lg, same)                                        # translated, not English copied over
    for k in keys:
        if k.startswith(("ft_", "tip_")):
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k


# ---------------------------------------------------------------- nothing is lost
def _ars_migration():
    path = next((ROOT / "alembic" / "versions").glob("*_ars01_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_one_migration_one_head_and_its_pins_keep_what_a_business_uses():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    mig = _ars_migration()
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1                                     # one head; v0.21 sits on top of this one (intentional)
    assert mig.revision in {r.revision for r in script.walk_revisions()} and script.get_revision(mig.revision).down_revision == "a424ed548d9d"
    assert set(mig.USES) == set(NEW)
    biz, _ = make_business("MX", "realtor", name="Casas")                   # a realtor kit has no register, shelf or staff
    other, _ = make_business("MX", "realtor", name="Otra")
    POS.sell(biz, "t", lines=[{"description": "Avalúo", "qty": 1, "unit_minor": 100000}], method="other")
    item(biz)
    with db.tx() as conn:
        SF.add_member(conn, biz, "t", {"name": "Sol"})
        conn.execute(update(db.businesses).values(tools={}))
        for sql in mig.pin_statements():
            conn.execute(text(sql))
        assert kits.in_use(conn, biz["id"]) >= {"pos", "inventory", "staff"} and "library" not in kits.in_use(conn, biz["id"])
    with db.tx() as conn:
        b = B.get(conn, biz["id"])
        assert b["tools"] == {"pos": True, "inventory": True, "staff": True} and B.get(conn, other["id"])["tools"] == {}
        for sql in mig.pin_statements():                                    # again: nothing changes
            conn.execute(text(sql))
        assert B.get(conn, biz["id"])["tools"] == b["tools"]


def test_a_new_trade_keeps_the_register_and_the_team_that_hold_data():
    biz, _ = make_business("MX", "salon", name="Estética")
    POS.sell(biz, "t", lines=[{"description": "Corte", "qty": 1, "unit_minor": 20000}], method="cash")
    with db.tx() as conn:
        SF.add_member(conn, biz, "t", {"name": "Sol"})
        B.patch(conn, biz["id"], "t", industry="realtor")
        b = B.get(conn, biz["id"])
    assert b["tools"] == {"pos": True, "staff": True} and kits.visible(b, "pos") and not kits.visible(b, "inventory")


def test_the_export_carries_the_new_records():
    biz, _ = make_business("MX", "shop")
    it = item(biz)
    POS.sell(biz, "t", lines=[{"item_id": it["id"], "qty": 1}], method="cash")
    from valeria_one.services import export
    with db.tx() as conn:
        out = export.business_data(conn, biz["id"], "t")
    assert len(out["pos_sales"]) == 1 and len(out["stock_moves"]) == 2 and out["catalog_items"][0]["stock"] == 9


def test_the_qr_code_is_the_standard_one():
    # matrices checked module for module against the segno reference encoder (all 8 masks, versions 1-10) when this was built
    for url, n, digest in (("https://one.test/p/Zm9vYmFyYmF6cXV4MTIz", 29, "b700964e7afde767452a311c182e7d175ae0df3a8b9e5c4bfa7e8b406a1fe3e4"),
                           ("https://valeriaone.valai.mx/p/" + "A" * 22, 33, "f8b20a39a933c29ea196fbf1c95f0b5d654b12d4272f35b51103f70b6e87844f")):
        m = qr.matrix(url)
        assert len(m) == n and hashlib.sha256("".join("1" if c else "0" for r in m for c in r).encode()).hexdigest() == digest
    assert qr.svg("https://one.test/p/x").startswith("<svg") and 'fill="#fff"' in qr.svg("x")
    with pytest.raises(qr.TooLong):
        qr.matrix("x" * 214)
