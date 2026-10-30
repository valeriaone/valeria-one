"""ars02 (v0.21) on screen: Email designs (/app/emails) — the campaign email builder (the same blocks, the same buttons),
kept as designs, with the email as a computer and as a phone show it, and its plain-text part. Used from a campaign
("start from a saved design") or a drip step ("use design")."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from .. import db
from ..services import Invalid, NotFound
from ..services import emails as EM
from . import _flash, _go, _guard, _lang, _multi_form, _need, _not_shared_demo, _own, render
from .campaigns import _blocks_from
from .i18n import t

router = APIRouter(include_in_schema=False)


def _owner(request: Request) -> dict:
    ctx = _own(request)
    _need(request, ctx, "followups")
    return ctx


def _uuid(v) -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound("design") from None


@router.get("/app/emails")
@_guard
async def emails_page(request: Request):
    ctx = _owner(request)
    with db.tx() as conn:
        rows = EM.list_(conn, ctx["biz"]["id"])
    return render(request, "emails.html", ctx, rows=rows, page="emails")


@router.post("/app/emails")
@_guard
async def emails_new(request: Request):
    ctx = _owner(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    lang = _lang(request, ctx["biz"])
    with db.tx() as conn:
        d = EM.save(conn, ctx["biz"], ctx["actor"], name=str(f.get("name") or ""), subject="",
                    blocks=[{"t": "heading", "text": t("em_start_title", lang)}, {"t": "text", "text": t("em_start_text", lang)},
                            {"t": "social"}])
    return _go(f"/app/emails/{d['id']}")


@router.get("/app/emails/{did}")
@_guard
async def email_page(request: Request, did: str):
    ctx = _owner(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        d = EM.get(conn, biz["id"], _uuid(did))
    html, text = EM.render(biz, d, {"name": t("cg_test_name", _lang(request, biz))})
    return render(request, "email_design.html", ctx, d=d, email_html=html, email_text=text, page="emails")


@router.post("/app/emails/{did}")
@_guard
async def email_save(request: Request, did: str):
    ctx = _owner(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    act = str(f.get("act") or "save")
    biz, du = ctx["biz"], _uuid(did)
    with db.tx() as conn:
        if act == "save":
            EM.save(conn, biz, ctx["actor"], name=str(f.get("name") or ""), subject=str(f.get("subject") or ""),
                    blocks=_blocks_from(f), design_id=du)
            _flash(request, "saved")
        elif act == "duplicate":
            d = EM.duplicate(conn, biz, ctx["actor"], du)
            return _go(f"/app/emails/{d['id']}")
        elif act == "delete":
            EM.delete(conn, biz, ctx["actor"], du)
            return _go("/app/emails")
        else:
            raise Invalid("?", "act")
    return _go(f"/app/emails/{did}#preview")
