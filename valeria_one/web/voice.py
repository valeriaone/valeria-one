"""v0.18 on screen: the phone agent — a toddler-proof wizard (one question per screen) and the calls it took."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import db
from ..services import voice_agent as VA
from . import _flash, _form, _go, _guard, _lang, _own, render
from .i18n import t

router = APIRouter(include_in_schema=False)
STEPS = ("greeting", "can", "record", "on")


@router.get("/app/voice-agent")
@_guard
async def voice_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        s = VA.settings(conn, biz["id"])
        need = VA.missing(conn, biz)
        calls = VA.calls_of(conn, biz["id"], limit=30)
        acc = VA.number_account(conn, biz["id"])
    from .. import crypto
    number = crypto.open_(acc["config"]).get("from_number") if acc else None
    step = request.query_params.get("step")
    if need:
        step = need[0]                                       # first the business's own accounts: its number, its AI key
    elif step not in STEPS:
        step = "home" if s["enabled"] else "greeting"
    lang = _lang(request, biz)
    default = t("vc_greet_default", lang).format(business=biz["name"])
    return render(request, "voice_agent.html", ctx, s=s, step=step, steps=STEPS, need=need, calls=calls, number=number,
                  default_greeting=default, page="settings")


@router.post("/app/voice-agent")
@_guard
async def voice_save(request: Request):
    ctx = _own(request)
    f = await _form(request)
    step = f.get("step")
    with db.tx() as conn:
        if step == "greeting":
            VA.save(conn, ctx["biz"], ctx["actor"], greeting=f.get("greeting", ""))
            return _go("/app/voice-agent?step=can")
        if step == "can":
            VA.save(conn, ctx["biz"], ctx["actor"], can_book=f.get("can_book") == "1", can_message=f.get("can_message") == "1",
                    transfer_to=f.get("transfer_to", ""))
            return _go("/app/voice-agent?step=record")
        if step == "record":
            VA.save(conn, ctx["biz"], ctx["actor"], record=f.get("record") == "1")
            return _go("/app/voice-agent?step=on")
        if step in ("on", "off"):
            VA.save(conn, ctx["biz"], ctx["actor"], enabled=step == "on")
            _flash(request, "vc_is_on" if step == "on" else "vc_is_off")
            return _go("/app/voice-agent")
    return _go("/app/voice-agent")
