"""The owner's two regular messages: the morning summary (08:00 their time) and the weekly proof (Monday).

Morning: today's bookings (and who hasn't confirmed), who is waiting for an answer, what she still has to ask —
short, with one-tap choices. Questions the owner put off ("later") come back to be asked again that day.

Weekly proof: ONE number Valeria caused in the last 7 days, and two or three short lines. Only results that can be
traced to something she did are counted, honestly:
  came back      customers who booked, paid or signed within 30 days of a win-back message (Grow's own measure)
  booked in chat bookings made in the chat flow
  kept           bookings confirmed by answering her reminder ("1")
  friends        friends who came through a customer's link
  directory      new customers who wrote from the directory
  reviews        people who opened the review page from her ask (opening is what we can see; Google doesn't say who wrote)
  voice          customer voice notes she wrote down that got an answer
  answered       customer messages she answered (AI or 24/7 answers)
Nothing to show: she says so kindly and suggests one thing to switch on.

Both are sent once (a dedupe row per day / per week), only between 08:00 and 21:00, never while she is quiet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, select, update

from .. import blocks, db
from . import owner as OW

MORNING_UNTIL = 12                                           # a job that was down: no "good morning" at 3 pm
WEEKLY_DAY, WEEKLY_FROM, WEEKLY_UNTIL = 0, 9, 14             # Monday, 09:00–14:00
ORDER = ("came_back", "booked_chat", "kept", "friends", "directory", "reviews", "voice", "answered")


def morning(conn, biz: dict, lang: str, now: datetime) -> tuple[str, list[str]]:
    from .commands import _summary
    text = OW.t("va_morning", lang) + "\n" + _summary(conn, biz, 0, lang, now)
    qs = OW.waiting(conn, biz["id"])
    if qs:
        text += "\n" + OW.t("va_open_questions", lang).format(n=len(qs))
    from .. import kits
    from . import inventory as INV
    low = INV.low(conn, biz["id"]) if kits.visible(biz, "inventory") else []      # v0.20: before it runs out
    if low:
        text += "\n" + OW.t("va_low_stock", lang).format(n=len(low), names=", ".join(INV.label(x) for x in low[:4]))
    if OW.settings(conn, biz["id"])["paused_at"]:
        text += "\n" + OW.t("va_paused_note", lang)
    menu = ["new", "tomorrow", "help"]
    names = [OW.t(f"va_m_{m}", lang) for m in menu]
    OW.ask(conn, biz, "pick", text, {"menu": menu, "names": names}, prompted=True, now=now)   # so "1" means something
    return text, names


def proof(conn, biz: dict, now: datetime, days: int = 7) -> dict:
    """The attributable results of the last `days` days."""
    from . import reminders as RM
    from . import voice as VC
    from .growth import CAME_BACK_DAYS
    bid = biz["id"]
    since = now - timedelta(days=days)
    E, SM, AU, AP, PY, Q, RR, RF, C = (db.events, db.scheduled_messages, db.automations, db.appointments, db.payments, db.quotes,
                                       db.review_requests, db.referrals, db.contacts)
    win = select(AU.c.id).where(AU.c.business_id == bid, AU.c.preset == "winback")
    sent = select(SM.c.contact_id, SM.c.send_at).where(SM.c.business_id == bid, SM.c.status == "sent", SM.c.automation_id.in_(win),
                                                       SM.c.send_at > since - timedelta(days=CAME_BACK_DAYS)).subquery()
    end = sent.c.send_at + timedelta(days=CAME_BACK_DAYS)
    back = (exists().where(and_(AP.c.business_id == bid, AP.c.contact_id == sent.c.contact_id, AP.c.status != "cancelled",
                                AP.c.created_at > sent.c.send_at, AP.c.created_at <= end, AP.c.created_at >= since))
            | exists().where(and_(PY.c.business_id == bid, PY.c.contact_id == sent.c.contact_id, PY.c.status == "paid",
                                  PY.c.paid_at > sent.c.send_at, PY.c.paid_at <= end, PY.c.paid_at >= since))
            | exists().where(and_(Q.c.business_id == bid, Q.c.contact_id == sent.c.contact_id, Q.c.status == "accepted",
                                  Q.c.signed_at > sent.c.send_at, Q.c.signed_at <= end, Q.c.signed_at >= since)))
    came_back = conn.execute(select(func.count(func.distinct(sent.c.contact_id))).where(back)).scalar()

    def events(*types):
        return conn.execute(select(func.count()).select_from(E).where(E.c.business_id == bid, E.c.type.in_(types),
                                                                      E.c.created_at >= since)).scalar()
    out = {"came_back": came_back, "booked_chat": events("booking.chat"), "kept": RM.kept(conn, bid, since),
           "friends": conn.execute(select(func.count()).select_from(RF).where(RF.c.business_id == bid, RF.c.created_at >= since)).scalar(),
           "directory": conn.execute(select(func.count()).select_from(C).where(C.c.business_id == bid, C.c.source == "directory",
                                                                               C.c.created_at >= since)).scalar(),
           "reviews": conn.execute(select(func.count()).select_from(RR).where(RR.c.business_id == bid, RR.c.clicked_at >= since)).scalar(),
           "voice": VC.answered(conn, bid, since), "answered": events("ai.replied", "answers.replied")}
    return out


def headline(p: dict) -> tuple[str | None, int]:
    best = max(ORDER, key=lambda k: (p.get(k) or 0, -ORDER.index(k)))
    return (best, p[best]) if p.get(best) else (None, 0)


def suggestion(conn, biz: dict) -> str:
    """One thing to switch on when the week had nothing to show."""
    from . import growth as GR
    from . import reminders as RM
    if blocks.on(biz, "calendar") and not RM.rule(conn, biz["id"]):
        return "va_try_reminders"
    if blocks.on(biz, "grow") and not GR.ready_made(conn, biz["id"], "winback"):
        return "va_try_winback"
    if not biz.get("review_url"):
        return "va_try_reviews"
    return "va_try_post"


def weekly(conn, biz: dict, lang: str, now: datetime) -> str:
    p = proof(conn, biz, now)
    key, n = headline(p)
    if not key:
        return OW.t("va_week_nothing", lang) + "\n" + OW.t(suggestion(conn, biz), lang)
    lines = [OW.t(f"va_week_{key}", lang).format(n=n)]
    for k in ORDER:
        if k != key and p.get(k):
            lines.append("· " + OW.t(f"va_wline_{k}", lang).format(n=p[k]))
        if len(lines) >= 4:
            break
    return "\n".join(lines)


def round_(now: datetime | None = None, http=None) -> dict:
    """Job: the morning summary (once a day) and the weekly proof (once a week), to each linked owner."""
    now = now or datetime.now(timezone.utc)
    stats = {"morning": 0, "weekly": 0}
    ON, AA = db.owner_numbers, db.assistant_actions
    with db.tx() as conn:
        bids = [r[0] for r in conn.execute(select(ON.c.business_id).where(ON.c.active, ON.c.channel.isnot(None), ON.c.role == "owner")
                                           .group_by(ON.c.business_id))]
    for bid in bids:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            s = OW.settings(conn, bid)
            local = now.astimezone(ZoneInfo(biz["timezone"]))
            if not OW.may_speak(conn, biz, s, now):
                continue
            todo = []
            if s["morning"] and local.hour < MORNING_UNTIL and OW.ask(
                    conn, biz, "summary", "morning", {}, dedupe=f"morning:{local.date().isoformat()}", now=now) is not None:
                conn.execute(update(AA).where(AA.c.business_id == bid, AA.c.dedupe == f"morning:{local.date().isoformat()}")
                             .values(status="done", done_at=now, prompted_at=now))
                conn.execute(update(AA).where(AA.c.business_id == bid, AA.c.status == "later").values(status="asked", prompted_at=None))
                todo.append("morning")
            iso = local.isocalendar()
            if (s["weekly"] and local.weekday() == WEEKLY_DAY and WEEKLY_FROM <= local.hour < WEEKLY_UNTIL
                    and OW.ask(conn, biz, "proof", "weekly", {}, dedupe=f"weekly:{iso[0]}-W{iso[1]:02d}", now=now) is not None):
                conn.execute(update(AA).where(AA.c.business_id == bid, AA.c.dedupe == f"weekly:{iso[0]}-W{iso[1]:02d}")
                             .values(status="done", done_at=now, prompted_at=now))
                todo.append("weekly")
            people = [p for p in OW.owners(conn, bid) if p["role"] == "owner"]
            msgs = []
            for what in todo:
                lang = OW.lang_of(biz, people[0]) if people else biz["language"]
                if what == "morning":
                    msgs.append(morning(conn, biz, lang, now))
                else:
                    msgs.append((weekly(conn, biz, lang, now), None))
            if msgs:
                OW.spoke(conn, biz, now)
        for text, opts in msgs:
            for p in people:
                OW.say(biz, p, text, opts, http=http, now=now)
        for what in todo:
            stats[what] += 1
    return stats
