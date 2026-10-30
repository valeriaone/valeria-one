"""The inbox: connected channels, conversations, sending and receiving — one model for every channel.

Rules applied to every send, whatever the channel (from the country pack and the channel adapter):
  * marketing needs recorded consent, and never goes to someone who unsubscribed;
  * marketing respects the country's quiet hours (e.g. US 21:00–08:00 local);
  * SMS to someone who texted STOP: nothing at all;
  * channels with a reply window (WhatsApp, Messenger, Instagram: 24 h) need an approved template outside it.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .. import channels, config, crypto, db, packs, phones
from ..channels import HookRequest, Inbound, Outbound, StatusUpdate
from . import Blocked, Invalid, NotFound, contacts, log

A, CV, M = db.channel_accounts, db.conversations, db.messages
_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}


# ---------------------------------------------------------------- channel accounts
def connect(conn: Connection, biz: dict, actor: str, *, channel: str, name: str, address: str | None = None,
            cfg: dict | None = None) -> dict:
    if channel not in channels.available():
        raise Invalid(f"unknown channel {channel!r}; available: {', '.join(channels.available())}", "channel")
    if (biz["sandbox"] or config.SANDBOX()) and channel != "sandbox":
        raise Invalid("sandbox businesses use the 'sandbox' channel only — nothing real is ever sent from the sandbox", "channel")
    ad = channels.adapter(channel)
    if not ad.manual:
        raise Invalid(f"connect {channel} from its own Connect screen (/app/connect)", "channel")
    cfg = ad.prepare({k: v for k, v in (cfg or {}).items() if v not in (None, "")})
    missing = ad.check_config(cfg)
    if missing:
        raise Invalid(f"missing: {', '.join(missing)}", "config")
    hook_id = secrets.token_urlsafe(18)
    if config.get("VO_REGISTER_WEBHOOKS", "1") != "0":       # tell the provider our address ourselves, when it allows
        try:
            ad.register_hook(cfg, f"{config.PUBLIC_URL()}/hooks/{hook_id}")
        except ValueError as e:
            raise Invalid(str(e), "config") from None
    row = conn.execute(insert(A).values(business_id=biz["id"], channel=channel, name=(name or channel)[:100],
                                        address=address, config=crypto.seal(cfg), hook_id=hook_id)
                       .returning(A)).mappings().one()
    log(conn, biz["id"], "channel.connected", actor, row["id"], {"channel": channel})
    return public_account(row)


def public_account(row) -> dict:
    d = dict(row)
    d["config"] = crypto.redact(d["config"])
    d["webhook_url"] = f"{config.PUBLIC_URL()}/hooks/{d.pop('hook_id')}"
    return d


def accounts(conn: Connection, business_id: UUID) -> list[dict]:
    return [public_account(r) for r in conn.execute(select(A).where(A.c.business_id == business_id, A.c.active)
                                                     .order_by(A.c.created_at)).mappings()]


def disconnect(conn: Connection, business_id: UUID, actor: str, account_id: UUID) -> None:
    row = conn.execute(update(A).where(A.c.business_id == business_id, A.c.id == account_id, A.c.active)
                       .values(active=False).returning(A.c.channel, A.c.config)).first()
    if not row:
        raise NotFound("channel account")
    log(conn, business_id, "channel.disconnected", actor, account_id)
    if config.get("VO_REGISTER_WEBHOOKS", "1") != "0" and row.channel in channels.available():
        channels.adapter(row.channel).unregister_hook(crypto.open_(row.config))


def _account(conn: Connection, business_id: UUID, account_id: UUID | None, channel: str | None):
    s = select(A).where(A.c.business_id == business_id, A.c.active)
    if account_id:
        s = s.where(A.c.id == account_id)
    elif channel:
        s = s.where(A.c.channel == channel).order_by(A.c.created_at)
    else:
        raise Invalid("say which channel_account_id or channel to send on", "channel")
    row = conn.execute(s).mappings().first()
    if not row:
        raise NotFound("channel account")
    return row


# ---------------------------------------------------------------- conversations
def _conversation(conn: Connection, business_id: UUID, account_id: UUID, contact_id: UUID) -> dict:
    conn.execute(pg_insert(CV).values(business_id=business_id, channel_account_id=account_id, contact_id=contact_id)
                 .on_conflict_do_nothing(constraint="uq_conversation"))
    return dict(conn.execute(select(CV).where(CV.c.channel_account_id == account_id, CV.c.contact_id == contact_id))
                .mappings().one())


def conversations(conn: Connection, business_id: UUID, *, status: str | None = None, contact_id: UUID | None = None,
                  limit: int = 50) -> list[dict]:
    s = (select(CV, A.c.channel, db.contacts.c.name.label("contact_name"))
         .join(A, A.c.id == CV.c.channel_account_id).join(db.contacts, db.contacts.c.id == CV.c.contact_id)
         .where(CV.c.business_id == business_id))
    if status:
        s = s.where(CV.c.status == status)
    if contact_id:
        s = s.where(CV.c.contact_id == contact_id)
    s = s.order_by(CV.c.last_message_at.desc().nulls_last()).limit(max(1, min(limit, 200)))
    return [dict(r) for r in conn.execute(s).mappings()]


def conversation(conn: Connection, business_id: UUID, conversation_id: UUID, *, limit: int = 100) -> dict:
    row = conn.execute(select(CV, A.c.channel).join(A, A.c.id == CV.c.channel_account_id)
                       .where(CV.c.business_id == business_id, CV.c.id == conversation_id)).mappings().first()
    if not row:
        raise NotFound("conversation")
    msgs = conn.execute(select(M).where(M.c.business_id == business_id, M.c.conversation_id == conversation_id)
                        .order_by(M.c.created_at.desc()).limit(max(1, min(limit, 500)))).mappings()
    return {**dict(row), "messages": [dict(m) for m in reversed(list(msgs))]}


def set_status(conn: Connection, business_id: UUID, actor: str, conversation_id: UUID, status: str) -> None:
    if status not in ("open", "closed"):
        raise Invalid("status is open or closed", "status")
    if not conn.execute(update(CV).where(CV.c.business_id == business_id, CV.c.id == conversation_id)
                        .values(status=status)).rowcount:
        raise NotFound("conversation")
    log(conn, business_id, f"conversation.{status}", actor, conversation_id)


# ---------------------------------------------------------------- sending
def _check_rules(biz: dict, contact: dict, channel: str, adapter, conv: dict, kind: str, template, now: datetime) -> None:
    pack = packs.country(biz["country"])
    if contact.get("deleted_at"):
        raise Blocked("erased", "this contact was erased")
    if contact.get("unsubscribed_at") and (kind == "marketing" or channel == "sms"):
        raise Blocked("unsubscribed", "this contact unsubscribed")
    if kind == "marketing":
        if not contact.get("marketing_consent"):
            raise Blocked("no_consent", f"marketing needs the contact's consent ({pack.messaging.marketing_consent} in {pack.code})")
        qh = pack.messaging.quiet_hours
        if qh:
            h = now.astimezone(ZoneInfo(biz["timezone"])).hour
            start, end = qh
            quiet = (h >= start or h < end) if start > end else (start <= h < end)
            if quiet:
                raise Blocked("quiet_hours", f"marketing can't go out between {start}:00 and {end}:00 local time in {pack.code}")
    if adapter.reply_window_hours and not adapter.templates:       # TikTok: no templates — they write first, we answer in time
        last = conv.get("last_inbound_at")
        if not last:
            raise Blocked("customer_first", f"on {channel} the customer writes first; then you can answer for "
                                            f"{adapter.reply_window_hours} h")
        if now - last > timedelta(hours=adapter.reply_window_hours):
            raise Blocked("window_closed",
                          f"{channel} only allows answers within {adapter.reply_window_hours} h of the customer's last message")
    elif adapter.reply_window_hours and not template:
        last = conv.get("last_inbound_at")
        if not last or now - last > timedelta(hours=adapter.reply_window_hours):
            raise Blocked("outside_reply_window",
                          f"more than {adapter.reply_window_hours} h since the contact last wrote: use an approved template")


def send(biz: dict, actor: str, *, contact_id: UUID, body: str = "", media: list | None = None,
         template: dict | None = None, kind: str = "service", channel_account_id: UUID | None = None,
         channel: str | None = None, http=None, now: datetime | None = None, options: list | None = None,
         email: dict | None = None) -> dict:
    """Send a message to a contact. Two transactions around the provider call, so a slow provider
    never holds the database, and the message is on record (queued) even if the call dies."""
    if kind not in ("service", "marketing"):
        raise Invalid("kind is service or marketing", "kind")
    if not (body or media or template):
        raise Invalid("nothing to send", "body")
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        if conn.execute(select(db.businesses.c.suspended_at).where(db.businesses.c.id == biz["id"])).scalar():
            raise Blocked("suspended", "this business is paused by its provider; nothing is sent")
        from . import billing
        if billing.paused(dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())):
            raise Blocked("paused", "the free launch months are over; choose a plan to send again (nothing was deleted)")
        contact = contacts.get(conn, biz["id"], contact_id)
        acc = _account(conn, biz["id"], channel_account_id, channel)
        ad = channels.adapter(acc["channel"], http)
        I = db.identities
        address = conn.execute(select(I.c.address).where(I.c.business_id == biz["id"], I.c.contact_id == contact_id,
                                                         I.c.channel == acc["channel"])).scalar()
        if not address:
            raise Blocked("no_address", f"no {acc['channel']} address for this contact")
        conv = _conversation(conn, biz["id"], acc["id"], contact_id)
        try:
            _check_rules(biz, contact, acc["channel"], ad, conv, kind, template, now)
        except Blocked as b:
            log(conn, biz["id"], "message.blocked", actor, contact_id, {"reason": b.reason, "channel": acc["channel"]})
            raise
        msg = conn.execute(insert(M).values(business_id=biz["id"], conversation_id=conv["id"], direction="out", kind=kind,
                                            body=body or "", media=media or [], template=template, status="queued")
                           .returning(M.c.id)).scalar()
        cfg = crypto.open_(acc["config"])
    res = None
    try:
        fresh = ad.refresh(cfg)                       # tokens that run out (TikTok): renewed and saved before sending
        if fresh is not None:
            save_config(acc["id"], fresh)
            cfg = fresh
    except ValueError as e:                           # can't be renewed: on record as failed, with the reason to show
        save_config(acc["id"], {**cfg, "error": str(e)[:200]})
        res = channels.SendResult("failed", error=str(e)[:200])
    if res is None:
        try:
            em = email or {}                          # v0.18: a campaign email's subject, HTML part and unsubscribe headers
            res = ad.send(cfg, address, Outbound(to=address, body=body or "", media=media or [], template=template,
                                                 options=options or [], subject=em.get("subject"), html=em.get("html"),
                                                 headers=em.get("headers") or {}))
        except Exception as e:  # provider/network failure: recorded, never a crash
            res = channels.SendResult("failed", error=f"{type(e).__name__}")
    try:
        with db.tx() as conn:
            conn.execute(update(M).where(M.c.id == msg).values(status=res.status, provider_id=res.provider_id, error=res.error))
    except IntegrityError:          # a provider reused an id we already hold: keep the send on record without it
        with db.tx() as conn:
            conn.execute(update(M).where(M.c.id == msg).values(status=res.status, error=f"duplicate provider id {res.provider_id}"))
    with db.tx() as conn:
        conn.execute(update(CV).where(CV.c.id == conv["id"]).values(last_message_at=func.now(), status="open"))
        log(conn, biz["id"], "message.sent" if res.status != "failed" else "message.failed", actor, msg,
            {"channel": acc["channel"], "kind": kind, **({"error": res.error} if res.error else {})})
        return dict(conn.execute(select(M).where(M.c.id == msg)).mappings().one())


def save_config(account_id: UUID, cfg: dict) -> None:
    """Store a channel account's settings again (a renewed token, a problem to show), secrets sealed."""
    with db.tx() as conn:
        conn.execute(update(A).where(A.c.id == account_id).values(config=crypto.seal(cfg)))


# ---------------------------------------------------------------- receiving
def account_by_hook(hook_id: str):
    with db.tx() as conn:
        return conn.execute(select(A).where(A.c.hook_id == hook_id, A.c.active)).mappings().first()


def handshake(acc, req: HookRequest) -> str | None:
    return channels.adapter(acc["channel"]).verify(crypto.open_(acc["config"]), req)


def receive(acc, req: HookRequest) -> dict:
    """A provider's webhook -> contacts, conversations and messages. Safe to replay: the same provider
    message id is stored once."""
    ad = channels.adapter(acc["channel"])
    items = ad.parse(crypto.open_(acc["config"]), req)          # raises BadSignature before touching anything
    return receive_items(acc, items)


def receive_items(acc, items: list) -> dict:
    """Store what a provider sent for this channel account (already verified)."""
    stats = {"messages": 0, "duplicates": 0, "statuses": 0, "new_contacts": 0}
    with db.tx() as conn:
        biz = conn.execute(select(db.businesses).where(db.businesses.c.id == acc["business_id"])).mappings().one()
        for it in items:
            if isinstance(it, StatusUpdate):
                stats["statuses"] += _status(conn, biz["id"], it)
            elif isinstance(it, Inbound) and it.address:
                _inbound(conn, dict(biz), acc, it, stats)
    return stats


def _address(biz: dict, channel: str, raw: str) -> str:
    """One person = one address: phones in E.164 (WhatsApp's +521… becomes +52…), emails lower-case."""
    kind = channels.adapter(channel).address_kind
    if kind == "phone":
        try:
            return phones.e164(raw, packs.country(biz["country"]))
        except phones.BadPhone:
            return raw
    return raw.strip().lower() if kind == "email" else raw


def _inbound(conn: Connection, biz: dict, acc, it: Inbound, stats: dict) -> None:
    address = _address(biz, acc["channel"], it.address)
    from . import owner as OW
    mine, mid = OW.inbound(conn, biz, acc, address, it)       # the owner's own phone: a command for Valeria, never a customer
    if mine:
        if mid:
            stats.setdefault("owner_ids", []).append(mid)
        else:
            stats["duplicates"] += 1
        return
    contact, new = contacts.find_or_create_by_identity(conn, biz, acc["channel"], address, it.name, acc["channel"])
    stats["new_contacts"] += int(new)
    conv = _conversation(conn, biz["id"], acc["id"], contact["id"])
    at = it.at or datetime.now(timezone.utc)
    ins = pg_insert(M).values(business_id=biz["id"], conversation_id=conv["id"], direction="in", body=it.body or "",
                              media=it.media or [], provider_id=it.provider_id, status="received", created_at=at)
    if it.provider_id:
        ins = ins.on_conflict_do_nothing(index_elements=[M.c.business_id, M.c.provider_id],
                                         index_where=M.c.provider_id.isnot(None))
    mid = conn.execute(ins.returning(M.c.id)).scalar()
    if not mid:
        stats["duplicates"] += 1
        return
    stats["messages"] += 1
    stats.setdefault("conversation_ids", [])
    if conv["id"] not in stats["conversation_ids"]:
        stats["conversation_ids"].append(conv["id"])
    conn.execute(update(CV).where(CV.c.id == conv["id"]).values(
        last_inbound_at=func.greatest(func.coalesce(CV.c.last_inbound_at, at), at),
        last_message_at=func.greatest(func.coalesce(CV.c.last_message_at, at), at), status="open"))
    log(conn, biz["id"], "message.received", f"webhook:{acc['channel']}", mid, {"contact_id": str(contact["id"])})
    if contacts.is_stop(it.body):
        contacts.unsubscribe(conn, biz["id"], contact["id"], f"webhook:{acc['channel']}")


def _status(conn: Connection, business_id: UUID, st: StatusUpdate) -> int:
    if not st.provider_id or st.status not in ("sent", "delivered", "read", "failed"):
        return 0
    rank = case(_RANK, value=M.c.status, else_=-1)
    q = update(M).where(M.c.business_id == business_id, M.c.provider_id == st.provider_id, M.c.direction == "out")
    if st.status == "failed":
        q = q.where(M.c.status.in_(("queued", "sent"))).values(status="failed", error=st.error)
    else:
        q = q.where(rank < _RANK[st.status]).values(status=st.status)   # never go backwards (read stays read)
    return conn.execute(q).rowcount
