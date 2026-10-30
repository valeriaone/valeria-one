"""Point of sale (ars01, v0.20): the register on the counter — on a phone or a laptop.

Pick items (Inventory's, at their own price) or type an amount, how many, a discount (an amount or a %), the
customer if there is one. Tax: the country pack's (IVA 16 % already in Mexican shelf prices, 19 % in German ones…)
unless the owner set the register's own (a US sales tax of 8.25 % on top; 0 for a business that doesn't charge it).
Paid by:
  cash   the change is worked out (cash given − total), and counted at the end of the day
  link   a pay link from the business's OWN payment account (services/payments.py: Stripe, Mercado Pago, …) shown as a
         QR for the customer's phone; the sale waits (pending) until the provider says it is paid (or the owner marks it)
  card   the business's own card machine: recorded here, the money goes through the machine
  other  a transfer, a voucher: recorded
Every sale is numbered 1, 2, 3… per business and is never deleted: a mistake is voided (the same day: as if it never
happened), a return is refunded (the money goes back the way it came; the day of the refund counts it). Stock goes
out on a sale and back on a void or a refund. The receipt is a page /receipt/<token> the owner can show or share by
WhatsApp or email — nothing is ever sent by itself. Money is in the smallest unit of the business's currency.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db, money, packs
from . import Invalid, NotFound, contacts, log
from . import inventory as INV

PS, PD = db.pos_sales, db.pos_days
METHODS = ("cash", "link", "card", "other")
STATUSES = ("paid", "pending", "void", "refunded")
MAX_LINES = 100
MAX_QTY = 10_000


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


def today_of(biz: dict, now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(biz["timezone"])).date()


# ---------------------------------------------------------------- the register's tax
def tax(biz: dict) -> dict:
    """The tax the register charges: the owner's own (pos_settings) or else the country pack's fixed tax; a country
    whose tax is worked out elsewhere (US sales tax) charges none until the owner sets the rate.
    -> {"rate_bp", "included", "name", "own"}"""
    pack = packs.country(biz["country"])
    own = biz.get("pos_settings") or {}
    if own.get("tax_bp") is not None:
        return {"rate_bp": int(own["tax_bp"]), "included": bool(own.get("included")), "name": pack.tax.name, "own": True}
    if pack.tax.mode == "fixed":
        return {"rate_bp": pack.tax.rate_bp, "included": pack.tax.prices_include_tax, "name": pack.tax.name, "own": False}
    return {"rate_bp": 0, "included": pack.tax.prices_include_tax, "name": pack.tax.name, "own": False}


def set_tax(conn: Connection, biz: dict, actor: str, rate: str | None, included: bool) -> dict:
    """rate: a percent ("8.25"), or blank = the country's again."""
    B = db.businesses
    rate = (rate or "").strip().replace(",", ".").rstrip("%").strip()
    if rate:
        try:
            bp = money.to_minor(rate, "USD")                     # 8.25 -> 825 basis points (two decimals, exactly)
        except (ValueError, ArithmeticError):
            raise Invalid("tax: a percent, e.g. 8.25", "tax") from None
        if not 0 <= bp <= 10_000:
            raise Invalid("tax: from 0 to 100 %", "tax")
        vals = {"tax_bp": bp, "included": bool(included)}
    else:
        vals = {}
    conn.execute(update(B).where(B.c.id == biz["id"]).values(pos_settings=vals, updated_at=func.now()))
    log(conn, biz["id"], "pos.settings", actor, biz["id"], vals)
    return dict(conn.execute(select(B).where(B.c.id == biz["id"])).mappings().one())


# ---------------------------------------------------------------- the sum
def _lines(conn: Connection, biz: dict, lines: list[dict]) -> list[dict]:
    """Items at their own price (never the screen's), typed amounts as typed."""
    out = []
    for ln in (lines or [])[:MAX_LINES]:
        qty = ln.get("qty", 1)
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_QTY:
            raise Invalid(f"quantity: a whole number from 1 to {MAX_QTY}", "lines")
        if ln.get("item_id"):
            it = INV.item(conn, biz["id"], ln["item_id"])
            if it["archived"] or it["price_minor"] is None:
                raise Invalid(f"{INV.label(it)}: no price", "lines")
            out.append({"item_id": str(it["id"]), "description": INV.label(it)[:200], "qty": qty, "unit_minor": int(it["price_minor"])})
            continue
        unit = ln.get("unit_minor")
        if not isinstance(unit, int) or isinstance(unit, bool) or unit <= 0:
            raise Invalid("amount: more than 0", "lines")
        desc = " ".join(str(ln.get("description") or "").split())[:200] or _t("pos_amount", biz["language"])
        out.append({"item_id": None, "description": desc, "qty": qty, "unit_minor": unit})
    if not out:
        raise Invalid(_t("pos_empty", biz["language"]), "lines")
    return out


def totals(biz: dict, lines: list[dict], *, discount_minor: int = 0, discount_bp: int = 0) -> dict:
    """Subtotal − discount = what the tax applies to; tax inside it (prices with tax) or on top. Exact integers."""
    subtotal = sum(ln["qty"] * ln["unit_minor"] for ln in lines)
    if discount_bp:
        if not 0 <= discount_bp <= 10_000:
            raise Invalid("discount: from 0 to 100 %", "discount")
        discount = money.part(subtotal, discount_bp)
    else:
        discount = int(discount_minor or 0)
    if discount < 0 or discount > subtotal:
        raise Invalid("discount: from 0 up to the subtotal", "discount")
    tx = tax(biz)
    _net, tax_minor, gross = money.split(subtotal - discount, tx["rate_bp"], tx["included"])
    return {"subtotal_minor": subtotal, "discount_minor": discount, "tax_minor": tax_minor, "total_minor": gross,
            "tax_name": tx["name"], "tax_rate_bp": tx["rate_bp"], "tax_included": tx["included"]}


def _number(conn: Connection, business_id: UUID) -> int:
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("pos:" + str(business_id)))))
    return conn.execute(select(func.coalesce(func.max(PS.c.number), 0) + 1).where(PS.c.business_id == business_id)).scalar()


def seller(conn: Connection, business_id: UUID, user_id: UUID | None) -> UUID | None:
    """The team member who is signed in (for their sales and commission), if they are on the team."""
    if not user_id:
        return None
    S = db.staff_members
    return conn.execute(select(S.c.id).where(S.c.business_id == business_id, S.c.user_id == user_id)).scalar()


def _insert(conn: Connection, biz: dict, actor: str, *, lines: list[dict], t: dict, method: str, contact_id, cash_given,
            payment_id, note: str, user_id) -> dict:
    change = None
    if method == "cash":
        given = t["total_minor"] if cash_given in (None, "") else cash_given
        if not isinstance(given, int) or isinstance(given, bool) or given < t["total_minor"]:
            raise Invalid(_t("pos_cash_short", biz["language"]), "cash")
        cash_given, change = given, given - t["total_minor"]
    else:
        cash_given = None
    paid = method != "link"
    row = dict(conn.execute(insert(PS).values(
        business_id=biz["id"], number=_number(conn, biz["id"]), contact_id=contact_id, staff_id=seller(conn, biz["id"], user_id),
        lines=lines, currency=biz["currency"], method=method, cash_given_minor=cash_given, change_minor=change, payment_id=payment_id,
        status="paid" if paid else "pending", token=secrets.token_urlsafe(16), note=(note or "").strip()[:300], day=today_of(biz),
        paid_at=func.now() if paid else None, created_by=actor[:100], **t).returning(PS)).mappings().one())
    for ln in lines:                                           # the shelf: out it goes
        if ln["item_id"]:
            INV.move(conn, biz["id"], actor, UUID(ln["item_id"]), -ln["qty"], "sale", sale_id=row["id"])
    log(conn, biz["id"], "pos.sale", actor, row["id"], {"number": row["number"], "total_minor": row["total_minor"], "method": method})
    return row


def sell(biz: dict, actor: str, *, lines: list[dict], method: str, discount_minor: int = 0, discount_bp: int = 0,
         contact_id: UUID | None = None, cash_given_minor: int | None = None, note: str = "", user_id: UUID | None = None,
         http=None) -> dict:
    """Ring it up. A pay link is made first with the business's own account (outside the sale's transaction: the
    provider is called); if the sale then can't be saved, the link is cancelled — nothing half-made stays."""
    from . import payments as PM
    if method not in METHODS:
        raise Invalid("cash, link, card or other", "method")
    with db.tx() as conn:
        if contact_id:
            contacts.get(conn, biz["id"], contact_id)
        clean = _lines(conn, biz, lines)
        t = totals(biz, clean, discount_minor=discount_minor, discount_bp=discount_bp)
        if method != "link":
            return _insert(conn, biz, actor, lines=clean, t=t, method=method, contact_id=contact_id, cash_given=cash_given_minor,
                           payment_id=None, note=note, user_id=user_id)
        if not PM.can_link(conn, biz["id"]):
            raise Invalid(_t("pos_no_account", biz["language"]), "method")
    if t["total_minor"] <= 0:
        raise Invalid("a pay link needs an amount above 0", "method")
    try:
        pay = PM.create(biz, actor, amount_minor=t["total_minor"], description=_t("pos_sale", biz["language"]), contact_id=contact_id,
                        http=http)
    except PM.PayError as e:
        raise Invalid(str(e), "method") from None
    try:
        with db.tx() as conn:
            clean = _lines(conn, biz, lines)                   # again, inside the sale's own transaction (prices, stock rows)
            t = totals(biz, clean, discount_minor=discount_minor, discount_bp=discount_bp)
            if t["total_minor"] != pay["amount_minor"]:
                raise Invalid("a price changed while ringing it up; try again", "lines")
            return _insert(conn, biz, actor, lines=clean, t=t, method="link", contact_id=contact_id, cash_given=None,
                           payment_id=pay["id"], note=note, user_id=user_id)
    except BaseException:
        with db.tx() as conn:
            conn.execute(update(db.payments).where(db.payments.c.id == pay["id"], db.payments.c.status == "pending")
                         .values(status="cancelled"))
        raise


# ---------------------------------------------------------------- reading
def get(conn: Connection, business_id: UUID, sale_id: UUID) -> dict:
    r = conn.execute(select(PS).where(PS.c.business_id == business_id, PS.c.id == sale_id)).mappings().first()
    if not r:
        raise NotFound("sale")
    return dict(r)


def by_token(conn: Connection, token: str) -> tuple[dict, dict]:
    r = conn.execute(select(PS).where(PS.c.token == (token or ""))).mappings().first() if token else None
    if not r:
        raise NotFound("sale")
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
    return dict(r), biz


def list_(conn: Connection, business_id: UUID, *, day: date | None = None, limit: int = 300) -> list[dict]:
    C, S = db.contacts, db.staff_members
    q = (select(PS, C.c.name.label("contact_name"), S.c.name.label("staff_name")).outerjoin(C, C.c.id == PS.c.contact_id)
         .outerjoin(S, S.c.id == PS.c.staff_id).where(PS.c.business_id == business_id))
    if day:
        q = q.where((PS.c.day == day) | (PS.c.refund_day == day))
    return [dict(r) for r in conn.execute(q.order_by(PS.c.number.desc()).limit(max(1, min(limit, 1000)))).mappings()]


def url(sale: dict) -> str:
    return f"{config.PUBLIC_URL()}/receipt/{sale['token']}"


def pay_url(conn: Connection, sale: dict) -> str | None:
    """The link the customer pays with (shown as a QR) while the sale waits."""
    if sale["status"] != "pending" or not sale["payment_id"]:
        return None
    p = conn.execute(select(db.payments).where(db.payments.c.id == sale["payment_id"])).mappings().first()
    if not p or p["status"] != "pending":
        return None
    return p["url"] or f"{config.PUBLIC_URL()}/p/{p['token']}"


def share_text(biz: dict, sale: dict, lang: str) -> str:
    amount = money.fmt(sale["total_minor"], sale["currency"], packs.country(biz["country"]).locale)
    return _t("pos_share_msg", lang).format(business=biz["name"], number=sale["number"], amount=amount, link=url(sale))


# ---------------------------------------------------------------- undo: void (a mistake) and refund (a return)
def _back_to_shelf(conn: Connection, biz: dict, actor: str, sale: dict, reason: str) -> None:
    for ln in sale["lines"]:
        if ln.get("item_id"):
            try:
                INV.move(conn, biz["id"], actor, UUID(ln["item_id"]), ln["qty"], reason, sale_id=sale["id"])
            except NotFound:
                pass                                           # the item is gone from the business: nothing to put back


def void(conn: Connection, biz: dict, actor: str, sale_id: UUID, reason: str) -> dict:
    """A mistake, the same day (or a pay link never paid): as if the sale never happened. Kept, marked, not counted.
    A paid pay link is money that arrived in the business's account: that is a refund, not a void."""
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        raise Invalid(_t("pos_why", biz["language"]), "reason")
    s = conn.execute(select(PS).where(PS.c.business_id == biz["id"], PS.c.id == sale_id).with_for_update()).mappings().first()
    if not s:
        raise NotFound("sale")
    if s["status"] not in ("paid", "pending") or (s["status"] == "paid" and (s["day"] != today_of(biz) or s["method"] == "link")):
        raise Invalid(_t("pos_cant_void", biz["language"]), "status")
    conn.execute(update(PS).where(PS.c.id == sale_id).values(status="void", voided_at=func.now(), undo_reason=reason))
    if s["payment_id"]:
        conn.execute(update(db.payments).where(db.payments.c.id == s["payment_id"], db.payments.c.status == "pending")
                     .values(status="cancelled"))
    _back_to_shelf(conn, biz, actor, dict(s), "void")
    log(conn, biz["id"], "pos.void", actor, sale_id, {"number": s["number"], "total_minor": s["total_minor"]})
    return get(conn, biz["id"], sale_id)


def refund(conn: Connection, biz: dict, actor: str, sale_id: UUID, reason: str) -> dict:
    """A return: all of the sale's money goes back the way it was paid (cash from the drawer; a link or a card in the
    business's own account or machine — done there, written down here). The items go back on the shelf."""
    reason = " ".join((reason or "").split())[:300]
    if not reason:
        raise Invalid(_t("pos_why", biz["language"]), "reason")
    s = conn.execute(select(PS).where(PS.c.business_id == biz["id"], PS.c.id == sale_id).with_for_update()).mappings().first()
    if not s:
        raise NotFound("sale")
    if s["status"] != "paid":
        raise Invalid(_t("pos_cant_refund", biz["language"]), "status")
    conn.execute(update(PS).where(PS.c.id == sale_id).values(status="refunded", refunded_at=func.now(), refund_day=today_of(biz),
                                                             undo_reason=reason))
    _back_to_shelf(conn, biz, actor, dict(s), "refund")
    log(conn, biz["id"], "pos.refund", actor, sale_id, {"number": s["number"], "total_minor": s["total_minor"]})
    return get(conn, biz["id"], sale_id)


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """A pay link was paid (the provider's webhook, the customer's phone prompt, the owner confirming): its sale is paid."""
    if type_ != "payment.paid" or not subject_id:
        return
    rows = conn.execute(update(PS).where(PS.c.business_id == business_id, PS.c.payment_id == subject_id, PS.c.status == "pending")
                        .values(status="paid", paid_at=func.now()).returning(PS.c.id, PS.c.number)).all()
    for sid, number in rows:
        log(conn, business_id, "pos.paid", "payment", sid, {"number": number})


# ---------------------------------------------------------------- the day
def day_summary(conn: Connection, biz: dict, day: date) -> dict:
    """The day by method (sales paid that day minus refunds given that day) and the cash the drawer should hold."""
    by = {m: {"n": 0, "sales_minor": 0, "refunds_minor": 0} for m in METHODS}
    pending = 0
    for s in list_(conn, biz["id"], day=day, limit=1000):
        if s["currency"] != biz["currency"]:
            continue
        m = by[s["method"]]
        if s["day"] == day and s["status"] in ("paid", "refunded"):
            m["n"] += 1
            m["sales_minor"] += s["total_minor"]
        if s["day"] == day and s["status"] == "pending":
            pending += 1
        if s["status"] == "refunded" and s["refund_day"] == day:
            m["refunds_minor"] += s["total_minor"]
    total = sum(m["sales_minor"] - m["refunds_minor"] for m in by.values())
    return {"by": by, "net_minor": total, "cash_minor": by["cash"]["sales_minor"] - by["cash"]["refunds_minor"],
            "count": sum(m["n"] for m in by.values()), "pending": pending}


def close_day(conn: Connection, biz: dict, actor: str, day: date, *, float_minor: int, counted_minor: int, note: str = "") -> dict:
    """Count the drawer: expected = the cash it started with + cash sales − cash refunds. A recount is a new row."""
    for v, f in ((float_minor, "float"), (counted_minor, "counted")):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise Invalid("an amount, 0 or more", f)
    if day > today_of(biz):
        raise Invalid("that day hasn't come yet", "day")
    s = day_summary(conn, biz, day)
    expected = float_minor + s["cash_minor"]
    row = dict(conn.execute(insert(PD).values(business_id=biz["id"], day=day, float_minor=float_minor, expected_minor=expected,
                                              counted_minor=counted_minor, totals={k: v for k, v in s["by"].items()},
                                              note=(note or "").strip()[:300], closed_by=actor[:100]).returning(PD)).mappings().one())
    log(conn, biz["id"], "pos.day_closed", actor, row["id"], {"day": day.isoformat(), "expected_minor": expected,
                                                               "counted_minor": counted_minor, "difference_minor": counted_minor - expected})
    return row


def closes(conn: Connection, business_id: UUID, *, day: date | None = None, limit: int = 30) -> list[dict]:
    q = select(PD).where(PD.c.business_id == business_id)
    if day:
        q = q.where(PD.c.day == day)
    return [dict(r) for r in conn.execute(q.order_by(PD.c.day.desc(), PD.c.created_at.desc()).limit(limit)).mappings()]
