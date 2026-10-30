"""Valeria for the owner: the business's own WhatsApp (or Telegram, SMS, the web) becomes the owner's assistant.

Who is trusted. Only the people the owner registered: their own phone (typed, or linked by sending a one-time
code "V-482913" to the business's chat) or chat id. A message from a registered number is a COMMAND — it never
becomes a customer, a conversation or an inbox row; everyone else is a customer as before. Staff numbers carry
per-person permissions (see bookings yes, send promos no). Anyone can claim to be anyone on SMS, so a number
only ever gets what its permissions allow, and money, prices, promos and cancellations always ask first.

How she acts. Anything that goes to a customer is a question first ("Send this to Juan? 1 yes 2 no 3 later"),
until the owner grants that kind of action "do it yourself" (per kind, stored, revocable). Money, prices, promos
and cancellations always ask in this release. Approved sends wait UNDO_SECONDS before going out, so "undo"
stops anything not yet sent; "Valeria, stop" pauses everything she does on her own (and cancels what was waiting
to go out on her own); "resume" switches her back on. Every question and action is a row in assistant_actions —
her honest log ("what did you do today?").

Never nag. One question at a time, between 08:00 and 21:00 the business's time; two unanswered in a row and she
goes quiet until the next day (the morning summary picks up what is still open).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, channels, crypto, db, packs, phones
from . import Invalid, NotFound, log

ON, AS, AA, AM = db.owner_numbers, db.assistant_settings, db.assistant_actions, db.assistant_messages
CHANNELS = ("whatsapp", "whatsapp_twilio", "sms", "telegram", "viber", "sandbox")   # where an owner can talk to her
PHONE = ("whatsapp", "whatsapp_twilio", "sms")               # a number registered as "phone" is trusted on all of these
PERMS = ("view", "reply", "bookings", "promos", "money")     # what a staff number may ask for (an owner: everything)
STAFF_DEFAULT = ["view"]
# what each kind of action needs, for staff
KIND_PERM = {"reply": "reply", "review_ask": "promos", "promo": "promos", "slowday": "promos", "holiday": "promos",
             "post": "promos", "loyalty": "promos", "paylink": "money", "pay_reminder": "money", "booking_move": "bookings",
             "booking_cancel": "bookings", "block_time": "bookings", "reschedule": "bookings", "hours": "owner",
             "info": "owner", "summary": "view", "proof": "view", "pick": "view",
             "notice": "view",                               # v0.16: a recipe tells the owner something ("Ana is waiting")
             "came": "bookings", "sold": "reply"}         # sch01: "did Juan come?" -> "mark the sofa sold?"
ALWAYS_ASK = ("promo", "slowday", "holiday", "paylink", "pay_reminder", "booking_cancel", "reschedule", "hours", "info")
GRANTABLE = ("reply", "review_ask", "loyalty", "booking_move", "block_time")
CUSTOMER = ("reply", "review_ask", "promo", "slowday", "paylink", "pay_reminder", "loyalty", "booking_move",
            "booking_cancel", "reschedule", "sold")          # sold tells the item's other buyers                  # these reach a customer: approved ones wait UNDO_SECONDS
UNDO_SECONDS = 60
PAIR_MINUTES = 30
OPEN_FROM, OPEN_UNTIL = 8, 21                                # she only speaks up first between 08:00 and 21:00 local
NAG_LIMIT = 2                                                # unanswered questions in a row before she goes quiet
ASK_DAYS = 3                                                 # an unanswered question expires after this long
DEFAULTS = {"paused_at": None, "autonomy": {}, "style": {}, "style_learned_on": None, "morning": True, "weekly": True,
            "slowday": True, "holidays": True, "holidays_skip": [], "answers": False, "reminder_hours": 24,
            "loyalty_every": None, "loyalty_reward": "", "unanswered": 0, "quiet_until": None}
_CODE = re.compile(r"\bV-?(\d{6})\b", re.I)


def t(key: str, lang: str | None) -> str:
    from ..web.i18n import t as _t, ui_lang
    return _t(key, ui_lang(lang))


def lang_of(biz: dict, number: dict | None = None) -> str:
    from ..web.i18n import ui_lang
    return ui_lang((number or {}).get("lang") or biz.get("language"))


# ---------------------------------------------------------------- settings
def settings(conn: Connection, business_id: UUID) -> dict:
    r = conn.execute(select(AS).where(AS.c.business_id == business_id)).mappings().first()
    return {**DEFAULTS, **(dict(r) if r else {}), "business_id": business_id}


def _upsert(conn: Connection, business_id: UUID, vals: dict) -> None:
    conn.execute(pg_insert(AS).values(business_id=business_id, **vals)
                 .on_conflict_do_update(index_elements=[AS.c.business_id], set_={**vals, "updated_at": func.now()}))


BOOLS = ("morning", "weekly", "slowday", "holidays", "answers")


def save_settings(conn: Connection, biz: dict, actor: str, **fields) -> dict:
    vals = {k: bool(fields[k]) for k in BOOLS if fields.get(k) is not None}
    if fields.get("reminder_hours") is not None:
        try:
            h = int(fields["reminder_hours"])
        except (TypeError, ValueError):
            raise Invalid("reminder: 1 to 72 hours before", "reminder_hours") from None
        if not 1 <= h <= 72:
            raise Invalid("reminder: 1 to 72 hours before", "reminder_hours")
        vals["reminder_hours"] = h
    if "holidays_skip" in fields and fields["holidays_skip"] is not None:
        vals["holidays_skip"] = sorted({str(k)[:60] for k in fields["holidays_skip"]})
    if vals:
        _upsert(conn, biz["id"], vals)
        log(conn, biz["id"], "assistant.updated", actor, biz["id"], {"fields": sorted(vals)})
    return settings(conn, biz["id"])


def paused(conn: Connection, business_id: UUID) -> bool:
    return bool(settings(conn, business_id)["paused_at"])


def pause(conn: Connection, biz: dict, actor: str, now: datetime | None = None) -> int:
    """'Valeria, stop': nothing on her own until 'resume'. What she queued on her own is cancelled. -> how many."""
    _upsert(conn, biz["id"], {"paused_at": now or datetime.now(timezone.utc)})
    n = conn.execute(update(AA).where(AA.c.business_id == biz["id"], AA.c.status == "queued", AA.c.by == "valeria")
                     .values(status="undone", decided_at=func.now(), result={"why": "stopped"})).rowcount
    log(conn, biz["id"], "assistant.paused", actor, biz["id"], {"cancelled": n})
    return n


def resume(conn: Connection, biz: dict, actor: str) -> None:
    _upsert(conn, biz["id"], {"paused_at": None})
    log(conn, biz["id"], "assistant.resumed", actor, biz["id"])


def grant(conn: Connection, biz: dict, actor: str, kind: str, on: bool) -> dict:
    """'Do it yourself' for one kind of action (or take it back). Money, prices, promos and cancellations: never."""
    if kind not in GRANTABLE:
        raise Invalid("Valeria always asks you first for this", "kind")
    cur = dict(settings(conn, biz["id"])["autonomy"] or {})
    cur[kind] = bool(on)
    _upsert(conn, biz["id"], {"autonomy": cur})
    log(conn, biz["id"], "assistant.autonomy", actor, biz["id"], {"kind": kind, "on": bool(on)})
    return cur


def alone(s: dict, kind: str) -> bool:
    """May she do this kind of thing without asking?"""
    return not s["paused_at"] and kind not in ALWAYS_ASK and kind in GRANTABLE and bool((s["autonomy"] or {}).get(kind))


# ---------------------------------------------------------------- trusted numbers
def numbers(conn: Connection, business_id: UUID, active_only: bool = True) -> list[dict]:
    q = select(ON).where(ON.c.business_id == business_id)
    if active_only:
        q = q.where(ON.c.active)
    return [dict(r) for r in conn.execute(q.order_by(ON.c.created_at)).mappings()]


def _perms(role: str, perms) -> list[str]:
    if role == "owner":
        return list(PERMS)
    return sorted({p for p in (perms if perms is not None else STAFF_DEFAULT) if p in PERMS}) or list(STAFF_DEFAULT)


def add_phone(conn: Connection, biz: dict, actor: str, *, phone: str, name: str = "", role: str = "owner",
              perms: list[str] | None = None, user_id: UUID | None = None, lang: str | None = None) -> dict:
    """The owner types their own number: it is trusted on WhatsApp and SMS of this business."""
    if role not in ("owner", "staff"):
        raise Invalid("owner or staff", "role")
    try:
        e164 = phones.e164(phone, packs.country(biz["country"]))
    except phones.BadPhone:
        raise Invalid("check the phone number", "phone") from None
    row = conn.execute(pg_insert(ON).values(business_id=biz["id"], user_id=user_id, name=(name or "")[:200], channel="phone",
                                            address=e164, role=role, perms=_perms(role, perms), lang=lang)
                       .on_conflict_do_update(constraint="uq_owner_number",
                                              set_={"active": True, "role": role, "perms": _perms(role, perms),
                                                    "name": (name or "")[:200]})
                       .returning(ON)).mappings().one()
    log(conn, biz["id"], "assistant.number_added", actor, row["id"], {"role": role, "channel": "phone"})
    return dict(row)


def start_pairing(conn: Connection, biz: dict, actor: str, *, name: str = "", role: str = "owner",
                  perms: list[str] | None = None, user_id: UUID | None = None, lang: str | None = None,
                  now: datetime | None = None) -> dict:
    """A one-time code ("V-482913") the person sends from their phone to the business's chat: that links it."""
    if role not in ("owner", "staff"):
        raise Invalid("owner or staff", "role")
    now = now or datetime.now(timezone.utc)
    conn.execute(update(ON).where(ON.c.business_id == biz["id"], ON.c.channel.is_(None), ON.c.user_id == user_id)
                 .values(active=False, pair_code=None))           # one open code per person
    for _ in range(10):
        code = f"V-{secrets.randbelow(900000) + 100000}"
        if not conn.execute(select(ON.c.id).where(ON.c.pair_code == code)).first():
            break
    row = conn.execute(insert(ON).values(business_id=biz["id"], user_id=user_id, name=(name or "")[:200], role=role,
                                         perms=_perms(role, perms), lang=lang, pair_code=code,
                                         pair_expires_at=now + timedelta(minutes=PAIR_MINUTES)).returning(ON)).mappings().one()
    log(conn, biz["id"], "assistant.pairing", actor, row["id"], {"role": role})
    return dict(row)


def remove_number(conn: Connection, business_id: UUID, actor: str, number_id: UUID) -> None:
    if not conn.execute(update(ON).where(ON.c.business_id == business_id, ON.c.id == number_id, ON.c.active)
                        .values(active=False, pair_code=None)).rowcount:
        raise NotFound("number")
    log(conn, business_id, "assistant.number_removed", actor, number_id)


def set_perms(conn: Connection, business_id: UUID, actor: str, number_id: UUID, perms: list[str]) -> dict:
    row = conn.execute(select(ON).where(ON.c.business_id == business_id, ON.c.id == number_id, ON.c.active)).mappings().first()
    if not row:
        raise NotFound("number")
    p = _perms(row["role"], perms)
    conn.execute(update(ON).where(ON.c.id == number_id).values(perms=p))
    log(conn, business_id, "assistant.perms", actor, number_id, {"perms": p})
    return {**dict(row), "perms": p}


def may(number: dict | None, kind_or_perm: str) -> bool:
    """Can this person ask for that? None = the web app's owner (already signed in as owner)."""
    if number is None or number["role"] == "owner":
        return True
    need = KIND_PERM.get(kind_or_perm, kind_or_perm)
    return need != "owner" and need in (number["perms"] or [])


def match(conn: Connection, business_id: UUID, channel: str, address: str) -> dict | None:
    """The trusted number this message came from, if it is one."""
    if channel not in CHANNELS:
        return None
    ch = ("phone", channel) if channel in PHONE else (channel,)
    r = conn.execute(select(ON).where(ON.c.business_id == business_id, ON.c.active, ON.c.channel.in_(ch),
                                      ON.c.address == address)).mappings().first()
    return dict(r) if r else None


def _pair(conn: Connection, biz: dict, channel: str, address: str, body: str, now: datetime) -> dict | None:
    m = _CODE.search(body or "")
    if not m or channel not in CHANNELS or (channel == "sandbox" and not biz["sandbox"]):
        return None
    code = f"V-{m.group(1)}"
    row = conn.execute(select(ON).where(ON.c.business_id == biz["id"], ON.c.pair_code == code, ON.c.active,
                                        ON.c.pair_expires_at > now)).mappings().first()
    if not row:
        return None
    ch = "phone" if channel in PHONE else channel
    taken = conn.execute(select(ON.c.id).where(ON.c.business_id == biz["id"], ON.c.channel == ch, ON.c.address == address)).scalar()
    if taken:                                                  # already linked (another code): this code is spent, that row wins
        conn.execute(update(ON).where(ON.c.id == row["id"]).values(active=False, pair_code=None))
        conn.execute(update(ON).where(ON.c.id == taken).values(active=True, role=row["role"], perms=row["perms"]))
        return dict(conn.execute(select(ON).where(ON.c.id == taken)).mappings().one())
    conn.execute(update(ON).where(ON.c.id == row["id"]).values(channel=ch, address=address, pair_code=None, pair_expires_at=None))
    log(conn, biz["id"], "assistant.paired", f"webhook:{channel}", row["id"], {"channel": ch})
    return dict(conn.execute(select(ON).where(ON.c.id == row["id"])).mappings().one())


def inbound(conn: Connection, biz: dict, acc, address: str, it, now: datetime | None = None) -> tuple[bool, UUID | None]:
    """Called for every inbound message before it becomes a customer's. -> (it was the owner's, the stored id to
    answer — None when it was a replay). A pairing code from a new phone links it here and is answered too."""
    now = now or datetime.now(timezone.utc)
    if acc["channel"] not in CHANNELS:
        return False, None
    number, paired = match(conn, biz["id"], acc["channel"], address), False
    if not number:
        number = _pair(conn, biz, acc["channel"], address, it.body or "", now)
        paired = bool(number)
    if not number:
        return False, None
    ins = pg_insert(AM).values(business_id=biz["id"], number_id=number["id"], channel_account_id=acc["id"], direction="in",
                               body=it.body or "", media=it.media or [], provider_id=it.provider_id,
                               status="paired" if paired else "received", created_at=it.at or now)
    if it.provider_id:
        ins = ins.on_conflict_do_nothing(index_elements=[AM.c.business_id, AM.c.provider_id],
                                         index_where=AM.c.provider_id.isnot(None))
    mid = conn.execute(ins.returning(AM.c.id)).scalar()
    if mid:
        conn.execute(update(ON).where(ON.c.id == number["id"]).values(last_seen_at=now))
        _upsert(conn, biz["id"], {"unanswered": 0, "quiet_until": None})   # they wrote: she may speak again
    return True, mid


# ---------------------------------------------------------------- talking to the owner
def _account(conn: Connection, business_id: UUID, number: dict, account_id: UUID | None = None):
    A = db.channel_accounts
    if account_id:
        r = conn.execute(select(A).where(A.c.id == account_id, A.c.business_id == business_id, A.c.active)).mappings().first()
        if r:
            return r
    last = conn.execute(select(AM.c.channel_account_id).where(AM.c.business_id == business_id, AM.c.number_id == number["id"],
                                                              AM.c.direction == "in", AM.c.channel_account_id.isnot(None))
                        .order_by(AM.c.created_at.desc()).limit(1)).scalar()
    if last:
        r = conn.execute(select(A).where(A.c.id == last, A.c.active)).mappings().first()
        if r:
            return r
    wanted = PHONE if number["channel"] == "phone" else (number["channel"],)
    for ch in wanted:
        r = conn.execute(select(A).where(A.c.business_id == business_id, A.c.active, A.c.channel == ch)
                         .order_by(A.c.created_at)).mappings().first()
        if r:
            return r
    return None


def _in_window(conn: Connection, number: dict, acc, now: datetime) -> bool:
    hours = channels.adapter(acc["channel"]).reply_window_hours
    if not hours:
        return True
    last = conn.execute(select(func.max(AM.c.created_at)).where(AM.c.number_id == number["id"], AM.c.direction == "in",
                                                                AM.c.channel_account_id == acc["id"])).scalar()
    return bool(last) and now - last < timedelta(hours=hours)


def numbered(text: str, options: list[str] | None) -> str:
    if not options:
        return text
    return text + "\n\n" + "\n".join(f"{i}) {o}" for i, o in enumerate(options, 1))


def say(biz: dict, number: dict, text: str, options: list[str] | None = None, *, account_id: UUID | None = None,
        http=None, now: datetime | None = None) -> str:
    """Write to the owner on their chat. -> sent | email | failed | none. Outside WhatsApp's 24 h window (she wrote
    first and they haven't written today) it goes to the owner's sign-in email instead, when there is one."""
    now = now or datetime.now(timezone.utc)
    body = numbered(text, options)
    with db.tx() as conn:
        b = conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one()
        from . import billing
        if b["suspended_at"] or billing.paused(dict(b)):
            return "none"
        acc = _account(conn, biz["id"], number, account_id) if number.get("channel") else None
        window = bool(acc) and _in_window(conn, number, acc, now)
        email = None
        if number.get("user_id"):
            email = conn.execute(select(db.users.c.email).where(db.users.c.id == number["user_id"])).scalar()
        cfg = crypto.open_(acc["config"]) if acc else None
    if not acc or not window:
        if email:
            from . import mailer
            mailer.send(email, f"Valeria · {biz['name']}", body)
            _record(biz["id"], number, None, body, "sent", None, "email")
            return "email"
        _record(biz["id"], number, acc["id"] if acc else None, body, "failed", None, "outside the chat window")
        return "failed"
    opts = [{"id": str(i), "title": o[:24], "button": t("va_pick", lang_of(biz, number))}
            for i, o in enumerate(options or [], 1)] if options and len(options) <= 10 else []
    address = number["address"]
    try:
        res = channels.adapter(acc["channel"], http).send(cfg, address, channels.Outbound(to=address, body=body, options=opts))
    except Exception as e:                                   # noqa: BLE001 — provider or network: on record, never a crash
        res = channels.SendResult("failed", error=type(e).__name__)
    _record(biz["id"], number, acc["id"], body, res.status, res.provider_id, res.error)
    return "failed" if res.status == "failed" else "sent"


def _record(business_id, number, account_id, body, status, provider_id, error) -> None:
    with db.tx() as conn:
        st = status if status in ("queued", "sent", "delivered", "read", "failed") else "sent"
        try:
            conn.execute(insert(AM).values(business_id=business_id, number_id=number.get("id"), channel_account_id=account_id,
                                           direction="out", body=body[:4000], status=st, provider_id=provider_id,
                                           media=[{"note": error}] if error else []))
        except Exception:                                    # noqa: BLE001 — a duplicate provider id: the send stands
            pass


def owners(conn: Connection, business_id: UUID, kind: str | None = None) -> list[dict]:
    """The linked people a question of this kind goes to: owners, and staff allowed to answer it."""
    return [n for n in numbers(conn, business_id) if n["channel"] and (n["role"] == "owner" or (kind and may(n, kind)))]


# ---------------------------------------------------------------- questions and actions
def ask(conn: Connection, biz: dict, kind: str, summary: str, payload: dict | None = None, *, contact_id: UUID | None = None,
        number_id: UUID | None = None, by: str = "valeria", dedupe: str | None = None, prompted: bool = False,
        now: datetime | None = None, owner_says_go: bool = False) -> dict | None:
    """A question for the owner — or, when this kind is granted "do it yourself" (or the owner just told her to,
    owner_says_go, for something that never needs to ask), straight into the queue. None = already asked (dedupe)."""
    now = now or datetime.now(timezone.utc)
    s = settings(conn, biz["id"])
    auto = (by == "valeria" and alone(s, kind)) or (owner_says_go and kind not in ALWAYS_ASK)
    vals = {"business_id": biz["id"], "kind": kind, "summary": summary[:2000], "payload": payload or {}, "contact_id": contact_id,
            "number_id": number_id, "by": by, "dedupe": dedupe}
    if auto:
        vals.update(status="queued", decided_at=now, send_at=now + timedelta(seconds=UNDO_SECONDS if kind in CUSTOMER else 0),
                    payload={**(payload or {}), "auto": by == "valeria"})
    elif prompted:
        vals["prompted_at"] = now
    ins = pg_insert(AA).values(**vals)
    if dedupe:
        ins = ins.on_conflict_do_nothing(constraint="uq_assistant_dedupe")
    row = conn.execute(ins.returning(AA)).mappings().first()
    if not row:
        return None
    log(conn, biz["id"], "assistant.queued" if auto else "assistant.asked", by, row["id"], {"kind": kind})
    return dict(row)


def get(conn: Connection, business_id: UUID, action_id: UUID) -> dict:
    r = conn.execute(select(AA).where(AA.c.business_id == business_id, AA.c.id == action_id)).mappings().first()
    if not r:
        raise NotFound("question")
    return dict(r)


def open_question(conn: Connection, business_id: UUID, number: dict | None, now: datetime | None = None) -> dict | None:
    """The question this person was asked last and hasn't answered (what "1", "yes" or "later" refers to)."""
    now = now or datetime.now(timezone.utc)
    q = select(AA).where(AA.c.business_id == business_id, AA.c.status == "asked", AA.c.prompted_at.isnot(None),
                         AA.c.prompted_at > now - timedelta(days=ASK_DAYS))
    if number is not None:
        q = q.where(or_(AA.c.number_id.is_(None), AA.c.number_id == number["id"]))
    for r in conn.execute(q.order_by(AA.c.prompted_at.desc()).limit(10)).mappings():
        if may(number, r["kind"]):
            return dict(r)
    return None


def waiting(conn: Connection, business_id: UUID, number: dict | None = None) -> list[dict]:
    """Every question still waiting for the owner (the Valeria page, the morning summary)."""
    rows = conn.execute(select(AA).where(AA.c.business_id == business_id, AA.c.status.in_(("asked", "later")), AA.c.kind != "pick")
                        .order_by(AA.c.created_at.desc()).limit(50)).mappings()
    return [dict(r) for r in rows if may(number, r["kind"])]


def decide(conn: Connection, biz: dict, action_id: UUID, answer: str, by: str, *, choice: str | None = None,
           text: str | None = None, now: datetime | None = None) -> dict:
    """yes (optionally a choice or the owner's own words) | no | later. Yes = queued (customer sends wait for undo)."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(select(AA).where(AA.c.business_id == biz["id"], AA.c.id == action_id,
                                        AA.c.status.in_(("asked", "later"))).with_for_update()).mappings().first()
    if not row:
        raise Invalid("that question was already answered", "action")
    _upsert(conn, biz["id"], {"unanswered": 0, "quiet_until": None})
    if answer == "no":
        conn.execute(update(AA).where(AA.c.id == action_id).values(status="declined", decided_at=now))
        log(conn, biz["id"], "assistant.declined", by, action_id, {"kind": row["kind"]})
    elif answer == "later":
        conn.execute(update(AA).where(AA.c.id == action_id).values(status="later", prompted_at=None))
    else:
        payload = dict(row["payload"] or {})
        if choice:
            payload["choice"] = choice
        if text:
            payload["text"] = text[:2000]
        wait = UNDO_SECONDS if row["kind"] in CUSTOMER and choice != "edit" else 0
        conn.execute(update(AA).where(AA.c.id == action_id).values(status="queued", decided_at=now, payload=payload,
                                                                    send_at=now + timedelta(seconds=wait)))
        log(conn, biz["id"], "assistant.approved", by, action_id, {"kind": row["kind"]})
    return get(conn, biz["id"], action_id)


def undo(conn: Connection, biz: dict, by: str, now: datetime | None = None) -> list[dict]:
    """Stop everything approved that hasn't gone out yet (the last 30 minutes). -> what was stopped."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(update(AA).where(AA.c.business_id == biz["id"], AA.c.status == "queued",
                                         AA.c.decided_at > now - timedelta(minutes=30))
                        .values(status="undone", result={"why": "undo"}).returning(AA)).mappings().all()
    for r in rows:
        log(conn, biz["id"], "assistant.undone", by, r["id"], {"kind": r["kind"]})
    return [dict(r) for r in rows]


def done_today(conn: Connection, biz: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(biz["timezone"])
    start = datetime.combine(now.astimezone(tz).date(), time(0), tz)
    return [dict(r) for r in conn.execute(select(AA).where(AA.c.business_id == biz["id"], AA.c.created_at >= start)
                                          .order_by(AA.c.created_at)).mappings()]


# ---------------------------------------------------------------- the job: what was approved goes out; questions are asked
def run_due(now: datetime | None = None, http=None, limit: int = 50) -> dict:
    """Approved actions whose undo time is over: claimed once (queued -> running, SKIP LOCKED), then done."""
    from . import actions
    now = now or datetime.now(timezone.utc)
    stats = {"done": 0, "failed": 0}
    with db.tx() as conn:
        pick = (select(AA.c.id).where(AA.c.status == "queued", AA.c.send_at <= now).order_by(AA.c.send_at).limit(limit)
                .with_for_update(skip_locked=True).scalar_subquery())
        rows = [dict(r) for r in conn.execute(update(AA).where(AA.c.id.in_(pick), AA.c.status == "queued")
                                              .values(status="running").returning(AA)).mappings()]
    for r in rows:
        ok = actions.run(r, http=http, now=now)
        stats["done" if ok else "failed"] += 1
    return stats


def run_now(biz: dict, action_id: UUID, http=None, now: datetime | None = None) -> str | None:
    """Run one approved action right away when it needs no undo time (a draft, a calendar block). -> the owner's line."""
    from . import actions
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        r = conn.execute(update(AA).where(AA.c.business_id == biz["id"], AA.c.id == action_id, AA.c.status == "queued",
                                          AA.c.send_at <= now).values(status="running").returning(AA)).mappings().first()
    if not r:
        return None
    actions.run(dict(r), http=http, now=now)
    with db.tx() as conn:
        return (get(conn, biz["id"], action_id)["result"] or {}).get("say")


def finish(action_id: UUID, ok: bool, result: dict, now: datetime | None = None) -> None:
    with db.tx() as conn:
        conn.execute(update(AA).where(AA.c.id == action_id).values(status="done" if ok else "failed", result=result,
                                                                    done_at=now or func.now()))


def _local(biz: dict, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(biz["timezone"]))


def may_speak(conn: Connection, biz: dict, s: dict, now: datetime) -> bool:
    """She writes first only in the day, never while quiet (ignored twice), never when the assistant block is off."""
    if not blocks.on(biz, "assistant"):
        return False
    h = _local(biz, now).hour
    if not OPEN_FROM <= h < OPEN_UNTIL:
        return False
    return not (s["quiet_until"] and s["quiet_until"] > now)


def spoke(conn: Connection, biz: dict, now: datetime) -> None:
    """She asked something: one more unanswered in a row; at the limit she goes quiet until tomorrow."""
    s = settings(conn, biz["id"])
    n = (s["unanswered"] or 0) + 1
    vals = {"unanswered": n}
    if n >= NAG_LIMIT:
        tz = ZoneInfo(biz["timezone"])
        vals["quiet_until"] = datetime.combine(_local(biz, now).date() + timedelta(days=1), time(0), tz)
    _upsert(conn, biz["id"], vals)


def prompt_due(now: datetime | None = None, http=None) -> dict:
    """Ask the owner the oldest question nobody was asked yet — one at a time, never nagging."""
    from . import actions
    now = now or datetime.now(timezone.utc)
    stats = {"asked": 0, "expired": 0}
    with db.tx() as conn:
        stats["expired"] = conn.execute(update(AA).where(AA.c.status == "asked", or_(
            AA.c.created_at < now - timedelta(days=ASK_DAYS),
            (AA.c.kind == "pick") & (AA.c.created_at < now - timedelta(hours=12)))).values(status="expired")).rowcount
        bids = [r[0] for r in conn.execute(select(AA.c.business_id).where(AA.c.status == "asked", AA.c.prompted_at.is_(None))
                                           .group_by(AA.c.business_id).limit(500))]
    for bid in bids:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            s = settings(conn, bid)
            if not may_speak(conn, biz, s, now):
                continue
            if conn.execute(select(AA.c.id).where(AA.c.business_id == bid, AA.c.status == "asked", AA.c.prompted_at.isnot(None),
                                                  AA.c.kind != "pick", AA.c.prompted_at > now - timedelta(hours=24))).first():
                continue                                       # one open question at a time
            row = conn.execute(select(AA).where(AA.c.business_id == bid, AA.c.status == "asked", AA.c.prompted_at.is_(None))
                               .order_by(AA.c.created_at).limit(1).with_for_update(skip_locked=True)).mappings().first()
            if not row:
                continue
            people = owners(conn, bid, row["kind"])
            if row["number_id"]:
                people = [p for p in people if p["id"] == row["number_id"]] or people
            from . import push                                 # v0.18: the installed app is a place to ask too
            if not people and not push.devices(conn, bid):
                continue
            conn.execute(update(AA).where(AA.c.id == row["id"]).values(prompted_at=now))
            spoke(conn, biz, now)
        text, opts = actions.question(biz, dict(row), lang_of(biz, people[0] if people else None))
        for p in people:
            say(biz, p, text, opts, http=http, now=now)
        push.notify(bid, biz["name"], text, "/app/valeria", http=http)
        stats["asked"] += 1
    return stats


def round_(now: datetime | None = None, http=None) -> dict:
    """The job step: approved actions out, questions asked, then the daily things (summary, proof, quiet days, holidays,
    style). Each part on its own: one failing never stops the others."""
    from . import briefing, holidays, slowday, style
    out = {}
    for name, fn in (("run", run_due), ("briefing", briefing.round_), ("slowday", slowday.round_),
                     ("holidays", holidays.round_), ("ask", prompt_due), ("style", style.learn_due)):
        try:
            out[name] = fn(now=now, http=http) if name != "style" else fn(now=now)
        except Exception as e:                                 # noqa: BLE001 — recorded in the jobs heartbeat
            import logging
            logging.getLogger("valeria_one.jobs").exception("assistant %s failed", name)
            out[name] = f"{type(e).__name__}: {e}"[:200]
    return out
