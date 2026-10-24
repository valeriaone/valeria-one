"""Review requests: "would you leave us a review?" with the business's own review link (Google or any).

The same polite ask goes to everyone — no asking only the happy ones (FTC rules on review gating). A person is
asked at most once every 90 days. Each ask carries a counted link (/r/<token>) that opens the review page,
so the owner sees who clicked. Ask in one tap from a customer's page, or automatically: any follow-up message
with {review_link} in it (e.g. "2 hours after each appointment") becomes a review request.
Sent as marketing, so each country's consent rules apply.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db
from . import Invalid, NotFound, log

RR = db.review_requests
EVERY_DAYS = 90


def set_url(conn: Connection, business_id: UUID, actor: str, url: str) -> None:
    u = (url or "").strip()
    if u and (not u.startswith(("https://", "http://")) or len(u) > 500 or " " in u):
        raise Invalid("paste the full link to your review page, starting with https://", "review_url")
    conn.execute(update(db.businesses).where(db.businesses.c.id == business_id).values(review_url=u or None))
    log(conn, business_id, "reviews.link_saved", actor, business_id, {"set": bool(u)})


def recently_asked(conn: Connection, business_id: UUID, contact_id: UUID, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return conn.execute(select(RR.c.id).where(RR.c.business_id == business_id, RR.c.contact_id == contact_id,
                                              RR.c.created_at > now - timedelta(days=EVERY_DAYS))).first() is not None


def new_link(conn: Connection, biz: dict, contact_id: UUID, how: str) -> tuple[UUID, str]:
    token = secrets.token_urlsafe(12)
    rid = conn.execute(insert(RR).values(business_id=biz["id"], contact_id=contact_id, token=token, how=how)
                       .returning(RR.c.id)).scalar()
    return rid, f"{config.PUBLIC_URL()}/r/{token}"


def forget(conn: Connection, request_id: UUID) -> None:
    """The message did not go out: the ask doesn't count."""
    conn.execute(delete(RR).where(RR.c.id == request_id))


def ask(biz: dict, actor: str, contact_id: UUID, channel_account_id: UUID | None = None) -> dict:
    """One tap on a customer's page. -> the message sent."""
    from ..web.i18n import t, ui_lang
    from . import Blocked
    from . import inbox as X
    if not biz.get("review_url"):
        raise Invalid("add your review link first (Settings)", "review_url")
    with db.tx() as conn:
        from . import contacts as C
        contact = C.get(conn, biz["id"], contact_id)
        if recently_asked(conn, biz["id"], contact_id):
            raise Invalid(f"already asked in the last {EVERY_DAYS} days", "review")
        if not channel_account_id:                              # the channel they use with us, like follow-ups
            from .followups import _pick_account
            channel_account_id = _pick_account(conn, biz, contact_id)
            if not channel_account_id:
                raise Invalid("no channel to reach this customer", "channel")
        rid, url = new_link(conn, biz, contact_id, "manual")
    first = (contact.get("name") or "").split(" ")[0]
    body = t("review_ask", ui_lang(biz["language"])).format(name=first, business=biz["name"], link=url).replace(" ,", ",")
    try:
        msg = X.send(biz, actor, contact_id=contact_id, body=body, kind="marketing", channel_account_id=channel_account_id)
    except Blocked:
        with db.tx() as conn:
            forget(conn, rid)
        raise
    with db.tx() as conn:
        if msg.get("status") in ("failed", "blocked"):
            forget(conn, rid)
        else:
            log(conn, biz["id"], "review.asked", actor, contact_id, {"how": "manual"})
    return msg


def click(token: str) -> str:
    """The customer tapped the link -> the business's review page (counted once)."""
    with db.tx() as conn:
        r = conn.execute(select(RR, db.businesses.c.review_url).join(db.businesses, db.businesses.c.id == RR.c.business_id)
                         .where(RR.c.token == (token or "")[:32])).mappings().first()
        if not r or not r["review_url"]:
            raise NotFound("review link")
        if not r["clicked_at"]:
            conn.execute(update(RR).where(RR.c.id == r["id"]).values(clicked_at=func.now()))
            log(conn, r["business_id"], "review.clicked", "customer", r["contact_id"])
    return r["review_url"]


def stats(conn: Connection, business_id: UUID) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=EVERY_DAYS)
    asked = conn.execute(select(func.count()).select_from(RR).where(RR.c.business_id == business_id, RR.c.created_at > since)).scalar()
    clicked = conn.execute(select(func.count()).select_from(RR).where(RR.c.business_id == business_id, RR.c.created_at > since,
                                                                     RR.c.clicked_at.isnot(None))).scalar()
    return {"asked": asked, "clicked": clicked, "days": EVERY_DAYS}


def last_asked(conn: Connection, business_id: UUID, contact_id: UUID):
    return conn.execute(select(RR.c.created_at, RR.c.clicked_at).where(RR.c.business_id == business_id, RR.c.contact_id == contact_id)
                        .order_by(RR.c.created_at.desc()).limit(1)).mappings().first()
