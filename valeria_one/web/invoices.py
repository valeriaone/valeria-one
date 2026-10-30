"""v0.18 on screen: invoices (the owner's list and page, the customer's page /inv/<token> in the business's look) and
subscriptions (plans, who is on them, MRR)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select

from .. import config, db, money, packs
from ..services import Invalid, NotFound
from ..services import invoices as IVS
from . import _ctx, _flash, _go, _guard, _multi_form, _need, _own, render

router = APIRouter(include_in_schema=False)


def _staff(request: Request) -> dict:
    ctx = _ctx(request)
    _need(request, ctx, "payments")
    return ctx


def _uuid(v, what: str = "invoice") -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


def _lines(f, currency: str) -> list[dict]:
    out = []
    for d, q, u in zip(f.getlist("line_desc"), f.getlist("line_qty"), f.getlist("line_unit")):
        if not str(d).strip():
            continue
        try:
            out.append({"description": str(d), "qty": int(q or 1),
                        "unit_minor": money.to_minor(str(u).replace(",", "").strip() or "0", currency)})
        except (ValueError, ArithmeticError):
            raise Invalid("check quantities and prices", "lines") from None
    return out


@router.get("/app/invoices")
@_guard
async def invoices_page(request: Request):
    ctx = _staff(request)
    biz = ctx["biz"]
    status = request.query_params.get("status")
    status = status if status in IVS.STATUSES else None
    from ..services import contacts as C
    with db.tx() as conn:
        rows = IVS.list_(conn, biz["id"], status=status)
        tot = IVS.totals(conn, biz["id"])
        plans = IVS.plans(conn, biz["id"])
        subs = IVS.subscriptions(conn, biz["id"], live_only=True)
        mrr = IVS.mrr(conn, biz["id"])
        people = C.search(conn, biz["id"], limit=200)
    tab = request.query_params.get("tab") if request.query_params.get("tab") in ("invoices", "subscriptions") else "invoices"
    return render(request, "invoices.html", ctx, rows=rows, tot=tot, plans=plans, subs=subs, mrr=mrr, people=people, tab=tab,
                  status=status, statuses=IVS.STATUSES, today=IVS.today_of(biz).isoformat(), page="invoices")


@router.post("/app/invoices")
@_guard
async def invoice_new(request: Request):
    ctx = _staff(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    cid = _uuid(f.get("contact_id"), "contact") if f.get("contact_id") else None
    try:
        due = int(f.get("due_days") or 15)
    except ValueError:
        raise Invalid("due in: a number of days", "due_days") from None
    with db.tx() as conn:
        inv = IVS.create(conn, biz, ctx["actor"], title=str(f.get("title") or ""), lines=_lines(f, biz["currency"]), contact_id=cid,
                         due_days=due, notes=str(f.get("notes") or ""))
    back = str(f.get("back") or "")
    return _go(back if back.startswith("/app/contacts/") else f"/app/invoices/{inv['id']}")


@router.get("/app/invoices/{iid}")
@_guard
async def invoice_owner(request: Request, iid: str):
    ctx = _staff(request)
    biz = ctx["biz"]
    from ..services import inbox as X
    from ..services import contacts as C
    with db.tx() as conn:
        inv = IVS.get(conn, biz["id"], _uuid(iid))
        contact = C.get(conn, biz["id"], inv["contact_id"]) if inv["contact_id"] else None
        ids = C.identities(conn, biz["id"], inv["contact_id"]) if inv["contact_id"] else []
        reach = [a for a in X.accounts(conn, biz["id"]) if any(i["channel"] == a["channel"] for i in ids)]
        pay = conn.execute(select(db.payments).where(db.payments.c.id == inv["payment_id"])).mappings().first() if inv["payment_id"] else None
        from ..services import payments as PM
        has_pay = bool(PM.accounts(conn, biz["id"]))
    return render(request, "invoice_owner.html", ctx, inv=inv, contact=contact, reach=reach, pay=pay, has_pay=has_pay,
                  url=IVS.url(inv), page="invoices")


@router.post("/app/invoices/{iid}")
@_guard
async def invoice_act(request: Request, iid: str):
    ctx = _staff(request)
    biz = ctx["biz"]
    f = await _multi_form(request)
    act = f.get("act")
    if act == "send":
        acc = _uuid(f.get("account_id"), "channel") if f.get("account_id") not in (None, "", "none") else None
        res = IVS.send(biz, ctx["actor"], _uuid(iid), channel_account_id=acc, message=f.get("account_id") != "none")
        if res.get("message_problem") and f.get("account_id") != "none":
            _flash(request, "iv_sent_no_message", "warn")
        else:
            _flash(request, "iv_sent")
        return _go(f"/app/invoices/{iid}")
    with db.tx() as conn:
        if act == "paid":
            IVS.mark_paid(conn, biz, ctx["actor"], _uuid(iid))
            _flash(request, "iv_marked_paid")
        elif act == "void":
            IVS.void(conn, biz, ctx["actor"], _uuid(iid))
        else:
            raise Invalid("?", "act")
    return _go(f"/app/invoices/{iid}")


@router.post("/app/quotes/{quote_id}/invoice")
@_guard
async def quote_to_invoice(request: Request, quote_id: UUID):
    ctx = _staff(request)
    await _multi_form(request)
    with db.tx() as conn:
        inv = IVS.from_quote(conn, ctx["biz"], ctx["actor"], quote_id)
    return _go(f"/app/invoices/{inv['id']}")


# ---------------------------------------------------------------- plans and subscriptions
@router.post("/app/plans")
@_guard
async def plan_new(request: Request):
    ctx = _own(request)
    _need(request, ctx, "payments")
    f = await _multi_form(request)
    try:
        amount = money.to_minor(str(f.get("amount") or "").replace(",", "").strip() or "0", ctx["biz"]["currency"])
        every = int(f.get("every") or 1)
    except (ValueError, ArithmeticError):
        raise Invalid("check the amount", "amount") from None
    with db.tx() as conn:
        IVS.create_plan(conn, ctx["biz"], ctx["actor"], name=str(f.get("name") or ""), amount_minor=amount,
                        interval=str(f.get("interval") or "month"), every=every)
    return _go("/app/invoices?tab=subscriptions")


@router.post("/app/plans/{plan_id}/archive")
@_guard
async def plan_archive(request: Request, plan_id: UUID):
    ctx = _own(request)
    await _multi_form(request)
    with db.tx() as conn:
        IVS.archive_plan(conn, ctx["biz"], ctx["actor"], plan_id)
    return _go("/app/invoices?tab=subscriptions")


@router.post("/app/subscriptions")
@_guard
async def subscription_new(request: Request):
    ctx = _staff(request)
    f = await _multi_form(request)
    start = None
    if f.get("start_on"):
        try:
            start = date.fromisoformat(str(f["start_on"]))
        except ValueError:
            raise Invalid("pick a date", "start_on") from None
    with db.tx() as conn:
        IVS.subscribe(conn, ctx["biz"], ctx["actor"], plan_id=_uuid(f.get("plan_id"), "plan"),
                      contact_id=_uuid(f.get("contact_id"), "contact"), start_on=start, auto_send=f.get("auto_send") == "1")
    _flash(request, "sb_added")
    back = str(f.get("back") or "")
    return _go(back if back.startswith("/app/contacts/") else "/app/invoices?tab=subscriptions")


@router.post("/app/subscriptions/{sub_id}")
@_guard
async def subscription_act(request: Request, sub_id: UUID):
    ctx = _staff(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        IVS.set_subscription(conn, ctx["biz"], ctx["actor"], sub_id, str(f.get("act") or ""))
    return _go("/app/invoices?tab=subscriptions")


# ---------------------------------------------------------------- the customer's invoice
@router.get("/inv/{token}")
@_guard
async def invoice_public(request: Request, token: str):
    with db.tx() as conn:
        inv, biz = IVS.by_token(conn, token)
        contact = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == inv["contact_id"])).scalar() if inv["contact_id"] else None
        pay = conn.execute(select(db.payments).where(db.payments.c.id == inv["payment_id"])).mappings().first() if inv["payment_id"] else None
    ctx = {"biz": biz, "user": None, "actor": "invoice_page", "role": None, "businesses": []}
    pay_url = (pay["url"] or f"{config.PUBLIC_URL()}/p/{pay['token']}") if pay and pay["status"] == "pending" else None
    overdue = inv["status"] in ("sent", "overdue") and inv["due_on"] < IVS.today_of(biz)
    return render(request, "invoice.html", ctx, inv=inv, contact_name=contact, pay_url=pay_url, pack=packs.country(biz["country"]),
                  overdue=overdue, now=datetime.now(timezone.utc))
