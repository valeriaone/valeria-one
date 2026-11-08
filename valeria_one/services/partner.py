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


# ---------------------------------------------------------------- pb01 (v0.45.0): a partner's own app opens the office
SSO_SECONDS = 120


def _sso_secret() -> str:
    from .. import config
    return crypto.sha256("sso:" + (config.get("VO_SECRET_KEY", required=True) or ""))


def login_link(conn: Connection, actor: str, business_id: UUID, *, email: str, name: str = "", role: str = "owner",
               next_: str = "/app", now: datetime | None = None) -> str:
    """A one-use sign-in for a person of the partner's app (e.g. a realtor on valeria.valai.mx): they become a member
    of the business if they are not yet (a new account gets an unusable password — «forgot password» sets one), and
    the link signs them in once, within 2 minutes. -> the token for /sso/{token}."""
    import base64
    import json as _json
    from . import users as US
    biz = _get(conn, business_id)
    e = US._email(email)
    if role not in ("owner", "staff"):
        raise Invalid("role is owner or staff", "role")
    u = conn.execute(select(U).where(func.lower(U.c.email) == e)).mappings().first()
    if not u:
        u = conn.execute(insert(U).values(email=e, name=(name or "").strip()[:200] or e.split("@")[0],
                                          password_hash=US._ph.hash(secrets.token_urlsafe(32))).returning(U)).mappings().one()
        log(conn, biz["id"], "user.joined", actor, u["id"], {"role": role, "via": "partner"})
    if not conn.execute(select(MB.c.user_id).where(MB.c.business_id == biz["id"], MB.c.user_id == u["id"])).first():
        conn.execute(insert(MB).values(business_id=biz["id"], user_id=u["id"], role=role))
        log(conn, biz["id"], "member.added", actor, u["id"], {"role": role, "via": "partner"})
    nxt = next_ if (next_ or "").startswith("/app") and "//" not in next_ else "/app"
    exp = int((now or datetime.now(timezone.utc)).timestamp()) + SSO_SECONDS
    body = base64.urlsafe_b64encode(_json.dumps({"u": str(u["id"]), "b": str(biz["id"]), "n": nxt, "e": exp,
                                                 "j": secrets.token_urlsafe(9)}, separators=(",", ":")).encode()).decode().rstrip("=")
    audit(conn, "sso.link", actor, {"business_id": str(biz["id"]), "user_id": str(u["id"])})
    return body + "." + crypto.hmac_sha256_hex(_sso_secret(), body.encode())


def use_login(conn: Connection, token: str, now: datetime | None = None) -> dict | None:
    """-> {"user_id", "business_id", "next"} once, or None (bad, expired, used, paused or no longer a member)."""
    import base64
    import json as _json
    body, _, sig = (token or "").partition(".")
    if not body or not crypto.same(sig, crypto.hmac_sha256_hex(_sso_secret(), body.encode())):
        return None
    try:
        d = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if int(d.get("e") or 0) < int((now or datetime.now(timezone.utc)).timestamp()):
        return None
    if conn.execute(select(IE.c.id).where(IE.c.type == "sso.used", IE.c.data["j"].astext == str(d.get("j")))).first():
        return None
    biz = conn.execute(select(B).where(B.c.id == UUID(d["b"]))).mappings().first()
    if not biz or biz["suspended_at"] or not conn.execute(select(MB.c.user_id).where(
            MB.c.business_id == biz["id"], MB.c.user_id == UUID(d["u"]))).first():
        return None
    audit(conn, "sso.used", "sso", {"j": str(d["j"]), "business_id": d["b"], "user_id": d["u"]})
    return {"user_id": d["u"], "business_id": d["b"], "next": d.get("n") or "/app"}


def contact_upsert(conn: Connection, actor: str, business_id: UUID, data: dict) -> tuple[dict, bool]:
    """A lead from the partner's app: found by phone (E.164) or email first, so sending it again never duplicates.
    New tags are added, nothing is taken away; consent is only ever added (a «no» is kept by the business's own STOP).
    -> (the contact, created?)."""
    from sqlalchemy import or_
    from .. import packs, phones
    from . import contacts as C
    biz = _get(conn, business_id)
    phone, email = (data.get("phone") or "").strip(), (data.get("email") or "").strip().lower()
    e164 = None
    if phone:
        try:
            e164 = phones.e164(phone, packs.country(biz["country"]))
        except phones.BadPhone:
            raise Invalid("phone: not a phone number", "phone") from None
    if not (e164 or email):
        raise Invalid("give a phone or an email", "phone")
    CT = db.contacts
    cond = ([CT.c.phone == e164] if e164 else []) + ([func.lower(CT.c.email) == email] if email else [])
    cur = conn.execute(select(CT).where(CT.c.business_id == biz["id"], CT.c.deleted_at.is_(None), or_(*cond))
                       .order_by(CT.c.created_at).limit(1)).mappings().first()
    tags = sorted({str(t).strip().lower()[:50] for t in (data.get("tags") or []) if str(t).strip()})
    consent = bool(data.get("marketing_consent"))
    if not cur:
        vals = {"name": (data.get("name") or "").strip()[:200] or (e164 or email), "source": (data.get("source") or "partner")[:40],
                "tags": tags, **({"notes": data["notes"].strip()[:4000]} if (data.get("notes") or "").strip() else {}),
                **({"phone": e164} if e164 else {}), **({"email": email} if email else {})}   # v0.46.1: no notes = left out
        if consent:
            vals.update(marketing_consent=True, marketing_consent_source=(data.get("marketing_consent_source") or "partner app")[:200])
        return C.create(conn, biz, actor, vals), True
    patch: dict = {}
    if tags and set(tags) - set(cur["tags"] or []):
        patch["tags"] = sorted(set(cur["tags"] or []) | set(tags))
    if email and not cur["email"]:
        patch["email"] = email
    if e164 and not cur["phone"]:
        patch["phone"] = e164
    if (data.get("name") or "").strip() and (cur["name"] or "") in ("", cur["phone"] or "", cur["email"] or ""):
        patch["name"] = data["name"].strip()[:200]
    if data.get("notes") and data["notes"].strip() not in (cur["notes"] or ""):
        patch["notes"] = ((cur["notes"] or "") + "\n" + data["notes"].strip()).strip()[:4000]
    if consent and not cur["marketing_consent"] and not cur["unsubscribed_at"]:
        patch.update(marketing_consent=True, marketing_consent_source=(data.get("marketing_consent_source") or "partner app")[:200])
    return (C.patch(conn, biz, actor, cur["id"], patch) if patch else dict(cur)), False


def claim_status(conn: Connection, code: str) -> dict:
    """pb02: a partner's sample site asks «was my claim link used, and which business did it become?» — so the site's
    leads go to that business. -> {"claimed": bool, "business_id": id | None}; unknown links look unclaimed."""
    CK = db.claim_kits
    code = (code or "").strip()
    row = conn.execute(select(CK.c.claimed_at, CK.c.business_id).where(CK.c.code == code)).first() if 0 < len(code) <= 32 else None
    if not row or not row.claimed_at or not row.business_id:
        return {"claimed": False, "business_id": None}
    biz = conn.execute(select(B.c.id, B.c.suspended_at).where(B.c.id == row.business_id, B.c.sandbox.is_(False))).first()
    return {"claimed": bool(biz), "business_id": biz.id if biz else None, "suspended": bool(biz and biz.suspended_at)}
