"""Loyalty without cards: every Nth visit gets something — counted for the owner, no card, no app.

A visit is a booking marked done, a payment received, or the owner's tap ("mark a visit"; or "Valeria, Juan came
in"). One per customer per day, whatever the mix. The owner sets ONE rule — every N visits, a reward in their own
words ("10% off", "a free coffee") — and it is OFF until they do. On the Nth visit the reward is written in the
ledger, and Valeria offers to tell the customer ("This is your 5th visit 🎉 Your reward: 10% off") — she asks the
owner first unless loyalty is granted "do it yourself". It goes as a promotion, so only to customers who agreed to
promotions and never to someone who unsubscribed. The owner ticks a reward off when it is used.

A reward is never tied to a review (Google forbids it, like Grow's rewards): the loyalty message never carries
a review link and no review request mentions a reward.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, db
from . import Invalid, NotFound, log

LV, LR = db.loyalty_visits, db.loyalty_rewards
SOURCES = ("booking", "payment", "manual")


def rule(conn: Connection, business_id: UUID) -> dict:
    from .owner import settings
    s = settings(conn, business_id)
    return {"every": s["loyalty_every"], "reward": s["loyalty_reward"] or "", "on": bool(s["loyalty_every"])}


def set_rule(conn: Connection, biz: dict, actor: str, every: int | None, reward: str = "") -> dict:
    """every N visits -> reward. every None/0 = off."""
    from .owner import _upsert
    reward = " ".join((reward or "").split())
    if every:
        try:
            every = int(every)
        except (TypeError, ValueError):
            raise Invalid("every: a number from 2 to 100", "every") from None
        if not 2 <= every <= 100:
            raise Invalid("every: a number from 2 to 100", "every")
        if not reward:
            raise Invalid("write the reward (for example: 10% off)", "reward")
        if len(reward) > 200:
            raise Invalid("reward: at most 200 characters", "reward")
    _upsert(conn, biz["id"], {"loyalty_every": every or None, "loyalty_reward": reward if every else ""})
    log(conn, biz["id"], "loyalty.rule", actor, biz["id"], {"every": every or None})
    return rule(conn, biz["id"])


def count(conn: Connection, business_id: UUID, contact_id: UUID) -> int:
    return conn.execute(select(func.count()).select_from(LV).where(LV.c.business_id == business_id, LV.c.contact_id == contact_id)).scalar()


def visit(conn: Connection, biz: dict, contact_id: UUID, source: str, ref_id: UUID | None = None, actor: str = "system",
          now: datetime | None = None) -> dict | None:
    """Count a visit (once a day per customer). On the Nth: the reward, and the message to the customer offered.
    -> the reward when this visit earned one."""
    if source not in SOURCES:
        raise Invalid("source", "source")
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo(biz["timezone"])).date()
    got = conn.execute(pg_insert(LV).values(business_id=biz["id"], contact_id=contact_id, on_date=day, source=source, ref_id=ref_id)
                       .on_conflict_do_nothing(constraint="uq_loyalty_visit").returning(LV.c.id)).scalar()
    if not got:
        return None
    r = rule(conn, biz["id"])
    if not r["on"] or not blocks.on(biz, "loyalty"):
        return None
    n = count(conn, biz["id"], contact_id)
    if n % r["every"]:
        return None
    rw = conn.execute(pg_insert(LR).values(business_id=biz["id"], contact_id=contact_id, visit_number=n, reward=r["reward"])
                      .on_conflict_do_nothing(constraint="uq_loyalty_reward").returning(LR)).mappings().first()
    if not rw:
        return None
    log(conn, biz["id"], "loyalty.earned", actor, rw["id"], {"contact_id": str(contact_id), "visit": n})
    _offer_message(conn, biz, dict(rw), now)
    return dict(rw)


def _offer_message(conn: Connection, biz: dict, rw: dict, now: datetime) -> None:
    """Tell the customer — asked first (unless granted), and only to someone who agreed to promotions."""
    from . import owner as OW
    from . import style as STY
    C = db.contacts
    c = conn.execute(select(C).where(C.c.id == rw["contact_id"], C.c.deleted_at.is_(None))).mappings().first()
    if not c or not c["marketing_consent"] or c["unsubscribed_at"]:
        return                                                 # the reward stands; no promotion goes to them
    first = (c["name"] or "").split(" ")[0]
    text = OW.t("va_loyalty_msg", biz["language"]).format(name=first, n=rw["visit_number"], reward=rw["reward"], business=biz["name"])
    text = STY.apply(STY.get(conn, biz["id"]), text.replace(" ,", ",").replace(" !", "!"))
    OW.ask(conn, biz, "loyalty", OW.t("va_q_loyalty", biz["language"]).format(name=first or "—", n=rw["visit_number"], text=text),
           {"text": text, "reward_id": str(rw["id"])}, contact_id=c["id"], dedupe=f"loyalty:{rw['id']}", now=now)


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """From services.log, in the event's own transaction: a booking done or a payment received is a visit."""
    if type_ not in ("appointment.done", "payment.paid") or not subject_id:
        return
    if type_ == "appointment.done":
        cid = conn.execute(select(db.appointments.c.contact_id).where(db.appointments.c.id == subject_id)).scalar()
        src = "booking"
    else:
        cid = conn.execute(select(db.payments.c.contact_id).where(db.payments.c.id == subject_id)).scalar()
        src = "payment"
    if not cid:
        return
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
    visit(conn, biz, cid, src, subject_id)


def mark_visit(conn: Connection, biz: dict, actor: str, contact_id: UUID, now: datetime | None = None) -> dict:
    from . import contacts as C
    C.get(conn, biz["id"], contact_id)
    rw = visit(conn, biz, contact_id, "manual", None, actor, now)
    return {"visits": count(conn, biz["id"], contact_id), "reward": rw}


def rewards(conn: Connection, business_id: UUID, *, open_only: bool = False, contact_id: UUID | None = None, limit: int = 100) -> list[dict]:
    C = db.contacts
    q = select(LR, C.c.name.label("contact_name")).join(C, C.c.id == LR.c.contact_id).where(LR.c.business_id == business_id)
    if open_only:
        q = q.where(LR.c.used_at.is_(None))
    if contact_id:
        q = q.where(LR.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(q.order_by(LR.c.created_at.desc()).limit(limit)).mappings()]


def mark_used(conn: Connection, business_id: UUID, actor: str, reward_id: UUID) -> dict:
    r = conn.execute(select(LR).where(LR.c.business_id == business_id, LR.c.id == reward_id)).mappings().first()
    if not r:
        raise NotFound("reward")
    if not r["used_at"]:
        conn.execute(update(LR).where(LR.c.id == reward_id, LR.c.used_at.is_(None)).values(used_at=func.now(), used_by=actor))
        log(conn, business_id, "loyalty.used", actor, reward_id)
    return dict(conn.execute(select(LR).where(LR.c.id == reward_id)).mappings().one())


def top(conn: Connection, business_id: UUID, limit: int = 20) -> list[dict]:
    C = db.contacts
    rows = conn.execute(select(C.c.id, C.c.name, func.count(LV.c.id).label("visits")).join(LV, LV.c.contact_id == C.c.id)
                        .where(LV.c.business_id == business_id, C.c.deleted_at.is_(None)).group_by(C.c.id, C.c.name)
                        .order_by(func.count(LV.c.id).desc()).limit(limit)).mappings()
    return [dict(r) for r in rows]
