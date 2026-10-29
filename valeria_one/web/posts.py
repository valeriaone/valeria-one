"""Posts on screen (/app/posts): write (or let the AI write) a post, approve it, publish now or schedule it, and see
what is waiting, what went out and what didn't (with the reason, calmly). Plus the signed picture address that
Meta and Google fetch (/pf/…)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import db
from ..services import Invalid
from ..services import ai as AI
from ..services import posts as PO
from . import _ctx, _flash, _go, _guard, _multi_form, _need, _not_shared_demo, render

router = APIRouter(include_in_schema=False)


def _pctx(request: Request) -> dict:
    ctx = _ctx(request)
    if ctx["role"] != "owner":
        _flash(request, "owners_only", "warn")
        from . import _Redirect
        raise _Redirect("/app")
    _need(request, ctx, "posts")
    return ctx


async def _fields(request: Request) -> tuple[dict, list, tuple | None]:
    """The form: (fields, the networks ticked, the picture uploaded if any)."""
    f = await _multi_form(request)
    fields, nets, up = {}, [], None
    for k, v in f.multi_items():
        if hasattr(v, "read"):
            raw = await v.read()
            if raw:
                up = (raw, v.content_type or "", v.filename or "")
        elif k == "net":
            nets.append(str(v))
        else:
            fields[k] = str(v)
    return fields, nets, up


def _when(biz: dict, v: str) -> datetime | None:
    """'2026-11-03T10:30' typed in the business's own time zone."""
    if not (v or "").strip():
        return None
    try:
        return datetime.fromisoformat(v.strip()).replace(tzinfo=ZoneInfo(biz["timezone"]))
    except ValueError:
        raise Invalid("pick a date and time", "when") from None


@router.get("/app/posts")
@_guard
async def posts_page(request: Request):
    ctx = _pctx(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        rows = PO.list_(conn, biz["id"])
        nets = PO.networks(conn, biz)
        ai_on = AI.public_settings(conn, biz["id"])["has_key"]
    cal = PO.calendar(rows, ZoneInfo(biz["timezone"]))
    groups = {s: [r for r in rows if r["status"] == s] for s in ("draft", "failed", "publishing", "published")}
    return render(request, "posts.html", ctx, rows=rows, nets=nets, cal=cal, groups=groups, ai_on=ai_on, kinds=PO.KINDS,
                  page="posts")


@router.post("/app/posts")
@_guard
async def posts_new(request: Request):
    """A new post: the AI drafts it from the idea (act=ai) or the owner writes it (act=manual, or when there is no AI)."""
    ctx = _pctx(request)
    _not_shared_demo(request, ctx)
    f, nets, up = await _fields(request)
    biz = ctx["biz"]
    texts, by_ai = {}, False
    if f.get("act") == "ai":
        try:
            texts, by_ai = PO.draft(biz, ctx["actor"], kind=f.get("kind") or "idea", idea=f.get("idea", ""), nets=nets), True
        except AI.AIError:
            _flash(request, "po_no_ai", "warn")              # no AI right now: the owner writes it in the boxes
    else:
        texts = {n: f.get("text", "") for n in nets}
    with db.tx() as conn:
        fid = PO.save_picture(conn, biz, ctx["actor"], *up)["id"] if up else None
        p = PO.create(conn, biz, ctx["actor"], kind=f.get("kind") or "idea", idea=f.get("idea", ""), nets=nets, texts=texts,
                      file_id=fid, by_ai=by_ai)
    return _go(f"/app/posts/{p['id']}")


@router.get("/app/posts/{post_id}")
@_guard
async def post_page(request: Request, post_id: UUID):
    ctx = _pctx(request)
    with db.tx() as conn:
        p = PO.get(conn, ctx["biz"]["id"], post_id)
        nets = PO.networks(conn, ctx["biz"])
        img = PO.image_url(conn, ctx["biz"], p["file_id"]) if p["file_id"] else None
    when = p["publish_at"].astimezone(ZoneInfo(ctx["biz"]["timezone"])).strftime("%Y-%m-%dT%H:%M") if p["publish_at"] else ""
    return render(request, "post.html", ctx, p=p, nets=nets, img=img, when=when, max_text=PO.MAX_TEXT, page="posts")


@router.post("/app/posts/{post_id}")
@_guard
async def post_act(request: Request, post_id: UUID):
    """save · now (publish now) · schedule · draft (un-schedule) · retry · cancel."""
    ctx = _pctx(request)
    _not_shared_demo(request, ctx)
    f, nets, up = await _fields(request)
    biz, act = ctx["biz"], f.get("act", "save")
    if act in ("save", "now", "schedule"):
        with db.tx() as conn:
            p = PO.get(conn, biz["id"], post_id)
            if p["status"] in PO.EDITABLE:
                fid = PO.save_picture(conn, biz, ctx["actor"], *up)["id"] if up else ("" if f.get("no_picture") == "1" else "keep")
                PO.save(conn, biz, ctx["actor"], post_id, nets=nets, texts={n: f.get(f"text_{n}", "") for n in PO.NETWORKS},
                        file_id=fid)
    if act == "now":
        p = PO.publish_now(biz, ctx["actor"], post_id)
        _flash(request, "po_published" if p["status"] == "published" else "po_failed", "ok" if p["status"] == "published" else "warn")
    elif act == "schedule":
        when = _when(biz, f.get("when", ""))
        if not when:
            raise Invalid("pick a date and time", "when")
        with db.tx() as conn:
            PO.approve(conn, biz, ctx["actor"], post_id, when)
        _flash(request, "po_scheduled")
    elif act in ("draft", "retry", "cancel"):
        with db.tx() as conn:
            {"draft": PO.back_to_draft, "retry": PO.retry, "cancel": PO.cancel}[act](conn, biz, ctx["actor"], post_id)
        if act == "cancel":
            return _go("/app/posts")
    else:
        _flash(request, "saved")
    return _go(f"/app/posts/{post_id}")


@router.get("/pf/{file_id}/{exp}/{sig}")
async def post_picture(file_id: UUID, exp: int, sig: str):
    """A post's picture for Meta and Google to fetch: a signed address that stops working after 3 days."""
    if not PO.check_signed(file_id, exp, sig):
        return Response("Not found", status_code=404)
    F = db.files
    with db.tx() as conn:
        from sqlalchemy import select
        row = conn.execute(select(F.c.data, F.c.mime).where(F.c.id == file_id, F.c.kind == "post")).first()
    if not row:
        return Response("Not found", status_code=404)
    return Response(row.data, media_type=row.mime, headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff",
                                                            "X-Robots-Tag": "noindex"})
