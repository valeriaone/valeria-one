"""What Valeria's actions DO, once the owner said yes (or the kind is granted "do it yourself").

Each handler gets the action row (claimed: status running) and returns (ok, the line for the owner). Everything
that reaches a customer goes through the same doors as the rest of Valeria One — inbox.send (consent, quiet
hours, STOP, WhatsApp's window), the follow-up outbox, payments.create with the business's OWN payment account,
posts (drafts the owner publishes) — so no rule is skipped because Valeria was the one asking.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from .. import config, db, money, packs
from . import Blocked, Invalid, NotFound, log
from . import owner as OW

CHOICES = {"holiday": ["post", "post_offer", "no"], "post": ["now", "schedule", "edit"], "reschedule": ["cancel_link", "myself"]}
DEFAULT_CHOICES = ["yes", "no", "later"]


def choices(kind: str) -> list[str]:
    return CHOICES.get(kind, DEFAULT_CHOICES)


def question(biz: dict, row: dict, lang: str) -> tuple[str, list[str]]:
    """The question as the owner reads it, with its tap answers."""
    opts = [OW.t(f"va_c_{c}", lang) for c in choices(row["kind"])]
    return row["summary"], opts


def _fmt(biz: dict, minor: int) -> str:
    return money.fmt(int(minor), biz["currency"], packs.country(biz["country"]).locale)


def _send(biz: dict, contact_id: UUID, text: str, *, kind: str = "service", account_id: UUID | None = None, http=None,
          now: datetime | None = None) -> dict:
    from . import inbox as X
    from .followups import _pick_account
    with db.tx() as conn:
        acc = account_id or _pick_account(conn, biz, contact_id)
    if not acc:
        raise Blocked("no_channel", "no channel to reach this customer")
    msg = X.send(biz, "valeria", contact_id=contact_id, body=text, kind=kind, channel_account_id=acc, http=http, now=now)
    if msg["status"] in ("failed", "blocked"):
        raise Blocked("failed", msg.get("error") or "the message didn't go out")
    return msg


def _name(conn, business_id, contact_id) -> str:
    n = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == contact_id)).scalar() if contact_id else None
    return (n or "—").split(" ")[0] or "—"


# ---------------------------------------------------------------- the handlers
def _reply(biz, a, lang, http, now):
    p = a["payload"]
    text = (p.get("text") or "").strip()
    if not text:
        return False, OW.t("va_no_text", lang)
    acc = None
    if p.get("conversation_id"):
        with db.tx() as conn:
            acc = conn.execute(select(db.conversations.c.channel_account_id).where(
                db.conversations.c.id == UUID(p["conversation_id"]), db.conversations.c.business_id == biz["id"])).scalar()
    _send(biz, a["contact_id"], text, account_id=acc, http=http, now=now)
    if p.get("draft") and p["draft"] != text:                 # the owner changed her draft: she learns from it
        from . import style
        with db.tx() as conn:
            style.record(conn, biz["id"], "assistant", p["draft"], text)
    with db.tx() as conn:
        return True, OW.t("va_sent_to", lang).format(name=_name(conn, biz["id"], a["contact_id"]))


def _review(biz, a, lang, http, now):
    from . import reviews as RV
    sent, skipped = 0, 0
    for cid in a["payload"].get("contact_ids") or []:
        try:
            RV.ask(biz, "valeria", UUID(cid), now=now)
            sent += 1
        except (Invalid, Blocked, NotFound):
            skipped += 1
    return sent > 0 or not skipped, OW.t("va_reviews_sent", lang).format(n=sent)


def _promo(biz, a, lang, http, now):
    """The win-back, now, to who is ready for it (Grow's rules) — or the owner's own offer to the regulars."""
    p = a["payload"]
    if p.get("mode") == "offer":
        return _offer(biz, a, lang, p.get("text") or "", now)
    from . import growth as GR
    with db.tx() as conn:
        rule = GR.ready_made(conn, biz["id"], "winback") or GR.set_winback(conn, biz, a["by"], True)
        ids = GR._candidates(conn, biz, GR.settings(conn, biz["id"]), rule, now)
        for cid in ids:
            conn.execute(insert(db.scheduled_messages).values(business_id=biz["id"], automation_id=rule["id"], contact_id=cid, send_at=now))
        if ids:
            log(conn, biz["id"], "winback.queued", "valeria", rule["id"], {"count": len(ids)})
    return True, OW.t("va_promo_queued", lang).format(n=len(ids))


def _offer(biz, a, lang, text, now):
    """One offer to the regulars, as a campaign through the outbox (slow day, holiday offer)."""
    from . import followups as FU
    from . import slowday as SD
    from . import style
    text = (text or "").strip()
    if not text:
        return False, OW.t("va_no_text", lang)
    with db.tx() as conn:
        text = style.apply(style.get(conn, biz["id"]), text)
        ids = SD.regulars(conn, biz, now)
        cp = FU.create_campaign(conn, biz, "valeria", name=OW.t("va_offer_name", biz["language"])[:120], message=text, send_at=now)
        from . import growth as GR
        wb = GR.ready_made(conn, biz["id"], "winback")
        conn.execute(db.campaigns.update().where(db.campaigns.c.id == cp["id"]).values(
            status="done", counts={"audience": len(ids)}, template=(wb or {}).get("template")))
        for cid in ids:
            conn.execute(insert(db.scheduled_messages).values(business_id=biz["id"], campaign_id=cp["id"], contact_id=cid, send_at=now))
        log(conn, biz["id"], "campaign.sent", "valeria", cp["id"], {"audience": len(ids), "by": "valeria"})
    return True, OW.t("va_promo_queued", lang).format(n=len(ids))


def offer_text(biz: dict, conn, key: str = "va_offer_msg") -> str:
    """The owner's own offer line (Grow), or none (just the invitation + the booking link): never an invented discount."""
    from . import growth as GR
    s = GR.settings(conn, biz["id"])
    link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
    base = " ".join(OW.t(key, biz["language"]).format(business=biz["name"], offer=s["offer"] or "").split(" ")).replace("  ", " ")
    return (base + (" " + link if link else "")).strip()


def _slowday(biz, a, lang, http, now):
    with db.tx() as conn:
        text = a["payload"].get("text") or offer_text(biz, conn, "va_slow_msg")
    return _offer(biz, a, lang, text, now)


def _holiday(biz, a, lang, http, now):
    """A post for the holiday (a draft in Posts) — and with post_offer, an offer waiting as a question."""
    from . import ai as AI
    from . import posts as PO
    p = a["payload"]
    choice = p.get("choice") or "post"
    if choice == "no":
        return True, OW.t("va_ok", lang)
    with db.tx() as conn:
        nets = [n["network"] for n in PO.networks(conn, biz) if n["connected"] and not n["reconnect"] and n["network"] != "instagram"]
    nets = nets or ["facebook"]
    idea = OW.t("va_holiday_idea", biz["language"]).format(name=p.get("name") or p.get("key"), date=p.get("date"))
    try:
        texts, by_ai = PO.draft(biz, "valeria", kind="holiday", idea=idea, nets=nets, http=http), True
    except (AI.AIError, Invalid):
        line = OW.t("va_holiday_post", biz["language"]).format(name=p.get("name") or "", business=biz["name"])
        texts, by_ai = {n: line for n in nets}, False
    with db.tx() as conn:
        from . import style
        if not by_ai:
            texts = {n: style.apply(style.get(conn, biz["id"]), v) for n, v in texts.items()}
        post = PO.create(conn, biz, "valeria", kind="holiday", idea=idea, nets=nets, texts=texts, by_ai=by_ai)
        first = next(iter(texts.values()), "")
        OW.ask(conn, biz, "post", OW.t("va_q_post", biz["language"]).format(text=first[:600]),
               {"post_id": str(post["id"]), "day": p.get("date")}, number_id=a["number_id"], by="valeria", now=now)
        if choice == "post_offer":
            OW.ask(conn, biz, "promo", OW.t("va_q_offer", biz["language"]).format(text=offer_text(biz, conn)),
                   {"mode": "offer", "text": offer_text(biz, conn)}, number_id=a["number_id"], now=now)
    return True, OW.t("va_post_drafted", lang)


def _post(biz, a, lang, http, now):
    from . import posts as PO
    p = a["payload"]
    pid = UUID(p["post_id"])
    choice = p.get("choice") or "now"
    if choice == "edit":
        return True, OW.t("va_post_edit_link", lang).format(link=f"{config.PUBLIC_URL()}/app/posts/{pid}")
    try:
        if choice == "now":
            post = PO.publish_now(biz, "valeria", pid, http=http, now=now)
            ok = post["status"] == "published"
            return ok, OW.t("va_post_published" if ok else "va_post_failed", lang).format(
                link=f"{config.PUBLIC_URL()}/app/posts/{pid}")
        tz = ZoneInfo(biz["timezone"])
        day = datetime.fromisoformat(p["day"]).date() if p.get("day") else (now.astimezone(tz).date() + timedelta(days=1))
        when = datetime.combine(day, time(9), tz)
        if when <= now:
            when = datetime.combine(now.astimezone(tz).date() + timedelta(days=1), time(9), tz)
        with db.tx() as conn:
            PO.approve(conn, biz, "valeria", pid, when, now)
        return True, OW.t("va_post_scheduled", lang).format(when=when.strftime("%Y-%m-%d %H:%M"))
    except Invalid as e:
        return False, OW.t("va_post_cant", lang).format(why=str(e), link=f"{config.PUBLIC_URL()}/app/posts/{pid}")


def _paylink(biz, a, lang, http, now):
    from . import payments as PM
    p = a["payload"]
    try:
        pay = PM.create(biz, "valeria", amount_minor=int(p["amount_minor"]), description=p.get("description") or "—",
                        contact_id=a["contact_id"], http=http)
    except Invalid as e:
        return False, str(e)
    except PM.PayError as e:
        return False, str(e)
    with db.tx() as conn:
        first = _name(conn, biz["id"], a["contact_id"])
    text = OW.t("va_paylink_msg", biz["language"]).format(name=first, amount=_fmt(biz, pay["amount_minor"]),
                                                          what=pay["description"], link=pay["url"] or pay["page_url"])
    _send(biz, a["contact_id"], text, http=http, now=now)
    return True, OW.t("va_paylink_sent", lang).format(name=first, amount=_fmt(biz, pay["amount_minor"]))


REMIND_EVERY_DAYS = 7


def _pay_reminder(biz, a, lang, http, now):
    """A kind reminder for each unpaid link (at most once a week per payment)."""
    PY, E = db.payments, db.events
    sent = 0
    for pid in a["payload"].get("payment_ids") or []:
        with db.tx() as conn:
            pay = conn.execute(select(PY).where(PY.c.business_id == biz["id"], PY.c.id == UUID(pid), PY.c.status == "pending")).mappings().first()
            if not pay or not pay["contact_id"]:
                continue
            if conn.execute(select(E.c.id).where(E.c.business_id == biz["id"], E.c.type == "payment.reminded", E.c.subject_id == pay["id"],
                                                 E.c.created_at > now - timedelta(days=REMIND_EVERY_DAYS))).first():
                continue
            first = _name(conn, biz["id"], pay["contact_id"])
        text = OW.t("va_payrem_msg", biz["language"]).format(name=first, amount=_fmt(biz, pay["amount_minor"]), what=pay["description"],
                                                             link=pay["url"] or f"{config.PUBLIC_URL()}/p/{pay['token']}", business=biz["name"])
        try:
            _send(biz, pay["contact_id"], text, http=http, now=now)
        except Blocked:
            continue
        with db.tx() as conn:
            log(conn, biz["id"], "payment.reminded", "valeria", pay["id"])
        sent += 1
    return True, OW.t("va_payrem_sent", lang).format(n=sent)


def _loyalty(biz, a, lang, http, now):
    _send(biz, a["contact_id"], a["payload"].get("text") or "", kind="marketing", http=http, now=now)
    with db.tx() as conn:
        return True, OW.t("va_sent_to", lang).format(name=_name(conn, biz["id"], a["contact_id"]))


def _appt(conn, biz, aid):
    AP = db.appointments
    r = conn.execute(select(AP).where(AP.c.business_id == biz["id"], AP.c.id == UUID(str(aid)))).mappings().first()
    if not r:
        raise NotFound("appointment")
    return dict(r)


def _move(biz, a, lang, http, now):
    from . import reminders as RM
    AP = db.appointments
    p = a["payload"]
    st = datetime.fromisoformat(p["starts_at"])
    with db.tx() as conn:
        ap = _appt(conn, biz, p["appointment_id"])
        if ap["status"] not in ("booked", "confirmed"):
            return False, OW.t("va_appt_gone", lang)
        en = st + (ap["ends_at"] - ap["starts_at"])
        clash = conn.execute(select(AP.c.id).where(AP.c.business_id == biz["id"], AP.c.id != ap["id"], AP.c.status.in_(("booked", "confirmed")),
                                                   AP.c.starts_at < en, AP.c.ends_at > st)).first()
        if clash:
            return False, OW.t("va_appt_clash", lang)
        conn.execute(AP.update().where(AP.c.id == ap["id"]).values(starts_at=st, ends_at=en, synced_at=None))
        log(conn, biz["id"], "appointment.moved", "valeria", ap["id"], {"from": ap["starts_at"].isoformat(), "to": st.isoformat()})
    if ap["contact_id"] and p.get("tell", True):
        try:
            _send(biz, ap["contact_id"], OW.t("va_moved_msg", biz["language"]).format(
                when=RM.when_text(biz, st), business=biz["name"]), http=http, now=now)
        except Blocked:
            return True, OW.t("va_moved_not_told", lang).format(when=RM.when_text(biz, st, lang))
    return True, OW.t("va_moved", lang).format(when=RM.when_text(biz, st, lang))


def _cancel(biz, a, lang, http, now):
    from . import deals as D
    from . import reminders as RM
    p = a["payload"]
    with db.tx() as conn:
        ap = _appt(conn, biz, p["appointment_id"])
        if ap["status"] in ("cancelled", "done"):
            return False, OW.t("va_appt_gone", lang)
        D.set_appointment_status(conn, biz["id"], "valeria", ap["id"], "cancelled")
    told = ""
    if ap["contact_id"] and p.get("tell", True):
        link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
        msg = OW.t("va_cancel_msg", biz["language"]).format(when=RM.when_text(biz, ap["starts_at"]), business=biz["name"])
        if link and p.get("link"):
            msg += "\n" + OW.t("va_book_again", biz["language"]).format(link=link)
        try:
            _send(biz, ap["contact_id"], msg, http=http, now=now)
        except Blocked:
            told = " " + OW.t("va_not_told", lang)
    return True, OW.t("va_cancelled", lang) + told


def _block(biz, a, lang, http, now):
    from . import deals as D
    p = a["payload"]
    st = datetime.fromisoformat(p["starts_at"])
    with db.tx() as conn:
        D.book(conn, biz, "valeria", {"title": OW.t("va_blocked_title", biz["language"]), "starts_at": st,
                                      "ends_at": st + timedelta(minutes=int(p.get("minutes") or 60)), "allow_overlap": True})
    from . import reminders as RM
    return True, OW.t("va_blocked", lang).format(when=RM.when_text(biz, st, lang), minutes=int(p.get("minutes") or 60))


def _reschedule(biz, a, lang, http, now):
    if (a["payload"].get("choice") or "cancel_link") == "myself":
        return True, OW.t("va_ok", lang)
    return _cancel(biz, {**a, "payload": {**a["payload"], "link": True}}, lang, http, now)


def _hours(biz, a, lang, http, now):
    from . import booking as BK
    with db.tx() as conn:
        cur = dict(conn.execute(select(db.businesses.c.hours).where(db.businesses.c.id == biz["id"])).mappings().one())["hours"] or {}
        new = {**cur, **(a["payload"].get("hours") or {})}
        new = {k: v for k, v in new.items() if v}
        BK.save_settings(conn, biz, "valeria", hours=new)
    return True, OW.t("va_hours_saved", lang)


def _info(biz, a, lang, http, now):
    """A fact from the owner ("closed on the 2nd") added to what the AI and the answers know."""
    from sqlalchemy import update
    note = (a["payload"].get("text") or "").strip()
    if not note:
        return False, OW.t("va_no_text", lang)
    AI = db.ai_settings
    tz = ZoneInfo(biz["timezone"])
    line = f"[{now.astimezone(tz).date().isoformat()}] {note}"
    with db.tx() as conn:
        cur = conn.execute(select(AI.c.instructions).where(AI.c.business_id == biz["id"])).scalar()
        if cur is None:                                        # no AI set up yet: the notes wait for it (AI stays off)
            conn.execute(insert(AI).values(business_id=biz["id"], provider="gemini", mode="off", instructions=line))
        else:
            conn.execute(update(AI).where(AI.c.business_id == biz["id"]).values(instructions=(cur + "\n" + line).strip()[-4000:]))
        log(conn, biz["id"], "ai.updated", "valeria", biz["id"], {"note": True})
    return True, OW.t("va_info_saved", lang)


HANDLERS = {"reply": _reply, "review_ask": _review, "promo": _promo, "slowday": _slowday, "holiday": _holiday, "post": _post,
            "paylink": _paylink, "pay_reminder": _pay_reminder, "loyalty": _loyalty, "booking_move": _move,
            "booking_cancel": _cancel, "block_time": _block, "reschedule": _reschedule, "hours": _hours, "info": _info}


def run(a: dict, http=None, now: datetime | None = None) -> bool:
    """Do one claimed action; the result is kept on it, and a failure is told to whoever asked for it."""
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == a["business_id"])).mappings().one())
        number = conn.execute(select(db.owner_numbers).where(db.owner_numbers.c.id == a["number_id"])).mappings().first() if a["number_id"] else None
    lang = OW.lang_of(biz, dict(number) if number else None)
    fn = HANDLERS.get(a["kind"])
    try:
        ok, say = fn(biz, a, lang, http, now) if fn else (False, "?")
    except Blocked as b:
        ok, say = False, OW.t("va_blocked_send", lang).format(why=str(b))
    except (Invalid, NotFound, ValueError, KeyError) as e:
        ok, say = False, OW.t("va_failed", lang).format(why=str(e) or type(e).__name__)
    OW.finish(a["id"], ok, {"say": say}, now)
    with db.tx() as conn:
        log(conn, biz["id"], "assistant.done" if ok else "assistant.failed", "valeria", a["id"], {"kind": a["kind"]})
    if not ok and number and number["channel"]:
        OW.say(biz, dict(number), say, http=http, now=now)
    return ok
