"""ars02 (v0.21) on screen: Affiliates (/app/affiliates) — the people who send customers, their own link, what they brought
and earned, what is owed, "I paid them" with its history, and a spreadsheet; the link itself (/a/<code>) and the Ads
helper (/app/ads: the words for an ad on the business's OWN ad accounts, and a tracked link /go/<code>)."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import db, money
from ..services import Invalid, NotFound
from ..services import ads as ADS
from ..services import affiliates as AF
from . import _flash, _form, _go, _guard, _multi_form, _not_shared_demo, _own, render

router = APIRouter(include_in_schema=False)


def _uuid(v, what: str = "affiliate") -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


def _data(biz: dict, f) -> dict:
    """The form -> the service's numbers: a % as basis points, an amount in the smallest unit."""
    kind = f.get("kind") if f.get("kind") in ("pct", "fixed") else "pct"
    try:
        rate = money.to_minor(str(f.get("rate") or "0").replace(",", ".").rstrip("%").strip() or "0", "USD") if kind == "pct" else 0
        fixed = money.to_minor(str(f.get("fixed") or "0").replace(",", "").strip() or "0", biz["currency"]) if kind == "fixed" else 0
    except (ValueError, ArithmeticError):
        raise Invalid("check the commission", "rate") from None
    return {"name": f.get("name"), "email": f.get("email"), "phone": f.get("phone"), "kind": kind, "rate_bp": rate,
            "fixed_minor": fixed, "first_only": f.get("first_only") == "1", "note": f.get("note"), "active": f.get("active", "1") == "1"}


@router.get("/app/affiliates")
@_guard
async def affiliates_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        rows = [_pct(a) for a in AF.list_(conn, ctx["biz"]["id"])]
    return render(request, "affiliates.html", ctx, rows=rows, url=AF.url, days=AF.COMMISSION_DAYS, page="affiliates")


def _pct(a: dict) -> dict:
    """1250 basis points -> "12.5" (what the owner typed), for the screen."""
    return {**a, "rate": money.to_major(a["rate_bp"], "USD")}


@router.post("/app/affiliates")
@_guard
async def affiliates_new(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    with db.tx() as conn:
        a = AF.save(conn, ctx["biz"], ctx["actor"], _data(ctx["biz"], f))
    return _go(f"/app/affiliates/{a['id']}")


@router.get("/app/affiliates.csv")
@_guard
async def affiliates_csv(request: Request):
    ctx = _own(request)
    from .pos import _csv
    with db.tx() as conn:
        body = AF.report_csv(conn, ctx["biz"], ctx["actor"])
    return _csv(body, "affiliates.csv")


@router.get("/app/affiliates/{aid}")
@_guard
async def affiliate_page(request: Request, aid: str):
    ctx = _own(request)
    biz = ctx["biz"]
    from ..services import contacts as C
    with db.tx() as conn:
        a = _pct(AF.get(conn, biz["id"], _uuid(aid)))
        people = AF.customers(conn, biz["id"], a["id"])
        lines = AF.lines(conn, biz["id"], a["id"])
        owed = AF.owed(conn, biz["id"], a["id"])
        pays = AF.payouts(conn, biz["id"], a["id"])
        everyone = C.search(conn, biz["id"], limit=300)
    from ..services import invoices as IVS
    return render(request, "affiliate.html", ctx, a=a, people=people, lines=lines, owed=owed, pays=pays, everyone=everyone,
                  link=AF.url(a["code"]), today=IVS.today_of(biz), page="affiliates")


@router.post("/app/affiliates/{aid}")
@_guard
async def affiliate_act(request: Request, aid: str):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    act, biz, au = f.get("act") or "", ctx["biz"], _uuid(aid)
    with db.tx() as conn:
        if act == "save":
            AF.save(conn, biz, ctx["actor"], _data(biz, f), au)
            _flash(request, "saved")
        elif act == "customer":
            AF.assign(conn, biz, ctx["actor"], au, _uuid(f.get("contact_id"), "contact"))
        elif act == "paid":
            try:
                day = date.fromisoformat(str(f.get("paid_on") or ""))
            except ValueError:
                raise Invalid("pick the day you paid", "paid_on") from None
            AF.pay(conn, biz, ctx["actor"], au, paid_on=day, note=f.get("note", ""))
            _flash(request, "af_paid_saved")
        else:
            raise Invalid("?", "act")
    return _go(f"/app/affiliates/{aid}")


@router.get("/a/{code}")
async def affiliate_link(request: Request, code: str):
    """An affiliate's link: counted, remembered on this phone (14 days), then the business's page."""
    try:
        a, biz = AF.open_link(code)
    except NotFound:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("Not found", status_code=404)
    request.session["aff"] = AF.remember(a)
    return RedirectResponse(_landing(biz), status_code=302)


def _landing(biz: dict) -> str:
    """Where a link opens when it doesn't say: the business's website, else its booking page, else its directory card."""
    if biz.get("slug") and biz.get("site_enabled"):
        return f"/s/{biz['slug']}"
    if biz.get("slug") and biz.get("booking_enabled"):
        return f"/b/{biz['slug']}"
    with db.tx() as conn:
        from ..services import funnels as FU
        live = next((f for f in FU.list_(conn, biz["id"]) if f["active"] and f["first"]), None)
    if live and biz.get("slug"):
        return f"/s/{biz['slug']}/p/{live['first']}"
    return f"/directory/{biz['slug']}" if biz.get("slug") else "/"


# ---------------------------------------------------------------- the ads helper
@router.get("/app/ads")
@_guard
async def ads_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        rows = ADS.list_(conn, ctx["biz"]["id"])
        dests = ADS.destinations(conn, ctx["biz"])
    return render(request, "ads.html", ctx, rows=rows, dests=dests, platforms=ADS.PLATFORMS, link=ADS.link, page="ads")


@router.post("/app/ads")
@_guard
async def ads_new(request: Request):
    """A new ad from the owner's idea: the words drafted (their own AI key, or a plain draft), a tracked link made."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    biz = ctx["biz"]
    platform = str(f.get("platform") or "meta")
    dest = str(f.get("dest_custom") or "").strip() or str(f.get("dest") or "")
    got = ADS.draft(biz, idea=str(f.get("idea") or ""), platform=platform)
    with db.tx() as conn:
        ad = ADS.save(conn, biz, ctx["actor"], {"name": f.get("name") or str(f.get("idea") or "")[:60], "platform": platform,
                                                "dest": dest, "idea": f.get("idea"), "headlines": got["headlines"], "body": got["body"],
                                                "audience": got["audience"], "by_ai": got["by_ai"]})
    _flash(request, "ad_ai_done" if got["by_ai"] else "ad_plain_done")
    return _go(f"/app/ads/{ad['id']}")


@router.get("/app/ads/{aid}")
@_guard
async def ad_page(request: Request, aid: str):
    ctx = _own(request)
    with db.tx() as conn:
        ad = ADS.get(conn, ctx["biz"]["id"], _uuid(aid, "ad"))
    return render(request, "ad.html", ctx, ad=ad, link=ADS.link(ad), landing=ADS.landing(ad), manager=ADS.MANAGERS[ad["platform"]],
                  warn=ADS.warnings(ad), limits=ADS.LIMITS[ad["platform"]], platforms=ADS.PLATFORMS, page="ads")


@router.post("/app/ads/{aid}")
@_guard
async def ad_act(request: Request, aid: str):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _form(request)
    act, biz, au = f.get("act") or "save", ctx["biz"], _uuid(aid, "ad")
    if act == "redraft":
        with db.tx() as conn:
            ad = ADS.get(conn, biz["id"], au)
        got = ADS.draft(biz, idea=f.get("idea") or ad["idea"], platform=ad["platform"])
        with db.tx() as conn:
            ADS.save(conn, biz, ctx["actor"], {**ad, "idea": f.get("idea") or ad["idea"], "headlines": got["headlines"],
                                               "body": got["body"], "audience": got["audience"], "by_ai": got["by_ai"]}, au)
        _flash(request, "ad_ai_done" if got["by_ai"] else "ad_plain_done")
        return _go(f"/app/ads/{aid}")
    with db.tx() as conn:
        if act == "delete":
            ADS.delete(conn, biz, ctx["actor"], au)
            return _go("/app/ads")
        ADS.save(conn, biz, ctx["actor"], {"name": f.get("name"), "platform": f.get("platform"), "dest": f.get("dest"),
                                           "idea": f.get("idea"), "headlines": f.get("headline"), "body": f.get("body"),
                                           "audience": f.get("audience")}, au)
    _flash(request, "saved")
    return _go(f"/app/ads/{aid}")


@router.get("/go/{code}")
async def ad_link(request: Request, code: str):
    """An ad's tracked link: counted, remembered on this phone, then the ad's page with its utm_* added."""
    try:
        ad, _ = ADS.open_link(code)
    except NotFound:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("Not found", status_code=404)
    request.session["ad"] = ADS.remember(ad)
    return RedirectResponse(ADS.landing(ad), status_code=302)
