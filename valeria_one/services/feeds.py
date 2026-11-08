"""Data feeds (gap04, v0.42.0): a private live CSV link — customers or deals — that Google Sheets reads with
=IMPORTDATA("https://…/feeds/<token>.csv") and keeps in sync by itself (Sheets refreshes it about every hour).

The token is the key: random, shown to the owner once, only its SHA-256 is kept (like the API keys and the incoming hooks).
Anyone with the link can read that list, so the screen says so plainly; the owner switches it off with one tap and the link
stops at once. Every read is written in the business's log (contacts.exported / deals.exported, by «feed:<prefix>»).
No Google account or key on our side: $0, the owner's own Sheets.
"""
from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import crypto, db
from . import Invalid, NotFound, log

F = db.data_feeds
KINDS = ("contacts", "deals")
MAX_PER_BUSINESS = 5


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    rows = conn.execute(select(F).where(F.c.business_id == business_id, F.c.active).order_by(F.c.created_at)).mappings()
    return [{k: v for k, v in dict(r).items() if k != "token_hash"} for r in rows]


def create(conn: Connection, biz: dict, actor: str, kind: str) -> tuple[dict, str]:
    """-> (the feed, its token — shown once)."""
    if kind not in KINDS:
        raise Invalid("pick customers or deals", "kind")
    if len(list_(conn, biz["id"])) >= MAX_PER_BUSINESS:
        raise Invalid(f"at most {MAX_PER_BUSINESS} links; switch one off first", "kind")
    token = "vfd_" + secrets.token_urlsafe(24)
    row = conn.execute(insert(F).values(business_id=biz["id"], kind=kind, prefix=token[:12], token_hash=crypto.sha256(token),
                                        created_by=actor).returning(F)).mappings().one()
    log(conn, biz["id"], "feed.created", actor, row["id"], {"kind": kind})
    return {k: v for k, v in dict(row).items() if k != "token_hash"}, token


def delete(conn: Connection, business_id: UUID, actor: str, feed_id: UUID) -> None:
    n = conn.execute(update(F).where(F.c.business_id == business_id, F.c.id == feed_id, F.c.active).values(active=False)).rowcount
    if not n:
        raise NotFound("link")
    log(conn, business_id, "feed.deleted", actor, feed_id, {})


def read(token: str) -> str:
    """The CSV behind a link. NotFound for an unknown or switched-off link, or a paused business."""
    from . import csvio
    if not token or not token.startswith("vfd_") or len(token) > 80:
        raise NotFound("link")
    with db.tx() as conn:
        f = conn.execute(select(F).where(F.c.token_hash == crypto.sha256(token), F.c.active)).mappings().first()
        if not f:
            raise NotFound("link")
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == f["business_id"])).mappings().one())
        if biz["suspended_at"]:
            raise NotFound("link")
        who = f"feed:{f['prefix']}"
        body = csvio.contacts_csv(conn, biz, who) if f["kind"] == "contacts" else csvio.deals_csv(conn, biz, who)
        conn.execute(update(F).where(F.c.id == f["id"]).values(uses=F.c.uses + 1, last_used_at=func.now()))
    return body
