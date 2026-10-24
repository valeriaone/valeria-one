"""Partner API: how a host or an agency runs Valeria One from its own systems (control panel, billing).

A partner key belongs to the server, not to a business. With it a partner can see the server's health,
ask for a backup or an update, create businesses for its customers (the owner gets an invitation), pause and
resume them (for example when a bill is unpaid), re-invite an owner, read usage for billing, and delete a
business for good. Every action is on record (instance_events, and the business's own events).

Paused means: nobody signs in, the business's API keys stop working, nothing is sent and every block is off;
messages from customers still arrive and are kept, so resuming loses nothing.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .. import blocks, crypto, db
from . import Invalid, NotFound, businesses, instance, log, team

PK, IE, B, U, MB = db.partner_keys, db.instance_events, db.businesses, db.users, db.memberships
PREFIX = "vo_partner_"


# ---------------------------------------------------------------- keys
def new_key(conn: Connection, name: str, actor: str) -> tuple[str, dict]:
    """-> (the full key, shown ONCE and never stored, the stored row)."""
    name = (name or "").strip()[:100]
    if not name:
        raise Invalid("give the key a name, e.g. the partner's name", "name")
    key = PREFIX + secrets.token_urlsafe(32)
    row = conn.execute(insert(PK).values(name=name, prefix=key[:18], key_hash=crypto.sha256(key))
                       .returning(PK.c.id, PK.c.name, PK.c.prefix, PK.c.created_at)).mappings().one()
    audit(conn, "partner_key.created", actor, {"name": name, "prefix": row["prefix"]})
    return key, dict(row)


def keys(conn: Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(select(PK.c.id, PK.c.name, PK.c.prefix, PK.c.created_at, PK.c.last_used_at,
                                                 PK.c.revoked_at).order_by(PK.c.created_at.desc())).mappings()]


def revoke_key(conn: Connection, key_id: UUID, actor: str) -> None:
    row = conn.execute(update(PK).where(PK.c.id == key_id, PK.c.revoked_at.is_(None)).values(revoked_at=func.now())
                       .returning(PK.c.prefix)).first()
    if not row:
        raise NotFound("partner key")
    audit(conn, "partner_key.revoked", actor, {"prefix": row.prefix})


def by_key(conn: Connection, key: str) -> dict | None:
    if not key or not key.startswith(PREFIX):
        return None
    k = conn.execute(select(PK).where(PK.c.key_hash == crypto.sha256(key), PK.c.revoked_at.is_(None))).mappings().first()
    if not k:
        return None
    conn.execute(update(PK).where(PK.c.id == k["id"]).values(last_used_at=func.now()))
    return dict(k)


def audit(conn: Connection, type_: str, actor: str, data: dict | None = None) -> None:
    conn.execute(insert(IE).values(type=type_, actor=actor, data=data or {}))


def audit_log(conn: Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(select(IE).order_by(IE.c.id.desc()).limit(limit)).mappings()]


# ---------------------------------------------------------------- businesses
def _out(conn: Connection, biz: dict) -> dict:
    people = conn.execute(select(func.count()).select_from(MB).where(MB.c.business_id == biz["id"])).scalar()
    return {"id": biz["id"], "name": biz["name"], "country": biz["country"], "industry": biz["industry"],
            "language": biz["language"], "timezone": biz["timezone"], "currency": biz["currency"],
            "partner_ref": biz["partner_ref"], "created_at": biz["created_at"], "people": people,
            "suspended": bool(biz["suspended_at"]), "suspended_at": biz["suspended_at"],
            "suspended_reason": biz["suspended_reason"],
            "blocks": {b: (biz.get("features") or {}).get(b, True) is not False for b in blocks.BLOCKS}}


def _get(conn: Connection, business_id: UUID) -> dict:
    biz = conn.execute(select(B).where(B.c.id == business_id, B.c.sandbox.is_(False))).mappings().first()
    if not biz:
        raise NotFound("business")
    return dict(biz)


def list_(conn: Connection, *, partner_ref: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    q = select(B).where(B.c.sandbox.is_(False)).order_by(B.c.created_at).limit(min(max(limit, 1), 500)).offset(max(offset, 0))
    if partner_ref:
        q = q.where(B.c.partner_ref == partner_ref)
    return [_out(conn, dict(r)) for r in conn.execute(q).mappings()]


def get(conn: Connection, business_id: UUID) -> dict:
    return _out(conn, _get(conn, business_id))


def create(conn: Connection, actor: str, *, name: str, country: str, owner_email: str, industry: str = "generic",
           language: str | None = None, timezone: str | None = None, partner_ref: str | None = None,
           blocks_on: list[str] | None = None) -> tuple[dict, str]:
    """A business for a partner's customer; its owner joins by the invitation (token shown once)."""
    ref = (partner_ref or "").strip()[:100] or None
    if ref and conn.execute(select(B.c.id).where(B.c.partner_ref == ref)).first():
        raise Invalid(f"a business with partner_ref {ref!r} already exists", "partner_ref")
    biz = businesses.create(conn, name=name, country=country, industry=industry, language=language, timezone=timezone)
    if ref:
        try:
            with conn.begin_nested():
                conn.execute(update(B).where(B.c.id == biz["id"]).values(partner_ref=ref))
        except IntegrityError:
            raise Invalid(f"a business with partner_ref {ref!r} already exists", "partner_ref") from None
    if blocks_on is not None:
        businesses.set_blocks(conn, biz["id"], actor, blocks_on)
    _, token = team.invite(conn, biz, actor, owner_email, role="owner")
    log(conn, biz["id"], "partner.business_created", actor, biz["id"], {"partner_ref": ref})
    audit(conn, "business.created", actor, {"business_id": str(biz["id"]), "name": biz["name"], "partner_ref": ref})
    return get(conn, biz["id"]), token


def invite(conn: Connection, actor: str, business_id: UUID, email: str, role: str = "owner") -> str:
    biz = _get(conn, business_id)
    _, token = team.invite(conn, biz, actor, email, role=role)
    return token


def suspend(conn: Connection, actor: str, business_id: UUID, reason: str = "") -> dict:
    biz = _get(conn, business_id)
    reason = (reason or "").strip()[:300] or None
    if not biz["suspended_at"]:
        conn.execute(update(B).where(B.c.id == business_id).values(suspended_at=func.now(), suspended_reason=reason))
        log(conn, business_id, "business.suspended", actor, business_id, {"reason": reason})
        audit(conn, "business.suspended", actor, {"business_id": str(business_id), "reason": reason})
    return get(conn, business_id)


def resume(conn: Connection, actor: str, business_id: UUID) -> dict:
    biz = _get(conn, business_id)
    if biz["suspended_at"]:
        conn.execute(update(B).where(B.c.id == business_id).values(suspended_at=None, suspended_reason=None))
        log(conn, business_id, "business.resumed", actor, business_id)
        audit(conn, "business.resumed", actor, {"business_id": str(business_id)})
    return get(conn, business_id)


def remove(conn: Connection, actor: str, business_id: UUID, confirm: str) -> None:
    """Delete a business and everything in it, for good. `confirm` must be the business's exact name.
    People left with no business at all lose their account too (the server's admin never does)."""
    biz = _get(conn, business_id)
    if (confirm or "") != biz["name"]:
        raise Invalid("to delete, send confirm = the business's exact name", "confirm")
    people = [r[0] for r in conn.execute(select(MB.c.user_id).where(MB.c.business_id == business_id))]
    conn.execute(delete(B).where(B.c.id == business_id))                      # every business table cascades
    gone = 0
    for uid in people:
        still = conn.execute(select(MB.c.user_id).where(MB.c.user_id == uid).limit(1)).first()
        if not still:
            gone += conn.execute(delete(U).where(U.c.id == uid, U.c.is_admin.is_(False))).rowcount
    audit(conn, "business.deleted", actor, {"business_id": str(business_id), "name": biz["name"],
                                            "partner_ref": biz["partner_ref"], "accounts_removed": gone})


# ---------------------------------------------------------------- usage, for billing
def _month(month: str | None) -> tuple[datetime, datetime, str]:
    today = date.today()
    try:
        y, m = (int(x) for x in (month or f"{today.year}-{today.month:02d}").split("-"))
        start = datetime(y, m, 1, tzinfo=timezone.utc)
    except ValueError:
        raise Invalid("month is YYYY-MM", "month") from None
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return start, end, f"{y}-{m:02d}"


def usage(conn: Connection, month: str | None = None, business_id: UUID | None = None) -> dict:
    """Per business, for the month (UTC): messages in and out, customers, people, appointments made,
    payments received. Counts only — never content."""
    start, end, label = _month(month)
    M, C, A, P = db.messages, db.contacts, db.appointments, db.payments

    def per(q):
        return {r[0]: r[1] for r in conn.execute(q)}

    inm = per(select(M.c.business_id, func.count()).where(M.c.direction == "in", M.c.created_at >= start,
                                                          M.c.created_at < end).group_by(M.c.business_id))
    outm = per(select(M.c.business_id, func.count()).where(M.c.direction == "out", M.c.created_at >= start,
                                                           M.c.created_at < end).group_by(M.c.business_id))
    cust = per(select(C.c.business_id, func.count()).where(C.c.deleted_at.is_(None)).group_by(C.c.business_id))
    ppl = per(select(MB.c.business_id, func.count()).group_by(MB.c.business_id))
    appts = per(select(A.c.business_id, func.count()).where(A.c.created_at >= start, A.c.created_at < end)
                .group_by(A.c.business_id))
    paid = per(select(P.c.business_id, func.count()).where(P.c.status == "paid", P.c.paid_at >= start, P.c.paid_at < end)
               .group_by(P.c.business_id))
    q = select(B.c.id, B.c.name, B.c.partner_ref, B.c.suspended_at).where(B.c.sandbox.is_(False)).order_by(B.c.created_at)
    if business_id:
        q = q.where(B.c.id == business_id)
    rows = [{"business_id": r.id, "name": r.name, "partner_ref": r.partner_ref, "suspended": bool(r.suspended_at),
             "messages_in": inm.get(r.id, 0), "messages_out": outm.get(r.id, 0), "customers": cust.get(r.id, 0),
             "people": ppl.get(r.id, 0), "appointments": appts.get(r.id, 0), "payments_paid": paid.get(r.id, 0)}
            for r in conn.execute(q)]
    if business_id and not rows:
        raise NotFound("business")
    return {"month": label, "from": start, "to": end, "businesses": rows}


# ---------------------------------------------------------------- the server
def server(conn: Connection) -> dict:
    st = instance.status(conn)
    st["backups"] = [{"name": b["name"], "size": b["size"], "at": b["at"]} for b in st["backups"]]
    return st


def request(conn: Connection, op: str, actor: str) -> None:
    instance.request(op, actor)
    audit(conn, f"server.{op}_requested", actor)
