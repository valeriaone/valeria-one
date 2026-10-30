"""Saved replies (v0.18): the owner's ready answers ("Our hours", "Prices", "How to find us") — one tap in the inbox puts
one in the reply box, to edit and send as usual (so every send rule still applies). Ready setups bring a few."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection

from .. import db
from . import Invalid, log

SR = db.saved_replies
MAX = 50


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(SR).where(SR.c.business_id == business_id)
                                          .order_by(SR.c.position, SR.c.created_at)).mappings()]


def add(conn: Connection, biz: dict, actor: str, *, title: str, body: str) -> dict:
    title, body = " ".join((title or "").split())[:80], (body or "").strip()[:2000]
    if not title or not body:
        raise Invalid("a name and the words", "title")
    have = list_(conn, biz["id"])
    if len(have) >= MAX:
        raise Invalid(f"at most {MAX} saved replies", "title")
    if any(r["title"].lower() == title.lower() for r in have):
        raise Invalid("there is already one with that name", "title")
    pos = conn.execute(select(func.coalesce(func.max(SR.c.position), 0) + 1).where(SR.c.business_id == biz["id"])).scalar()
    row = conn.execute(insert(SR).values(business_id=biz["id"], title=title, body=body, position=pos).returning(SR)).mappings().one()
    log(conn, biz["id"], "reply.saved", actor, row["id"])
    return dict(row)


def remove(conn: Connection, business_id: UUID, reply_id: UUID) -> int:
    return conn.execute(SR.delete().where(SR.c.business_id == business_id, SR.c.id == reply_id)).rowcount
