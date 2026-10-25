"""Deals (orders, jobs, packages, treatment plans… — the industry pack names them) and appointments."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import blocks, db, packs
from . import Invalid, NotFound, contacts, log

D, AP = db.deals, db.appointments


def _stage(biz: dict, key: str | None) -> str:
    ip = packs.industry(biz["industry"])
    if key is None:
        return ip.first_stage
    if not ip.stage(key):
        raise Invalid(f"unknown stage {key!r} for {ip.code}: {', '.join(s.key for s in ip.stages)}", "stage")
    return key


def _amount(v) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise Invalid("amount_minor is a whole number in the smallest currency unit (cents)", "amount_minor")
    return v


def create(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    title = (data.get("title") or "").strip()
    if not title:
        raise Invalid("title is required", "title")
    if data.get("contact_id"):
        contacts.get(conn, biz["id"], data["contact_id"])
    stage = _stage(biz, data.get("stage"))
    outcome = packs.industry(biz["industry"]).stage(stage).outcome
    row = conn.execute(insert(D).values(
        business_id=biz["id"], contact_id=data.get("contact_id"), title=title[:200], stage=stage,
        amount_minor=_amount(data.get("amount_minor")), currency=biz["currency"],
        expected_close=data.get("expected_close"), notes=data.get("notes") or "",
        closed_at=func.now() if outcome != "open" else None).returning(D)).mappings().one()
    log(conn, biz["id"], "deal.created", actor, row["id"], {"stage": stage})
    return dict(row)


def get(conn: Connection, business_id: UUID, deal_id: UUID) -> dict:
    row = conn.execute(select(D).where(D.c.business_id == business_id, D.c.id == deal_id)).mappings().first()
    if not row:
        raise NotFound("deal")
    return dict(row)


def search(conn: Connection, business_id: UUID, *, stage: str | None = None, contact_id: UUID | None = None,
           limit: int = 50) -> list[dict]:
    s = select(D).where(D.c.business_id == business_id)
    if stage:
        s = s.where(D.c.stage == stage)
    if contact_id:
        s = s.where(D.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(s.order_by(D.c.updated_at.desc()).limit(max(1, min(limit, 200)))).mappings()]


def patch(conn: Connection, biz: dict, actor: str, deal_id: UUID, data: dict) -> dict:
    cur = get(conn, biz["id"], deal_id)
    vals = {}
    if "title" in data:
        if not (data["title"] or "").strip():
            raise Invalid("title is required", "title")
        vals["title"] = data["title"].strip()[:200]
    if "amount_minor" in data:
        vals["amount_minor"] = _amount(data["amount_minor"])
        vals["currency"] = biz["currency"]             # a new amount is in the business's currency today
    for k in ("expected_close", "notes"):
        if k in data:
            vals[k] = data[k] if k != "notes" else (data[k] or "")
    if "contact_id" in data:
        if data["contact_id"]:
            contacts.get(conn, biz["id"], data["contact_id"])
        vals["contact_id"] = data["contact_id"]
    if data.get("stage") and data["stage"] != cur["stage"]:
        vals["stage"] = _stage(biz, data["stage"])
        outcome = packs.industry(biz["industry"]).stage(vals["stage"]).outcome
        vals["closed_at"] = func.now() if outcome != "open" else None
    if vals:
        conn.execute(update(D).where(D.c.business_id == biz["id"], D.c.id == deal_id).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "deal.stage_changed" if "stage" in vals else "deal.updated", actor, deal_id,
            {"from": cur["stage"], "to": vals["stage"]} if "stage" in vals else {"fields": sorted(vals)})
    return get(conn, biz["id"], deal_id)


def pipeline(conn: Connection, biz: dict, lang: str | None = None) -> list[dict]:
    """Each stage of this business's industry, with how many deals and how much money sit in it.
    Labels in `lang` (the reader's language), else the business's."""
    rows = {r["stage"]: r for r in conn.execute(
        select(D.c.stage, func.count().label("count"), func.coalesce(func.sum(D.c.amount_minor).filter(D.c.currency == biz["currency"]), 0).label("amount_minor"))
        .where(D.c.business_id == biz["id"]).group_by(D.c.stage)).mappings()}
    out = []
    for s in packs.industry(biz["industry"]).stages:
        r = rows.get(s.key)
        out.append({"key": s.key, "label": blocks.word(biz, f"stage:{s.key}", lang or biz["language"]), "outcome": s.outcome,
                    "count": r["count"] if r else 0, "amount_minor": int(r["amount_minor"]) if r else 0})
    return out


# ---------------------------------------------------------------- appointments
def book(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    title = (data.get("title") or "").strip()
    st, en = data.get("starts_at"), data.get("ends_at")
    if not title:
        raise Invalid("title is required", "title")
    if not isinstance(st, datetime) or not isinstance(en, datetime) or st.tzinfo is None or en.tzinfo is None:
        raise Invalid("starts_at and ends_at need a date, time and time zone", "starts_at")
    if en <= st:
        raise Invalid("ends_at must be after starts_at", "ends_at")
    if data.get("contact_id"):
        contacts.get(conn, biz["id"], data["contact_id"])
    if data.get("deal_id"):
        get(conn, biz["id"], data["deal_id"])
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(biz["id"])))))   # one booking at a time per business
    clash = conn.execute(select(AP.c.id).where(AP.c.business_id == biz["id"], AP.c.status.in_(("booked", "confirmed")),
                                               AP.c.starts_at < en, AP.c.ends_at > st)).first()
    if clash and not data.get("allow_overlap"):
        raise Invalid("that time overlaps another appointment (send allow_overlap=true to book anyway)", "starts_at")
    row = conn.execute(insert(AP).values(business_id=biz["id"], contact_id=data.get("contact_id"), deal_id=data.get("deal_id"),
                                         title=title[:200], starts_at=st, ends_at=en, location=data.get("location"),
                                         notes=data.get("notes") or "").returning(AP)).mappings().one()
    log(conn, biz["id"], "appointment.booked", actor, row["id"])
    return dict(row)


def appointments(conn: Connection, business_id: UUID, *, start: datetime | None = None, end: datetime | None = None,
                 limit: int = 100) -> list[dict]:
    s = select(AP).where(AP.c.business_id == business_id)
    if start:
        s = s.where(AP.c.ends_at > start)
    if end:
        s = s.where(AP.c.starts_at < end)
    return [dict(r) for r in conn.execute(s.order_by(AP.c.starts_at).limit(max(1, min(limit, 500)))).mappings()]


def set_appointment_status(conn: Connection, business_id: UUID, actor: str, appt_id: UUID, status: str) -> dict:
    if status not in ("booked", "confirmed", "done", "no_show", "cancelled"):
        raise Invalid("status: booked, confirmed, done, no_show or cancelled", "status")
    row = conn.execute(update(AP).where(AP.c.business_id == business_id, AP.c.id == appt_id).values(status=status, updated_at=func.now())
                       .returning(AP)).mappings().first()
    if not row:
        raise NotFound("appointment")
    log(conn, business_id, f"appointment.{status}", actor, appt_id)
    return dict(row)
