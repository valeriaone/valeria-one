"""kit01 (v0.19) on screen: "Add more" — every tool this business's kit didn't switch on, grouped, one plain line each,
one tap to show it in the menu (or to hide one); and Valeria's one suggestion, dismissed for good with "no thanks"."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import db, kits, packs
from ..services import NotFound
from . import _flash, _form, _go, _guard, _lang, _not_shared_demo, _own, render

router = APIRouter(include_in_schema=False)


@router.get("/app/add")
@_guard
async def add_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        tip = kits.suggestion(conn, biz)
    return render(request, "add_more.html", ctx, groups=kits.catalog(biz), tip=tip, page="add_more",
                  kit_name=packs.text(packs.industry(biz["industry"]).name, _lang(request, biz)))


@router.post("/app/add")
@_guard
async def add_save(request: Request):
    """tool=<key>, on=1 shows it in the menu, on=0 hides it (its data stays)."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    key, on = str(f.get("tool") or ""), f.get("on") == "1"
    if key not in kits.OPTIONAL:
        raise NotFound("tool")
    with db.tx() as conn:
        kits.set_tool(conn, ctx["biz"], ctx["actor"], key, on)
    _flash(request, "ad_shown" if on else "ad_hidden")
    if f.get("back") == "1" and on:                          # "show it" from the tool's own page: stay there
        return _go(kits.TOOLS[key].href)
    return _go(f"/app/add#t-{key}")


@router.post("/app/tips/dismiss")
@_guard
async def tip_dismiss(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        kits.dismiss(conn, ctx["biz"], ctx["actor"], str(f.get("tip") or ""))
    return _go("/app/add" if f.get("from") == "add" else "/app")
