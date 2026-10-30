"""Automatic follow-ups and campaigns.

Automations ("rules that send themselves"):
  appointment.reminder  N minutes BEFORE an appointment   e.g. the day before: "See you tomorrow at 10:00"
  appointment.done      N minutes AFTER it is marked done e.g. thank-you + review link
  contact.created       N minutes after a new customer    e.g. welcome + booking link
  deal.won              N minutes after a deal is won     e.g. thanks / next visit
  payment.paid          N minutes after a payment         e.g. thanks + "bring a friend" link
  contact.quiet         nothing from them in N days       the win-back, made on the Grow page (services/growth.py)
Ready-made rules from Grow carry a preset (winback, referral) and belong to the Grow block, not to Follow-ups.
Valeria's no-show reminder ("reply 1 to confirm, 2 to change", services/reminders.py) is the preset reminder and
belongs to the Calendar block.
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
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, db, packs
from . import Blocked, Invalid, NotFound, log

AU, CP, SM = db.automations, db.campaigns, db.scheduled_messages
TRIGGERS = ("appointment.reminder", "appointment.done", "contact.created", "deal.won", "payment.paid")
QUIET = "contact.quiet"                                        # not an event: Grow's daily round picks who went quiet
PRESETS = ("winback", "referral", "reminder")
GROW_PRESETS = ("winback", "referral")
LEASE_MINUTES = 10                                             # a claimed row waits this long before anyone may retry it


def needs(rule: dict) -> tuple[str, ...]:
    """The blocks a rule needs switched on to send: Grow's ready-made rules are Grow's; a bring-a-friend link needs Grow too."""
    if rule.get("preset") == "reminder":
        return ("calendar",)
    if rule.get("preset"):
        return ("grow",)
    return ("followups", "grow") if "{referral_link}" in (rule.get("message") or "") else ("followups",)


def _working(biz: dict, rule: dict) -> bool:
    return all(blocks.on(biz, b) for b in needs(rule))


# ---------------------------------------------------------------- automations
def create_automation(conn: Connection, biz: dict, actor: str, *, trigger: str, minutes: int, message: str,
                      template: str | None = None, kind: str = "service", preset: str | None = None) -> dict:
    if trigger not in TRIGGERS and not (trigger == QUIET and preset == "winback"):
        raise Invalid(f"trigger: one of {', '.join(TRIGGERS)}", "trigger")
    if preset is not None and preset not in PRESETS:
        raise Invalid(f"preset: one of {', '.join(PRESETS)}", "preset")
    if not isinstance(minutes, int) or not 0 <= minutes <= 525600:
        raise Invalid("minutes: 0 to 525600 (a year)", "minutes")
    message = (message or "").strip()
    if not message:
        raise Invalid("write the message", "message")
    if kind not in ("service", "marketing"):
        raise Invalid("kind is service or marketing", "kind")
    if "{review_link}" in message and "{referral_link}" in message:   # a review ask never comes with a reward (Google, FTC)
        raise Invalid("keep the review request and the bring-a-friend link in separate messages", "message")
    if "{referral_link}" in message:
        kind = "marketing"                                     # an offer: only to customers who agreed to promotions
    row = conn.execute(insert(AU).values(business_id=biz["id"], trigger=trigger, minutes=minutes, message=message[:1000],
                                         template=(template or "").strip()[:100] or None, kind=kind, preset=preset)
                       .returning(AU)).mappings().one()
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


def switched_off(conn: Connection, business_id: UUID, block: str) -> None:
    """A block was switched off: what it had waiting to go out is skipped (follow-ups also cancel their campaigns)."""
    grow = select(AU.c.id).where(AU.c.business_id == business_id, AU.c.preset.in_(GROW_PRESETS))
    ready = select(AU.c.id).where(AU.c.business_id == business_id, AU.c.preset.isnot(None))     # Grow's and the reminder
    pending = update(SM).where(SM.c.business_id == business_id, SM.c.status == "pending")
    if block == "grow":
        conn.execute(pending.where(SM.c.automation_id.in_(grow)).values(status="skipped", error="grow switched off"))
    elif block == "followups":
        conn.execute(pending.where(or_(SM.c.automation_id.is_(None), SM.c.automation_id.notin_(ready)))
                     .values(status="skipped", error="follow-ups switched off"))
        conn.execute(update(CP).where(CP.c.business_id == business_id, CP.c.status.in_(("scheduled", "sending")))
                     .values(status="cancelled"))


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
              "contact.created": "contact.created", "deal.stage_changed": "deal.won", "payment.paid": "payment.paid"}.get(type_)
    if not wanted:
        return
    rules = conn.execute(select(AU).where(AU.c.business_id == business_id, AU.c.active, AU.c.trigger == wanted)).mappings().all()
    if not rules:
        return
    st = blocks.state(conn, business_id)
    rules = [r for r in rules if _working(st, r)]                 # switched off: nothing new is scheduled
    if not rules:
        return
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
    elif wanted == "payment.paid":
        contact_id = conn.execute(select(db.payments.c.contact_id).where(db.payments.c.id == subject_id)).scalar()
        if not contact_id:
            return
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
            if cp["channel"]:                                 # v0.18: the campaign builder's audience, throttled, one token each
                from . import campaigns as CG
                count = CG.dispatch(conn, dict(cp), now)
            else:
                if cp["tag"]:
                    q = q.where(C.c.tags.any(cp["tag"]))
                ids = [r[0] for r in conn.execute(q)]
                for cid in ids:
                    conn.execute(pg_insert(SM).values(business_id=cp["business_id"], campaign_id=cp["id"], contact_id=cid,
                                                      send_at=now).on_conflict_do_nothing(constraint="uq_sched_campaign"))
                count = len(ids)
            conn.execute(update(CP).where(CP.c.id == cp["id"]).values(status="done", counts={"audience": count}))
            log(conn, cp["business_id"], "campaign.sent", "system", cp["id"], {"audience": count})
            n += 1
    return n


# ---------------------------------------------------------------- sending
def _fill(text: str, *, name: str, business: str, when: str = "", link: str = "", review: str = "") -> str:
    first = (name or "").split(" ")[0]
    return (text.replace("{name}", first).replace("{business}", business).replace("{when}", when)
            .replace("{review_link}", review).replace("{link}", link).strip())


def _pick_account(conn: Connection, biz: dict, contact_id: UUID, avoid: frozenset[str] = frozenset()):
    """The channel they last talked on; else the country's favourite channel they can be reached on (never one in `avoid`)."""
    CV, A, I = db.conversations, db.channel_accounts, db.identities
    last = conn.execute(select(CV.c.channel_account_id).join(A, A.c.id == CV.c.channel_account_id)
                        .where(CV.c.business_id == biz["id"], CV.c.contact_id == contact_id, A.c.active,
                               A.c.channel.notin_(("webchat", *avoid)))
                        .order_by(CV.c.last_message_at.desc().nulls_last()).limit(1)).scalar()
    if last:
        return last
    chans = {r[0] for r in conn.execute(select(I.c.channel).where(I.c.business_id == biz["id"], I.c.contact_id == contact_id))}
    accs = conn.execute(select(A.c.id, A.c.channel).where(A.c.business_id == biz["id"], A.c.active)).all()
    usual = packs.country(biz["country"]).channels + ["sandbox"]       # the country's usual channels first,
    for ch in usual + sorted({ach for _, ach in accs} - set(usual)):  # then any other the business connected
        for aid, ach in accs:
            if ach == ch and ch in chans and ch not in avoid:
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
        if due:                                    # v0.18: claimed — a second worker never picks the same rows up meanwhile
            conn.execute(update(SM).where(SM.c.id.in_([r["id"] for r in due])).values(send_at=now + timedelta(minutes=LEASE_MINUTES)))
    for row in due:
        res = _send_one(row, now, http)
        stats[res] += 1
        if res != "later":                         # done: it keeps the time it was due (a crash leaves it to retry after the lease)
            with db.tx() as conn:
                conn.execute(update(SM).where(SM.c.id == row["id"], SM.c.status != "pending").values(send_at=row["send_at"]))
    return stats


def _done(row_id, status: str, error: str | None = None, message_id=None, send_at=None) -> None:
    with db.tx() as conn:
        vals = {"status": status, "error": error, "sent_message_id": message_id}
        if send_at:
            vals = {"send_at": send_at, "error": error}
        conn.execute(update(SM).where(SM.c.id == row_id).values(**vals))


def _send_v18(row: dict, now: datetime, http) -> str | None:
    """v0.18: a campaign-builder message or a drip's step (services/campaigns.py, sequences.py). None = an older row."""
    from . import contacts as C
    with db.tx() as conn:
        cp = conn.execute(select(CP).where(CP.c.id == row["campaign_id"])).mappings().first() if row["campaign_id"] else None
        if not row["enrollment_id"] and not (cp and cp["channel"]):
            return None
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
        if not blocks.on(biz, "followups"):
            _done(row["id"], "skipped", "follow-ups switched off")
            return "skipped"
        try:
            contact = C.get(conn, biz["id"], row["contact_id"])
        except NotFound:
            _done(row["id"], "skipped", "customer erased")
            return "skipped"
    if row["enrollment_id"]:
        from . import sequences as SQ
        return _v18(row, SQ.send_row(row, biz, contact, now, http))
    from . import campaigns as CG
    return _v18(row, CG.send_row(row, dict(cp), biz, contact, now, http))


def _send_one(row: dict, now: datetime, http) -> str:
    from . import contacts as C
    v18 = _send_v18(row, now, http)
    if v18:
        return v18
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
        rule = dict(conn.execute(select(AU).where(AU.c.id == row["automation_id"])).mappings().one()) if row["automation_id"] else None
        off = next((b for b in (needs(rule) if rule else ("followups",)) if not blocks.on(biz, b)), None)
        if off:
            _done(row["id"], "skipped", {"grow": "grow switched off", "calendar": "calendar switched off"}.get(off, "follow-ups switched off"))
            return "skipped"
        try:
            contact = C.get(conn, biz["id"], row["contact_id"])
        except NotFound:
            _done(row["id"], "skipped", "customer erased")
            return "skipped"
        if rule:
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
        if rule and rule["trigger"] in ("appointment.reminder", "appointment.done"):
            ap = conn.execute(select(db.appointments).where(db.appointments.c.id == row["subject_id"])).mappings().first()
            if not ap or (rule["trigger"] == "appointment.reminder" and ap["status"] not in ("booked", "confirmed")):
                _done(row["id"], "skipped", "appointment changed or cancelled")
                return "skipped"
            tz = ZoneInfo(biz["timezone"])
            loc = packs.country(biz["country"]).locale
            when = format_date(ap["starts_at"].astimezone(tz), "EEEE d MMMM", locale=loc) + " " + \
                format_time(ap["starts_at"].astimezone(tz), "short", locale=loc)
        avoid = frozenset()
        if rule and rule["preset"] == "winback":
            from .growth import avoid_channels
            avoid = avoid_channels(rule)                       # no approved template: not on WhatsApp-like channels
        account_id = _pick_account(conn, biz, contact["id"], avoid)
    if not account_id:
        _done(row["id"], "skipped", "no channel to reach them")
        return "skipped"
    link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
    shared = None
    if rule and rule["preset"] == "reminder":                   # Valeria's reminder: in her owner's style
        from . import style as STY
        with db.tx() as conn:
            text = STY.apply(STY.get(conn, biz["id"]), text)
    elif rule and (rule["preset"] or "{referral_link}" in (text or "")):   # Grow: the offer, the booking line, the friend link
        from . import growth as GR
        with db.tx() as conn:
            try:
                text, shared = GR.prepare(conn, biz, contact, rule, text, link, now)
            except GR.Skip as sk:
                _done(row["id"], "skipped", str(sk))
                return "skipped"
    review_rid, review = None, ""
    if "{review_link}" in (text or ""):                         # a review request: the same ask for everyone, once per 90 days
        from . import reviews as RV
        with db.tx() as conn:
            if not biz.get("review_url"):
                _done(row["id"], "skipped", "no review link set")
                return "skipped"
            if RV.recently_asked(conn, biz["id"], contact["id"], now):
                _done(row["id"], "skipped", "asked for a review in the last 90 days")
                return "skipped"
            review_rid, review = RV.new_link(conn, biz, contact["id"], "automatic")
    res = _deliver(row, biz, contact, text, template, kind, when, link, review, account_id, http, now)
    if shared and res == "sent":
        from . import growth as GR
        with db.tx() as conn:
            GR.link_sent(conn, shared, now)
    if review_rid:
        from . import reviews as RV
        with db.tx() as conn:
            if res == "sent":
                log(conn, biz["id"], "review.asked", "followup", contact["id"], {"how": "automatic"})
            else:
                RV.forget(conn, review_rid)
    return res


def _v18(row: dict, res: tuple) -> str:
    """Record what campaigns/sequences.deliver did with one outbox row."""
    status, why, when, mid = res
    if status == "later":
        _done(row["id"], "pending", why, send_at=when)
        return "later"
    _done(row["id"], status, why, mid)
    if row.get("enrollment_id"):
        from . import sequences as SQ
        SQ.after_send(row, status, why)
    return status


def _deliver(row, biz, contact, text, template, kind, when, link, review, account_id, http, now) -> str:
    from . import inbox as X
    body = _fill(text, name=contact["name"], business=biz["name"], when=when, link=link, review=review)
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
