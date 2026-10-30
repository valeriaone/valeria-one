"""Affiliates (ars02, v0.21): people who send the business customers — a partner, an influencer, a happy client — each
with their own link /a/<code> (random, can't be guessed) and what they earn: a % of the sale before tax, or a fixed
amount per sale, on every paid sale for COMMISSION_DAYS after the customer came (or only the first one).

How a customer is theirs (once, the first affiliate wins, never moved by itself): the link was opened on the customer's
phone and the customer then became NEW at the business — a booking, the website, a form, a funnel page (the phone
remembers it like the "bring a friend" link: the session, 14 days) — or the owner says so on the affiliate's page.

What they earn is written when the money is in: an invoice paid, a sale at the register paid (cash, card, or its pay
link paid). A refund or a void takes it back with a "reverse" line of the same amount (never deleted: the history stays
true). Valeria One handles no money: the owner pays the affiliate outside the app and writes it down (a payout), which
settles everything owed up to then. Money is integers in the smallest unit; a % is basis points, rounded half up.
"""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, db, money
from . import Invalid, NotFound, log

AF, AR, AC, AP = db.affiliates, db.affiliate_referrals, db.affiliate_commissions, db.affiliate_payouts
COMMISSION_DAYS = 365
REMEMBER_DAYS = 14                     # the phone remembers an affiliate's link this long (the session cookie's life)
_CODE = re.compile(r"^[A-Za-z0-9_-]{8,16}$")


def url(code: str) -> str:
    return f"{config.PUBLIC_URL()}/a/{code}"


# ---------------------------------------------------------------- the people
def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(AF).where(AF.c.business_id == business_id).order_by(AF.c.active.desc(), AF.c.name))
            .mappings()]
    tot = {r["affiliate_id"]: r for r in conn.execute(select(
        AC.c.affiliate_id, func.coalesce(func.sum(AC.c.amount_minor), 0).label("earned"),
        func.coalesce(func.sum(AC.c.amount_minor).filter(AC.c.payout_id.is_(None)), 0).label("owed"))
        .where(AC.c.business_id == business_id).group_by(AC.c.affiliate_id)).mappings()}
    people = dict(conn.execute(select(AR.c.affiliate_id, func.count()).where(AR.c.business_id == business_id)
                               .group_by(AR.c.affiliate_id)).all())
    for r in rows:
        t = tot.get(r["id"]) or {}
        r.update(earned=t.get("earned", 0), owed=t.get("owed", 0), customers=people.get(r["id"], 0))
    return rows


def get(conn: Connection, business_id: UUID, aff_id: UUID) -> dict:
    r = conn.execute(select(AF).where(AF.c.business_id == business_id, AF.c.id == aff_id)).mappings().first()
    if not r:
        raise NotFound("affiliate")
    return dict(r)


def _clean(biz: dict, data: dict) -> dict:
    name = " ".join(str(data.get("name") or "").split())[:200]
    if not name:
        raise Invalid("their name", "name")
    kind = data.get("kind") if data.get("kind") in ("pct", "fixed") else "pct"
    rate, fixed = int(data.get("rate_bp") or 0), int(data.get("fixed_minor") or 0)
    if kind == "pct" and not 0 < rate <= 10_000:
        raise Invalid("commission: a % from 0.01 to 100", "rate")
    if kind == "fixed" and fixed <= 0:
        raise Invalid("commission: an amount per sale", "fixed")
    email = str(data.get("email") or "").strip().lower()[:320]
    if email and ("@" not in email or " " in email):
        raise Invalid("check the email", "email")
    return {"name": name, "email": email, "phone": str(data.get("phone") or "").strip()[:30], "kind": kind,
            "rate_bp": rate if kind == "pct" else 0, "fixed_minor": fixed if kind == "fixed" else 0,
            "first_only": bool(data.get("first_only")), "note": str(data.get("note") or "").strip()[:300],
            "active": bool(data.get("active", True))}


def save(conn: Connection, biz: dict, actor: str, data: dict, aff_id: UUID | None = None) -> dict:
    """New or changed. A new rate applies to sales from now on; what was already earned stays as it was."""
    vals = _clean(biz, data)
    if aff_id:
        get(conn, biz["id"], aff_id)
        conn.execute(update(AF).where(AF.c.business_id == biz["id"], AF.c.id == aff_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "affiliate.updated", actor, aff_id)
        return get(conn, biz["id"], aff_id)
    row = conn.execute(insert(AF).values(business_id=biz["id"], code=secrets.token_urlsafe(9), **vals).returning(AF)).mappings().one()
    log(conn, biz["id"], "affiliate.added", actor, row["id"])
    return dict(row)


# ---------------------------------------------------------------- the link and who it brought
def open_link(code: str) -> tuple[dict, dict]:
    """/a/<code> opened: (the affiliate, the business). Counted. NotFound when unknown, off or the business is paused."""
    if not _CODE.match(code or ""):
        raise NotFound("link")
    with db.tx() as conn:
        a = conn.execute(select(AF).where(AF.c.code == code, AF.c.active)).mappings().first()
        if not a:
            raise NotFound("link")
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == a["business_id"])).mappings().one())
        if biz["suspended_at"]:
            raise NotFound("link")
        conn.execute(update(AF).where(AF.c.id == a["id"]).values(clicks=AF.c.clicks + 1))
    return dict(a), biz


def remember(aff: dict, now: datetime | None = None) -> dict:
    return {"c": aff["code"], "b": str(aff["business_id"]), "at": (now or datetime.now(timezone.utc)).isoformat()}


def claim(conn: Connection, biz: dict, ref: dict | None, contact: dict) -> dict | None:
    """A new customer arrived on a phone that opened an affiliate's link: theirs, once (first affiliate wins)."""
    if not isinstance(ref, dict) or ref.get("b") != str(biz["id"]):
        return None
    try:
        opened = datetime.fromisoformat(str(ref.get("at")))
    except ValueError:
        return None
    if datetime.now(timezone.utc) - opened > timedelta(days=REMEMBER_DAYS):
        return None
    if contact.get("created_at") and contact["created_at"] < opened - timedelta(minutes=1):
        return None                                            # already a customer before the link: not theirs
    a = conn.execute(select(AF).where(AF.c.business_id == biz["id"], AF.c.code == str(ref.get("c") or "")[:16], AF.c.active)
                     ).mappings().first()
    return _link(conn, biz, a, contact["id"], "link") if a else None


def assign(conn: Connection, biz: dict, actor: str, aff_id: UUID, contact_id: UUID) -> dict:
    """The owner says this customer came through them (a phone call, the code said at the counter)."""
    from . import contacts
    a = get(conn, biz["id"], aff_id)
    contacts.get(conn, biz["id"], contact_id)
    row = _link(conn, biz, a, contact_id, "owner")
    if not row:
        raise Invalid("this customer already came through someone", "contact")
    log(conn, biz["id"], "affiliate.customer_added", actor, contact_id, {"affiliate_id": str(aff_id)})
    return row


def _link(conn: Connection, biz: dict, a: dict, contact_id: UUID, how: str) -> dict | None:
    row = conn.execute(pg_insert(AR).values(business_id=biz["id"], affiliate_id=a["id"], contact_id=contact_id, how=how)
                       .on_conflict_do_nothing(constraint="uq_affiliate_customer").returning(AR)).mappings().first()
    if row and how == "link":
        log(conn, biz["id"], "affiliate.customer", "customer", contact_id, {"affiliate_id": str(a["id"])})
    return dict(row) if row else None


def customers(conn: Connection, business_id: UUID, aff_id: UUID) -> list[dict]:
    C = db.contacts
    return [dict(r) for r in conn.execute(select(AR.c.contact_id, AR.c.how, AR.c.created_at, C.c.name)
                                          .join(C, C.c.id == AR.c.contact_id)
                                          .where(AR.c.business_id == business_id, AR.c.affiliate_id == aff_id)
                                          .order_by(AR.c.created_at.desc()).limit(200)).mappings()]


# ---------------------------------------------------------------- what they earn
def commission(a: dict, base_minor: int) -> int:
    """The affiliate's share of a sale (before tax): a % (basis points, half up) or the fixed amount."""
    if a["kind"] == "fixed":
        return int(a["fixed_minor"])
    return money.part(max(0, int(base_minor)), int(a["rate_bp"]))


def _sale(conn: Connection, business_id: UUID, source: str, sid: UUID) -> dict | None:
    """(contact, base before tax, currency, paid) of an invoice or a register sale."""
    T = db.invoices if source == "invoice" else db.pos_sales
    r = conn.execute(select(T.c.contact_id, T.c.total_minor, T.c.tax_minor, T.c.currency)
                     .where(T.c.business_id == business_id, T.c.id == sid)).mappings().first()
    if not r or not r["contact_id"]:
        return None
    return {"contact_id": r["contact_id"], "base": int(r["total_minor"]) - int(r["tax_minor"] or 0), "currency": r["currency"]}


def earn(conn: Connection, business_id: UUID, source: str, sid: UUID, now: datetime | None = None) -> dict | None:
    """A sale was paid: if its customer came through an affiliate (in the window), write what they earned. Once per sale."""
    s = _sale(conn, business_id, source, sid)
    if not s:
        return None
    ref = conn.execute(select(AR).where(AR.c.business_id == business_id, AR.c.contact_id == s["contact_id"])).mappings().first()
    if not ref:
        return None
    a = conn.execute(select(AF).where(AF.c.id == ref["affiliate_id"])).mappings().one()
    now = now or datetime.now(timezone.utc)
    if now - ref["created_at"] > timedelta(days=COMMISSION_DAYS):
        return None
    if a["first_only"] and conn.execute(select(AC.c.id).where(AC.c.business_id == business_id, AC.c.affiliate_id == a["id"],
                                                              AC.c.contact_id == s["contact_id"], AC.c.kind == "earn")).first():
        return None
    amt = commission(a, s["base"])
    if amt <= 0:
        return None
    row = conn.execute(pg_insert(AC).values(business_id=business_id, affiliate_id=a["id"], contact_id=s["contact_id"], source=source,
                                            source_id=sid, kind="earn", base_minor=s["base"], amount_minor=amt, currency=s["currency"])
                       .on_conflict_do_nothing(constraint="uq_affiliate_commission").returning(AC)).mappings().first()
    if row:
        log(conn, business_id, "affiliate.earned", "system", a["id"], {"source": source, "source_id": str(sid), "amount_minor": amt})
    return dict(row) if row else None


def reverse(conn: Connection, business_id: UUID, source: str, sid: UUID) -> dict | None:
    """A sale given back (refund, void): what was earned on it comes off — a negative line, so history stays true."""
    e = conn.execute(select(AC).where(AC.c.business_id == business_id, AC.c.source == source, AC.c.source_id == sid,
                                      AC.c.kind == "earn")).mappings().first()
    if not e:
        return None
    row = conn.execute(pg_insert(AC).values(business_id=business_id, affiliate_id=e["affiliate_id"], contact_id=e["contact_id"],
                                            source=source, source_id=sid, kind="reverse", base_minor=-e["base_minor"],
                                            amount_minor=-e["amount_minor"], currency=e["currency"])
                       .on_conflict_do_nothing(constraint="uq_affiliate_commission").returning(AC)).mappings().first()
    return dict(row) if row else None


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """Called by services.log(): money in -> earned; money back -> reversed."""
    if not subject_id or type_ not in ("invoice.paid", "pos.sale", "pos.paid", "pos.refund", "pos.void"):
        return
    if type_ == "invoice.paid":
        earn(conn, business_id, "invoice", subject_id)
    elif type_ in ("pos.sale", "pos.paid"):
        if conn.execute(select(db.pos_sales.c.status).where(db.pos_sales.c.id == subject_id)).scalar() == "paid":
            earn(conn, business_id, "pos", subject_id)
    else:
        reverse(conn, business_id, "pos", subject_id)


def lines(conn: Connection, business_id: UUID, aff_id: UUID, limit: int = 200) -> list[dict]:
    C = db.contacts
    return [dict(r) for r in conn.execute(select(AC, C.c.name.label("contact_name")).outerjoin(C, C.c.id == AC.c.contact_id)
                                          .where(AC.c.business_id == business_id, AC.c.affiliate_id == aff_id)
                                          .order_by(AC.c.created_at.desc()).limit(limit)).mappings()]


def owed(conn: Connection, business_id: UUID, aff_id: UUID) -> int:
    return int(conn.execute(select(func.coalesce(func.sum(AC.c.amount_minor), 0)).where(
        AC.c.business_id == business_id, AC.c.affiliate_id == aff_id, AC.c.payout_id.is_(None))).scalar())


def pay(conn: Connection, biz: dict, actor: str, aff_id: UUID, *, paid_on: date, note: str = "") -> dict:
    """The owner paid them (outside the app): everything owed up to now is settled by this payout, kept in the history."""
    a = get(conn, biz["id"], aff_id)
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("aff:" + str(aff_id)))))
    ids = [r[0] for r in conn.execute(select(AC.c.id).where(AC.c.business_id == biz["id"], AC.c.affiliate_id == a["id"],
                                                            AC.c.payout_id.is_(None)))]
    total = int(conn.execute(select(func.coalesce(func.sum(AC.c.amount_minor), 0)).where(AC.c.id.in_(ids or [None]))).scalar())
    if total <= 0:
        raise Invalid("nothing is owed to them right now", "amount")
    p = dict(conn.execute(insert(AP).values(business_id=biz["id"], affiliate_id=a["id"], amount_minor=total, currency=biz["currency"],
                                            paid_on=paid_on, note=(note or "").strip()[:300], created_by=actor).returning(AP)).mappings().one())
    conn.execute(update(AC).where(AC.c.id.in_(ids)).values(payout_id=p["id"]))
    log(conn, biz["id"], "affiliate.paid", actor, a["id"], {"amount_minor": total, "payout_id": str(p["id"])})
    return p


def payouts(conn: Connection, business_id: UUID, aff_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(AP).where(AP.c.business_id == business_id, AP.c.affiliate_id == aff_id)
                                          .order_by(AP.c.paid_on.desc(), AP.c.created_at.desc())).mappings()]


def report_csv(conn: Connection, biz: dict, actor: str) -> str:
    """Every affiliate: customers, earned, paid, owed — for the owner's records."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "phone", "code", "commission", "customers", "earned", "paid", "owed", "currency"])
    for a in list_(conn, biz["id"]):
        rate = f"{a['rate_bp'] / 100:g}%" if a["kind"] == "pct" else money.to_major(a["fixed_minor"], biz["currency"])
        w.writerow([a["name"], a["email"], a["phone"], a["code"], rate, a["customers"], money.to_major(a["earned"], biz["currency"]),
                    money.to_major(a["earned"] - a["owed"], biz["currency"]), money.to_major(a["owed"], biz["currency"]), biz["currency"]])
    log(conn, biz["id"], "affiliates.exported", actor, biz["id"])
    return buf.getvalue()
