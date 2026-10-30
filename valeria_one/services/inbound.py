"""Apps that send customers IN (ars02, v0.21): an address per connection — POST /hooks/in/<token> — that Zapier, Make, a
website builder, a lead form or any app can call to create a customer (and, if the owner chose so, a deal) in this
business. The other way (this business's events OUT to other apps) is services/webhooks.py; the API keys are
services/businesses.py (new_key) — together: "Connect other apps" (/app/connect-apps), documented in docs/WEBHOOKS.md.

The token is the key: random, shown to the owner once, only its SHA-256 is kept (like the API keys). What comes in is
a lead like one from the website: name and a phone or an email are needed; the customer is found by phone or email
before a new one is made; the note lands in the inbox; the owner's tag goes on; nothing is sent to anyone. Marketing
consent is never set from outside: the owner's own consent rules decide who gets offers.
"""
from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import crypto, db, packs
from . import Invalid, NotFound, log

IH = db.inbound_hooks
MAX_PER_BUSINESS = 10
MAX_BODY = 32 * 1024


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    return [_public(r) for r in conn.execute(select(IH).where(IH.c.business_id == business_id, IH.c.active)
                                             .order_by(IH.c.created_at)).mappings()]


def _public(r) -> dict:
    d = dict(r)
    d.pop("token_hash", None)
    return d


def create(conn: Connection, biz: dict, actor: str, *, name: str, makes: str = "contact", stage: str | None = None,
           tag: str | None = None) -> tuple[dict, str]:
    """-> (the connection, its token — shown once)."""
    name = " ".join(str(name or "").split())[:100]
    if not name:
        raise Invalid("name it after the app that sends (Zapier, my website…)", "name")
    if makes not in ("contact", "deal"):
        raise Invalid("a customer, or a customer and a deal", "makes")
    if makes == "deal" and not packs.industry(biz["industry"]).stage(str(stage or "")):
        raise Invalid("pick a stage for the deal", "stage")
    n = conn.execute(select(func.count()).select_from(IH).where(IH.c.business_id == biz["id"], IH.c.active)).scalar()
    if n >= MAX_PER_BUSINESS:
        raise Invalid(f"at most {MAX_PER_BUSINESS} connections", "name")
    token = "vin_" + secrets.token_urlsafe(24)
    row = conn.execute(insert(IH).values(business_id=biz["id"], name=name, prefix=token[:10], token_hash=crypto.sha256(token),
                                         makes=makes, stage=stage if makes == "deal" else None,
                                         tag=(str(tag or "").strip().lower()[:60] or None), created_by=actor).returning(IH)).mappings().one()
    log(conn, biz["id"], "inbound.created", actor, row["id"], {"name": name})
    return _public(row), token


def delete(conn: Connection, business_id: UUID, actor: str, hook_id: UUID) -> None:
    if not conn.execute(update(IH).where(IH.c.business_id == business_id, IH.c.id == hook_id, IH.c.active)
                        .values(active=False)).rowcount:
        raise NotFound("connection")
    log(conn, business_id, "inbound.deleted", actor, hook_id)


def receive(token: str, data: dict) -> dict:
    """A call to /hooks/in/<token>: -> {"contact_id", "deal_id"?}. Invalid when what came is not enough; NotFound for a
    token that is unknown, switched off, or a business that is paused."""
    from . import contacts as CT
    from . import deals as D
    from . import site as ST
    if not token or not token.startswith("vin_") or len(token) > 80:
        raise NotFound("connection")
    with db.tx() as conn:
        h = conn.execute(select(IH).where(IH.c.token_hash == crypto.sha256(token), IH.c.active)).mappings().first()
        if not h:
            raise NotFound("connection")
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == h["business_id"])).mappings().one())
        if biz["suspended_at"]:
            raise NotFound("connection")
        g = lambda *ks: next((str(data.get(k)).strip() for k in ks if data.get(k) not in (None, "")), "")   # noqa: E731
        name = g("name", "full_name", "first_name") or g("email", "phone")
        note = g("message", "note", "notes", "comment")[:2000]
        source = f"app:{h['name']}"[:60]
        res = ST.contact_form(conn, biz, name=name, phone=g("phone", "phone_number", "mobile"), email=g("email"),
                              message=f"📥 {h['name']}" + (f"\n{note}" if note else ""), source=source, event="lead.app")
        contact = res["contact"]
        tags = [x.strip().lower()[:60] for x in str(data.get("tags") or "").split(",") if x.strip()][:5]
        if h["tag"]:
            tags.append(h["tag"])
        new = [x for x in tags if x not in (contact["tags"] or [])]
        if new:
            contact = CT.patch(conn, biz, source, contact["id"], {"tags": [*(contact["tags"] or []), *new]})
        out = {"contact_id": str(contact["id"])}
        if h["makes"] == "deal":
            amount = data.get("amount_minor")
            deal = D.create(conn, biz, source, {"title": (g("deal", "title") or f"{h['name']} · {contact['name']}")[:200],
                                                "contact_id": contact["id"], "stage": h["stage"],
                                                **({"amount_minor": amount} if isinstance(amount, int) and not isinstance(amount, bool)
                                                   else {})})
            out["deal_id"] = str(deal["id"])
        conn.execute(update(IH).where(IH.c.id == h["id"]).values(uses=IH.c.uses + 1, last_used_at=func.now()))
    return out
