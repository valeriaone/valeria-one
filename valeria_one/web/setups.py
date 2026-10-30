"""v0.18 on screen: ready setups ("What kind of business? → set it all up for me"), with a preview of exactly what is
added, undo for a day, the owner's own setup out as a file (no customer data) and in again — or straight into another
business they own. And the saved replies those setups bring (Settings › Saved replies; one tap in the inbox)."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import db
from ..services import Invalid, NotFound
from ..services import replies as SR
from ..services import snapshots as SN
from . import _flash, _form, _go, _guard, _lang, _multi_form, _not_shared_demo, _own, render
from .i18n import t

router = APIRouter(include_in_schema=False)
MAX_FILE = 512 * 1024


def _cards(biz: dict, lang: str) -> list[dict]:
    out = []
    for d in SN.for_business(biz):
        out.append({"key": d["key"], "icon": d.get("icon") or "⭐", "name": t(f"sn_{d['key']}", lang), "about": t(f"sn_{d['key']}_d", lang),
                    "mine": biz["industry"] in (d.get("industries") or [])})
    return out


@router.get("/app/setups")
@_guard
async def setups_page(request: Request):
    ctx = _own(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        done = SN.applied(conn, biz["id"])
    others = [b for b in ctx["businesses"] if b["id"] != biz["id"]]
    return render(request, "setups.html", ctx, cards=_cards(biz, _lang(request, biz)), done=done, others=others, page="start")


def _preview_page(request: Request, ctx: dict, res: dict, *, key: str | None, bundle_json: str | None = None):
    biz = ctx["biz"]
    lang = _lang(request, biz)
    title = t(f"sn_{key}", lang) if key else t("sn_import_title", lang)
    return render(request, "setup_preview.html", ctx, res=res, key=key, title=title, bundle_json=bundle_json, parts=SN.PARTS,
                  page="start")


@router.get("/app/setups/{key}")
@_guard
async def setup_preview(request: Request, key: str):
    ctx = _own(request)
    if key not in SN.shipped():
        raise NotFound("setup")
    with db.tx() as conn:
        res = SN.apply_shipped(conn, ctx["biz"], ctx["actor"], key, dry=True)
    return _preview_page(request, ctx, res, key=key)


@router.post("/app/setups/{key}")
@_guard
async def setup_apply(request: Request, key: str):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    await _form(request)
    if key not in SN.shipped():
        raise NotFound("setup")
    with db.tx() as conn:
        res = SN.apply_shipped(conn, ctx["biz"], ctx["actor"], key)
    n = sum(len(v) for v in res["added"].values())
    _flash(request, "sn_applied", raw=t("sn_applied", _lang(request, ctx["biz"])).format(n=n))
    return _go("/app/setups#done")


@router.post("/app/setups/undo/{apply_id}")
@_guard
async def setup_undo(request: Request, apply_id: UUID):
    ctx = _own(request)
    await _form(request)
    with db.tx() as conn:
        res = SN.undo(conn, ctx["biz"], ctx["actor"], apply_id)
    lang = _lang(request, ctx["biz"])
    msg = t("sn_undone", lang).format(n=res["removed"])
    if res["kept"]:
        msg += " " + t("sn_undo_kept", lang).format(names=", ".join(res["kept"][:6]))
    _flash(request, "sn_undone", raw=msg)
    return _go("/app/setups#done")


@router.get("/app/setup-export.json")
@_guard
async def setup_export(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        data = SN.export(conn, ctx["biz"])
    return Response(json.dumps(data, ensure_ascii=False, indent=1, default=str), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="valeria-one-setup.json"'})


@router.post("/app/setup-import")
@_guard
async def setup_import(request: Request):
    """A setup file (or the confirmed one): preview first, then apply."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    raw = f.get("bundle") or ""
    up = f.get("file")
    if up is not None and hasattr(up, "read"):
        raw = (await up.read(MAX_FILE + 1)).decode("utf-8", errors="replace")
    if len(raw) > MAX_FILE:
        raise Invalid(t("sn_file_big", _lang(request, ctx["biz"])), "file")
    try:
        bundle = SN.check_bundle(json.loads(raw or "{}"))
    except ValueError:
        raise Invalid("this is not a Valeria One setup file", "file") from None
    with db.tx() as conn:
        if f.get("go") == "1":
            res = SN.apply(conn, ctx["biz"], ctx["actor"], bundle, "import")
            n = sum(len(v) for v in res["added"].values())
            _flash(request, "sn_applied", raw=t("sn_applied", _lang(request, ctx["biz"])).format(n=n))
            return _go("/app/setups#done")
        res = SN.preview(conn, ctx["biz"], ctx["actor"], bundle, "import")
    return _preview_page(request, ctx, res, key=None, bundle_json=json.dumps({"format": SN.FORMAT, **bundle}, ensure_ascii=False))


@router.post("/app/setup-copy")
@_guard
async def setup_copy(request: Request):
    """This business's setup into another business the same person owns (no customer data goes)."""
    ctx = _own(request)
    f = await _form(request)
    try:
        target = UUID(str(f.get("business_id")))
    except ValueError:
        raise NotFound("business") from None
    with db.tx() as conn:
        res = SN.copy_to(conn, ctx["user"]["id"] if ctx["user"] else UUID(int=0), ctx["biz"], target, ctx["actor"])
    n = sum(len(v) for v in res["added"].values())
    _flash(request, "sn_copied", raw=t("sn_copied", _lang(request, ctx["biz"])).format(n=n))
    return _go("/app/setups#copy")


# ---------------------------------------------------------------- saved replies
@router.get("/app/replies")
@_guard
async def replies_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        rows = SR.list_(conn, ctx["biz"]["id"])
    return render(request, "replies.html", ctx, rows=rows, page="settings")


@router.post("/app/replies")
@_guard
async def replies_save(request: Request):
    ctx = _own(request)
    f = await _form(request)
    with db.tx() as conn:
        if f.get("act") == "delete":
            try:
                SR.remove(conn, ctx["biz"]["id"], UUID(str(f.get("id"))))
            except ValueError:
                raise NotFound("reply") from None
        else:
            SR.add(conn, ctx["biz"], ctx["actor"], title=f.get("title", ""), body=f.get("body", ""))
    _flash(request, "saved")
    return _go("/app/replies")
