"""ars01 (v0.20) on screen: the register (/app/pos), its sales and the end-of-day count, the customer's receipt
(/receipt/<token>), and Inventory (/app/inventory). Everyone on the team sells and counts stock; voids, refunds, prices,
costs and the register's tax are the owner's."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response
from markupsafe import Markup

from .. import db, money, packs, qr
from ..services import Invalid, NotFound
from ..services import inventory as INV
from ..services import pos as POS
from . import _ctx, _flash, _go, _guard, _lang, _multi_form, _own, render

router = APIRouter(include_in_schema=False)


def _uuid(v, what: str = "sale") -> UUID | None:
    if v in (None, ""):
        return None
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


def _amt(biz: dict, v, field: str = "amount") -> int | None:
    """'1,250.50' -> 125050 (the currency's smallest unit); blank -> None."""
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


def _pct(v) -> int:
    """'12.5' (%) -> 1250 basis points."""
    v = str(v or "").replace(",", ".").rstrip("%").strip()
    if not v:
        return 0
    try:
        return money.to_minor(v, "USD")
    except (ValueError, ArithmeticError):
        raise Invalid("discount: a percent, e.g. 10", "discount") from None


def _csv(body: str, name: str) -> Response:
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------- the register
@router.get("/app/pos")
@_guard
async def pos_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    from ..services import contacts as C
    from ..services import payments as PM
    with db.tx() as conn:
        items = INV.items(conn, biz["id"], for_sale=True)
        people = C.search(conn, biz["id"], limit=300)
        can_link = PM.can_link(conn, biz["id"])
        today = POS.day_summary(conn, biz, POS.today_of(biz))
    return render(request, "pos.html", ctx, page="pos", items=items, people=people, can_link=can_link, tax=POS.tax(biz),
                  today=today, digits=packs.country(biz["country"]).currency_digits)


@router.post("/app/pos")
@_guard
async def pos_sell(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    lines = []
    for item, desc, q, unit in zip(f.getlist("line_item"), f.getlist("line_desc"), f.getlist("line_qty"), f.getlist("line_unit")):
        try:
            qty = int(str(q or "1").strip())
        except ValueError:
            raise Invalid("quantity: a whole number", "lines") from None
        if item:
            lines.append({"item_id": _uuid(item, "item"), "qty": qty})
        else:
            lines.append({"description": str(desc or ""), "qty": qty, "unit_minor": _amt(biz, unit, "lines") or 0})
    disc_minor, disc_bp = 0, 0
    if f.get("discount_kind") == "pct":
        disc_bp = _pct(f.get("discount"))
    else:
        disc_minor = _amt(biz, f.get("discount"), "discount") or 0
    sale = POS.sell(biz, ctx["actor"], lines=lines, method=str(f.get("method") or "cash"), discount_minor=disc_minor,
                    discount_bp=disc_bp, contact_id=_uuid(f.get("contact_id"), "contact"),
                    cash_given_minor=_amt(biz, f.get("cash_given"), "cash"), note=str(f.get("note") or ""),
                    user_id=(ctx["user"] or {}).get("id"))
    return _go(f"/app/pos/sales/{sale['id']}")


@router.post("/app/pos/settings")
@_guard
async def pos_settings(request: Request):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        POS.set_tax(conn, ctx["biz"], ctx["actor"], str(f.get("tax") or ""), f.get("included") == "1")
    _flash(request, "saved")
    return _go("/app/pos#settings")


@router.get("/app/pos/sales")
@_guard
async def pos_sales(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    try:
        day = date.fromisoformat(request.query_params.get("day") or "") if request.query_params.get("day") else POS.today_of(biz)
    except ValueError:
        day = POS.today_of(biz)
    with db.tx() as conn:
        rows = POS.list_(conn, biz["id"], day=day)
        summary = POS.day_summary(conn, biz, day)
        closes = POS.closes(conn, biz["id"], day=day)
        recent = POS.closes(conn, biz["id"], limit=14)
    return render(request, "pos_sales.html", ctx, page="pos", rows=rows, summary=summary, closes=closes, recent=recent, the_day=day,
                  is_today=day == POS.today_of(biz), methods=POS.METHODS)


@router.post("/app/pos/close")
@_guard
async def pos_close(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    try:
        day = date.fromisoformat(str(f.get("day") or ""))
    except ValueError:
        raise Invalid("pick a day", "day") from None
    with db.tx() as conn:
        POS.close_day(conn, biz, ctx["actor"], day, float_minor=_amt(biz, f.get("float"), "float") or 0,
                      counted_minor=_amt(biz, f.get("counted"), "counted") or 0, note=str(f.get("note") or ""))
    _flash(request, "pos_closed")
    return _go(f"/app/pos/sales?day={day.isoformat()}#close")


@router.get("/app/pos/sales/{sid}")
@_guard
async def pos_sale(request: Request, sid: str):
    ctx = _ctx(request)
    biz = ctx["biz"]
    from ..services import contacts as C
    with db.tx() as conn:
        sale = POS.get(conn, biz["id"], _uuid(sid))
        contact = C.get(conn, biz["id"], sale["contact_id"]) if sale["contact_id"] else None
        pay_url = POS.pay_url(conn, sale)
    text = POS.share_text(biz, sale, ui_lang_of(biz))
    phone = "".join(ch for ch in ((contact or {}).get("phone") or "") if ch.isdigit())
    wa = f"https://wa.me/{phone}?text={quote(text)}"
    mail = f"mailto:{(contact or {}).get('email') or ''}?subject={quote(biz['name'])}&body={quote(text)}"
    code = Markup(qr.svg(pay_url)) if pay_url else None
    return render(request, "pos_sale.html", ctx, page="pos", sale=sale, contact=contact, pay_url=pay_url, qr=code, wa=wa, mail=mail,
                  url=POS.url(sale), can_void=sale["status"] == "pending" or (
                      sale["status"] == "paid" and sale["day"] == POS.today_of(biz) and sale["method"] != "link"))


def ui_lang_of(biz: dict) -> str:
    """What goes to the customer is in the business's language (like its invoices), not the screen's."""
    from .i18n import ui_lang
    return ui_lang(biz["language"])


@router.post("/app/pos/sales/{sid}")
@_guard
async def pos_sale_act(request: Request, sid: str):
    f = await _multi_form(request)
    act = f.get("act")
    if act == "paid":                                         # the pay link was paid another way: anyone at the till may say so
        ctx = _ctx(request)
        from ..services import payments as PM
        with db.tx() as conn:
            sale = POS.get(conn, ctx["biz"]["id"], _uuid(sid))
            if sale["status"] != "pending" or not sale["payment_id"]:
                raise Invalid("this sale is not waiting for a payment", "status")
            PM.owner_confirms(conn, ctx["biz"]["id"], ctx["actor"], sale["payment_id"])
        return _go(f"/app/pos/sales/{sid}")
    ctx = _own(request)                                       # money going back: the owner's
    with db.tx() as conn:
        if act == "void":
            POS.void(conn, ctx["biz"], ctx["actor"], _uuid(sid), str(f.get("reason") or ""))
            _flash(request, "pos_voided")
        elif act == "refund":
            POS.refund(conn, ctx["biz"], ctx["actor"], _uuid(sid), str(f.get("reason") or ""))
            _flash(request, "pos_refunded")
        else:
            raise Invalid("?", "act")
    return _go(f"/app/pos/sales/{sid}")


# ---------------------------------------------------------------- the customer's receipt
@router.get("/receipt/{token}")
@_guard
async def receipt_public(request: Request, token: str):
    with db.tx() as conn:
        sale, biz = POS.by_token(conn, token)
    ctx = {"biz": biz, "user": None, "actor": "receipt_page", "role": None, "businesses": []}
    return render(request, "receipt.html", ctx, sale=sale, pack=packs.country(biz["country"]))


# ---------------------------------------------------------------- inventory
@router.get("/app/inventory")
@_guard
async def inventory_page(request: Request):
    ctx = _ctx(request)
    biz = ctx["biz"]
    q = request.query_params.get("q", "")
    low = request.query_params.get("low") == "1"
    arch = request.query_params.get("archived") == "1"
    from ..services import projects as PR
    with db.tx() as conn:
        rows = INV.items(conn, biz["id"], q=q, low=low, archived=arch)
        mains = [r for r in INV.items(conn, biz["id"]) if not r["parent_id"]]
        n_low = len(INV.low(conn, biz["id"]))
        sups = PR.suppliers(conn, biz["id"]) if ctx["role"] == "owner" else []
    return render(request, "inventory.html", ctx, page="inventory", rows=rows, mains=mains, q=q, low=low, archived=arch, n_low=n_low,
                  suppliers=sups)


def _item_data(biz: dict, f: dict) -> dict:
    d = {"name": f.get("name"), "sku": f.get("sku") or "", "variant": f.get("variant") or "",
         "parent_id": _uuid(f.get("parent_id"), "item"), "supplier_id": _uuid(f.get("supplier_id"), "supplier"),
         "price_minor": _amt(biz, f.get("price"), "price"), "cost_minor": _amt(biz, f.get("cost"), "cost"),
         "low_at": f.get("low_at") or None}
    return d


@router.post("/app/inventory")
@_guard
async def inventory_add(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    from ..services import files as FL
    from .studio import _upload
    f, ups = await _upload(request)
    d = _item_data(biz, f)
    d["stock"] = f.get("stock") or None
    with db.tx() as conn:
        if ups:
            raw, mime, name = ups[0]
            d["file_id"] = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, kind="photo")["id"]
        it = INV.add(conn, biz, ctx["actor"], d)
    _flash(request, "saved")
    return _go(f"/app/inventory#i-{it['id']}")


@router.get("/app/inventory.csv")
@_guard
async def inventory_csv(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        body = INV.csv_out(conn, ctx["biz"], ctx["actor"])
    return _csv(body, "inventory.csv")


@router.post("/app/inventory/import")
@_guard
async def inventory_import(request: Request):
    ctx = _own(request)
    from .studio import _upload
    _f, ups = await _upload(request)
    if not ups:
        raise Invalid("pick a CSV file", "file")
    with db.tx() as conn:
        st = INV.import_csv(conn, ctx["biz"], f"import:{ctx['actor']}"[:100], ups[0][0])
    from .i18n import t
    _flash(request, "inv_imported", raw=t("inv_imported", _lang(request, ctx["biz"])).format(**st))
    return _go("/app/inventory")


@router.get("/app/inventory/{iid}")
@_guard
async def inventory_item(request: Request, iid: str):
    ctx = _ctx(request)
    biz = ctx["biz"]
    from ..services import projects as PR
    with db.tx() as conn:
        it = INV.item(conn, biz["id"], _uuid(iid, "item"))
        moves = INV.moves(conn, biz["id"], it["id"])
        mains = [r for r in INV.items(conn, biz["id"]) if not r["parent_id"] and r["id"] != it["id"]]
        sups = PR.suppliers(conn, biz["id"]) if ctx["role"] == "owner" else []
    return render(request, "inventory_item.html", ctx, page="inventory", it=it, moves=moves, mains=mains, suppliers=sups)


@router.post("/app/inventory/{iid}")
@_guard
async def inventory_item_save(request: Request, iid: str):
    from ..services import files as FL
    from .studio import _upload
    f, ups = await _upload(request)
    act = f.get("act")
    if act == "adjust":                                       # counting the shelf: anyone on the team
        ctx = _ctx(request)
        with db.tx() as conn:
            INV.adjust(conn, ctx["biz"], ctx["actor"], _uuid(iid, "item"), str(f.get("reason") or ""), f.get("qty"),
                       str(f.get("note") or ""))
        _flash(request, "saved")
        return _go(f"/app/inventory/{iid}")
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        if act in ("archive", "unarchive"):
            INV.patch(conn, biz, ctx["actor"], _uuid(iid, "item"), {"archived": act == "archive"})
        else:
            d = _item_data(biz, f)
            if ups:
                raw, mime, name = ups[0]
                d["file_id"] = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, kind="photo")["id"]
            INV.patch(conn, biz, ctx["actor"], _uuid(iid, "item"), d)
    _flash(request, "saved")
    return _go(f"/app/inventory/{iid}")
