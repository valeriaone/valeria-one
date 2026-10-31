"""Public booking: a customer opens the business's link, picks a service, a day and a time, and the
appointment is in the calendar. Times are always the business's local time; slots never overlap an
existing appointment (checked again, under a lock, at the moment of booking).

bk01 (v0.26): lanes, seats and party size.
* A lane is who does the booking. A business where nobody "takes bookings" (staff_members.bookable) has ONE lane — the
  business itself — and everything below works exactly as before (the same code path, kept as it was). Once the owner
  says who takes bookings, each of them is a lane: their working time is that week's shifts in the schedule (if they
  have any that week), else their own weekly hours, else the opening hours — always inside the opening hours — minus
  their time off; two people can be booked at 10:00, one person never twice. An appointment with nobody named (every
  appointment made before, the owner's own blocks) is the business's lane and still blocks everyone.
* "Anyone available" picks fairly among the people free then: fill a class already forming first, then whoever has the
  fewest bookings that day, then whoever was given one longest ago.
* A service with capacity N (a class of 12, a restaurant's covers) offers a time while the seats booked over it are
  fewer than N (each booking takes its party size); capacity 1 = as before.
* Every check runs again under the business's lock at the moment of booking, so two people can never take the last
  seat or the same person's time."""
from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import blocks, db, packs, phones
from . import Invalid, NotFound, contacts, deals, gcal, log

BS = db.booking_services
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
STEP_MINUTES = 15
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------- settings
def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:48]
    return s or "negocio"


def clean_hours(hours: dict) -> dict:
    """{"mon": [["09:00","14:00"], ["16:00","19:00"]], …} — validated, sorted, no overlaps."""
    out = {}
    for d in DAYS:
        spans = []
        for span in (hours or {}).get(d) or []:
            if not (isinstance(span, (list, tuple)) and len(span) == 2):
                raise Invalid(f"{d}: each opening needs a start and an end", "hours")
            a, b = (str(x).strip() for x in span)
            if not (_HHMM.match(a) and _HHMM.match(b)) or a >= b:
                raise Invalid(f"{d}: {a}–{b} is not a valid opening (HH:MM, start before end)", "hours")
            spans.append([a, b])
        spans.sort()
        for (a1, b1), (a2, _) in zip(spans, spans[1:]):
            if a2 < b1:
                raise Invalid(f"{d}: openings overlap", "hours")
        if spans:
            out[d] = spans
    return out


def save_settings(conn: Connection, biz: dict, actor: str, *, enabled: bool | None = None, slug: str | None = None,
                  hours: dict | None = None, notice_minutes: int | None = None, days_ahead: int | None = None,
                  hold_minutes: int | None = None) -> dict:
    vals = {}
    if hold_minutes is not None:                              # bk01: a time waits this long for its deposit (5 min – 24 h)
        if not 5 <= hold_minutes <= 1440:
            raise Invalid("deposit wait: between 5 minutes and 24 hours", "hold_minutes")
        vals["deposit_hold_minutes"] = hold_minutes
    if slug is not None:
        sl = slugify(slug)
        taken = conn.execute(select(db.businesses.c.id).where(db.businesses.c.slug == sl, db.businesses.c.id != biz["id"])).first()
        if taken:
            raise Invalid("that link is taken — try another", "slug")
        vals["slug"] = sl
    if hours is not None:
        vals["hours"] = clean_hours(hours)
    if notice_minutes is not None:
        if not 0 <= notice_minutes <= 7 * 24 * 60:
            raise Invalid("notice: between 0 minutes and 7 days", "notice_minutes")
        vals["booking_notice_minutes"] = notice_minutes
    if days_ahead is not None:
        if not 1 <= days_ahead <= 365:
            raise Invalid("days ahead: between 1 and 365", "days_ahead")
        vals["booking_days_ahead"] = days_ahead
    if enabled is not None:
        vals["booking_enabled"] = enabled
    if vals:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "booking.updated", actor, biz["id"], {"fields": sorted(vals)})
    row = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
    if row["booking_enabled"] and not row["slug"]:
        row = save_settings(conn, row, actor, slug=_free_slug(conn, row["name"]))
    return row


def _free_slug(conn: Connection, name: str) -> str:
    base = slugify(name)
    sl = base
    while conn.execute(select(db.businesses.c.id).where(db.businesses.c.slug == sl)).first():
        sl = f"{base}-{secrets.token_hex(2)}"
    return sl


def add_service(conn: Connection, biz: dict, actor: str, *, name: str, minutes: int, price_minor: int | None = None,
                capacity: int = 1, ask_party: bool = False) -> dict:
    name = (name or "").strip()
    if not name:
        raise Invalid("name the service", "name")
    if not isinstance(minutes, int) or not 5 <= minutes <= 1440:
        raise Invalid("minutes: between 5 and 1440", "minutes")
    if price_minor is not None and (not isinstance(price_minor, int) or price_minor < 0):
        raise Invalid("price_minor is a whole number in the smallest currency unit", "price_minor")
    pos = conn.execute(select(func.coalesce(func.max(BS.c.position), 0) + 1).where(BS.c.business_id == biz["id"])).scalar()
    capacity = _int(capacity, "capacity", 1, 1000, "seats: from 1 to 1000") or 1     # bk01: a class / a restaurant's covers
    row = conn.execute(insert(BS).values(business_id=biz["id"], name=name[:120], minutes=minutes, price_minor=price_minor,
                                         position=pos, capacity=capacity, ask_party=bool(ask_party)).returning(BS)).mappings().one()
    log(conn, biz["id"], "booking.service_added", actor, row["id"])
    return dict(row)


def _int(v, field: str, lo: int, hi: int, msg: str) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v).strip())
        except ValueError:
            raise Invalid(msg, field) from None
    if not lo <= v <= hi:
        raise Invalid(msg, field)
    return v


def clean_starts(starts: dict) -> dict:
    """{"mon": ["07:00", "18:00"], …} — a class's fixed start times, validated and sorted."""
    out = {}
    for d in DAYS:
        xs = sorted({str(x).strip() for x in (starts or {}).get(d) or [] if str(x).strip()})
        for x in xs:
            if not _HHMM.match(x):
                raise Invalid(f"{d}: {x} is not a time (HH:MM)", "starts")
        if xs:
            out[d] = xs
    return out


def save_options(conn: Connection, biz: dict, actor: str, service_id: UUID, data: dict) -> dict:
    """bk01: a service's seats, party size, fixed start times, waitlist, who does it, and its money policy (deposit fixed
    or a percent of the price, no-show / late-cancel fee, cancellation window). Only the keys given change."""
    svc = _service(conn, biz, service_id)
    vals: dict = {}
    if "capacity" in data:
        vals["capacity"] = _int(data["capacity"], "capacity", 1, 1000, "seats: from 1 to 1000") or 1
    if "ask_party" in data:
        vals["ask_party"] = bool(data["ask_party"])
    if "max_party" in data:
        vals["max_party"] = _int(data["max_party"], "max_party", 1, 1000, "people per booking: from 1 to 1000")
    if "starts" in data:
        vals["starts"] = clean_starts(data["starts"] or {})
    if "waitlist" in data:
        vals["waitlist"] = bool(data["waitlist"])
    if "deposit_minor" in data:
        d = data["deposit_minor"]
        if d not in (None, "") and (isinstance(d, bool) or not isinstance(d, int) or d <= 0):
            raise Invalid("deposit: an amount above 0", "deposit_minor")
        vals["deposit_minor"] = d or None
    if "deposit_bp" in data:
        vals["deposit_bp"] = _int(data["deposit_bp"], "deposit_bp", 1, 10000, "deposit: from 1 to 100 %")
    if vals.get("deposit_minor") and vals.get("deposit_bp"):
        raise Invalid("a deposit is a fixed amount or a percent, not both", "deposit_bp")
    if vals.get("deposit_minor"):
        vals.setdefault("deposit_bp", None)
    if vals.get("deposit_bp"):
        vals.setdefault("deposit_minor", None)
        if (svc["price_minor"] or 0) <= 0:
            raise Invalid("a percent deposit needs the service's price", "deposit_bp")
    if "fee_minor" in data:
        f = data["fee_minor"]
        if f not in (None, "") and (isinstance(f, bool) or not isinstance(f, int) or f <= 0):
            raise Invalid("fee: an amount above 0", "fee_minor")
        vals["fee_minor"] = f or None
    if "cancel_hours" in data:
        vals["cancel_hours"] = _int(data["cancel_hours"], "cancel_hours", 1, 720, "cancellation window: from 1 to 720 hours")
    cap = vals.get("capacity", svc["capacity"])
    if vals.get("max_party", svc["max_party"]) and vals.get("max_party", svc["max_party"]) > cap:
        raise Invalid("people per booking can't be more than the seats", "max_party")
    if vals:
        conn.execute(update(BS).where(BS.c.id == svc["id"]).values(**vals))
    if "staff_ids" in data:
        ids = []
        for x in data["staff_ids"] or []:
            u = _uid(x)
            if not isinstance(u, UUID):
                continue
            if not conn.execute(select(db.staff_members.c.id).where(db.staff_members.c.business_id == biz["id"],
                                                                    db.staff_members.c.id == u)).first():
                raise NotFound("team member")
            ids.append(u)
        conn.execute(STAFF_LINK.delete().where(STAFF_LINK.c.business_id == biz["id"], STAFF_LINK.c.service_id == svc["id"]))
        for u in dict.fromkeys(ids):
            conn.execute(insert(STAFF_LINK).values(business_id=biz["id"], service_id=svc["id"], staff_id=u))
        vals["staff_ids"] = True
    if vals:
        log(conn, biz["id"], "booking.service_options", actor, svc["id"], {"fields": sorted(vals)})
    return {**_service(conn, biz, service_id), "staff_ids": service_staff(conn, biz["id"], service_id)}


def services(conn: Connection, business_id: UUID, active_only: bool = False) -> list[dict]:
    s = select(BS).where(BS.c.business_id == business_id)
    if active_only:
        s = s.where(BS.c.active)
    return [dict(r) for r in conn.execute(s.order_by(BS.c.position, BS.c.created_at)).mappings()]


def remove_service(conn: Connection, business_id: UUID, actor: str, service_id: UUID) -> None:
    if not conn.execute(update(BS).where(BS.c.business_id == business_id, BS.c.id == service_id, BS.c.active)
                        .values(active=False)).rowcount:
        raise NotFound("service")
    log(conn, business_id, "booking.service_removed", actor, service_id)


# ---------------------------------------------------------------- availability
def public_business(conn: Connection, slug: str) -> dict:
    row = conn.execute(select(db.businesses).where(db.businesses.c.slug == (slug or "").lower(),
                                                   db.businesses.c.booking_enabled)).mappings().first()
    if not row or not blocks.on(dict(row), "calendar"):
        raise NotFound("booking page")
    return dict(row)


def _service(conn: Connection, biz: dict, service_id: UUID) -> dict:
    row = conn.execute(select(BS).where(BS.c.business_id == biz["id"], BS.c.id == service_id, BS.c.active)).mappings().first()
    if not row:
        raise NotFound("service")
    return dict(row)


# ---------------------------------------------------------------- bk01: lanes (who does it), seats, party size
ACTIVE = ("booked", "confirmed")
BUSINESS = "business"                                        # the business's own lane: nobody in particular (as before bk01)
STAFF_LINK = db.booking_service_staff


def lanes(conn: Connection, business_id: UUID) -> list[dict]:
    """The people customers can book (active, "takes bookings"), by name. [] = one lane: the business itself."""
    SM = db.staff_members
    return [dict(r) for r in conn.execute(select(SM).where(SM.c.business_id == business_id, SM.c.active, SM.c.bookable)
                                          .order_by(func.lower(SM.c.name), SM.c.id)).mappings()]


def service_staff(conn: Connection, business_id: UUID, service_id: UUID) -> list[UUID]:
    """Who the owner said does this service (none named = everyone who takes bookings)."""
    return [r[0] for r in conn.execute(select(STAFF_LINK.c.staff_id).where(STAFF_LINK.c.business_id == business_id,
                                                                            STAFF_LINK.c.service_id == service_id))]


def eligible(conn: Connection, biz: dict, svc: dict | None, all_lanes: list[dict] | None = None) -> list[dict]:
    """The people who can do this service. Nobody named, or everyone named has left: everyone who takes bookings."""
    ls = lanes(conn, biz["id"]) if all_lanes is None else all_lanes
    if not ls or not svc:
        return ls
    ids = set(service_staff(conn, biz["id"], svc["id"]))
    return [x for x in ls if x["id"] in ids] or ls


def _uid(v):
    if v in (None, "", "any"):
        return None
    if v == BUSINESS:
        return BUSINESS
    try:
        return v if isinstance(v, UUID) else UUID(str(v))
    except ValueError:
        raise Invalid("pick who you would like", "staff") from None


def _simple(conn: Connection, biz: dict, svc: dict | None, staff, party: int) -> bool:
    """The business works as before bk01: one lane, one customer at a time, any free start — the old code path."""
    return (party == 1 and staff in (None, BUSINESS) and (not svc or (svc.get("capacity", 1) == 1 and not svc.get("starts")))
            and not lanes(conn, biz["id"]))


def _outside(conn: Connection, biz: dict, start: datetime, end: datetime) -> list:
    """The owner's own Google busy time. With lanes, the events Valeria One put in that calendar itself don't count
    (one stylist's booking is not everyone's busy time)."""
    return gcal.busy(conn, biz["id"], start, end, ours=not lanes(conn, biz["id"]))


def _spans(hours: dict, day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    return [(datetime.combine(day, time.fromisoformat(a), tz), datetime.combine(day, time.fromisoformat(b), tz))
            for a, b in (hours or {}).get(DAYS[day.weekday()]) or []]


def _intersect(a: list, b: list) -> list[tuple[datetime, datetime]]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if s < e:
                out.append((s, e))
    return sorted(out)


def _staff_spans(biz: dict, person: dict, day: date, shifts: list[dict]) -> list[tuple[datetime, datetime]]:
    """When this person works on this day, inside the opening hours: that week's shifts when the schedule has any for
    them that week (no shift that day = off), else their own weekly hours, else the opening hours."""
    tz = ZoneInfo(biz["timezone"])
    opening = _spans(biz["hours"], day, tz)
    if not opening:
        return []
    wk = day - timedelta(days=day.weekday())
    mine = [s for s in shifts if s["staff_id"] == person["id"]]
    if any(wk <= s["day"] < wk + timedelta(days=7) for s in mine):
        d0 = datetime.combine(day, time(0), tz)
        own = []
        for s in mine:
            st = datetime.combine(s["day"], s["starts"], tz)
            en = datetime.combine(s["day"] + timedelta(days=0 if s["ends"] > s["starts"] else 1), s["ends"], tz)
            a, b = max(st, d0), min(en, d0 + timedelta(days=1))
            if a < b:
                own.append((a, b))
        return _intersect(opening, own)
    if person.get("booking_hours"):
        return _intersect(opening, _spans(person["booking_hours"], day, tz))
    return opening


def _plan(conn: Connection, biz: dict, minutes: int, day: date, now: datetime, outside_busy: list, ignore, svc: dict | None,
          staff, party: int, only: datetime | None = None) -> tuple[dict, set]:
    """The lanes' free starts on this local day -> ({start: [(lane id | None, seats left)]}, {full starts}).
    only: just this exact start (any minute), for a time written in a chat."""
    tz = ZoneInfo(biz["timezone"])
    today = now.astimezone(tz).date()
    if day < today or day > today + timedelta(days=biz["booking_days_ahead"]):
        return {}, set()
    opening = _spans(biz["hours"], day, tz)
    if not opening:
        return {}, set()
    all_lanes = lanes(conn, biz["id"])
    pool = eligible(conn, biz, svc, all_lanes)
    if isinstance(staff, UUID):
        pool = [x for x in pool if x["id"] == staff]
        if not pool:
            return {}, set()
    try_lanes = [None] if staff == BUSINESS or not pool else pool
    cap = int((svc or {}).get("capacity") or 1)
    earliest = now + timedelta(minutes=biz["booking_notice_minutes"])
    dur = timedelta(minutes=minutes)
    d0 = datetime.combine(day, time(0), tz)
    d1 = d0 + timedelta(days=1, hours=2)
    AP = db.appointments
    rows = [dict(r) for r in conn.execute(select(AP.c.id, AP.c.staff_id, AP.c.service_id, AP.c.starts_at, AP.c.ends_at, AP.c.party).where(
        AP.c.business_id == biz["id"], AP.c.status.in_(ACTIVE), AP.c.starts_at < d1, AP.c.ends_at > d0)).mappings()
            if not ignore or r["id"] != ignore]
    shifts, off = [], []
    if try_lanes != [None]:
        SH, TO = db.shifts, db.staff_time_off
        ids = [x["id"] for x in try_lanes]
        wk = day - timedelta(days=day.weekday())
        shifts = [dict(r) for r in conn.execute(select(SH).where(SH.c.business_id == biz["id"], SH.c.staff_id.in_(ids),
                                                                 SH.c.day >= wk - timedelta(days=1), SH.c.day < wk + timedelta(days=7))).mappings()]
        off = [dict(r) for r in conn.execute(select(TO).where(TO.c.business_id == biz["id"], TO.c.staff_id.in_(ids),
                                                              TO.c.starts_at < d1, TO.c.ends_at > d0)).mappings()]
    fixed = (svc or {}).get("starts") or {}
    fixed_today = sorted(fixed.get(DAYS[day.weekday()]) or []) if fixed else None
    free: dict[datetime, list] = {}
    full: set[datetime] = set()
    for lane in try_lanes:
        lid = lane["id"] if lane else None
        spans = opening if lane is None else _staff_spans(biz, lane, day, shifts)
        mine = [a for a in rows if lid is None or a["staff_id"] in (lid, None)]
        group = [a for a in mine if cap > 1 and svc and a["service_id"] == svc["id"]]
        busy = [(a["starts_at"], a["ends_at"]) for a in mine if a not in group] + list(outside_busy or [])
        busy += [(o["starts_at"], o["ends_at"]) for o in off if o["staff_id"] == lid]
        for a, b in spans:
            if only is not None:
                local = only.astimezone(tz)
                cands = [local] if a <= local and local + dur <= b else []
            elif fixed_today is not None:
                cands = [datetime.combine(day, time.fromisoformat(x), tz) for x in fixed_today]
                cands = [t for t in cands if a <= t and t + dur <= b]
            else:
                cands, t = [], a
                while t + dur <= b:
                    cands.append(t)
                    t += timedelta(minutes=STEP_MINUTES)
            for t in cands:
                if fixed_today is not None and t.strftime("%H:%M") not in fixed_today:
                    continue
                if t < earliest or any(s < t + dur and e > t for s, e in busy):
                    continue
                left = cap - sum(g["party"] for g in group if g["starts_at"] < t + dur and g["ends_at"] > t)
                if left >= party:
                    free.setdefault(t, []).append((lid, left))
                elif cap > 1:
                    full.add(t)
    return free, full - set(free)


def _fair(conn: Connection, biz: dict, options: list[tuple], start: datetime, cap: int):
    """"Anyone available": a class already forming first (fewest seats left), then the fewest bookings that day, then
    whoever was given one longest ago, then by name."""
    if len(options) == 1 or options[0][0] is None:
        return options[0][0]
    tz = ZoneInfo(biz["timezone"])
    d0 = datetime.combine(start.astimezone(tz).date(), time(0), tz)
    AP = db.appointments
    ids = [o[0] for o in options]
    day_n = dict(conn.execute(select(AP.c.staff_id, func.count()).where(
        AP.c.business_id == biz["id"], AP.c.staff_id.in_(ids), AP.c.status.in_(ACTIVE), AP.c.starts_at >= d0,
        AP.c.starts_at < d0 + timedelta(days=1)).group_by(AP.c.staff_id)).all())
    last = dict(conn.execute(select(AP.c.staff_id, func.max(AP.c.created_at)).where(
        AP.c.business_id == biz["id"], AP.c.staff_id.in_(ids)).group_by(AP.c.staff_id)).all())
    never = datetime.min.replace(tzinfo=timezone.utc)
    return min(options, key=lambda o: (o[1] if cap > 1 and o[1] < cap else cap + 1, day_n.get(o[0], 0),
                                       last.get(o[0]) or never, ids.index(o[0])))[0]


def place(conn: Connection, biz: dict, minutes: int, start: datetime, now: datetime | None = None, ignore: UUID | None = None, *,
          service: dict | None = None, staff=None, party: int = 1, grid: bool = False):
    """Is this exact start free for this ask, and in whose lane? -> (True, lane id | None) or (False, None).
    grid: only the starts the booking page offers (the 15-minute steps / the service's fixed times)."""
    now = now or datetime.now(timezone.utc)
    staff = _uid(staff)
    tz = ZoneInfo(biz["timezone"])
    local = start.astimezone(tz)
    if local.date() > now.astimezone(tz).date() + timedelta(days=biz["booking_days_ahead"]) or \
            local < now + timedelta(minutes=biz["booking_notice_minutes"]):
        return False, None
    outside = _outside(conn, biz, local, local + timedelta(minutes=minutes))
    if grid:
        got, _ = _plan(conn, biz, minutes, local.date(), now, outside, ignore, service, staff, party)
        options = got.get(local)
    else:
        got, _ = _plan(conn, biz, minutes, local.date(), now, outside, ignore, service, staff, party, only=local)
        options = got.get(local)
    if not options:
        return False, None
    return True, _fair(conn, biz, options, local, int((service or {}).get("capacity") or 1))


def clash(conn: Connection, biz: dict, st: datetime, en: datetime, staff_id: UUID | None = None, ignore: UUID | None = None) -> bool:
    """Does anything already booked overlap this time in this lane? Nobody named = the business's lane: everything."""
    AP = db.appointments
    q = select(AP.c.id).where(AP.c.business_id == biz["id"], AP.c.status.in_(ACTIVE), AP.c.starts_at < en, AP.c.ends_at > st)
    if staff_id:
        q = q.where((AP.c.staff_id == staff_id) | AP.c.staff_id.is_(None))
    if ignore:
        q = q.where(AP.c.id != ignore)
    return conn.execute(q.limit(1)).first() is not None


def spots(conn: Connection, biz: dict, service_id: UUID, day: date, now: datetime | None = None, *, staff=None,
          party: int = 1) -> tuple[dict, list]:
    """For the booking page: ({start: seats left (the most in any lane)}, [full starts]) — full only for seated services."""
    now = now or datetime.now(timezone.utc)
    svc = _service(conn, biz, service_id)
    staff = _uid(staff)
    if _simple(conn, biz, svc, staff, party):
        return {t: 1 for t in free(conn, biz, svc["minutes"], day, now)}, []
    tz = ZoneInfo(biz["timezone"])
    d0 = datetime.combine(day, time(0), tz)
    got, full = _plan(conn, biz, svc["minutes"], day, now, _outside(conn, biz, d0, d0 + timedelta(days=1, hours=2)), None, svc,
                      staff, party)
    return {t: max(x[1] for x in v) for t, v in sorted(got.items())}, sorted(full)


def _busy(conn: Connection, biz: dict, start: datetime, end: datetime, ignore: UUID | None = None) -> list[tuple[datetime, datetime]]:
    AP = db.appointments
    q = select(AP.c.starts_at, AP.c.ends_at).where(AP.c.business_id == biz["id"], AP.c.status.in_(("booked", "confirmed")),
                                                   AP.c.starts_at < end, AP.c.ends_at > start)
    if ignore:                                                   # sch01: moving a booking — its own time doesn't block it
        q = q.where(AP.c.id != ignore)
    return [(r.starts_at, r.ends_at) for r in conn.execute(q)]


def slots(conn: Connection, biz: dict, service_id: UUID, day: date, now: datetime | None = None,
          outside_busy: list | None = None, *, staff=None, party: int = 1) -> list[datetime]:
    """Free start times (aware, business time zone) for this service on this local day. Busy time in the
    owner's own Google Calendar counts too (fetched here unless the caller already has it). bk01: for this person
    (None = anyone available) and this many people."""
    svc = _service(conn, biz, service_id)
    return free(conn, biz, svc["minutes"], day, now, outside_busy, service=svc, staff=staff, party=party)


def free(conn: Connection, biz: dict, minutes: int, day: date, now: datetime | None = None, outside_busy: list | None = None,
         ignore: UUID | None = None, *, service: dict | None = None, staff=None, party: int = 1) -> list[datetime]:
    """Free start times for something that takes `minutes` (a service, a Marketplace pickup) on this local day —
    the same rules as the booking page: opening hours, notice, days ahead, nothing booked, the owner's Google busy time.
    bk01: with lanes / seats / a party, each person's own time (anyone = free for at least one of them)."""
    now = now or datetime.now(timezone.utc)
    staff = _uid(staff)
    if not _simple(conn, biz, service, staff, party):
        tz = ZoneInfo(biz["timezone"])
        d0 = datetime.combine(day, time(0), tz)
        if outside_busy is None:
            outside_busy = _outside(conn, biz, d0, d0 + timedelta(days=1, hours=2))
        got, _ = _plan(conn, biz, minutes, day, now, outside_busy, ignore, service, staff, party)
        return sorted(got)
    tz = ZoneInfo(biz["timezone"])
    today = now.astimezone(tz).date()
    if day < today or day > today + timedelta(days=biz["booking_days_ahead"]):
        return []
    spans = (biz["hours"] or {}).get(DAYS[day.weekday()]) or []
    if not spans:
        return []
    earliest = now + timedelta(minutes=biz["booking_notice_minutes"])
    dur = timedelta(minutes=minutes)
    day_start = datetime.combine(day, time(0), tz)
    busy = _busy(conn, biz, day_start, day_start + timedelta(days=1, hours=2), ignore)
    if outside_busy is None:
        outside_busy = gcal.busy(conn, biz["id"], day_start, day_start + timedelta(days=1, hours=2))
    busy += outside_busy
    out = []
    for a, b in spans:
        t = datetime.combine(day, time.fromisoformat(a), tz)
        close = datetime.combine(day, time.fromisoformat(b), tz)
        while t + dur <= close:
            if t >= earliest and not any(s < t + dur and e > t for s, e in busy):
                out.append(t)
            t += timedelta(minutes=STEP_MINUTES)
    return out


def fits(conn: Connection, biz: dict, minutes: int, start: datetime, now: datetime | None = None, ignore: UUID | None = None, *,
         service: dict | None = None, staff=None, party: int = 1) -> bool:
    """Is this exact start free (any minute, not only the 15-minute steps)? The same rules as free()."""
    now = now or datetime.now(timezone.utc)
    if not _simple(conn, biz, service, _uid(staff), party):
        return place(conn, biz, minutes, start, now, ignore, service=service, staff=staff, party=party)[0]
    tz = ZoneInfo(biz["timezone"])
    local = start.astimezone(tz)
    today = now.astimezone(tz).date()
    end = local + timedelta(minutes=minutes)
    if local < now + timedelta(minutes=biz["booking_notice_minutes"]) or local.date() > today + timedelta(days=biz["booking_days_ahead"]):
        return False
    if not any(datetime.combine(local.date(), time.fromisoformat(a), tz) <= local and end <= datetime.combine(local.date(), time.fromisoformat(b), tz)
               for a, b in (biz["hours"] or {}).get(DAYS[local.weekday()]) or []):
        return False
    busy = _busy(conn, biz, local, end, ignore) + gcal.busy(conn, biz["id"], local, end)
    return not any(s < end and e > local for s, e in busy)


def open_days(conn: Connection, biz: dict, service_id: UUID, now: datetime | None = None, limit: int = 14, *,
              staff=None, party: int = 1) -> list[date]:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(biz["timezone"])
    d0 = now.astimezone(tz).date()
    start = datetime.combine(d0, time(0), tz)
    outside = _outside(conn, biz, start, start + timedelta(days=biz["booking_days_ahead"] + 2))   # one call, not one a day
    out = []
    for i in range(biz["booking_days_ahead"] + 1):
        d = d0 + timedelta(days=i)
        if slots(conn, biz, service_id, d, now, outside_busy=outside, staff=staff, party=party):
            out.append(d)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- booking
def party_of(svc: dict, party) -> int:
    """How many people (seats) a booking takes: 1 unless the service asks; never more than it seats."""
    try:
        n = int(party or 1)
    except (TypeError, ValueError):
        raise Invalid("how many people: a number", "party") from None
    top = min(int(svc.get("max_party") or svc.get("capacity") or 1), int(svc.get("capacity") or 1))
    if not svc.get("ask_party") and int(svc.get("capacity") or 1) == 1:
        top = 1
    if not 1 <= n <= max(1, top):
        raise Invalid(f"how many people: from 1 to {max(1, top)}", "party")
    return n


def book(conn: Connection, biz: dict, *, service_id: UUID, starts_at: datetime, name: str, phone: str,
         email: str | None = None, marketing_consent: bool = False, notes: str = "", now: datetime | None = None,
         conversation_id: UUID | None = None, staff_id=None, party: int = 1) -> dict:
    """A customer books. The slot is re-checked under a per-business lock, so two people can never get it.
    bk01: staff_id = the person they picked (None = anyone available); party = how many people. With a deposit, the
    booking waits for it (the caller sends the business's own pay link: bookpay.ask_deposit)."""
    now = now or datetime.now(timezone.utc)
    if not (name or "").strip():
        raise Invalid("tell us your name", "name")
    try:
        e164 = phones.e164(phone, packs.country(biz["country"]))
    except phones.BadPhone:
        raise Invalid("check the phone number", "phone") from None
    svc = _service(conn, biz, service_id)
    party = party_of(svc, party)
    staff = _uid(staff_id)
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(biz["id"])))))
    tz = ZoneInfo(biz["timezone"])
    local = starts_at.astimezone(tz)
    lane, simple = None, _simple(conn, biz, svc, staff, party)
    if simple:
        if local not in slots(conn, biz, service_id, local.date(), now):
            raise Invalid("that time was just taken or is no longer available — pick another", "starts_at")
    else:
        ok, lane = place(conn, biz, svc["minutes"], local, now, service=svc, staff=staff, party=party, grid=True)
        if not ok:
            raise Invalid("that time was just taken or is no longer available — pick another", "starts_at")
    contact = customer(conn, biz, name=name, e164=e164, email=email, marketing_consent=marketing_consent)
    data = {"title": svc["name"], "contact_id": contact["id"], "starts_at": local, "ends_at": local + timedelta(minutes=svc["minutes"]),
            "notes": (notes or "").strip()[:1000], "conversation_id": conversation_id, "service_id": svc["id"]}
    if not simple:                                              # bk01: checked above, lane by lane (seats, people)
        data.update(staff_id=lane, party=party, checked=True)
    from . import bookpay
    data.update(bookpay.policy_of(svc, party))                  # bk01: the deposit / fee / window as they are now
    appt = deals.book(conn, biz, "booking_page", data)
    log(conn, biz["id"], "booking.made", "booking_page", appt["id"], {"service": svc["name"], **({"staff_id": str(lane)} if lane else {}),
                                                                       **({"party": party} if party > 1 else {})})
    return {"appointment": appt, "contact": contact, "service": svc, "staff": staff_name(conn, biz["id"], lane)}


def customer(conn: Connection, biz: dict, *, name: str, e164: str, email: str | None = None,
             marketing_consent: bool = False) -> dict:
    """The customer behind a booking (or a waitlist entry): found by their WhatsApp number, else made — the same as the
    booking page always did."""
    I = db.identities
    cid = conn.execute(select(I.c.contact_id).where(I.c.business_id == biz["id"], I.c.channel == "whatsapp",
                                                    I.c.address == e164)).scalar()
    if cid:
        contact = contacts.get(conn, biz["id"], cid)
        upd = {}
        if not contact["name"]:
            upd["name"] = name
        if email and not contact["email"]:
            upd["email"] = email
        if upd:
            contact = contacts.patch(conn, biz, "booking_page", cid, upd)
    else:
        data = {"name": name, "phone": e164, "source": "booking_page"}
        if email:
            data["email"] = email
        if marketing_consent:
            data.update(marketing_consent=True, marketing_consent_source="booking page checkbox")
        try:
            contact = contacts.create(conn, biz, "booking_page", data)
        except Invalid as e:
            if e.field == "email":
                raise Invalid("check the email", "email") from None
            raise
    if cid and marketing_consent and not contact["marketing_consent"]:
        contact = contacts.patch(conn, biz, "booking_page", cid, {"marketing_consent": True,
                                                                   "marketing_consent_source": "booking page checkbox"})
    return contact


def staff_name(conn: Connection, business_id: UUID, staff_id) -> str | None:
    if not staff_id:
        return None
    return conn.execute(select(db.staff_members.c.name).where(db.staff_members.c.business_id == business_id,
                                                              db.staff_members.c.id == staff_id)).scalar()


def first_name(name: str | None) -> str:
    return ((name or "").strip().split(" ") or [""])[0]


def _ics_time(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_text(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets go on in a new line that starts with a space (never inside a character)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def vevent(biz: dict, appt: dict, summary: str | None = None, description: str = "") -> list[str]:
    """One event. Times in UTC (always right, whatever the time zone or summer time); the same UID for the life of the
    booking and SEQUENCE +1 on each move or cancel (sch01), so a calendar replaces it instead of adding a second one."""
    lines = ["BEGIN:VEVENT", f"UID:{appt['id']}@valeria-one", f"DTSTAMP:{_ics_time(appt.get('updated_at') or datetime.now(timezone.utc))}",
             f"SEQUENCE:{int(appt.get('sequence') or 0)}", f"DTSTART:{_ics_time(appt['starts_at'])}", f"DTEND:{_ics_time(appt['ends_at'])}",
             f"SUMMARY:{_ics_text(summary or (appt['title'] + ' — ' + biz['name']))}",
             "STATUS:" + ("CANCELLED" if appt.get("status") == "cancelled" else "CONFIRMED")]
    if appt.get("location"):
        lines.append(f"LOCATION:{_ics_text(appt['location'])}")
    if description:
        lines.append(f"DESCRIPTION:{_ics_text(description)}")
    return lines + ["END:VEVENT"]


def calendar(events: list[list[str]], name: str = "") -> str:
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Valeria One//Booking//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    if name:
        head.append(f"X-WR-CALNAME:{_ics_text(name)}")
    return "\r\n".join(_fold(x) for x in head + [x for e in events for x in e] + ["END:VCALENDAR"]) + "\r\n"


def ics(biz: dict, appt: dict) -> str:
    """A calendar file the customer can add to their phone."""
    return calendar([vevent(biz, appt)])
