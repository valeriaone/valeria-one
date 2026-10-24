"""Quotes the customer accepts by electronic signature.

A quote is frozen when it is created: its content gets a SHA-256 fingerprint (doc_hash). The customer opens
/q/<token>, reads it and accepts by typing their full name and ticking "I agree". We record who, when, from
which IP and device, and check the fingerprint again — so the record proves exactly what was accepted.
Which law gives that signature its weight (ESIGN/UETA, eIDAS, Mexico's Código de Comercio…) comes from the
country pack and is shown on the page. To change a quote, send a new one; a sent quote never changes.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db, money, packs
from . import Invalid, NotFound, contacts, log

Q = db.quotes
MAX_LINES = 30


def _canonical(q: dict) -> str:
    keep = ("number", "title", "lines", "currency", "subtotal_minor", "tax_minor", "total_minor", "tax_name", "tax_rate_bp",
            "notes", "valid_until", "business_id", "contact_id")
    return json.dumps({k: (str(q[k]) if isinstance(q.get(k), (UUID, date)) else q.get(k)) for k in keep},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(q: dict) -> str:
    return hashlib.sha256(_canonical(q).encode()).hexdigest()


def create(conn: Connection, biz: dict, actor: str, *, title: str, lines: list[dict], contact_id: UUID | None = None,
           deal_id: UUID | None = None, notes: str = "", valid_days: int = 15) -> dict:
    title = (title or "").strip()
    if not title:
        raise Invalid("give the quote a title", "title")
    clean = []
    for ln in (lines or [])[:MAX_LINES]:
        desc = str(ln.get("description") or "").strip()
        if not desc:
            continue
        qty, unit = ln.get("qty", 1), ln.get("unit_minor")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 100000:
            raise Invalid("quantity: a whole number from 1", "lines")
        if not isinstance(unit, int) or isinstance(unit, bool) or unit < 0:
            raise Invalid("price: a whole number in the smallest currency unit", "lines")
        clean.append({"description": desc[:200], "qty": qty, "unit_minor": unit})
    if not clean:
        raise Invalid("add at least one line", "lines")
    if not 1 <= int(valid_days) <= 365:
        raise Invalid("valid for 1 to 365 days", "valid_days")
    if contact_id:
        contacts.get(conn, biz["id"], contact_id)
    if deal_id:
        from . import deals
        deals.get(conn, biz["id"], deal_id)
    pack = packs.country(biz["country"])
    subtotal = sum(ln["qty"] * ln["unit_minor"] for ln in clean)
    p = money.price(subtotal, pack, includes_tax=False)
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("quote:" + str(biz["id"])))))
    number = conn.execute(select(func.coalesce(func.max(Q.c.number), 0) + 1).where(Q.c.business_id == biz["id"])).scalar()
    q = {"business_id": biz["id"], "number": number, "contact_id": contact_id, "deal_id": deal_id, "title": title[:200],
         "lines": clean, "currency": biz["currency"], "subtotal_minor": subtotal, "tax_minor": p.tax_minor,
         "total_minor": p.gross_minor if p.gross_minor is not None else subtotal, "tax_name": pack.tax.name,
         "tax_rate_bp": pack.tax.rate_bp, "notes": (notes or "").strip()[:2000],
         "valid_until": (datetime.now(timezone.utc) + timedelta(days=int(valid_days))).date()}
    q["doc_hash"] = fingerprint(q)
    row = conn.execute(insert(Q).values(**q, token=secrets.token_urlsafe(16)).returning(Q)).mappings().one()
    log(conn, biz["id"], "quote.sent", actor, row["id"], {"number": number, "total_minor": q["total_minor"]})
    return dict(row)


def by_token(conn: Connection, token: str) -> tuple[dict, dict]:
    row = conn.execute(select(Q).where(Q.c.token == (token or ""))).mappings().first()
    if not row:
        raise NotFound("quote")
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
    return dict(row), biz


def accept(conn: Connection, token: str, *, name: str, agree: bool, ip: str, ua: str, today: date | None = None) -> dict:
    q, biz = by_token(conn, token)
    name = (name or "").strip()
    if q["status"] != "sent":
        raise Invalid("this quote can't be accepted anymore", "status")
    if (today or datetime.now(timezone.utc).date()) > q["valid_until"]:
        raise Invalid("this quote has expired — ask for a new one", "valid_until")
    if len(name) < 3:
        raise Invalid("type your full name to sign", "name")
    if not agree:
        raise Invalid("tick the box to agree", "agree")
    if fingerprint(q) != q["doc_hash"]:                       # the quote must be exactly what was sent
        raise Invalid("this quote was changed after it was sent and can't be signed", "doc_hash")
    row = conn.execute(update(Q).where(Q.c.id == q["id"], Q.c.status == "sent").values(
        status="accepted", signed_name=name[:200], signed_at=func.now(), signed_ip=(ip or "")[:64], signed_ua=(ua or "")[:300])
        .returning(Q)).mappings().one()
    log(conn, q["business_id"], "quote.accepted", "customer", q["id"], {"number": q["number"], "signed_name": name[:200],
                                                                        "doc_hash": q["doc_hash"]})
    return dict(row)


def decline(conn: Connection, token: str, reason: str = "") -> dict:
    q, _ = by_token(conn, token)
    if q["status"] != "sent":
        raise Invalid("this quote can't be declined anymore", "status")
    row = conn.execute(update(Q).where(Q.c.id == q["id"]).values(status="declined", decline_reason=(reason or "").strip()[:1000])
                       .returning(Q)).mappings().one()
    log(conn, q["business_id"], "quote.declined", "customer", q["id"], {"number": q["number"]})
    return dict(row)


def cancel(conn: Connection, business_id: UUID, actor: str, quote_id: UUID) -> None:
    if not conn.execute(update(Q).where(Q.c.business_id == business_id, Q.c.id == quote_id, Q.c.status == "sent")
                        .values(status="cancelled")).rowcount:
        raise NotFound("open quote")
    log(conn, business_id, "quote.cancelled", actor, quote_id)


def list_(conn: Connection, business_id: UUID, *, contact_id=None, deal_id=None, limit: int = 50) -> list[dict]:
    s = select(Q).where(Q.c.business_id == business_id)
    if contact_id:
        s = s.where(Q.c.contact_id == contact_id)
    if deal_id:
        s = s.where(Q.c.deal_id == deal_id)
    return [dict(r) for r in conn.execute(s.order_by(Q.c.number.desc()).limit(max(1, min(limit, 200)))).mappings()]
