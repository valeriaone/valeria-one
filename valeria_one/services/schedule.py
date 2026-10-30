"""The posting calendar (ars02, v0.21): every planned post on every network the business has — published by the jobs on
its OWN connected Facebook Page, Instagram and Google profile; on the others (TikTok, WhatsApp status, LinkedIn, X, or a
network not connected) the owner is reminded at the time with the words ready to copy (services/posts.py "by hand").
Days and times are the business's own (its time zone). Nothing goes on the calendar without the owner's tap.

Best times are rules, not AI and not anyone's data: common windows per network, and — when the business has enough
messages of its own — the hour its customers write most.
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from .. import db

P = db.posts
# common windows people see posts in (local time): (weekdays 0=Mon..6, from hour, to hour)
WINDOWS = {"facebook": ((0, 1, 2, 3, 4), 9, 13), "instagram": ((0, 1, 2, 3, 4), 11, 13), "google": ((0, 1, 2, 3, 4), 8, 10),
           "tiktok": ((0, 1, 2, 3, 4, 5, 6), 19, 22), "whatsapp": ((0, 1, 2, 3, 4, 5, 6), 8, 9),
           "linkedin": ((1, 2, 3), 8, 10), "x": ((0, 1, 2, 3, 4), 8, 10)}
OWN_MIN = 20                     # messages from customers in the last 60 days before "your customers write most at …" counts


def month(conn: Connection, biz: dict, y: int, m: int) -> dict:
    """The month as weeks of days (Monday first), each day with its posts (planned, out, waiting to be posted by hand)."""
    tz = ZoneInfo(biz["timezone"])
    first = date(y, m, 1)
    last = date(y, m, _cal.monthrange(y, m)[1])
    start = datetime.combine(first, time(0), tz)
    end = datetime.combine(last + timedelta(days=1), time(0), tz)
    when = func.coalesce(P.c.publish_at, P.c.published_at)
    rows = [dict(r) for r in conn.execute(select(P).where(P.c.business_id == biz["id"], P.c.status != "cancelled",
                                                          when >= start, when < end).order_by(when)).mappings()]
    by_day: dict = {}
    for r in rows:
        at = (r["publish_at"] or r["published_at"]).astimezone(tz)
        r["local"] = at
        r["hand_due"] = [n for n in r["by_hand"] if (r["results"].get(n) or {}).get("hand") and not r["results"][n].get("done_at")]
        by_day.setdefault(at.date(), []).append(r)
    weeks, d = [], first - timedelta(days=first.weekday())
    while d <= last:
        weeks.append([{"day": d + timedelta(days=i), "in": (d + timedelta(days=i)).month == m,
                       "posts": by_day.get(d + timedelta(days=i), [])} for i in range(7)])
        d += timedelta(days=7)
    return {"weeks": weeks, "first": first, "count": len(rows)}


def own_hour(conn: Connection, business_id: UUID, tz: str, now: datetime | None = None) -> int | None:
    """The hour (business time) its customers write most, from its own last 60 days — None when there is too little."""
    now = now or datetime.now(timezone.utc)
    M = db.messages
    hour = func.extract("hour", func.timezone(tz, M.c.created_at))
    rows = conn.execute(select(hour, func.count()).where(M.c.business_id == business_id, M.c.direction == "in",
                                                         M.c.created_at >= now - timedelta(days=60)).group_by(hour)).all()
    if sum(n for _, n in rows) < OWN_MIN:
        return None
    return int(max(rows, key=lambda r: (r[1], -r[0]))[0])


def hints(conn: Connection, biz: dict, nets: list[str]) -> list[dict]:
    """[{"network", "days", "from", "to"}] for the networks shown, plus {"network": None, "hour"} from the business's own data."""
    out = [{"network": n, "days": WINDOWS[n][0], "from": WINDOWS[n][1], "to": WINDOWS[n][2]} for n in nets if n in WINDOWS]
    h = own_hour(conn, biz["id"], biz["timezone"])
    if h is not None:
        out.insert(0, {"network": None, "hour": h})
    return out


def next_good(biz: dict, network: str, now: datetime | None = None) -> datetime:
    """The next start of the network's good window from now (business time) — a sensible default for "when"."""
    tz = ZoneInfo(biz["timezone"])
    days, h, _ = WINDOWS.get(network, ((0, 1, 2, 3, 4), 10, 12))
    t = (now or datetime.now(timezone.utc)).astimezone(tz)
    for i in range(8):
        d = (t + timedelta(days=i)).date()
        at = datetime.combine(d, time(h), tz)
        if d.weekday() in days and at > t + timedelta(minutes=10):
            return at
    return t + timedelta(days=1)
