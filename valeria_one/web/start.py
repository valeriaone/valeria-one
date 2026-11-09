"""start01 (Tad 2026-11-09 «they need to see the whole demo … with u valeria talking to them like ooohhh aaaahh live animated … i give
u an offer u cant refuse, so sign up!»): /start — one link for any business. Valeria greets them (animated), asks their trade, plays
herself with one of their customers on a phone until the money lands, makes the offer (everything free, no card, she sets it up),
then sign up — or «do it for me» (name, email, WhatsApp → a customer in the house business + Tad told within a minute).
Valeria's real live chat (the house business's website assistant, its own AI) rides along. EN + ES (Tad 2026-11-08).

What the visitor does is recorded only as events of the house business (start.opened after 3 s on screen, start.chat, start.lead) —
no IP, no device. Tad's alert job reads them (~/backups/tools/start_alert.py). The house business is VO_START_BUSINESS (valeria-one).
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select

from .. import db
from ..services import log
from ..services import site as SI

router = APIRouter()
_seen: dict = {}
_notes: dict = {}
_leads: dict = {}
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-zA-Z]{2,24}$")


def _house() -> dict | None:
    slug = os.environ.get("VO_START_BUSINESS", "valeria-one")
    with db.tx() as conn:
        row = conn.execute(select(db.businesses).where(db.businesses.c.slug == slug)).mappings().first()
    return dict(row) if row else None


def _lang(request: Request) -> str:
    hl = (request.query_params.get("hl") or "").lower()
    if hl in ("en", "es"):
        return hl
    return "es" if (request.headers.get("accept-language") or "").lower().startswith("es") else "en"


@router.get("/start")
@router.get("/empezar")
async def start_page(request: Request):
    from . import render
    lang = "es" if request.url.path == "/empezar" and not request.query_params.get("hl") else _lang(request)
    house = _house()
    r = render(request, "start_live.html", None, L=lang, es=lang == "es",
               chat=f"/w/{house['slug']}/assistant.js" if house and (house.get("site") or {}).get("assistant") else None)
    r.headers["Vary"] = "Accept-Language"
    return r


async def _json(request: Request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _event(type_: str, data: dict) -> bool:
    house = _house()
    if not house:
        return False
    with db.tx() as conn:
        log(conn, house["id"], type_, "website:start", None, data)
    return True


@router.post("/start/seen")
async def start_seen(request: Request):
    """3 seconds on a real screen (the page's own script) — one «opened» per visitor per hour; link scanners never run it."""
    from . import _limit
    d = await _json(request)
    if _limit(_seen, request, 1):
        _event("start.opened", {"trade": str(d.get("trade") or "")[:30], "lang": str(d.get("lang") or "")[:2]})
    return Response(status_code=204)


@router.post("/start/note")
async def start_note(request: Request):
    """A line the visitor typed: their trade, or a question to Valeria's live chat — so Tad hears it within a minute."""
    from . import _limit
    d = await _json(request)
    text = " ".join(str(d.get("t") or "").split())[:600]
    kind = d.get("k") if d.get("k") in ("trade", "chat") else "chat"
    if text and _limit(_notes, request, 40):
        _event("start.chat", {"kind": kind, "text": text})
    return Response(status_code=204)


@router.post("/start/lead")
async def start_lead(request: Request):
    """«Do it for me»: name, email, WhatsApp, business → a customer + conversation in the house business, and Tad is told."""
    from . import _limit
    d = await _json(request)
    if (d.get("website") or "").strip():                       # the hidden trap: only a bot fills it
        return JSONResponse({"ok": True})
    name = " ".join(str(d.get("name") or "").split())[:120]
    email = str(d.get("email") or "").strip().lower()[:200]
    wa = re.sub(r"[^0-9+]", "", str(d.get("whatsapp") or ""))[:20]
    biz = " ".join(str(d.get("business") or "").split())[:160]
    if not _EMAIL.match(email) or not 8 <= len(re.sub(r"\D", "", wa)) <= 15:
        return JSONResponse({"ok": False, "error": "email/whatsapp"}, status_code=400)
    if not _limit(_leads, request, 5):
        return JSONResponse({"ok": True})
    house = _house()
    if not house:
        return JSONResponse({"ok": False, "error": "offline"}, status_code=503)
    from ..services import Invalid
    msg = f"[/start · hazlo por mí] {biz or '—'} · WhatsApp {wa}"
    try:
        with db.tx() as conn:
            SI.contact_form(conn, house, name=name or email, phone=wa, email=email, message=msg, source="start")
    except Invalid:                                            # a number from another country without its +code: keep the email
        with db.tx() as conn:
            SI.contact_form(conn, house, name=name or email, email=email, message=msg, source="start")
    _event("start.lead", {"name": name, "email": email, "whatsapp": wa, "business": biz})      # Tad's alert: who, how to reach them
    return JSONResponse({"ok": True})
