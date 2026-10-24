"""Public booking: a customer opens the business's link, picks a service, a day and a time, and the
appointment is in the calendar. Times are always the business's local time; slots never overlap an
existing appointment (checked again, under a lock, at the moment of booking)."""
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
                  hours: dict | None = None, notice_minutes: int | None = None, days_ahead: int | None = None) -> dict:
    vals = {}
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


def add_service(conn: Connection, biz: dict, actor: str, *, name: str, minutes: int, price_minor: int | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise Invalid("name the service", "name")
    if not isinstance(minutes, int) or not 5 <= minutes <= 1440:
        raise Invalid("minutes: between 5 and 1440", "minutes")
    if price_minor is not None and (not isinstance(price_minor, int) or price_minor < 0):
        raise Invalid("price_minor is a whole number in the smallest currency unit", "price_minor")
    pos = conn.execute(select(func.coalesce(func.max(BS.c.position), 0) + 1).where(BS.c.business_id == biz["id"])).scalar()
    row = conn.execute(insert(BS).values(business_id=biz["id"], name=name[:120], minutes=minutes, price_minor=price_minor,
                                         position=pos).returning(BS)).mappings().one()
    log(conn, biz["id"], "booking.service_added", actor, row["id"])
    return dict(row)


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


def _busy(conn: Connection, biz: dict, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    AP = db.appointments
    return [(r.starts_at, r.ends_at) for r in conn.execute(
        select(AP.c.starts_at, AP.c.ends_at).where(AP.c.business_id == biz["id"], AP.c.status.in_(("booked", "confirmed")),
                                                   AP.c.starts_at < end, AP.c.ends_at > start))]


def slots(conn: Connection, biz: dict, service_id: UUID, day: date, now: datetime | None = None,
          outside_busy: list | None = None) -> list[datetime]:
    """Free start times (aware, business time zone) for this service on this local day. Busy time in the
    owner's own Google Calendar counts too (fetched here unless the caller already has it)."""
    now = now or datetime.now(timezone.utc)
    svc = _service(conn, biz, service_id)
    tz = ZoneInfo(biz["timezone"])
    today = now.astimezone(tz).date()
    if day < today or day > today + timedelta(days=biz["booking_days_ahead"]):
        return []
    spans = (biz["hours"] or {}).get(DAYS[day.weekday()]) or []
    if not spans:
        return []
    earliest = now + timedelta(minutes=biz["booking_notice_minutes"])
    dur = timedelta(minutes=svc["minutes"])
    day_start = datetime.combine(day, time(0), tz)
    busy = _busy(conn, biz, day_start, day_start + timedelta(days=1, hours=2))
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


def open_days(conn: Connection, biz: dict, service_id: UUID, now: datetime | None = None, limit: int = 14) -> list[date]:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(biz["timezone"])
    d0 = now.astimezone(tz).date()
    start = datetime.combine(d0, time(0), tz)
    outside = gcal.busy(conn, biz["id"], start, start + timedelta(days=biz["booking_days_ahead"] + 2))   # one call, not one a day
    out = []
    for i in range(biz["booking_days_ahead"] + 1):
        d = d0 + timedelta(days=i)
        if slots(conn, biz, service_id, d, now, outside_busy=outside):
            out.append(d)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- booking
def book(conn: Connection, biz: dict, *, service_id: UUID, starts_at: datetime, name: str, phone: str,
         email: str | None = None, marketing_consent: bool = False, notes: str = "", now: datetime | None = None) -> dict:
    """A customer books. The slot is re-checked under a per-business lock, so two people can never get it."""
    now = now or datetime.now(timezone.utc)
    if not (name or "").strip():
        raise Invalid("tell us your name", "name")
    try:
        e164 = phones.e164(phone, packs.country(biz["country"]))
    except phones.BadPhone:
        raise Invalid("check the phone number", "phone") from None
    svc = _service(conn, biz, service_id)
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(biz["id"])))))
    tz = ZoneInfo(biz["timezone"])
    local = starts_at.astimezone(tz)
    if local not in slots(conn, biz, service_id, local.date(), now):
        raise Invalid("that time was just taken or is no longer available — pick another", "starts_at")
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
    appt = deals.book(conn, biz, "booking_page", {"title": svc["name"], "contact_id": contact["id"], "starts_at": local,
                                                  "ends_at": local + timedelta(minutes=svc["minutes"]),
                                                  "notes": (notes or "").strip()[:1000]})
    log(conn, biz["id"], "booking.made", "booking_page", appt["id"], {"service": svc["name"]})
    return {"appointment": appt, "contact": contact, "service": svc}


def ics(biz: dict, appt: dict) -> str:
    """A calendar file the customer can add to their phone."""
    def f(d: datetime) -> str:
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Valeria One//Booking//EN", "BEGIN:VEVENT",
        f"UID:{appt['id']}@valeria-one", f"DTSTAMP:{f(datetime.now(timezone.utc))}",
        f"DTSTART:{f(appt['starts_at'])}", f"DTEND:{f(appt['ends_at'])}",
        f"SUMMARY:{esc(appt['title'] + ' — ' + biz['name'])}", "END:VEVENT", "END:VCALENDAR", ""])
