"""Slow-day rescue: "Tomorrow looks quiet (2 bookings, usually 6). Send a last-minute offer to your regulars?"

Once a day (from 16:00 the business's time) she compares tomorrow's bookings with a usual day like it: the median
of the same weekday over the last 8 weeks (only weeks that had bookings that day). Tomorrow is "quiet" when a
usual day has at least 3 bookings and tomorrow has half of that or fewer. Then she ASKS — offers always ask, she
never sends one on her own in this release. On yes, the offer goes to regulars only (2 visits or more in the last
180 days) and with Grow's own rules: promotions consent, never someone who unsubscribed, nobody who got a win-back
within Grow's gap or any offer in the last 30 days, at most Grow's daily cap — through the same outbox (quiet
hours, WhatsApp's window and template).

The offer line is the owner's own (Grow › offer) or, with none, just "we have free times tomorrow" and the booking
link: she never invents a discount.
"""
from __future__ import annotations

import statistics
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .. import blocks, db

AP = db.appointments
FROM_HOUR = 16
WEEKS = 8
MIN_USUAL = 3
REGULAR_VISITS, REGULAR_DAYS = 2, 180
OFFER_GAP_DAYS = 30


def _count(conn, business_id, start: datetime) -> int:
    return conn.execute(select(func.count()).select_from(AP).where(
        AP.c.business_id == business_id, AP.c.status.in_(("booked", "confirmed", "done", "no_show")),
        AP.c.contact_id.isnot(None), AP.c.starts_at >= start, AP.c.starts_at < start + timedelta(days=1))).scalar()


def look(conn, biz: dict, now: datetime) -> dict:
    """Tomorrow vs a usual day like it -> {"tomorrow", "usual", "quiet", "day"}."""
    tz = ZoneInfo(biz["timezone"])
    day = now.astimezone(tz).date() + timedelta(days=1)
    start = datetime.combine(day, time(0), tz)
    past = [_count(conn, biz["id"], start - timedelta(weeks=w)) for w in range(1, WEEKS + 1)]
    seen = [n for n in past if n > 0]
    usual = statistics.median(seen) if len(seen) >= 3 else 0
    n = _count(conn, biz["id"], start)
    return {"day": day, "tomorrow": n, "usual": usual, "quiet": usual >= MIN_USUAL and n <= usual / 2}


def regulars(conn, biz: dict, now: datetime) -> list:
    """Who gets the offer: regulars who agreed to promotions, reachable, not offered lately (Grow's rules)."""
    from . import growth as GR
    C, SM, CP = db.contacts, db.scheduled_messages, db.campaigns
    s = GR.settings(conn, biz["id"])
    visits = (select(func.count()).where(AP.c.business_id == biz["id"], AP.c.contact_id == C.c.id,
                                         AP.c.status.in_(("booked", "confirmed", "done")),
                                         AP.c.starts_at > now - timedelta(days=REGULAR_DAYS), AP.c.starts_at < now)
              .scalar_subquery())
    offered = (select(SM.c.id).join(CP, CP.c.id == SM.c.campaign_id).where(
        SM.c.business_id == biz["id"], SM.c.contact_id == C.c.id, SM.c.created_at > now - timedelta(days=OFFER_GAP_DAYS))
        .exists())
    rule = GR.ready_made(conn, biz["id"], "winback") or {"preset": "winback"}
    q = (select(C.c.id).where(C.c.business_id == biz["id"], C.c.deleted_at.is_(None), C.c.marketing_consent,
                              C.c.unsubscribed_at.is_(None), visits >= REGULAR_VISITS, ~offered,
                              GR._reachable(biz["id"], GR.avoid_channels(rule)),
                              ~GR._won_back_lately(biz["id"], now - timedelta(days=s["gap_days"])))
         .order_by(visits.desc()).limit(s["daily_cap"]))
    return [r[0] for r in conn.execute(q)]


def round_(now: datetime | None = None, http=None) -> dict:
    """Job: once a day per business with Valeria linked; a question when tomorrow looks quiet."""
    from . import owner as OW
    now = now or datetime.now(timezone.utc)
    stats = {"asked": 0}
    ON = db.owner_numbers
    with db.tx() as conn:
        bids = [r[0] for r in conn.execute(select(ON.c.business_id).where(ON.c.active, ON.c.channel.isnot(None))
                                           .group_by(ON.c.business_id))]
    for bid in bids:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            s = OW.settings(conn, bid)
            if (not s["slowday"] or s["paused_at"] or not blocks.on(biz, "assistant") or not blocks.on(biz, "calendar")
                    or now.astimezone(ZoneInfo(biz["timezone"])).hour < FROM_HOUR):
                continue
            lk = look(conn, biz, now)
            if not lk["quiet"]:
                continue
            usual = int(round(lk["usual"]))
            if OW.ask(conn, biz, "slowday", OW.t("va_q_slowday", biz["language"]).format(n=lk["tomorrow"], usual=usual),
                      {"day": lk["day"].isoformat(), "tomorrow": lk["tomorrow"], "usual": usual},
                      dedupe=f"slowday:{lk['day'].isoformat()}", now=now):
                stats["asked"] += 1
    return stats
