"""Google Business Profile on screen (/app/google): connect, pick the profile, its reviews (answered here, always with
the owner's OK), its info and its last 30 days. Off ("not connected yet") until the server has it set up."""
from __future__ import annotations

import secrets
from uuid import UUID

import httpx

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..services import Invalid, NotFound
from ..services import ai as AI
from ..services import gbp as G
from . import _flash, _form, _go, _guard, _not_shared_demo, _own, render

router = APIRouter(include_in_schema=False)


@router.get("/app/google")
@_guard
async def google_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        lk = G.link(conn, biz["id"])
        revs = G.list_reviews(conn, biz["id"])
        stats = G.review_stats(conn, biz["id"])
    places, problem = [], None
    if lk and not lk["location"] and lk["status"] == "ok" and G.configured():
        try:
            places = G.locations(biz["id"])
        except (G.GoogleError, httpx.HTTPError) as e:
            problem = str(e) if isinstance(e, G.GoogleError) else "could not reach Google; try again in a minute"
    ins = (lk or {}).get("insights") or {}
    peak = max([1, *[v for d in (ins.get("days") or {}).values() for v in d]])
    return render(request, "google.html", ctx, on=G.configured() and not biz["sandbox"], lk=lk, reviews=revs, stats=stats,
                  places=places, problem=problem, remind=G.reminder(lk), ins=ins, peak=peak, page="google")


@router.get("/app/google/connect")
@_guard
async def google_connect(request: Request):
    ctx = _own(request)
    if not G.configured() or ctx["biz"]["sandbox"]:
        raise NotFound("page")
    state = secrets.token_urlsafe(24)
    request.session["gbp_state"] = state
    return RedirectResponse(G.auth_url(state), status_code=303)


@router.get("/app/google/callback")
@_guard
async def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    ctx = _own(request)
    expected = request.session.pop("gbp_state", None)
    if error or not code or not expected or not secrets.compare_digest(state or "", expected):
        _flash(request, "gb_failed", "warn")
        return _go("/app/google")
    with db.tx() as conn:
        G.connect(conn, ctx["biz"], ctx["actor"], code)
    _flash(request, "gb_connected")
    return _go("/app/google")


@router.post("/app/google/location")
@_guard
async def google_location(request: Request):
    ctx = _own(request)
    f = await _form(request)
    account, _, location = str(f.get("place", "")).partition("|")
    try:
        with db.tx() as conn:
            G.choose(conn, ctx["biz"], ctx["actor"], account, location)
        G.refresh_info(ctx["biz"]["id"])
    except (G.GoogleError, httpx.HTTPError) as e:
        raise Invalid(str(e), "google") from None
    _flash(request, "saved")
    return _go("/app/google")


@router.post("/app/google/refresh")
@_guard
async def google_refresh(request: Request):
    """Read the reviews, info and insights now (still within Google's limits: a pause after a 429 is respected)."""
    ctx = _own(request)
    await _form(request)
    bid = ctx["biz"]["id"]
    try:
        G.poll_reviews(bid)
        G.refresh_info(bid)
        G.insights(bid)
    except (G.GoogleError, httpx.HTTPError) as e:
        raise Invalid(str(e), "google") from None
    _flash(request, "saved")
    return _go("/app/google")


@router.post("/app/google/disconnect")
@_guard
async def google_disconnect(request: Request):
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        G.disconnect(conn, ctx["biz"]["id"], ctx["actor"])
    return _go("/app/google")


@router.post("/app/google/reviews/{review_id}/draft")
@_guard
async def review_draft(request: Request, review_id: UUID):
    """The AI writes a draft answer (saved, never sent). No AI: the owner writes it."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    await _form(request)
    try:
        G.draft_reply(ctx["biz"], ctx["actor"], review_id)
        _flash(request, "gb_draft_ready")
    except AI.AIError:
        _flash(request, "gb_no_ai", "warn")
    return _go(f"/app/google#r-{review_id}")


@router.post("/app/google/reviews/{review_id}/reply")
@_guard
async def review_reply(request: Request, review_id: UUID):
    """Save the answer as a draft, or — with the owner's own "approve" tap — publish exactly this text on Google."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    text = str(f.get("reply") or "")
    if f.get("act") == "publish":
        G.send_reply(ctx["biz"], ctx["actor"], review_id, text, approved=f.get("approve") == "1")
        _flash(request, "gb_replied")
    else:
        with db.tx() as conn:
            G.save_draft(conn, ctx["biz"], ctx["actor"], review_id, text)
        _flash(request, "saved")
    return _go(f"/app/google#r-{review_id}")
