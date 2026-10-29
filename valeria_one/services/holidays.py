"""Holiday autopilot: a week before a day that matters (Día de las Madres, Diwali, Eid, Black Friday…), Valeria offers
the owner a post for it — and, if they want, an offer for their regulars.

The calendar is data (packs/holidays.yaml): each country's days, fixed, computed (nth weekday, Easter) or listed
(lunar calendars, 2026–2028, marked approximate). Every morning (from 09:00 the business's time) she looks 7 days
ahead; each holiday is offered once ("holiday:<key>:<date>"). One tap: 1 a post (the AI writes it in the owner's
style via Posts, or a short ready line without AI — it lands in Posts as a draft, the owner publishes or schedules
it), 2 a post and an offer (the offer waits as a question: offers always ask), 3 no. The owner can switch holidays
off, or switch single ones off.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select

from .. import blocks, db

FILE = Path(__file__).resolve().parent.parent / "packs" / "holidays.yaml"
DAYS_AHEAD = 7
FROM_HOUR = 9
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@lru_cache(maxsize=1)
def _data() -> dict:
    return yaml.safe_load(FILE.read_text(encoding="utf-8"))


def easter(year: int) -> date:
    """Western (Gregorian) Easter Sunday — the anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nth_weekday(year: int, month: int, weekday: str, n: int) -> date:
    wd = WEEKDAYS.index(weekday)
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return last - timedelta(days=(last.weekday() - wd) % 7)


def on(h: dict, year: int) -> date | None:
    """The date of one holiday in one year (None when the file has no date for that year)."""
    if "fixed" in h:
        mm, dd = h["fixed"].split("-")
        return date(year, int(mm), int(dd))
    if "nth" in h:
        r = h["nth"]
        return nth_weekday(year, int(r["month"]), r["weekday"], int(r["n"])) + timedelta(days=int(r.get("plus", 0)))
    if "easter" in h:
        return easter(year) + timedelta(days=int(h["easter"]))
    v = (h.get("dates") or {}).get(year) or (h.get("dates") or {}).get(str(year))
    return date.fromisoformat(v) if v else None


def entries(country: str) -> list[dict]:
    d = _data()
    return list(d.get("global") or []) + list((d.get("countries") or {}).get((country or "").upper()) or [])


def name(h: dict, lang: str, local: str | None = None) -> str:
    n = h.get("name") or {}
    return n.get(lang) or (n.get(local) if local else None) or n.get("en") or h["key"]


def upcoming(country: str, start: date, days: int = DAYS_AHEAD) -> list[dict]:
    """Holidays from `start` to `start + days`, soonest first."""
    out = []
    for h in entries(country):
        for y in (start.year, start.year + 1):
            d = on(h, y)
            if d and start <= d <= start + timedelta(days=days):
                out.append({**h, "date": d})
    return sorted(out, key=lambda x: x["date"])


def year_list(country: str, year: int) -> list[dict]:
    """Every holiday of a year (the settings screen, where the owner picks)."""
    out = [{**h, "date": on(h, year)} for h in entries(country)]
    return sorted((x for x in out if x["date"]), key=lambda x: x["date"])


def round_(now: datetime | None = None, http=None) -> dict:
    """Job: once a day per business with Valeria linked, offer the holidays 1–7 days away (each one once)."""
    from zoneinfo import ZoneInfo

    from . import owner as OW
    now = now or datetime.now(timezone.utc)
    stats = {"offered": 0}
    ON = db.owner_numbers
    with db.tx() as conn:
        bids = [r[0] for r in conn.execute(select(ON.c.business_id).where(ON.c.active, ON.c.channel.isnot(None))
                                           .group_by(ON.c.business_id))]
    for bid in bids:
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            s = OW.settings(conn, bid)
            local = now.astimezone(ZoneInfo(biz["timezone"]))
            if not s["holidays"] or s["paused_at"] or not blocks.on(biz, "assistant") or local.hour < FROM_HOUR:
                continue
            for h in upcoming(biz["country"], local.date() + timedelta(days=1), DAYS_AHEAD - 1):
                if h["key"] in (s["holidays_skip"] or []):
                    continue
                lang = biz["language"]
                nm = name(h, OW.lang_of(biz), lang.split("-")[0])
                days = (h["date"] - local.date()).days
                summary = OW.t("va_q_holiday", lang).format(name=nm, days=days) + (" " + OW.t("va_approx", lang) if h.get("approx") else "")
                if OW.ask(conn, biz, "holiday", summary, {"key": h["key"], "date": h["date"].isoformat(), "name": nm,
                                                          "choices": ["post", "post_offer", "no"]},
                          dedupe=f"holiday:{h['key']}:{h['date'].isoformat()}", now=now):
                    stats["offered"] += 1
    return stats
