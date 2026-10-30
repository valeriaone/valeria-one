"""Email designs (ars02, v0.21): the email builder of the campaigns, kept as designs the owner reuses — a newsletter look,
an offer, a welcome — in any campaign or drip step.

A design is a subject and the same blocks a campaign email has (services/campaigns.py: title, text, picture, button,
line, space, quote, the business's own links), drawn by the same renderer: one 600 px column of tables with inline
styles, no scripts and no web fonts (what mail apps show the same), a plain-text part, and the unsubscribe link always
in both. Using a design copies its subject and blocks into the campaign or the drip step (the design can change later
without changing what was already scheduled). Sending stays where it was: the business's OWN email account, only to
customers who said yes to offers, never by itself — nothing in here sends anything.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, NotFound, log
from . import campaigns as CG

ED = db.email_designs
MAX_DESIGNS = 50


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(ED).where(ED.c.business_id == business_id).order_by(ED.c.updated_at.desc()))
            .mappings()]


def get(conn: Connection, business_id: UUID, design_id: UUID) -> dict:
    r = conn.execute(select(ED).where(ED.c.business_id == business_id, ED.c.id == design_id)).mappings().first()
    if not r:
        raise NotFound("design")
    return dict(r)


def save(conn: Connection, biz: dict, actor: str, *, name: str, subject: str = "", blocks=None,
         design_id: UUID | None = None) -> dict:
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise Invalid("give the design a name", "name")
    vals = {"name": name, "subject": " ".join(str(subject or "").split())[:200], "blocks": CG.clean_blocks(blocks)}
    if not vals["blocks"]:
        raise Invalid("add at least one block", "blocks")
    if design_id:
        get(conn, biz["id"], design_id)
        conn.execute(update(ED).where(ED.c.business_id == biz["id"], ED.c.id == design_id).values(**vals, updated_at=func.now()))
        return get(conn, biz["id"], design_id)
    if conn.execute(select(func.count()).select_from(ED).where(ED.c.business_id == biz["id"])).scalar() >= MAX_DESIGNS:
        raise Invalid(f"at most {MAX_DESIGNS} designs", "name")
    row = conn.execute(insert(ED).values(business_id=biz["id"], created_by=actor, **vals).returning(ED)).mappings().one()
    log(conn, biz["id"], "email_design.created", actor, row["id"])
    return dict(row)


def duplicate(conn: Connection, biz: dict, actor: str, design_id: UUID) -> dict:
    d = get(conn, biz["id"], design_id)
    return save(conn, biz, actor, name=(d["name"] + " (2)")[:120], subject=d["subject"], blocks=d["blocks"])


def delete(conn: Connection, biz: dict, actor: str, design_id: UUID) -> None:
    """Campaigns and drips that used it keep their own copy."""
    get(conn, biz["id"], design_id)
    conn.execute(ED.delete().where(ED.c.business_id == biz["id"], ED.c.id == design_id))
    log(conn, biz["id"], "email_design.deleted", actor, design_id)


def render(biz: dict, design: dict, contact: dict | None = None) -> tuple[str, str]:
    """(html, plain text) exactly as a customer gets it (no tracking: a preview)."""
    content = {"channel": "email", "subject": design.get("subject") or "", "blocks": design.get("blocks") or []}
    content["links"] = CG.links_of(content)
    return CG.render_email(biz, content, contact, None, False)


def use_in_campaign(conn: Connection, biz: dict, actor: str, design_id: UUID, campaign_id: UUID) -> dict:
    """The design's subject (when the campaign has none yet) and blocks into an email campaign that is still a draft."""
    d = get(conn, biz["id"], design_id)
    cp = CG.get(conn, biz["id"], campaign_id)
    if cp["channel"] != "email":
        raise Invalid("designs are for email campaigns", "channel")
    data = {"blocks": d["blocks"]}
    if not cp.get("subject") and d["subject"]:
        data["subject"] = d["subject"]
    out = CG.save(conn, biz, actor, campaign_id, data)
    log(conn, biz["id"], "email_design.used", actor, design_id, {"campaign_id": str(campaign_id)})
    return out


def from_campaign(conn: Connection, biz: dict, actor: str, campaign_id: UUID, name: str) -> dict:
    """"Save as a design": a campaign's email kept for next time."""
    cp = CG.get(conn, biz["id"], campaign_id)
    blocks = cp.get("blocks") or ([{"t": "text", "text": cp["message"]}] if cp.get("message") else [])
    return save(conn, biz, actor, name=name or cp["name"], subject=cp.get("subject") or "", blocks=blocks)


def step_from(conn: Connection, business_id: UUID, step: dict) -> dict:
    """A drip step that says "use design X": its blocks (and its subject, if the step has none) come from the design."""
    did = str(step.get("design") or "").strip()
    if not did:
        return step
    try:
        d = get(conn, business_id, UUID(did))
    except (ValueError, NotFound):
        raise Invalid("pick one of your email designs", "steps") from None
    return {**step, "channel": "email", "blocks": d["blocks"], "subject": step.get("subject") or d["subject"],
            "text": step.get("text") or "", "design_id": did}
