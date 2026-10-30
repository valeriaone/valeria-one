"""Books (ars01, v0.20): money in and money out, by month — simple, in the business's own currency, for any country.

Money in is read where it already is, never copied (so it can't drift or be counted twice):
  invoices   an invoice paid (its pay link is the same money: that payment is not counted again)
  register   a sale at the counter, paid (its pay link likewise); a refund is money out on the day it was given
  payments   a pay link or a "received" record that belongs to neither an invoice nor a sale
plus money in written by hand (a sale made elsewhere, a deposit). Money out is written by hand: what it was for (a
category), the supplier, a photo of the receipt. A mistake is voided — kept, not counted — never deleted.

Profit & loss by month and by category, and the whole ledger as a spreadsheet for the business's accountant. Nothing
here files or computes taxes: that stays with the accountant, and the screen says so. Only the business's own currency
is counted (a record in another currency stays where it is, outside these sums).
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from .. import db, money
from . import Invalid, NotFound, log

BE = db.book_entries
OUT = ("stock", "supplies", "rent", "staff", "utilities", "marketing", "transport", "fees", "taxes", "other")
IN = ("sales", "other_in")
SOURCES = ("manual", "invoice", "sale", "payment", "refund")
MAX_AMOUNT = 10 ** 15


def _tz(biz: dict) -> ZoneInfo:
    return ZoneInfo(biz["timezone"])


def today_of(biz: dict) -> date:
    return datetime.now(timezone.utc).astimezone(_tz(biz)).date()


def add(conn: Connection, biz: dict, actor: str, *, direction: str, on_date, amount_minor: int, category: str,
        description: str = "", supplier_id: UUID | None = None, file_id: UUID | None = None) -> dict:
    if direction not in ("in", "out"):
        raise Invalid("in or out", "direction")
    if category not in (IN if direction == "in" else OUT):
        raise Invalid("pick a category", "category")
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or not 0 < amount_minor < MAX_AMOUNT:
        raise Invalid("amount: more than 0", "amount")
    if not isinstance(on_date, date):
        try:
            on_date = date.fromisoformat(str(on_date))
        except ValueError:
            raise Invalid("pick a day", "on_date") from None
    if on_date > today_of(biz) + timedelta(days=1):
        raise Invalid("that day hasn't come yet", "on_date")
    if supplier_id:
        from . import projects as PR
        PR.supplier(conn, biz["id"], supplier_id)
    if file_id:
        from . import files as FL
        FL.get(conn, biz["id"], file_id)
    row = dict(conn.execute(insert(BE).values(business_id=biz["id"], direction=direction, on_date=on_date, amount_minor=amount_minor,
                                              currency=biz["currency"], category=category, description=(description or "").strip()[:300],
                                              supplier_id=supplier_id, file_id=file_id, created_by=actor[:100]).returning(BE))
               .mappings().one())
    log(conn, biz["id"], "books.added", actor, row["id"], {"direction": direction, "amount_minor": amount_minor, "category": category})
    return row


def entry(conn: Connection, business_id: UUID, entry_id: UUID) -> dict:
    r = conn.execute(select(BE).where(BE.c.business_id == business_id, BE.c.id == entry_id)).mappings().first()
    if not r:
        raise NotFound("entry")
    return dict(r)


def void(conn: Connection, biz: dict, actor: str, entry_id: UUID, reason: str) -> dict:
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        raise Invalid("say why", "reason")
    n = conn.execute(update(BE).where(BE.c.business_id == biz["id"], BE.c.id == entry_id, BE.c.voided_at.is_(None))
                     .values(voided_at=datetime.now(timezone.utc), void_reason=reason)).rowcount
    if not n:
        raise NotFound("entry")
    log(conn, biz["id"], "books.voided", actor, entry_id, {"reason": reason})
    return entry(conn, biz["id"], entry_id)


# ---------------------------------------------------------------- the ledger: one list, every source once
def ledger(conn: Connection, biz: dict, start: date, end: date, *, with_void: bool = False) -> list[dict]:
    """Every line of money from start to end (the business's own days, both included), oldest first:
    {on_date, direction, source, category, description, amount_minor, href, id, voided}."""
    tz, cur, bid = _tz(biz), biz["currency"], biz["id"]
    a = datetime.combine(start, time(0), tz)
    b = datetime.combine(end + timedelta(days=1), time(0), tz)
    day = lambda ts: ts.astimezone(tz).date()               # noqa: E731
    out = []
    q = select(BE).where(BE.c.business_id == bid, BE.c.currency == cur, BE.c.on_date >= start, BE.c.on_date <= end)
    if not with_void:
        q = q.where(BE.c.voided_at.is_(None))
    S = db.suppliers
    sup = dict(conn.execute(select(S.c.id, S.c.name).where(S.c.business_id == bid)).all())
    for r in conn.execute(q).mappings():
        out.append({"on_date": r["on_date"], "direction": r["direction"], "source": "manual", "category": r["category"],
                    "description": r["description"], "amount_minor": int(r["amount_minor"]), "href": None, "id": r["id"],
                    "supplier": sup.get(r["supplier_id"]), "file_id": r["file_id"], "voided": r["voided_at"] is not None,
                    "void_reason": r["void_reason"]})
    IV, PS, PY = db.invoices, db.pos_sales, db.payments
    for r in conn.execute(select(IV.c.id, IV.c.number, IV.c.title, IV.c.total_minor, IV.c.paid_at)
                          .where(IV.c.business_id == bid, IV.c.status == "paid", IV.c.currency == cur, IV.c.paid_at >= a,
                                 IV.c.paid_at < b, IV.c.total_minor > 0)).mappings():
        out.append({"on_date": day(r["paid_at"]), "direction": "in", "source": "invoice", "category": "sales",
                    "description": f"#{r['number']} · {r['title']}", "amount_minor": int(r["total_minor"]),
                    "href": f"/app/invoices/{r['id']}", "id": r["id"], "voided": False})
    for r in conn.execute(select(PS.c.id, PS.c.number, PS.c.total_minor, PS.c.paid_at, PS.c.refunded_at, PS.c.status)
                          .where(PS.c.business_id == bid, PS.c.currency == cur, PS.c.status.in_(("paid", "refunded")),
                                 PS.c.total_minor > 0,
                                 ((PS.c.paid_at >= a) & (PS.c.paid_at < b)) | ((PS.c.refunded_at >= a) & (PS.c.refunded_at < b))))\
            .mappings():
        if r["paid_at"] and a <= r["paid_at"] < b:
            out.append({"on_date": day(r["paid_at"]), "direction": "in", "source": "sale", "category": "sales",
                        "description": f"#{r['number']}", "amount_minor": int(r["total_minor"]), "href": f"/app/pos/sales/{r['id']}",
                        "id": r["id"], "voided": False})
        if r["status"] == "refunded" and r["refunded_at"] and a <= r["refunded_at"] < b:
            out.append({"on_date": day(r["refunded_at"]), "direction": "out", "source": "refund", "category": "refunds",
                        "description": f"#{r['number']}", "amount_minor": int(r["total_minor"]), "href": f"/app/pos/sales/{r['id']}",
                        "id": r["id"], "voided": False})
    claimed = select(IV.c.payment_id).where(IV.c.business_id == bid, IV.c.payment_id.isnot(None)).union(
        select(PS.c.payment_id).where(PS.c.business_id == bid, PS.c.payment_id.isnot(None)))
    for r in conn.execute(select(PY.c.id, PY.c.description, PY.c.amount_minor, PY.c.paid_at, PY.c.contact_id)
                          .where(PY.c.business_id == bid, PY.c.status == "paid", PY.c.currency == cur, PY.c.paid_at >= a,
                                 PY.c.paid_at < b, PY.c.id.not_in(claimed))).mappings():
        out.append({"on_date": day(r["paid_at"]), "direction": "in", "source": "payment", "category": "sales",
                    "description": r["description"], "amount_minor": int(r["amount_minor"]),
                    "href": f"/app/contacts/{r['contact_id']}#payments" if r["contact_id"] else None, "id": r["id"], "voided": False})
    out.sort(key=lambda x: (x["on_date"], SOURCES.index(x["source"]), str(x["id"])))
    return out


def _month(y: int, m: int) -> tuple[date, date]:
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def month(conn: Connection, biz: dict, y: int, m: int) -> dict:
    """One month's profit & loss: in and out by category (and money in by where it came from)."""
    a, b = _month(y, m)
    rows = ledger(conn, biz, a, b)
    ins, outs, src = {}, {}, {}
    for r in rows:
        if r["direction"] == "in":
            ins[r["category"]] = ins.get(r["category"], 0) + r["amount_minor"]
            src[r["source"]] = src.get(r["source"], 0) + r["amount_minor"]
        else:
            outs[r["category"]] = outs.get(r["category"], 0) + r["amount_minor"]
    tin, tout = sum(ins.values()), sum(outs.values())
    return {"year": y, "month": m, "from": a, "to": b, "in": ins, "out": outs, "sources": src, "in_minor": tin, "out_minor": tout,
            "profit_minor": tin - tout, "rows": rows}


def year(conn: Connection, biz: dict, y: int) -> list[dict]:
    """Twelve months (up to this one when it is this year): in, out, profit."""
    rows = ledger(conn, biz, date(y, 1, 1), date(y, 12, 31))
    last = today_of(biz).month if y == today_of(biz).year else 12
    out = []
    for m in range(1, last + 1):
        mine = [r for r in rows if r["on_date"].month == m]
        tin = sum(r["amount_minor"] for r in mine if r["direction"] == "in")
        tout = sum(r["amount_minor"] for r in mine if r["direction"] == "out")
        out.append({"month": m, "in_minor": tin, "out_minor": tout, "profit_minor": tin - tout})
    return out


def csv_out(conn: Connection, biz: dict, actor: str, start: date, end: date) -> str:
    """The ledger for the accountant: one row per line of money, amounts in the currency's units (not cents)."""
    from .csvio import _cell
    cur = biz["currency"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "in_out", "source", "category", "description", "supplier", "amount", "currency", "voided", "void_reason"])
    rows = ledger(conn, biz, start, end, with_void=True)
    for r in rows:
        w.writerow([r["on_date"].isoformat(), r["direction"], r["source"], r["category"], _cell(r["description"]),
                    _cell(r.get("supplier") or ""), money.to_major(r["amount_minor"], cur), cur, "yes" if r["voided"] else "",
                    _cell(r.get("void_reason") or "")])
    log(conn, biz["id"], "books.exported", actor, biz["id"], {"from": start.isoformat(), "to": end.isoformat(), "rows": len(rows)})
    return out.getvalue()
