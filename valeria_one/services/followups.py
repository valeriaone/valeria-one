"""Automatic follow-ups and campaigns.

Automations ("rules that send themselves"):
  appointment.reminder  N minutes BEFORE an appointment   e.g. the day before: "See you tomorrow at 10:00"
  appointment.done      N minutes AFTER it is marked done e.g. thank-you + review link
  contact.created       N minutes after a new customer    e.g. welcome + booking link
  deal.won              N minutes after a deal is won     e.g. thanks / next visit
Campaigns: one message to every customer with a tag (or everyone) who AGREED to promotions.

Everything goes through one outbox (scheduled_messages) filled in the same transaction as the event that
caused it, and is sent by a job with the normal country rules: no consent -> skipped, quiet hours -> moved
to the next allowed hour, outside WhatsApp's 24 h window -> the approved template, or skipped.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_time
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, db, packs
from . import Blocked, Invalid, NotFound, log

AU, CP, SM = db.automations, db.campaigns, db.scheduled_messages
TRIGGERS = ("appointment.reminder", "appointment.done", "contact.created", "deal.won")


# ---------------------------------------------------------------- automations
def create_automation(conn: Connection, biz: dict, actor: str, *, trigger: str, minutes: int, message: str,
                      template: str | None = None) -> dict:
    if trigger not in TRIGGERS:
        raise Invalid(f"trigger: one of {', '.join(TRIGGERS)}", "trigger")
    if not isinstance(minutes, int) or not 0 <= minutes <= 525600:
        raise Invalid("minutes: 0 to 525600 (a year)", "minutes")
    message = (message or "").strip()
    if not message:
        raise Invalid("write the message", "message")
    row = conn.execute(insert(AU).values(business_id=biz["id"], trigger=trigger, minutes=minutes, message=message[:1000],
                                         template=(template or "").strip()[:100] or None).returning(AU)).mappings().one()
    log(conn, biz["id"], "automation.created", actor, row["id"], {"trigger": trigger})
    return dict(row)


def automations(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(AU).where(AU.c.business_id == business_id, AU.c.active)
                                          .order_by(AU.c.created_at)).mappings()]


def delete_automation(conn: Connection, business_id: UUID, actor: str, automation_id: UUID) -> None:
    if not conn.execute(update(AU).where(AU.c.business_id == business_id, AU.c.id == automation_id, AU.c.active)
                        .values(active=False)).rowcount:
        raise NotFound("automation")
    conn.execute(update(SM).where(SM.c.automation_id == automation_id, SM.c.status == "pending").values(status="skipped",
                                                                                                    error="rule removed"))
    log(conn, business_id, "automation.deleted", actor, automation_id)


# ---------------------------------------------------------------- campaigns
def create_campaign(conn: Connection, biz: dict, actor: str, *, name: str, message: str, tag: str | None = None,
                    send_at: datetime | None = None, template: str | None = None) -> dict:
    name, message = (name or "").strip(), (message or "").strip()
    if not name or not message:
        raise Invalid("name and message are required", "message")
    when = send_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise Invalid("send_at needs a time zone", "send_at")
    row = conn.execute(insert(CP).values(business_id=biz["id"], name=name[:120], message=message[:1000],
                                         tag=(tag or "").strip().lower() or None, send_at=when,
                                         template=(template or "").strip()[:100] or None).returning(CP)).mappings().one()
    log(conn, biz["id"], "campaign.created", actor, row["id"], {"tag": row["tag"]})
    return dict(row)


def campaigns(conn: Connection, business_id: UUID) -> list[dict]:
    rows = [dict(r) for r in conn.execute(select(CP).where(CP.c.business_id == business_id).order_by(CP.c.created_at.desc())).mappings()]
    for r in rows:
        stats = dict(conn.execute(select(SM.c.status, func.count()).where(SM.c.campaign_id == r["id"]).group_by(SM.c.status)).all())
        r["counts"] = {**r["counts"], **stats}
    return rows


def cancel_campaign(conn: Connection, business_id: UUID, actor: str, campaign_id: UUID) -> None:
    if not conn.execute(update(CP).where(CP.c.business_id == business_id, CP.c.id == campaign_id,
                                         CP.c.status.in_(("scheduled", "sending"))).values(status="cancelled")).rowcount:
        raise NotFound("campaign")
    conn.execute(update(SM).where(SM.c.campaign_id == campaign_id, SM.c.status == "pending").values(status="skipped", error="cancelled"))
    log(conn, business_id, "campaign.cancelled", actor, campaign_id)


# ---------------------------------------------------------------- events -> outbox (inside the event's transaction)
def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    if not subject_id:
        return
    wanted = {"appointment.booked": "appointment.reminder", "appointment.done": "appointment.done",
              "contact.created": "contact.created", "deal.stage_changed": "deal.won"}.get(type_)
    if not wanted:
        return
    rules = conn.execute(select(AU).where(AU.c.business_id == business_id, AU.c.active, AU.c.trigger == wanted)).mappings().all()
    if not rules:
        return
    if not blocks.on(blocks.state(conn, business_id), "followups"):
        return                                                 # switched off: nothing new is scheduled
    now = datetime.now(timezone.utc)
    if wanted == "appointment.reminder" or wanted == "appointment.done":
        ap = conn.execute(select(db.appointments).where(db.appointments.c.id == subject_id)).mappings().first()
        if not ap or not ap["contact_id"]:
            return
        contact_id = ap["contact_id"]
    elif wanted == "deal.won":
        d = conn.execute(select(db.deals).where(db.deals.c.id == subject_id)).mappings().first()
        biz = conn.execute(select(db.businesses.c.industry).where(db.businesses.c.id == business_id)).scalar()
        st = packs.industry(biz).stage(data.get("to") or "")
        if not d or not d["contact_id"] or not st or st.outcome != "won":
            return
        contact_id = d["contact_id"]
    else:
        contact_id = subject_id
    for r in rules:
        if wanted == "appointment.reminder":
            when = ap["starts_at"] - timedelta(minutes=r["minutes"])
            if when <= now:
                continue                                       # booked too late for this reminder
        else:
            when = now + timedelta(minutes=r["minutes"])
        conn.execute(pg_insert(SM).values(business_id=business_id, automation_id=r["id"], contact_id=contact_id,
                                          subject_id=subject_id, send_at=when).on_conflict_do_nothing(constraint="uq_sched_auto"))


def dispatch_campaigns(now: datetime | None = None) -> int:
    """Due campaigns -> one outbox row per customer who agreed (and has the tag)."""
    now = now or datetime.now(timezone.utc)
    C = db.contacts
    n = 0
    with db.tx() as conn:
        due = conn.execute(select(CP).where(CP.c.status == "scheduled", CP.c.send_at <= now).with_for_update(skip_locked=True)).mappings().all()
        for cp in due:
            if not blocks.on(blocks.state(conn, cp["business_id"]), "followups"):
                conn.execute(update(CP).where(CP.c.id == cp["id"]).values(status="cancelled"))
                continue
            q = select(C.c.id).where(C.c.business_id == cp["business_id"], C.c.deleted_at.is_(None), C.c.marketing_consent,
                                     C.c.unsubscribed_at.is_(None))
            if cp["tag"]:
                q = q.where(C.c.tags.any(cp["tag"]))
            ids = [r[0] for r in conn.execute(q)]
            for cid in ids:
                conn.execute(pg_insert(SM).values(business_id=cp["business_id"], campaign_id=cp["id"], contact_id=cid,
                                                  send_at=now).on_conflict_do_nothing(constraint="uq_sched_campaign"))
            conn.execute(update(CP).where(CP.c.id == cp["id"]).values(status="done", counts={"audience": len(ids)}))
            log(conn, cp["business_id"], "campaign.sent", "system", cp["id"], {"audience": len(ids)})
            n += 1
    return n


# ---------------------------------------------------------------- sending
def _fill(text: str, *, name: str, business: str, when: str = "", link: str = "") -> str:
    first = (name or "").split(" ")[0]
    return text.replace("{name}", first).replace("{business}", business).replace("{when}", when).replace("{link}", link).strip()


def _pick_account(conn: Connection, biz: dict, contact_id: UUID):
    """The channel they last talked on; else the country's favourite channel they can be reached on."""
    CV, A, I = db.conversations, db.channel_accounts, db.identities
    last = conn.execute(select(CV.c.channel_account_id).join(A, A.c.id == CV.c.channel_account_id)
                        .where(CV.c.business_id == biz["id"], CV.c.contact_id == contact_id, A.c.active,
                               A.c.channel.notin_(("webchat",)))
                        .order_by(CV.c.last_message_at.desc().nulls_last()).limit(1)).scalar()
    if last:
        return last
    chans = {r[0] for r in conn.execute(select(I.c.channel).where(I.c.business_id == biz["id"], I.c.contact_id == contact_id))}
    accs = conn.execute(select(A.c.id, A.c.channel).where(A.c.business_id == biz["id"], A.c.active)).all()
    for ch in packs.country(biz["country"]).channels + ["sandbox"]:
        for aid, ach in accs:
            if ach == ch and ch in chans:
                return aid
    return None


def _next_allowed(biz: dict, now: datetime) -> datetime:
    qh = packs.country(biz["country"]).messaging.quiet_hours
    tz = ZoneInfo(biz["timezone"])
    local = now.astimezone(tz)
    if not qh:
        return now
    target = datetime.combine(local.date(), time(qh[1]), tz)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def send_due(now: datetime | None = None, http=None, limit: int = 200) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"sent": 0, "skipped": 0, "failed": 0, "later": 0}
    with db.tx() as conn:
        due = conn.execute(select(SM).where(SM.c.status == "pending", SM.c.send_at <= now).order_by(SM.c.send_at)
                           .limit(limit).with_for_update(skip_locked=True)).mappings().all()
        due = [dict(r) for r in due]
    for row in due:
        res = _send_one(row, now, http)
        stats[res] += 1
    return stats


def _done(row_id, status: str, error: str | None = None, message_id=None, send_at=None) -> None:
    with db.tx() as conn:
        vals = {"status": status, "error": error, "sent_message_id": message_id}
        if send_at:
            vals = {"send_at": send_at, "error": error}
        conn.execute(update(SM).where(SM.c.id == row_id).values(**vals))


def _send_one(row: dict, now: datetime, http) -> str:
    from . import contacts as C
    from . import inbox as X
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
        if not blocks.on(biz, "followups"):
            _done(row["id"], "skipped", "follow-ups switched off")
            return "skipped"
        try:
            contact = C.get(conn, biz["id"], row["contact_id"])
        except NotFound:
            _done(row["id"], "skipped", "customer erased")
            return "skipped"
        if row["automation_id"]:
            rule = conn.execute(select(AU).where(AU.c.id == row["automation_id"])).mappings().one()
            if not rule["active"]:
                _done(row["id"], "skipped", "rule removed")
                return "skipped"
            text, template, kind = rule["message"], rule["template"], rule["kind"]
        else:
            cp = conn.execute(select(CP).where(CP.c.id == row["campaign_id"])).mappings().one()
            if cp["status"] == "cancelled":
                _done(row["id"], "skipped", "cancelled")
                return "skipped"
            text, template, kind = cp["message"], cp["template"], "marketing"
        when = ""
        if row["automation_id"] and rule["trigger"] in ("appointment.reminder", "appointment.done"):
            ap = conn.execute(select(db.appointments).where(db.appointments.c.id == row["subject_id"])).mappings().first()
            if not ap or (rule["trigger"] == "appointment.reminder" and ap["status"] not in ("booked", "confirmed")):
                _done(row["id"], "skipped", "appointment changed or cancelled")
                return "skipped"
            tz = ZoneInfo(biz["timezone"])
            loc = packs.country(biz["country"]).locale
            when = format_date(ap["starts_at"].astimezone(tz), "EEEE d MMMM", locale=loc) + " " + \
                format_time(ap["starts_at"].astimezone(tz), "short", locale=loc)
        account_id = _pick_account(conn, biz, contact["id"])
    if not account_id:
        _done(row["id"], "skipped", "no channel to reach them")
        return "skipped"
    link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
    body = _fill(text, name=contact["name"], business=biz["name"], when=when, link=link)
    try:
        msg = X.send(biz, "followup", contact_id=contact["id"], body=body, kind=kind, channel_account_id=account_id, http=http, now=now)
    except Blocked as b:
        if b.reason == "quiet_hours":
            _done(row["id"], "pending", "quiet hours", send_at=_next_allowed(biz, now))
            return "later"
        if b.reason == "outside_reply_window" and template:
            try:
                msg = X.send(biz, "followup", contact_id=contact["id"], kind=kind, channel_account_id=account_id, http=http, now=now,
                             template={"name": template, "language": biz["language"],
                                       "params": [p for p in ((contact["name"] or "").split(" ")[0], when) if p]})
            except Blocked as b2:
                _done(row["id"], "skipped", b2.reason)
                return "skipped"
        else:
            _done(row["id"], "skipped", b.reason)
            return "skipped"
    if msg["status"] == "failed":
        _done(row["id"], "failed", msg.get("error"), msg["id"])
        return "failed"
    _done(row["id"], "sent", None, msg["id"])
    return "sent"


def outbox(conn: Connection, business_id: UUID, limit: int = 50) -> list[dict]:
    C = db.contacts
    return [dict(r) for r in conn.execute(select(SM, C.c.name.label("contact_name")).join(C, C.c.id == SM.c.contact_id)
                                          .where(SM.c.business_id == business_id).order_by(SM.c.send_at.desc())
                                          .limit(max(1, min(limit, 200)))).mappings()]
