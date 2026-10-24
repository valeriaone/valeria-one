"""Comment to private message: someone writes "precio?" under a Facebook or Instagram post and Valeria One
answers them privately, once, with the business's own words.

Meta allows ONE private reply per comment, within 7 days of it. A business sets rules (keywords -> reply);
every comment is kept so the owner can see who asked, even without a rule.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, crypto, db
from ..channels.meta import graph
from . import Invalid, NotFound, log

R, SC = db.comment_rules, db.social_comments
PLATFORMS = ("messenger", "instagram", "sandbox")
REPLY_WINDOW = timedelta(days=7)


def fold(s: str) -> str:
    """'¿PRECIO?' -> 'precio?' : lower-case, no accents, so 'precio' matches 'Precío' too."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# ---------------------------------------------------------------- rules
def create_rule(conn: Connection, biz: dict, actor: str, *, keywords: list[str] | str, reply: str,
                channels: list[str] | None = None) -> dict:
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    kws = sorted({fold(k).strip() for k in keywords if fold(k).strip()})
    reply = (reply or "").strip()
    if not reply:
        raise Invalid("write the private message to send", "reply")
    if len(reply) > 1000:
        raise Invalid("keep the message under 1,000 characters", "reply")
    chans = [c for c in (channels or ["messenger", "instagram"]) if c in PLATFORMS]
    if not chans:
        raise Invalid("pick Facebook, Instagram or both", "channels")
    row = conn.execute(insert(R).values(business_id=biz["id"], keywords=kws, reply=reply, channels=chans)
                       .returning(R)).mappings().one()
    log(conn, biz["id"], "comment_rule.created", actor, row["id"], {"keywords": kws})
    return dict(row)


def rules(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(R).where(R.c.business_id == business_id).order_by(R.c.created_at)).mappings()]


def delete_rule(conn: Connection, business_id: UUID, actor: str, rule_id: UUID) -> None:
    if not conn.execute(delete(R).where(R.c.business_id == business_id, R.c.id == rule_id)).rowcount:
        raise NotFound("rule")
    log(conn, business_id, "comment_rule.deleted", actor, rule_id)


def match(rule_rows: list[dict], text: str, platform: str) -> dict | None:
    t = fold(text)
    for r in rule_rows:
        if not r["active"] or platform not in r["channels"]:
            continue
        if not r["keywords"] or any(k in t for k in r["keywords"]):
            return r
    return None


def comments(conn: Connection, business_id: UUID, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(select(SC).where(SC.c.business_id == business_id)
                                          .order_by(SC.c.created_at.desc()).limit(max(1, min(limit, 200)))).mappings()]


def _fill(reply: str, name: str | None) -> str:
    first = (name or "").split(" ")[0]
    return reply.replace("{name}", first).replace("  ", " ").strip()


# ---------------------------------------------------------------- a comment arrives
def ingest(acc, platform: str, c: dict, http: httpx.Client | None = None, now: datetime | None = None) -> str:
    """Store one comment and, when a rule matches, answer it privately. Returns what happened:
    duplicate | own | stored | replied | too_old | failed."""
    now = now or datetime.now(timezone.utc)
    cfg = crypto.open_(acc["config"]) if platform != "sandbox" else {}
    own = {cfg.get("page_id"), cfg.get("ig_user_id")} - {None}
    if c.get("author_id") and c["author_id"] in own:
        return "own"                                           # the business commenting on its own post
    with db.tx() as conn:
        rid = conn.execute(pg_insert(SC).values(
            business_id=acc["business_id"], channel_account_id=acc["id"], platform=platform, comment_id=c["comment_id"],
            post_id=c.get("post_id"), author_id=c.get("author_id"), author_name=(c.get("author_name") or "")[:200],
            text=c.get("text") or "", commented_at=c.get("at"))
            .on_conflict_do_nothing(constraint="uq_social_comment").returning(SC.c.id)).scalar()
        if not rid:
            return "duplicate"
        rule = match(rules(conn, acc["business_id"]), c.get("text") or "", platform) \
            if blocks.on(blocks.state(conn, acc["business_id"]), "comments") else None   # off or paused: kept, not answered
        log(conn, acc["business_id"], "comment.received", f"webhook:{platform}", rid, {"matched": bool(rule)})
        if not rule:
            return "stored"
        conn.execute(update(SC).where(SC.c.id == rid).values(rule_id=rule["id"]))
    at = c.get("at") or now
    if now - at > REPLY_WINDOW:
        _done(rid, acc["business_id"], error="older than 7 days: Meta allows no private reply")
        return "too_old"
    text = _fill(rule["reply"], c.get("author_name"))
    if platform == "sandbox":
        _sandbox_dm(acc, c, text)
        _done(rid, acc["business_id"])
        return "replied"
    try:
        r = (http or httpx.Client(timeout=20)).post(f"{graph()}/me/messages", params={"access_token": cfg["page_token"]},
                                                    json={"recipient": {"comment_id": c["comment_id"]}, "message": {"text": text}})
        if r.status_code >= 300:
            msg = (r.json().get("error") or {}).get("message") if r.headers.get("content-type", "").startswith("application/json") else None
            _done(rid, acc["business_id"], error=msg or f"Meta answered {r.status_code}")
            return "failed"
    except httpx.HTTPError as e:
        _done(rid, acc["business_id"], error=type(e).__name__)
        return "failed"
    _done(rid, acc["business_id"])
    return "replied"


def _done(rid, business_id, error: str | None = None) -> None:
    with db.tx() as conn:
        conn.execute(update(SC).where(SC.c.id == rid).values(replied_at=None if error else datetime.now(timezone.utc),
                                                             reply_error=error))
        log(conn, business_id, "comment.reply_failed" if error else "comment.replied", "system", rid,
            {"error": error} if error else {})


def _sandbox_dm(acc, c: dict, text: str) -> None:
    """In the sandbox the private message lands in the inbox, so a visitor sees the whole loop."""
    from . import contacts as C
    from . import inbox as X
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == acc["business_id"])).mappings().one())
        contact, _ = C.find_or_create_by_identity(conn, biz, "sandbox", f"comment-{c.get('author_id')}",
                                                  c.get("author_name"), "comment")
        conv = X._conversation(conn, biz["id"], acc["id"], contact["id"])
        conn.execute(insert(db.messages).values(business_id=biz["id"], conversation_id=conv["id"], direction="out",
                                                body=text, status="delivered"))
        conn.execute(update(db.conversations).where(db.conversations.c.id == conv["id"])
                     .values(last_message_at=datetime.now(timezone.utc)))


# ---------------------------------------------------------------- reading Meta's webhook shapes
def from_page_change(value: dict) -> dict | None:
    """Facebook Page 'feed' change -> our comment, or None for anything that isn't a new comment."""
    if value.get("item") != "comment" or value.get("verb") != "add" or not value.get("comment_id"):
        return None
    who = value.get("from") or {}
    ts = value.get("created_time")
    at = datetime.fromtimestamp(int(ts), tz=timezone.utc) if str(ts or "").isdigit() else None
    return {"comment_id": value["comment_id"], "post_id": value.get("post_id"), "author_id": who.get("id"),
            "author_name": who.get("name"), "text": value.get("message") or "", "at": at}


def from_instagram_change(value: dict) -> dict | None:
    """Instagram 'comments' change -> our comment."""
    if not value.get("id"):
        return None
    who = value.get("from") or {}
    return {"comment_id": value["id"], "post_id": (value.get("media") or {}).get("id"), "author_id": who.get("id"),
            "author_name": who.get("username"), "text": value.get("text") or "", "at": None}
