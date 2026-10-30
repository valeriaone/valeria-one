"""ars01 (v0.20) on screen: Staff (/app/staff) — the owner's team, weekly schedule, hours and pay summary; a team member
signed in on their own phone sees their own shifts and one big button to clock in or out."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import db, money
from ..services import Invalid, NotFound
from ..services import staff as SF
from . import _ctx, _flash, _go, _guard, _lang, _multi_form, _own, render
from .i18n import ui_lang

router = APIRouter(include_in_schema=False)


def _uuid(v, what: str) -> UUID | None:
    if v in (None, ""):
        return None
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


def _amt(biz: dict, v, field: str) -> int | None:
    v = str(v or "").replace(",", "").strip()
    if not v:
        return None
    try:
        n = money.to_minor(v, biz["currency"])
    except (ValueError, ArithmeticError):
        raise Invalid("check the amount", field) from None
    if n < 0:
        raise Invalid("check the amount", field)
    return n


def _day(v, default: date) -> date:
    try:
        return date.fromisoformat(str(v)) if v else default
    except ValueError:
        return default


def _csv(body: str, name: str) -> Response:
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------- staff
@router.get("/app/staff")
@_guard
async def staff_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    today = SF.today_of(biz)
    wk = SF.monday(_day(request.query_params.get("week"), today))
    owner = ctx["role"] == "owner"
    start = _day(request.query_params.get("from"), SF.monday(today))
    end = _day(request.query_params.get("to"), start + timedelta(days=6))
    with db.tx() as conn:
        me = SF.for_user(conn, biz["id"], (ctx["user"] or {}).get("id"))
        mine_open = SF.open_entry(conn, biz["id"], me["id"]) if me else None
        team = SF.members(conn, biz["id"], active_only=False) if owner else []
        shifts = SF.week(conn, biz["id"], wk, None if owner else (me or {}).get("id") or UUID(int=0))
        sheet = SF.timesheet(conn, biz, start, end) if owner else []
        hours = SF.entries(conn, biz, start, end) if owner else (SF.entries(conn, biz, start, end, me["id"]) if me else [])
        logins = SF.sign_ins(conn, biz["id"]) if owner else []
    days = [wk + timedelta(days=i) for i in range(7)]
    lang = _lang(request, biz)
    from babel.dates import format_date
    from . import _locale
    loc = _locale(biz, lang)

    def wd(d: date, f: str = "EEE d", where: str = loc) -> str:
        try:
            return format_date(d, f, locale=where)
        except Exception:                                                # a language + country Babel doesn't pair
            return format_date(d, f, locale=lang)
    for e in hours:                                                      # the times before each fix, as the business's day
        e["was"] = [{"in_at": datetime.fromisoformat(h["in_at"]), "out_at": datetime.fromisoformat(h["out_at"]) if h.get("out_at") else None,
                     "note": h.get("note") or ""} for h in (e["history"] or [])]
    shares = {}
    if owner:
        to_them = _locale(biz, ui_lang(biz["language"]))                 # what goes to the team is in the business's language
        fd = lambda d: wd(d, "EEE d MMM", to_them)                       # noqa: E731
        for m in team:
            if m["active"]:
                shares[m["id"]] = SF.share_link(biz, m, [s for s in shifts if s["staff_id"] == m["id"]], biz["language"], fd)
    tab = request.query_params.get("tab") if request.query_params.get("tab") in ("schedule", "hours", "team") else "schedule"
    return render(request, "staff.html", ctx, page="staff", me=me, mine_open=mine_open, team=team, shifts=shifts, days=days, wk=wk,
                  prev_wk=wk - timedelta(days=7), next_wk=wk + timedelta(days=7), sheet=sheet, hours=hours, start=start, end=end,
                  logins=logins, shares=shares, tab=tab, today=today, minutes=SF.minutes, owner=owner,
                  linked={m["user_id"] for m in team if m["user_id"]}, wd=wd)


@router.post("/app/staff/clock")
@_guard
async def staff_clock(request: Request):
    ctx = _ctx(request)
    await _multi_form(request)
    with db.tx() as conn:
        me = SF.for_user(conn, ctx["biz"]["id"], (ctx["user"] or {}).get("id"))
        if not me:
            raise Invalid("ask the owner to add you to the team first", "staff")
        e = SF.clock(conn, ctx["biz"], ctx["actor"], me["id"])
    _flash(request, "sf_clocked_out" if e.get("out_at") else "sf_clocked_in")
    return _go("/app/staff")


def _member_form(biz: dict, f) -> dict:
    c = str(f.get("commission") or "").replace(",", ".").rstrip("%").strip()
    try:
        bp = money.to_minor(c, "USD") if c else None
    except (ValueError, ArithmeticError):
        raise Invalid("commission: a percent, e.g. 10", "commission") from None
    return {"name": f.get("name"), "phone": f.get("phone") or "", "job": f.get("job") or "",
            "hourly_minor": _amt(biz, f.get("hourly"), "hourly"), "per_shift_minor": _amt(biz, f.get("per_shift"), "per_shift"),
            "commission_bp": bp, "user_id": _uuid(f.get("user_id"), "sign-in")}


@router.post("/app/staff/members")
@_guard
async def staff_member_add(request: Request):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        SF.add_member(conn, ctx["biz"], ctx["actor"], _member_form(ctx["biz"], f))
    _flash(request, "saved")
    return _go("/app/staff?tab=team")


@router.post("/app/staff/members/{mid}")
@_guard
async def staff_member_save(request: Request, mid: str):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        if f.get("act") in ("leave", "back"):
            SF.patch_member(conn, ctx["biz"], ctx["actor"], _uuid(mid, "team member"), {"active": f.get("act") == "back"})
        else:
            SF.patch_member(conn, ctx["biz"], ctx["actor"], _uuid(mid, "team member"), _member_form(ctx["biz"], f))
    _flash(request, "saved")
    return _go("/app/staff?tab=team")


@router.post("/app/staff/shifts")
@_guard
async def staff_shift_add(request: Request):
    ctx = _own(request)
    f = await _multi_form(request)
    days = [d for d in f.getlist("day") if d]
    if not days:
        raise Invalid("pick a day", "day")
    with db.tx() as conn:
        for d in days[:7]:                                     # the same hours on several days at once
            SF.add_shift(conn, ctx["biz"], ctx["actor"], _uuid(f.get("staff_id"), "team member"), d, f.get("starts"), f.get("ends"),
                         str(f.get("note") or ""))
    return _go(f"/app/staff?week={SF.monday(_day(days[0], SF.today_of(ctx['biz']))).isoformat()}")


@router.post("/app/staff/shifts/{shid}/remove")
@_guard
async def staff_shift_remove(request: Request, shid: str):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        SF.remove_shift(conn, ctx["biz"], ctx["actor"], _uuid(shid, "shift"))
    return _go(f"/app/staff?week={f.get('week') or ''}")


@router.post("/app/staff/copy-week")
@_guard
async def staff_copy_week(request: Request):
    ctx = _own(request)
    f = await _multi_form(request)
    wk = SF.monday(_day(f.get("week"), SF.today_of(ctx["biz"])))
    with db.tx() as conn:
        n = SF.copy_week(conn, ctx["biz"], ctx["actor"], wk - timedelta(days=7), wk)
    from .i18n import t
    _flash(request, "sf_copied", raw=t("sf_copied", _lang(request, ctx["biz"])).format(n=n))
    return _go(f"/app/staff?week={wk.isoformat()}")


@router.post("/app/staff/hours")
@_guard
async def staff_hours_add(request: Request):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        SF.add_entry(conn, ctx["biz"], ctx["actor"], _uuid(f.get("staff_id"), "team member"), f.get("in_at"), f.get("out_at"),
                     str(f.get("note") or ""))
    _flash(request, "saved")
    return _go("/app/staff?tab=hours")


@router.post("/app/staff/hours/{eid}")
@_guard
async def staff_hours_fix(request: Request, eid: str):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        SF.fix_entry(conn, ctx["biz"], ctx["actor"], _uuid(eid, "hours"), in_at=f.get("in_at"), out_at=f.get("out_at"),
                     void=f.get("act") == "void", note=str(f.get("note") or ""))
    _flash(request, "saved")
    return _go(f"/app/staff?tab=hours&from={f.get('from') or ''}&to={f.get('to') or ''}")


@router.get("/app/staff/timesheet.csv")
@_guard
async def staff_csv(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    start = _day(request.query_params.get("from"), SF.monday(SF.today_of(biz)))
    end = _day(request.query_params.get("to"), start + timedelta(days=6))
    with db.tx() as conn:
        body = SF.timesheet_csv(conn, biz, ctx["actor"], start, end)
    return _csv(body, f"timesheet-{start.isoformat()}-{end.isoformat()}.csv")
