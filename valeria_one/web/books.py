"""ars01 (v0.20) on screen: Books (/app/books) — the owner's money in and out, by month and category, the receipts, and
the spreadsheet for the accountant."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request

from .. import db
from ..services import Invalid
from ..services import books as BK
from . import _flash, _go, _guard, _multi_form, _own, render
from .staff import _amt, _csv, _day, _uuid

router = APIRouter(include_in_schema=False)


@router.get("/app/books")
@_guard
async def books_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    today = BK.today_of(biz)
    try:
        y, m = (int(x) for x in (request.query_params.get("m") or "").split("-"))
        date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
    from ..services import projects as PR
    with db.tx() as conn:
        mo = BK.month(conn, biz, y, m)
        yr = BK.year(conn, biz, y)
        voided = [r for r in BK.ledger(conn, biz, mo["from"], mo["to"], with_void=True) if r["voided"]]
        sups = PR.suppliers(conn, biz["id"])
    from babel.dates import format_date
    from . import _lang, _locale
    lang = _lang(request, biz)

    def mname(yy: int, mm: int, f: str) -> str:
        for where in (_locale(biz, lang), lang):
            try:
                return format_date(date(yy, mm, 1), f, locale=where)
            except Exception:                                    # a language + country Babel doesn't pair
                continue
        return f"{yy}-{mm:02d}"
    prev = date(y - (m == 1), 12 if m == 1 else m - 1, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return render(request, "books.html", ctx, page="books", mo=mo, yr=yr, voided=voided, suppliers=sups, outs=BK.OUT, ins=BK.IN,
                  prev=prev, nxt=nxt if nxt <= today.replace(day=1) else None, today=today, y=y, m=m, mname=mname)


@router.post("/app/books")
@_guard
async def books_add(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    from ..services import files as FL
    from .studio import _upload
    f, ups = await _upload(request)
    amount = _amt(biz, f.get("amount"), "amount")
    with db.tx() as conn:
        fid = None
        if ups:
            raw, mime, name = ups[0]
            fid = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name or "receipt",
                          kind="doc" if mime == "application/pdf" else "photo")["id"]
        e = BK.add(conn, biz, ctx["actor"], direction=str(f.get("direction") or "out"), on_date=f.get("on_date") or BK.today_of(biz),
                   amount_minor=amount or 0, category=str(f.get("category") or ""), description=str(f.get("description") or ""),
                   supplier_id=_uuid(f.get("supplier_id"), "supplier"), file_id=fid)
    _flash(request, "saved")
    return _go(f"/app/books?m={e['on_date'].year}-{e['on_date'].month:02d}")


@router.post("/app/books/{eid}/void")
@_guard
async def books_void(request: Request, eid: str):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        e = BK.void(conn, ctx["biz"], ctx["actor"], _uuid(eid, "entry"), str(f.get("reason") or ""))
    return _go(f"/app/books?m={e['on_date'].year}-{e['on_date'].month:02d}")


@router.get("/app/books.csv")
@_guard
async def books_csv(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    today = BK.today_of(biz)
    start = _day(request.query_params.get("from"), date(today.year, 1, 1))
    end = _day(request.query_params.get("to"), today)
    if end < start or (end - start).days > 3660:
        raise Invalid("check the days", "to")
    with db.tx() as conn:
        body = BK.csv_out(conn, biz, ctx["actor"], start, end)
    return _csv(body, f"books-{start.isoformat()}-{end.isoformat()}.csv")
