"""Staff (ars01, v0.20): the team, the weekly schedule, clocking in and out, timesheets and what each person earned.

A team member is a name with a phone and, when they have their own sign-in to the business (Settings › Team), that
sign-in — then they clock in and out from their own phone. Pay is written down as the owner agreed it: per hour, per
day worked, and/or a share of what they sold at the register (commission on the sale before tax; a refunded or voided
sale earns nothing). The pay summary is hours × rate etc., in the business's currency — no tax, no withholding, no
filing: that is for the business's accountant or payroll service, and the screen says so.

Hours are never silently changed: an owner's fix keeps the old times in the entry's history with the reason, and an
entry is voided (kept, not counted), never deleted. The schedule is shared by the owner tapping a WhatsApp link —
nothing goes out by itself.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db, money, packs, phones
from . import Invalid, NotFound, log

SM, SH, CE = db.staff_members, db.shifts, db.clock_entries
MAX_SHIFT_HOURS = 24


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


def _tz(biz: dict) -> ZoneInfo:
    return ZoneInfo(biz["timezone"])


def today_of(biz: dict) -> date:
    return datetime.now(timezone.utc).astimezone(_tz(biz)).date()


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _money(v, field: str) -> int | None:
    if v in (None, ""):
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise Invalid("an amount, 0 or more", field)
    return v


# ---------------------------------------------------------------- the team
def _member_vals(conn: Connection, biz: dict, data: dict, partial: bool) -> dict:
    vals: dict = {}
    if not partial or "name" in data:
        vals["name"] = " ".join(str(data.get("name") or "").split())[:200]
        if not vals["name"]:
            raise Invalid("their name", "name")
    if not partial or "phone" in data:
        raw = str(data.get("phone") or "").strip()
        if raw:
            try:
                raw = phones.e164(raw, packs.country(biz["country"]))
            except phones.BadPhone:
                raise Invalid("that phone number doesn't look right", "phone") from None
        vals["phone"] = raw
    if not partial or "job" in data:
        vals["job"] = str(data.get("job") or "").strip()[:80]
    for k in ("hourly_minor", "per_shift_minor"):
        if not partial or k in data:
            vals[k] = _money(data.get(k), k)
    if not partial or "commission_bp" in data:
        c = data.get("commission_bp")
        if c not in (None, "") and (not isinstance(c, int) or isinstance(c, bool) or not 0 <= c <= 10_000):
            raise Invalid("commission: from 0 to 100 %", "commission")
        vals["commission_bp"] = None if c in (None, "") else c
    if not partial or "user_id" in data:
        uid = data.get("user_id") or None
        if uid and not conn.execute(select(db.memberships.c.user_id).where(db.memberships.c.business_id == biz["id"],
                                                                           db.memberships.c.user_id == uid)).first():
            raise NotFound("team sign-in")
        vals["user_id"] = uid
    if "active" in data:
        vals["active"] = bool(data["active"])
    return vals


def add_member(conn: Connection, biz: dict, actor: str, data: dict) -> dict:
    vals = _member_vals(conn, biz, data, partial=False)
    if vals.get("user_id") and for_user(conn, biz["id"], vals["user_id"]):
        raise Invalid("that sign-in is already on the team", "user_id")
    row = dict(conn.execute(insert(SM).values(business_id=biz["id"], **vals).returning(SM)).mappings().one())
    log(conn, biz["id"], "staff.added", actor, row["id"], {"name": row["name"]})
    return row


def member(conn: Connection, business_id: UUID, staff_id: UUID) -> dict:
    r = conn.execute(select(SM).where(SM.c.business_id == business_id, SM.c.id == staff_id)).mappings().first()
    if not r:
        raise NotFound("team member")
    return dict(r)


def members(conn: Connection, business_id: UUID, *, active_only: bool = True) -> list[dict]:
    q = select(SM).where(SM.c.business_id == business_id)
    if active_only:
        q = q.where(SM.c.active)
    return [dict(r) for r in conn.execute(q.order_by(func.lower(SM.c.name))).mappings()]


def patch_member(conn: Connection, biz: dict, actor: str, staff_id: UUID, data: dict) -> dict:
    cur = member(conn, biz["id"], staff_id)
    vals = _member_vals(conn, biz, data, partial=True)
    if vals.get("user_id") and str(vals["user_id"]) != str(cur["user_id"]) and for_user(conn, biz["id"], vals["user_id"]):
        raise Invalid("that sign-in is already on the team", "user_id")
    if vals:
        conn.execute(update(SM).where(SM.c.id == staff_id).values(**vals))
        log(conn, biz["id"], "staff.updated", actor, staff_id, {"fields": sorted(vals)})
    return member(conn, biz["id"], staff_id)


def for_user(conn: Connection, business_id: UUID, user_id) -> dict | None:
    if not user_id:
        return None
    r = conn.execute(select(SM).where(SM.c.business_id == business_id, SM.c.user_id == user_id)).mappings().first()
    return dict(r) if r else None


def sign_ins(conn: Connection, business_id: UUID) -> list[dict]:
    """The business's sign-ins (Settings › Team) a team member can be linked to."""
    U, M = db.users, db.memberships
    return [dict(r) for r in conn.execute(select(U.c.id, U.c.name, U.c.email, M.c.role).join(M, M.c.user_id == U.c.id)
                                          .where(M.c.business_id == business_id).order_by(func.lower(U.c.email))).mappings()]


# ---------------------------------------------------------------- the schedule
def _hm(v, field: str) -> time:
    if isinstance(v, time):
        return v
    try:
        h, m = str(v).strip().split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        raise Invalid("a time, e.g. 09:00", field) from None


def shift_minutes(starts: time, ends: time) -> int:
    """Ends earlier than it starts = it ends the next day (a night shift)."""
    a, b = starts.hour * 60 + starts.minute, ends.hour * 60 + ends.minute
    return b - a if b > a else b + 24 * 60 - a


def add_shift(conn: Connection, biz: dict, actor: str, staff_id: UUID, day, starts, ends, note: str = "") -> dict:
    member(conn, biz["id"], staff_id)
    if not isinstance(day, date):
        try:
            day = date.fromisoformat(str(day))
        except ValueError:
            raise Invalid("pick a day", "day") from None
    s, e = _hm(starts, "starts"), _hm(ends, "ends")
    if s == e:
        raise Invalid("the shift starts and ends at the same time", "ends")
    row = dict(conn.execute(insert(SH).values(business_id=biz["id"], staff_id=staff_id, day=day, starts=s, ends=e,
                                              note=(note or "").strip()[:200], created_by=actor[:100]).returning(SH)).mappings().one())
    log(conn, biz["id"], "staff.shift_added", actor, row["id"], {"staff_id": str(staff_id), "day": day.isoformat()})
    return row


def remove_shift(conn: Connection, biz: dict, actor: str, shift_id: UUID) -> None:
    r = conn.execute(SH.delete().where(SH.c.business_id == biz["id"], SH.c.id == shift_id).returning(SH.c.staff_id, SH.c.day)).first()
    if not r:
        raise NotFound("shift")
    log(conn, biz["id"], "staff.shift_removed", actor, shift_id, {"staff_id": str(r.staff_id), "day": r.day.isoformat()})


def week(conn: Connection, business_id: UUID, start: date, staff_id: UUID | None = None) -> list[dict]:
    q = select(SH).where(SH.c.business_id == business_id, SH.c.day >= start, SH.c.day < start + timedelta(days=7))
    if staff_id:
        q = q.where(SH.c.staff_id == staff_id)
    return [dict(r) for r in conn.execute(q.order_by(SH.c.day, SH.c.starts)).mappings()]


def copy_week(conn: Connection, biz: dict, actor: str, src: date, dst: date) -> int:
    """Last week's shifts again, a week later — but never a shift that is already there."""
    have = {(s["staff_id"], s["day"], s["starts"], s["ends"]) for s in week(conn, biz["id"], dst)}
    active = {m["id"] for m in members(conn, biz["id"])}
    n = 0
    for s in week(conn, biz["id"], src):
        d = dst + (s["day"] - src)
        if s["staff_id"] in active and (s["staff_id"], d, s["starts"], s["ends"]) not in have:
            conn.execute(insert(SH).values(business_id=biz["id"], staff_id=s["staff_id"], day=d, starts=s["starts"], ends=s["ends"],
                                           note=s["note"], created_by=actor[:100]))
            n += 1
    log(conn, biz["id"], "staff.week_copied", actor, None, {"from": src.isoformat(), "to": dst.isoformat(), "shifts": n})
    return n


def share_link(biz: dict, m: dict, shifts: list[dict], lang: str, fmt_day) -> str:
    """wa.me link with this person's week, for the owner to tap (their phone, or pick the chat)."""
    lines = [_t("sf_share_head", lang).format(name=m["name"].split(" ")[0], business=biz["name"])]
    for s in shifts:
        lines.append(f"· {fmt_day(s['day'])} {s['starts'].strftime('%H:%M')}–{s['ends'].strftime('%H:%M')}"
                     + (f" ({s['note']})" if s["note"] else ""))
    if not shifts:
        lines.append(_t("sf_share_none", lang))
    digits = "".join(ch for ch in (m["phone"] or "") if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(chr(10).join(lines))}"


# ---------------------------------------------------------------- clock in / out
def open_entry(conn: Connection, business_id: UUID, staff_id: UUID) -> dict | None:
    r = conn.execute(select(CE).where(CE.c.business_id == business_id, CE.c.staff_id == staff_id, CE.c.out_at.is_(None),
                                      CE.c.voided.is_(False)).order_by(CE.c.in_at.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def clock(conn: Connection, biz: dict, actor: str, staff_id: UUID, now: datetime | None = None) -> dict:
    """In if they are out, out if they are in (one button on their phone)."""
    m = member(conn, biz["id"], staff_id)
    if not m["active"]:
        raise Invalid("not on the team any more", "staff")
    conn.execute(select(SM.c.id).where(SM.c.id == staff_id).with_for_update())      # two taps at once: one entry
    now = now or datetime.now(timezone.utc)
    cur = open_entry(conn, biz["id"], staff_id)
    if cur:
        if now <= cur["in_at"]:
            raise Invalid("clocked in just now; try again in a minute", "clock")
        conn.execute(update(CE).where(CE.c.id == cur["id"]).values(out_at=now))
        log(conn, biz["id"], "staff.clock_out", actor, cur["id"], {"staff_id": str(staff_id)})
        return {**cur, "out_at": now}
    row = dict(conn.execute(insert(CE).values(business_id=biz["id"], staff_id=staff_id, in_at=now, source="self").returning(CE))
               .mappings().one())
    log(conn, biz["id"], "staff.clock_in", actor, row["id"], {"staff_id": str(staff_id)})
    return row


def _when(biz: dict, v, field: str) -> datetime | None:
    """A local date-time from a form ("2026-10-30T09:00") -> aware UTC."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=_tz(biz))
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=_tz(biz)).astimezone(timezone.utc)
    except ValueError:
        raise Invalid("a day and a time", field) from None


def _check(in_at: datetime, out_at: datetime | None) -> None:
    if out_at is not None and out_at <= in_at:
        raise Invalid("out must be after in", "out_at")
    if out_at is not None and out_at - in_at > timedelta(hours=MAX_SHIFT_HOURS):
        raise Invalid(f"more than {MAX_SHIFT_HOURS} hours in one go: check the day", "out_at")
    if in_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise Invalid("that time hasn't come yet", "in_at")


def entry(conn: Connection, business_id: UUID, entry_id: UUID) -> dict:
    r = conn.execute(select(CE).where(CE.c.business_id == business_id, CE.c.id == entry_id)).mappings().first()
    if not r:
        raise NotFound("hours")
    return dict(r)


def _why(note: str, lang: str) -> str:
    note = " ".join((note or "").split())[:300]
    if not note:
        raise Invalid(_t("sf_why", lang), "note")
    return note


def add_entry(conn: Connection, biz: dict, actor: str, staff_id: UUID, in_at, out_at, note: str) -> dict:
    """The owner writes hours someone forgot to clock (with the reason)."""
    member(conn, biz["id"], staff_id)
    note = _why(note, biz["language"])
    a, b = _when(biz, in_at, "in_at"), _when(biz, out_at, "out_at")
    if a is None or b is None:
        raise Invalid("in and out", "in_at")
    _check(a, b)
    row = dict(conn.execute(insert(CE).values(business_id=biz["id"], staff_id=staff_id, in_at=a, out_at=b, source="owner", note=note)
                            .returning(CE)).mappings().one())
    log(conn, biz["id"], "staff.hours_added", actor, row["id"], {"staff_id": str(staff_id), "note": note})
    return row


def fix_entry(conn: Connection, biz: dict, actor: str, entry_id: UUID, *, in_at=None, out_at=None, void: bool = False,
              note: str = "") -> dict:
    """Change the times (or void it) — the reason is required and the old times stay in its history."""
    cur = conn.execute(select(CE).where(CE.c.business_id == biz["id"], CE.c.id == entry_id).with_for_update()).mappings().first()
    if not cur:
        raise NotFound("hours")
    note = _why(note, biz["language"])
    if cur["voided"]:
        raise Invalid("already voided", "entry")
    a = _when(biz, in_at, "in_at") or cur["in_at"]
    b = _when(biz, out_at, "out_at") if out_at not in (None, "") else cur["out_at"]
    if not void:
        _check(a, b)
    hist = list(cur["history"] or []) + [{"at": datetime.now(timezone.utc).isoformat(), "by": actor[:100], "note": note,
                                          "in_at": cur["in_at"].isoformat(), "out_at": cur["out_at"].isoformat() if cur["out_at"] else None,
                                          "void": bool(void)}]
    vals = {"history": hist, **({"voided": True} if void else {"in_at": a, "out_at": b})}
    conn.execute(update(CE).where(CE.c.id == entry_id).values(**vals))
    log(conn, biz["id"], "staff.hours_voided" if void else "staff.hours_fixed", actor, entry_id, {"note": note})
    return entry(conn, biz["id"], entry_id)


def entries(conn: Connection, biz: dict, start: date, end: date, staff_id: UUID | None = None) -> list[dict]:
    """Entries that began between start and end (the business's own days, both included), newest first."""
    tz = _tz(biz)
    a = datetime.combine(start, time(0), tz)
    b = datetime.combine(end + timedelta(days=1), time(0), tz)
    q = (select(CE, SM.c.name.label("staff_name")).join(SM, SM.c.id == CE.c.staff_id)
         .where(CE.c.business_id == biz["id"], CE.c.in_at >= a, CE.c.in_at < b))
    if staff_id:
        q = q.where(CE.c.staff_id == staff_id)
    return [dict(r) for r in conn.execute(q.order_by(CE.c.in_at.desc())).mappings()]


def minutes(e: dict) -> int:
    return int((e["out_at"] - e["in_at"]).total_seconds() // 60) if e["out_at"] and not e["voided"] else 0


# ---------------------------------------------------------------- timesheets and pay
def timesheet(conn: Connection, biz: dict, start: date, end: date) -> list[dict]:
    """Per person, start..end: minutes worked (finished, not voided entries), days worked, their sales at the register
    (before tax; paid ones — refunded and voided earn nothing) and the pay those make at their agreed rates."""
    if end < start:
        raise Invalid("the period ends before it starts", "end")
    if (end - start).days > 366:
        raise Invalid("one year at most", "end")
    tz = _tz(biz)
    out = []
    es = entries(conn, biz, start, end)
    PS = db.pos_sales
    sales = dict(conn.execute(select(PS.c.staff_id, func.coalesce(func.sum(PS.c.total_minor - PS.c.tax_minor), 0))
                              .where(PS.c.business_id == biz["id"], PS.c.status == "paid", PS.c.currency == biz["currency"],
                                     PS.c.day >= start, PS.c.day <= end, PS.c.staff_id.isnot(None))
                              .group_by(PS.c.staff_id)).all())
    for m in members(conn, biz["id"], active_only=False):
        mine = [e for e in es if e["staff_id"] == m["id"]]
        mins = sum(minutes(e) for e in mine)
        days = len({e["in_at"].astimezone(tz).date() for e in mine if minutes(e)})
        sold = int(sales.get(m["id"], 0))
        if not m["active"] and not mins and not sold:
            continue
        hours_pay = _per_hour(mins, m["hourly_minor"] or 0)
        day_pay = days * (m["per_shift_minor"] or 0)
        commission = money.part(sold, m["commission_bp"] or 0)
        out.append({"member": m, "minutes": mins, "days": days, "open": any(e["out_at"] is None and not e["voided"] for e in mine),
                    "sales_minor": sold, "hours_pay_minor": hours_pay, "day_pay_minor": day_pay, "commission_minor": commission,
                    "total_minor": hours_pay + day_pay + commission})
    return out


def _per_hour(mins: int, rate_minor: int) -> int:
    """minutes × the hourly rate ÷ 60, rounded half up once (on the total, never per entry)."""
    from ..money import _half_up
    return _half_up(mins * rate_minor, 60)


def timesheet_csv(conn: Connection, biz: dict, actor: str, start: date, end: date) -> str:
    import csv
    import io
    from .csvio import _cell
    cur = biz["currency"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "job", "from", "to", "hours", "days", "hourly_rate", "hours_pay", "per_day", "days_pay", "sales_before_tax",
                "commission_pct", "commission", "total", "currency"])
    for r in timesheet(conn, biz, start, end):
        m = r["member"]
        w.writerow([_cell(m["name"]), _cell(m["job"]), start.isoformat(), end.isoformat(), f"{r['minutes'] // 60}:{r['minutes'] % 60:02d}",
                    r["days"], money.to_major(m["hourly_minor"], cur), money.to_major(r["hours_pay_minor"], cur),
                    money.to_major(m["per_shift_minor"], cur), money.to_major(r["day_pay_minor"], cur),
                    money.to_major(r["sales_minor"], cur), money.to_major(m["commission_bp"], "USD") if m["commission_bp"] is not None else "",
                    money.to_major(r["commission_minor"], cur), money.to_major(r["total_minor"], cur), cur])
    log(conn, biz["id"], "staff.timesheet_exported", actor, biz["id"], {"from": start.isoformat(), "to": end.isoformat()})
    return out.getvalue()
