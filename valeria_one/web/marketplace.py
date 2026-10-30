"""mkt01 (v0.22) on screen: the Marketplace view (/app/marketplace) — each item as a card with its buyers under it and
where each one stands. Everyone on the team sees it, sets where a buyer stands and ties a chat to its item; adding items,
prices, pausing and Mark sold (which writes to buyers) are the owner's."""
from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from .. import db
from ..services import marketplace as MK
from . import _ctx, _flash, _form, _go, _guard, _lang, _own, render
from .staff import _amt

router = APIRouter(include_in_schema=False)


@router.get("/app/marketplace")
@_guard
async def marketplace_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    products = []
    with db.tx() as conn:
        bd = MK.board(conn, biz)
        if ctx["role"] == "owner":
            C = db.catalog_items
            products = [dict(r) for r in conn.execute(select(C.c.id, C.c.name, C.c.variant, C.c.price_minor)
                                                       .where(C.c.business_id == biz["id"], C.c.archived.is_(False))
                                                       .order_by(C.c.name).limit(500)).mappings()]
    open_items = [i for i in bd["items"] if i["status"] == "open"]
    return render(request, "marketplace.html", ctx, page="marketplace", items=bd["items"], unknown=bd["unknown"],
                  connected=bd["connected"], open_items=open_items, products=products, taps=MK.TAPS, icon=MK.DEFAULT_ICON)


def _data(biz: dict, f: dict) -> dict:
    return {"title": f.get("title") or "", "price_minor": _amt(biz, f.get("price"), "price"), "pickup": f.get("pickup") or "",
            "delivery": bool(f.get("delivery")), "notes": f.get("notes") or "", "icon": f.get("icon") or "",
            "listing": f.get("listing") or ""}


@router.post("/app/marketplace")
@_guard
async def marketplace_add(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    from ..services import files as FL
    from .studio import _upload
    f, ups = await _upload(request)
    d = _data(biz, f)
    if f.get("catalog_item_id"):
        d["catalog_item_id"] = f["catalog_item_id"]
        if not str(f.get("price") or "").strip():
            d["price_minor"] = None                          # the product's own price comes along
    with db.tx() as conn:
        if ups:
            raw, mime, name = ups[0]
            d["file_id"] = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, kind="photo")["id"]
        it = MK.add(conn, biz, ctx["actor"], d)
    _flash(request, "saved")
    return _go(f"/app/marketplace#m-{it['id']}")


@router.post("/app/marketplace/buyers/{bid}")
@_guard
async def marketplace_buyer(request: Request, bid: str):
    """The team's taps: where a buyer stands, or which item the chat is about."""
    ctx = _ctx(request)
    f = await _form(request)
    with db.tx() as conn:
        if f.get("act") == "assign":
            MK.assign(conn, ctx["biz"], ctx["actor"], bid, f.get("item_id"))
        else:
            MK.set_status(conn, ctx["biz"], ctx["actor"], bid, str(f.get("status") or ""))
    _flash(request, "saved")
    return _go("/app/marketplace")


@router.post("/app/marketplace/{iid}")
@_guard
async def marketplace_item(request: Request, iid: str):
    ctx = _own(request)
    biz = ctx["biz"]
    f = await _form(request)
    act = f.get("act")
    if act == "sold":
        res = MK.mark_sold(biz, ctx["actor"], iid, f.get("buyer_id") or None)
        from .i18n import t
        lang = _lang(request, biz)
        lines = [t("mk_sold_done", lang)]
        if res["told"]:
            lines.append(t("mk_told", lang).format(names=", ".join(res["told"])))
        if res["not_told"]:
            lines.append(t("mk_not_told", lang).format(names=", ".join(res["not_told"])))
        _flash(request, "mk_sold_done", "warn" if res["not_told"] else "ok", raw=" · ".join(lines))
        return _go(f"/app/marketplace#m-{iid}")
    with db.tx() as conn:
        if act == "price":
            MK.patch(conn, biz, ctx["actor"], iid, {"price_minor": _amt(biz, f.get("price"), "price")})
        elif act in ("pause", "unpause"):
            MK.pause(conn, biz, ctx["actor"], iid, act == "pause")
        elif act == "reopen":
            MK.reopen(conn, biz, ctx["actor"], iid)
        else:
            MK.patch(conn, biz, ctx["actor"], iid, _data(biz, f))
    _flash(request, "saved")
    return _go(f"/app/marketplace#m-{iid}")
