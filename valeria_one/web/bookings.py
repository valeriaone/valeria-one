"""bk01 (v0.26) on screen: booking for a team, classes and deposits.

* /app/booking/team (owner): who takes bookings, their colour, their own weekly hours (else the schedule's shifts, else the
  opening hours) and their time off.
* /app/booking/services/<id> (owner): a service's seats, party size, fixed start times, waitlist, who does it, and its
  money policy (deposit fixed or a percent, no-show / late-cancel fee, cancellation window).
* /app/booking/roster (the team): one time of a class or a table — who is coming, how many seats are left, the waitlist;
  came / didn't come per person.
* The calendar's buttons: deposit received (owner), send the deposit's pay link, send the no-show fee (owner).
* /b/<slug>/<service>/waitlist (public): "tell me if a spot opens" for a full time.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from .. import db, money
from ..services import Invalid, NotFound
from ..services import booking as BK
from ..services import bookpay as BP
from ..services import staff as SF
from . import _book_ok, _ctx, _flash, _form, _go, _guard, _lang, _need, _own, _pub, render

router = APIRouter(include_in_schema=False)


def _uuid(v, what: str) -> UUID:
    try:
        return UUID(str(v))
    except (ValueError, TypeError):
        raise NotFound(what) from None


def _hours(f, prefix: str = "") -> dict:
    out = {}
    for d in BK.DAYS:
        spans = []
        for k in ("1", "2"):
            a, b = (f.get(f"{prefix}{d}_a{k}") or "").strip(), (f.get(f"{prefix}{d}_b{k}") or "").strip()
            if a or b:
                spans.append([a, b])
        if spans:
            out[d] = spans
    return out


def _amount(biz: dict, v, field: str) -> int | None:
    v = str(v or "").replace(",", "").strip()
    if not v:
        return None
    try:
        n = money.to_minor(v, biz["currency"])
    except (ValueError, ArithmeticError):
        raise Invalid("check the amount", field) from None
    if n <= 0:
        raise Invalid("an amount above 0", field)
    return n


# ---------------------------------------------------------------- who takes bookings
@router.get("/app/booking/team")
@_guard
async def team_page(request: Request):
    ctx = _own(request)
    _need(request, ctx, "calendar")
    biz = ctx["biz"]
    with db.tx() as conn:
        team = SF.members(conn, biz["id"])
        off = SF.time_off(conn, biz["id"])
    return render(request, "booking_team.html", ctx, page="calendar", team=team, off=off, bdays=BK.DAYS,
                  on=[m for m in team if m["bookable"]])


@router.post("/app/booking/team/{mid}")
@_guard
async def team_save(request: Request, mid: str):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        SF.set_booking(conn, ctx["biz"], ctx["actor"], _uuid(mid, "team member"), bookable=f.get("bookable") == "1",
                       hours=_hours(f), color=f.get("color") or "")
    _flash(request, "saved")
    return _go("/app/booking/team")


@router.post("/app/booking/off")
@_guard
async def team_off_add(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        SF.add_time_off(conn, ctx["biz"], ctx["actor"], _uuid(f.get("staff_id"), "team member"), f.get("starts_at"), f.get("ends_at"),
                        f.get("note") or "")
    _flash(request, "saved")
    return _go("/app/booking/team#off")


@router.post("/app/booking/off/{oid}/remove")
@_guard
async def team_off_remove(request: Request, oid: str):
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        SF.remove_time_off(conn, ctx["biz"], ctx["actor"], _uuid(oid, "time off"))
    _flash(request, "saved")
    return _go("/app/booking/team#off")


# ---------------------------------------------------------------- one service's seats, people and money
@router.get("/app/booking/services/{sid}")
@_guard
async def service_page(request: Request, sid: str):
    ctx = _own(request)
    _need(request, ctx, "calendar")
    biz = ctx["biz"]
    with db.tx() as conn:
        svc = BK._service(conn, biz, _uuid(sid, "service"))
        chosen = set(BK.service_staff(conn, biz["id"], svc["id"]))
        team = BK.lanes(conn, biz["id"])
        policy = BP.service_policy(conn, biz, svc, _lang(request, biz))
        from ..services import payments as PM
        can_pay = PM.can_link(conn, biz["id"])
    return render(request, "booking_service.html", ctx, page="calendar", svc=svc, chosen=chosen, team=team, bdays=BK.DAYS,
                  policy=policy, can_pay=can_pay)


@router.post("/app/booking/services/{sid}")
@_guard
async def service_save(request: Request, sid: str):
    ctx = _own(request)
    f = await _form(request)
    biz = ctx["biz"]
    kind = f.get("deposit_kind") or "none"
    pct = str(f.get("deposit_pct") or "").replace(",", ".").rstrip("%").strip()
    try:
        bp = money.to_minor(pct, "USD") if kind == "percent" and pct else None
    except (ValueError, ArithmeticError):
        raise Invalid("deposit: a percent, e.g. 20", "deposit_bp") from None
    data = {"capacity": f.get("capacity") or 1, "ask_party": f.get("ask_party") == "1", "max_party": f.get("max_party") or None,
            "waitlist": f.get("waitlist") == "1",
            "starts": {d: [x.strip() for x in (f.get(f"starts_{d}") or "").replace(";", ",").split(",") if x.strip()] for d in BK.DAYS},
            "deposit_minor": _amount(biz, f.get("deposit_fixed"), "deposit_minor") if kind == "fixed" else None,
            "deposit_bp": bp, "fee_minor": _amount(biz, f.get("fee"), "fee_minor"),
            "cancel_hours": f.get("cancel_hours") or None}
    if f.get("staff_form") == "1":
        data["staff_ids"] = [k[6:] for k in f if k.startswith("staff_") and k != "staff_form" and f.get(k) == "1"]
    with db.tx() as conn:
        BK.save_options(conn, biz, ctx["actor"], _uuid(sid, "service"), data)
    _flash(request, "saved")
    return _go(f"/app/booking/services/{sid}")


# ---------------------------------------------------------------- a class's / a table's list for one time
@router.get("/app/booking/roster")
@_guard
async def roster_page(request: Request):
    ctx = _ctx(request)
    _need(request, ctx, "calendar")
    biz = ctx["biz"]
    q = request.query_params
    try:
        at = datetime.fromisoformat(q.get("at") or "")
    except ValueError:
        raise NotFound("roster") from None
    if at.tzinfo is None:
        at = at.replace(tzinfo=ZoneInfo(biz["timezone"]))
    staff = _uuid(q["staff"], "team member") if q.get("staff") else None
    AP, C = db.appointments, db.contacts
    from sqlalchemy import select
    with db.tx() as conn:
        svc = BK._service(conn, biz, _uuid(q.get("service"), "service"))
        sq = (select(AP, C.c.name.label("contact_name"), C.c.phone.label("contact_phone")).outerjoin(C, C.c.id == AP.c.contact_id)
              .where(AP.c.business_id == biz["id"], AP.c.service_id == svc["id"], AP.c.starts_at == at))
        if staff:
            sq = sq.where(AP.c.staff_id == staff)
        rows = [dict(r) for r in conn.execute(sq.order_by(AP.c.created_at)).mappings()]
        wl = BP.waitlist(conn, biz["id"], svc["id"], at)
        who = BK.staff_name(conn, biz["id"], staff)
    booked = sum(r["party"] for r in rows if r["status"] in BK.ACTIVE)
    return render(request, "booking_roster.html", ctx, page="calendar", svc=svc, at=at, rows=rows, wl=wl, staff=staff, who=who,
                  booked=booked, left=max(0, svc["capacity"] - booked), back=str(request.url.path) + "?" + str(request.url.query))


@router.post("/app/booking/waitlist/{wid}/remove")
@_guard
async def waitlist_remove(request: Request, wid: str):
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        BP.remove_waiting(conn, ctx["biz"]["id"], ctx["actor"], _uuid(wid, "waitlist"))
    back = f.get("back") or ""
    return _go(back if back.startswith("/app/") else "/app/calendar")


# ---------------------------------------------------------------- the deposit and the fee, from the calendar
def _back(f) -> str:
    b = f.get("back") or ""
    return b if b.startswith("/app/") else "/app/calendar"


@router.post("/app/appointments/{aid}/deposit")
@_guard
async def deposit_received(request: Request, aid: str):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        BP.mark_deposit(conn, ctx["biz"], ctx["actor"], _uuid(aid, "appointment"))
    _flash(request, "bkg_deposit_marked")
    return _go(_back(f))


@router.post("/app/appointments/{aid}/deposit-link")
@_guard
async def deposit_link(request: Request, aid: str):
    ctx = _ctx(request)
    f = await _form(request)
    got = BP.ask_deposit(ctx["biz"], _uuid(aid, "appointment"), message=True, actor=ctx["actor"])
    _flash(request, "bkg_link_sent" if got and got.get("url") else "bkg_fee_no_pay", "ok" if got and got.get("url") else "warn")
    return _go(_back(f))


@router.post("/app/appointments/{aid}/fee")
@_guard
async def fee_send(request: Request, aid: str):
    ctx = _own(request)
    f = await _form(request)
    try:
        got = BP.send_fee(ctx["biz"], _uuid(aid, "appointment"), ctx["actor"])
    except Invalid:
        _flash(request, "bkg_fee_no_pay", "warn")
        return _go(_back(f))
    _flash(request, "bkg_fee_sent" if got else "bkg_fee_none", "ok" if got else "warn")
    return _go(_back(f))


# ---------------------------------------------------------------- public: "tell me if a spot opens"
@router.post("/b/{slug}/{service_id}/waitlist")
@_guard
async def waitlist_join(request: Request, slug: str, service_id: UUID):
    pub, svcs = _pub(request, slug)
    f = await _form(request)
    svc = next((x for x in svcs if x["id"] == service_id), None)
    if not svc or not svc["waitlist"]:
        raise NotFound("service")
    if f.get("website"):
        return _go(f"/b/{slug}")
    if not _book_ok(request):
        raise Invalid("too many bookings from this connection; try again later", "rate")
    try:
        st = datetime.fromisoformat(f.get("starts_at", ""))
        party = int(f.get("party") or 1)
    except ValueError:
        raise Invalid("pick a time", "starts_at") from None
    with db.tx() as conn:
        BP.join_waitlist(conn, pub["biz"], service_id=service_id, starts_at=st, name=f.get("name", ""), phone=f.get("phone", ""),
                         party=party, staff_id=f.get("staff") or None, email=(f.get("email") or "").strip() or None)
    return render(request, "book.html", pub, step="waitlisted", svc=svc, at=st, slug=slug)
