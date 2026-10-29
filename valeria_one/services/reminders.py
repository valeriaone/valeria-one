"""Reminders that cut no-shows: "See you tomorrow at 10:00 — reply 1 to confirm, 2 to change the time."

Built on the follow-up rules that already existed (trigger appointment.reminder, the same outbox, consent and
WhatsApp-window rules): switching it on makes ONE ready-made rule (preset reminder, N hours before — 24 unless the
owner says otherwise) and schedules it for appointments already booked. The customer's answer is read in every
language ("1", "sí", "confirm", "はい"… / "2", "cambiar", "reagendar"…), only when it is the whole message and only
within 72 hours of a reminder that went out for an appointment still coming:

* 1 -> the appointment is confirmed (and counted in the weekly proof: they kept their booking);
* 2 -> she tells the customer someone will write with a new time, and asks the owner (a cancellation always asks):
  "Ana wants to change Tuesday 10:00. 1 Cancel it and send her the booking link 2 I'll handle it".

Bookings still unconfirmed the day they happen are in the owner's morning summary.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_time
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, db, packs
from . import log

AU, SM, AP = db.automations, db.scheduled_messages, db.appointments
WINDOW_HOURS = 72
YES = {"1", "yes", "y", "confirm", "confirmed", "ok", "okay", "si", "sí", "confirmo", "confirmado", "sim", "confirmar",
       "oui", "je confirme", "ja", "bestätigt", "bestatigt", "oo", "opo", "sige", "kumpirmado", "ya", "iya", "konfirmasi",
       "हाँ", "हां", "haan", "ji", "जी", "पक्का", "نعم", "أكيد", "تأكيد", "はい", "確認", "是", "好", "确认", "確認しました"}
CHANGE = {"2", "change", "reschedule", "move", "cambiar", "reagendar", "cambio", "mover", "remarcar", "mudar", "changer",
          "décaler", "decaler", "verschieben", "ändern", "andern", "palitan", "ilipat", "ganti", "ubah", "jadwal ulang",
          "बदलें", "बदलना", "बदलो", "تغيير", "غير", "変更", "変えたい", "改期", "改时间", "更改"}


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().strip(".!¡?¿ ").split())


def answer_of(text: str) -> str | None:
    """'confirm' | 'change' | None — only when the whole message is the answer."""
    t = _norm(text)
    return "confirm" if t in YES else "change" if t in CHANGE else None


def rule(conn: Connection, business_id: UUID) -> dict | None:
    r = conn.execute(select(AU).where(AU.c.business_id == business_id, AU.c.preset == "reminder", AU.c.active)
                     .order_by(AU.c.created_at).limit(1)).mappings().first()
    return dict(r) if r else None


def message(biz: dict) -> str:
    from .owner import t
    return t("va_reminder_msg", biz["language"])


def set_on(conn: Connection, biz: dict, actor: str, on: bool, hours: int | None = None, now: datetime | None = None) -> dict | None:
    """Switch the reminder on (one rule; appointments already booked get it too) or off."""
    from . import followups as FU
    from . import owner as OW
    now = now or datetime.now(timezone.utc)
    cur = rule(conn, biz["id"])
    if not on:
        if cur:
            FU.delete_automation(conn, biz["id"], actor, cur["id"])
            log(conn, biz["id"], "reminders.off", actor, cur["id"])
        return None
    h = int(hours or OW.settings(conn, biz["id"])["reminder_hours"] or 24)
    OW.save_settings(conn, biz, actor, reminder_hours=h)
    if cur and cur["minutes"] == h * 60:
        return cur
    if cur:
        FU.delete_automation(conn, biz["id"], actor, cur["id"])
    r = FU.create_automation(conn, biz, actor, trigger="appointment.reminder", minutes=h * 60, message=message(biz),
                             kind="service", preset="reminder")
    for ap in conn.execute(select(AP.c.id, AP.c.starts_at, AP.c.contact_id).where(
            AP.c.business_id == biz["id"], AP.c.status.in_(("booked", "confirmed")), AP.c.contact_id.isnot(None),
            AP.c.starts_at > now + timedelta(minutes=h * 60))).all():        # already booked: they get it too
        conn.execute(pg_insert(SM).values(business_id=biz["id"], automation_id=r["id"], contact_id=ap.contact_id, subject_id=ap.id,
                                          send_at=ap.starts_at - timedelta(minutes=h * 60))
                     .on_conflict_do_nothing(constraint="uq_sched_auto"))
    log(conn, biz["id"], "reminders.on", actor, r["id"], {"hours": h})
    return r


def _sent_for(conn: Connection, business_id: UUID, contact_id: UUID, now: datetime):
    """The appointment a reminder went out for, to this customer, still to come (the latest)."""
    return conn.execute(select(AP).join(SM, SM.c.subject_id == AP.c.id).join(AU, AU.c.id == SM.c.automation_id)
                        .where(SM.c.business_id == business_id, SM.c.contact_id == contact_id, SM.c.status == "sent",
                               AU.c.preset == "reminder", SM.c.send_at > now - timedelta(hours=WINDOW_HOURS),
                               AP.c.starts_at > now, AP.c.status.in_(("booked", "confirmed")))
                        .order_by(SM.c.send_at.desc()).limit(1)).mappings().first()


def when_text(biz: dict, dt: datetime, lang: str | None = None) -> str:
    tz = ZoneInfo(biz["timezone"])
    pack = packs.country(biz["country"])
    loc = pack.locale if not lang or pack.locale.startswith(lang) else lang
    try:
        return format_date(dt.astimezone(tz), "EEEE d MMMM", locale=loc) + " " + format_time(dt.astimezone(tz), "short", locale=loc)
    except Exception:                                          # noqa: BLE001 — an odd locale: the plain one
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def reply(business_id: UUID, conversation_id: UUID, http=None, now: datetime | None = None) -> str | None:
    """A customer answered a reminder? -> confirmed | change | None (not an answer to a reminder)."""
    from . import inbox as X
    from . import owner as OW
    from .ai import detect_language
    from .owner import t
    now = now or datetime.now(timezone.utc)
    M, CV = db.messages, db.conversations
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        if not blocks.on(biz, "calendar"):
            return None
        conv = conn.execute(select(CV).where(CV.c.id == conversation_id, CV.c.business_id == business_id)).mappings().first()
        last = conn.execute(select(M.c.direction, M.c.body).where(M.c.conversation_id == conversation_id)
                            .order_by(M.c.created_at.desc()).limit(1)).first()
        if not conv or not last or last.direction != "in" or (conv["flow"] or None):
            return None
        ans = answer_of(last.body)
        if not ans:
            return None
        ap = _sent_for(conn, business_id, conv["contact_id"], now)
        if not ap:
            return None
        lang = detect_language(last.body) or biz["language"]
        if ans == "confirm":
            if ap["status"] == "booked":
                conn.execute(update(AP).where(AP.c.id == ap["id"]).values(status="confirmed", updated_at=func.now()))
                log(conn, business_id, "appointment.confirmed", "customer", ap["id"], {"via": "reminder"})
            text = t("va_rem_thanks", lang).format(when=when_text(biz, ap["starts_at"], lang))
        else:
            name = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == conv["contact_id"])).scalar() or "—"
            OW.ask(conn, biz, "reschedule", t("va_q_reschedule", biz["language"]).format(
                name=name.split(" ")[0] or "—", when=when_text(biz, ap["starts_at"])),
                {"appointment_id": str(ap["id"]), "conversation_id": str(conversation_id)}, contact_id=conv["contact_id"],
                dedupe=f"reschedule:{ap['id']}", now=now)
            log(conn, business_id, "appointment.change_asked", "customer", ap["id"], {"via": "reminder"})
            text = t("va_rem_change", lang)
    try:
        X.send(biz, "valeria", contact_id=conv["contact_id"], body=text, channel_account_id=conv["channel_account_id"], http=http, now=now)
    except Exception:                                          # noqa: BLE001 — the answer is on record either way
        pass
    return "confirmed" if ans == "confirm" else "change"


def unconfirmed(conn: Connection, biz: dict, day) -> list[dict]:
    """That day's bookings whose reminder went out but which nobody confirmed (the morning summary)."""
    tz = ZoneInfo(biz["timezone"])
    start = datetime.combine(day, time(0), tz)
    C = db.contacts
    rows = conn.execute(select(AP.c.id, AP.c.starts_at, AP.c.title, C.c.name).join(C, C.c.id == AP.c.contact_id)
                        .join(SM, SM.c.subject_id == AP.c.id).join(AU, AU.c.id == SM.c.automation_id)
                        .where(AP.c.business_id == biz["id"], AP.c.status == "booked", AP.c.starts_at >= start,
                               AP.c.starts_at < start + timedelta(days=1), SM.c.status == "sent", AU.c.preset == "reminder")
                        .order_by(AP.c.starts_at)).mappings().all()
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(dict(r))
    return out


def kept(conn: Connection, business_id: UUID, since: datetime) -> int:
    """Bookings confirmed by answering the reminder since `since` (the weekly proof's 'kept their booking')."""
    E = db.events
    return conn.execute(select(func.count()).select_from(E).where(E.c.business_id == business_id, E.c.type == "appointment.confirmed",
                                                                  E.c.actor == "customer", E.c.created_at >= since)).scalar()
