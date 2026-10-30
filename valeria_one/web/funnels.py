"""ars02 (v0.21) on screen: Funnels (/app/funnels) — the owner builds landing pages in steps from blocks (add, move up or
down, remove: no dragging needed, works on a phone) — and the pages themselves, public at /s/<business>/p/<page>, in the
business's own look. A page's form is one of the business's own forms; sending it makes the customer (and counts a lead)."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import config, db
from ..services import Invalid, NotFound
from ..services import funnels as FU
from ..services import invoices as IVS
from . import _flash, _go, _guard, _lang, _limit, _multi_form, _not_shared_demo, _own, render
from .i18n import t

router = APIRouter(include_in_schema=False)
_fn_hits: dict = {}


def _uuid(v, what: str = "funnel") -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


def _base(biz: dict) -> str:
    return f"{config.PUBLIC_URL()}/s/{biz['slug']}/p/" if biz.get("slug") else ""


# ---------------------------------------------------------------- the office
@router.get("/app/funnels")
@_guard
async def funnels_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        rows = FU.list_(conn, biz["id"])
    return render(request, "funnels.html", ctx, rows=rows, starters=FU.suggested(biz["industry"]), base=_base(biz), page="funnels")


@router.post("/app/funnels")
@_guard
async def funnels_new(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    with db.tx() as conn:
        fn = FU.create(conn, ctx["biz"], ctx["actor"], str(f.get("name") or ""), str(f.get("template") or "") or None,
                       _lang(request, ctx["biz"]))
    return _go(f"/app/funnels/{fn['id']}")


@router.get("/app/funnels/{fid}")
@_guard
async def funnel_page(request: Request, fid: str):
    ctx = _own(request)
    biz = ctx["biz"]
    days = 30 if request.query_params.get("days") != "all" else None
    with db.tx() as conn:
        fn = FU.get(conn, biz["id"], _uuid(fid))
        st = FU.stats(conn, biz["id"], fn["id"], since=(IVS.today_of(biz) - timedelta(days=days)) if days else None)
        from ..services import businesses as B
        biz = B.get(conn, biz["id"])
    return render(request, "funnel.html", {**ctx, "biz": biz}, fn=fn, st=st, base=_base(biz), days=days, page="funnels")


@router.post("/app/funnels/{fid}")
@_guard
async def funnel_act(request: Request, fid: str):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    act = str(f.get("act") or "")
    biz, fu = ctx["biz"], _uuid(fid)
    with db.tx() as conn:
        if act == "save":
            FU.save(conn, biz, ctx["actor"], fu, name=f.get("name"))
        elif act in ("on", "off"):
            FU.save(conn, biz, ctx["actor"], fu, active=act == "on")
            _flash(request, "fn_live" if act == "on" else "fn_paused")
        elif act == "delete":
            FU.delete(conn, biz, ctx["actor"], fu)
            return _go("/app/funnels")
        elif act == "add_page":
            p = FU.add_page(conn, biz, ctx["actor"], fu, str(f.get("title") or ""))
            return _go(f"/app/funnels/pages/{p['id']}")
        elif act.split(":")[0] in ("up", "down", "drop"):
            what, _, pid = act.partition(":")
            if what == "drop":
                FU.delete_page(conn, biz, ctx["actor"], _uuid(pid, "page"))
            else:
                FU.move_page(conn, biz, ctx["actor"], _uuid(pid, "page"), 1 if what == "down" else -1)
        else:
            raise Invalid("?", "act")
    return _go(f"/app/funnels/{fid}")


def _blocks_from(f) -> tuple[list[dict], dict]:
    """The editor's fields b-<i>-<name> -> blocks in order; and the pictures sent, by block index."""
    blocks: dict[int, dict] = {}
    for k, v in f.multi_items():
        if not k.startswith("b-") or hasattr(v, "read"):
            continue
        _, i, name = k.split("-", 2)
        if i.isdigit():
            blocks.setdefault(int(i), {})[name] = str(v)
    out = []
    for i in sorted(blocks):
        b = blocks[i]
        if b.get("t") == "faq":
            b["items"] = b.get("items", "")
        out.append(b)
    return out, blocks


@router.get("/app/funnels/pages/{pid}")
@_guard
async def funnel_page_edit(request: Request, pid: str):
    ctx = _own(request)
    biz = ctx["biz"]
    from ..services import forms as FM
    with db.tx() as conn:
        p = FU.page(conn, biz["id"], _uuid(pid, "page"))
        fn = FU.get(conn, biz["id"], p["funnel_id"])
        forms = FM.list_(conn, biz["id"])
        from ..services import businesses as B
        biz = B.get(conn, biz["id"])
    new = request.session.pop("fn_new", None)
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(biz["timezone"])
    return render(request, "funnel_page.html", {**ctx, "biz": biz}, p=p, fn=fn, forms=forms, types=FU.BLOCK_TYPES, goes=FU.GOES,
                  base=_base(biz), new_kind=new["t"] if isinstance(new, dict) and new.get("p") == pid else None,
                  local=lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%dT%H:%M") if iso else "",
                  page="funnels")


@router.post("/app/funnels/pages/{pid}")
@_guard
async def funnel_page_save(request: Request, pid: str):
    """Every button of the editor saves what is on screen first, then does its one thing (add, up, down, remove)."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    biz = ctx["biz"]
    f = await _multi_form(request)
    raw, _ = _blocks_from(f)
    for b in raw:                                              # the owner types a price as money and a date in their own time
        for k in ("price", "was"):
            if k in b:
                b[k] = _amt(biz, b[k])
        if b.get("t") == "countdown" and b.get("until"):
            b["until"] = _local(biz, b["until"])
        if b.get("t") == "video" and b.get("url") is not None:
            b.pop("p", None)
            b.pop("id", None)
    act, _, i = str(f.get("act") or "save").partition(":")
    pu = _uuid(pid, "page")
    from ..services import files as FL
    with db.tx() as conn:
        for k, v in f.multi_items():                            # a picture for a block: kept, public only while the funnel is on
            if k.startswith("b-") and k.endswith("-upload") and hasattr(v, "read"):
                data = await v.read()
                n = int(k.split("-")[1])
                if data and n < len(raw):
                    up = FL.save(conn, biz, ctx["actor"], raw=data, mime=v.content_type or "", name=v.filename or "funnel", kind="photo")
                    conn.execute(db.files.update().where(db.files.c.id == up["id"]).values(public=True))
                    raw[n]["file"] = str(up["id"])
        if act == "add":                                        # a new block shows, empty, at the end until it is filled in
            kind = str(f.get("kind") or "")
            if kind not in FU.BLOCK_TYPES:
                raise Invalid("pick a block", "kind")
            request.session["fn_new"] = {"p": pid, "t": kind}
        elif act != "save":
            raw = FU.block_edit(raw, act, int(i) if i.isdigit() else -1)
        p = FU.save_page(conn, biz, ctx["actor"], pu, title=f.get("title"), slug=f.get("slug"), blocks=raw)
    if act == "save":
        _flash(request, "saved")
    return _go(f"/app/funnels/pages/{p['id']}" + ("#new" if act == "add" else f"#b{i}" if i.isdigit() else ""))


def _amt(biz: dict, v) -> int | None:
    from .. import money
    v = str(v or "").replace(",", "").strip()
    if not v:
        return None
    try:
        n = money.to_minor(v, biz["currency"])
    except (ValueError, ArithmeticError):
        raise Invalid("check the price", "price") from None
    if n < 0:
        raise Invalid("check the price", "price")
    return n


def _local(biz: dict, v: str):
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(str(v).strip()).replace(tzinfo=ZoneInfo(biz["timezone"]))
    except ValueError:
        raise Invalid("countdown: pick a date and time", "until") from None


# ---------------------------------------------------------------- the public page
def _viewer_owns(request: Request, biz: dict) -> bool:
    """The business's own people see a page that is not live yet (a preview), and are never counted."""
    return request.session.get("business_id") == str(biz["id"]) and bool(request.session.get("user_id"))


def _target(biz: dict, b: dict, nxt: dict | None) -> str | None:
    to = b.get("to") or "next"
    if to == "url":
        return b.get("url")
    if to == "book":
        return f"/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else None
    return f"/s/{biz['slug']}/p/{nxt['slug']}" if nxt else None


def _public(request: Request, slug: str, page_slug: str, *, error: str | None = None, values: dict | None = None,
            sent: bool = False, status: int = 200):
    from ..services import forms as FM
    from .crm import _signer
    with db.tx() as conn:
        biz, fn, p, nxt = FU.public(conn, slug, page_slug)
        mine = _viewer_owns(request, biz)
        if not fn["active"] and not mine:
            raise NotFound("page")
        forms = {}
        for b in p["blocks"]:
            if b["t"] == "form" and b["form"] not in forms:
                try:
                    fm = FM.get(conn, biz["id"], UUID(b["form"]))
                except NotFound:
                    continue
                if fm["active"]:
                    forms[b["form"]] = {"fm": fm, "shown": FM.shown(conn, biz, fm)}
        if fn["active"] and not mine and request.method == "GET" and status == 200:
            FU.hit(conn, biz, p["id"], request.client.host if request.client else "?")
    ctx = {"biz": biz, "user": None, "actor": "funnel", "role": None, "businesses": []}
    blocks = [{**b, "href": _target(biz, b, nxt) if b["t"] in ("hero", "offer", "button") else None,
               "src": FU.video_src(b) if b["t"] == "video" else None} for b in p["blocks"]]
    resp = render(request, "funnel_public.html", ctx, s=biz["site"] or {}, p=p, fn=fn, blocks=blocks, forms=forms,
                  preview=not fn["active"], token=_signer().dumps(f"fn:{p['id']}"), error=error, values=values or {}, sent=sent,
                  book=f"/b/{biz['slug']}" if biz.get("booking_enabled") else None,
                  next_href=f"/s/{biz['slug']}/p/{nxt['slug']}" if nxt else None)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    if not fn["active"] or biz["sandbox"]:
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/s/{slug}/p/{page_slug}")
@_guard
async def funnel_public(request: Request, slug: str, page_slug: str):
    return _public(request, slug, page_slug, sent=request.query_params.get("sent") == "1")


@router.post("/s/{slug}/p/{page_slug}")
@_guard
async def funnel_form(request: Request, slug: str, page_slug: str):
    """A page's form was sent: the customer (forms.submit: their consent boxes, the inbox, a deal if the form makes one),
    a lead counted for this step, who brought them (an affiliate, an ad), then the next step."""
    from itsdangerous import BadSignature
    from ..services import forms as FM
    from . import _attribute
    from .crm import _signer
    raw = await request.form()
    f = {k: v for k, v in raw.multi_items() if isinstance(v, str)}
    with db.tx() as conn:
        biz, fn, p, nxt = FU.public(conn, slug, page_slug)
    if f.get("website"):                                      # honeypot
        return _go(f"/s/{slug}/p/{page_slug}?sent=1")
    try:
        if _signer().loads(f.get("token", ""), max_age=2 * 86400) != f"fn:{p['id']}":
            raise BadSignature("other page")
    except BadSignature:
        return _public(request, slug, page_slug, error=t("fm_expired", _lang(request, biz)), values=f, status=400)
    if not _limit(_fn_hits, request, 10):
        return _public(request, slug, page_slug, error=t("fm_too_many", _lang(request, biz)), values=f, status=429)
    form_id = str(f.get("form") or "")
    if not fn["active"] or not any(b["t"] == "form" and b["form"] == form_id for b in p["blocks"]):
        raise NotFound("page")
    lang = _lang(request, biz)
    try:
        with db.tx() as conn:
            fm = FM.get(conn, biz["id"], UUID(form_id))
            if not fm["active"]:
                raise NotFound("form")
            res = FM.submit(conn, biz, fm, f, request_consent=f.get("consent_request") == "1",
                            marketing_consent=f.get("consent_marketing") == "1", lang=lang)
            FU.hit(conn, biz, p["id"], request.client.host if request.client else "?", "lead")
            _attribute(request, conn, biz, res["contact"])
    except Invalid as e:
        return _public(request, slug, page_slug, error=str(e), values=f, status=400)
    return _go(f"/s/{slug}/p/{nxt['slug']}" if nxt else f"/s/{slug}/p/{page_slug}?sent=1")


@router.get("/s/{slug}/fi/{fid}")
async def funnel_image(slug: str, fid: UUID):
    """A funnel's picture: only while a live page of that business uses it."""
    from sqlalchemy import select
    from .studio import _file
    with db.tx() as conn:
        bid = conn.execute(select(db.businesses.c.id).where(db.businesses.c.slug == slug, db.businesses.c.suspended_at.is_(None))).scalar()
        row = conn.execute(select(db.files.c.data, db.files.c.mime).where(
            db.files.c.business_id == bid, db.files.c.id == fid, db.files.c.public.is_(True))).first() if bid else None
        if not row or not FU.file_ok(conn, bid, fid):
            return Response("Not found", status_code=404)
    return _file(row.data, row.mime, private=False)
