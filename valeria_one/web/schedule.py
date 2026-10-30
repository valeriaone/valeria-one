"""ars02 (v0.21) on screen: the posting calendar (/app/schedule) — the month, every planned post on every network, what is
waiting to be posted by hand (copy, share, "posted"), and a quick "plan a post": the owner's tap puts it on the
calendar (the same approval as Posts: connected networks are published by the jobs, the others are a reminder)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from .. import db
from ..services import Invalid
from ..services import posts as PO
from ..services import schedule as SC
from . import _flash, _go, _guard, _multi_form, _not_shared_demo, render
from .posts import _pctx

router = APIRouter(include_in_schema=False)


@router.get("/app/schedule")
@_guard
async def schedule_page(request: Request):
    ctx = _pctx(request)
    biz = ctx["biz"]
    tz = ZoneInfo(biz["timezone"])
    today = datetime.now(tz).date()
    try:
        y, m = (int(x) for x in (request.query_params.get("m") or "").split("-"))
        date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
    with db.tx() as conn:
        mo = SC.month(conn, biz, y, m)
        nets = PO.networks(conn, biz)
        due = PO.hand_due(conn, biz["id"])
        hints = SC.hints(conn, biz, [n["network"] for n in nets] + list(PO.HAND))
    prev = date(y - (m == 1), 12 if m == 1 else m - 1, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    pick = request.query_params.get("day") or ""
    try:
        day = date.fromisoformat(pick) if pick else None
    except ValueError:
        day = None
    first = next((n["network"] for n in nets if n["connected"] and not n["reconnect"]), "facebook")
    sug = SC.next_good(biz, first)
    when = (datetime.combine(day, sug.timetz().replace(tzinfo=None)) if day else sug.replace(tzinfo=None)).strftime("%Y-%m-%dT%H:%M")
    from babel.dates import format_date, get_day_names
    from . import _lang, _locale
    lang = _lang(request, biz)
    month_name, wk_names = f"{y}-{m:02d}", ["1", "2", "3", "4", "5", "6", "7"]
    for where in (_locale(biz, lang), lang):                  # a language + country Babel doesn't pair: the language alone
        try:
            month_name = format_date(date(y, m, 1), "LLLL y", locale=where)
            names = get_day_names("abbreviated", locale=where)
            wk_names = [names[i] for i in range(7)]
            break
        except Exception:                                      # noqa: BLE001
            continue
    return render(request, "schedule.html", ctx, mo=mo, nets=nets, hand=PO.HAND, due=due, hints=hints, prev=prev, nxt=nxt, y=y, m=m,
                  today=today, when=when, max_text=PO.MAX_TEXT, month_name=month_name, wk_names=wk_names, page="schedule")


@router.post("/app/schedule")
@_guard
async def schedule_plan(request: Request):
    """Plan a post from the calendar: one text for the networks picked, a day and time, and the owner's OK in one tap.
    If something stops it (a network not connected, Instagram without a picture), it stays a draft and opens to fix."""
    ctx = _pctx(request)
    _not_shared_demo(request, ctx)
    biz = ctx["biz"]
    f = await _multi_form(request)
    nets = list(dict.fromkeys([str(x) for x in f.getlist("net")] + [str(x) for x in f.getlist("hand")]))
    text = str(f.get("text") or "").strip()
    if not nets:
        raise Invalid("pick where it goes", "networks")
    if not text:
        raise Invalid("write the post", "text")
    from .posts import _when
    when = _when(biz, str(f.get("when") or ""))
    if not when:
        raise Invalid("pick a date and time", "when")
    texts = {n: text[:PO.MAX_TEXT[n]] for n in nets if n in PO.MAX_TEXT}
    with db.tx() as conn:
        p = PO.create(conn, biz, ctx["actor"], kind="idea", idea="", nets=nets, texts=texts, by_hand=[str(x) for x in f.getlist("hand")])
    try:
        with db.tx() as conn:
            PO.approve(conn, biz, ctx["actor"], p["id"], when)
    except Invalid as e:
        _flash(request, "error", "warn", raw=str(e))
        return _go(f"/app/posts/{p['id']}")
    _flash(request, "po_scheduled")
    return _go(f"/app/schedule?m={when.year}-{when.month:02d}")
