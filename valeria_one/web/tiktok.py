"""TikTok on screen (/app/tiktok): what a business needs (a TikTok Business Account linked to TikTok for Business,
outside the EEA, Switzerland and the UK), TikTok's own sign-in, and how its messages work (they write first; you
answer within 48 hours). Off ("not connected yet") until the server has a TikTok app."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..services import Invalid
from ..services import tiktok as TT
from . import _flash, _form, _go, _guard, _own, render

router = APIRouter(include_in_schema=False)


@router.get("/app/tiktok")
@_guard
async def tiktok_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        acc = TT.account(conn, biz["id"])
    return render(request, "tiktok.html", ctx, on=TT.configured() and not biz["sandbox"], acc=acc,
                  here=TT.available_in(biz["country"]), page="tiktok")


@router.get("/app/tiktok/connect")
@_guard
async def tiktok_connect(request: Request):
    ctx = _own(request)
    user = ctx["user"]
    with db.tx() as conn:
        url = TT.start(conn, ctx["biz"], user["id"] if user else None)
    return RedirectResponse(url, status_code=303)


@router.get("/app/tiktok/callback")
@_guard
async def tiktok_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    ctx = _own(request)
    if error or not code:
        _flash(request, "tt_failed", "warn")
        return _go("/app/tiktok")
    try:
        bid, _acc = TT.finish(state or "", code)
    except TT.TikTokError as e:
        raise Invalid(str(e), "tiktok") from None
    if bid != ctx["biz"]["id"]:                              # started from another business in this browser
        _flash(request, "tt_failed", "warn")
        return _go("/app/tiktok")
    _flash(request, "tt_connected")
    return _go("/app/tiktok")


@router.post("/app/tiktok/{account_id}/disconnect")
@_guard
async def tiktok_disconnect(request: Request, account_id: UUID):
    from ..services import inbox as X
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        X.disconnect(conn, ctx["biz"]["id"], ctx["actor"], account_id)
    return _go("/app/tiktok")
