"""Projects and the design studio, on screen: the team's office (/app/projects…) and the customer's portal (/portal/<token>).

The portal needs no password: the link is the key, sent by WhatsApp (or email). The customer follows the
work, approves or turns down each piece, likes or comments on boards and pictures, signs quotes and pays —
all from their phone. Every action lands in the project's story, and the owners get an email.
"""
from __future__ import annotations

import json
import secrets

import httpx
from urllib.parse import quote as urlquote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select

from .. import blocks, config, db, money, packs
from ..services import Blocked, Invalid, NotFound
from ..services import ai as AI
from ..services import files as FL
from ..services import projects as PR
from ..services import studio as ST
from . import (_Redirect, _ctx, _flash, _form, _go, _guard, _lang, _limit, _multi_form, _need, _own, render)
from .i18n import t, ui_lang

router = APIRouter(include_in_schema=False)
TABS = ("overview", "rooms", "design", "selections", "orders", "tasks", "money")
_portal_hits: dict = {}
_preview_hits: dict = {}


def _pctx(request: Request, block: str = "projects") -> dict:
    ctx = _ctx(request)
    _need(request, ctx, block)
    return ctx


def _uuid(v) -> UUID | None:
    try:
        return UUID(str(v)) if v not in (None, "") else None
    except ValueError:
        raise Invalid("something on the form is out of date; reload the page", "id") from None


def _amount(biz: dict, v) -> int | None:
    v = str(v or "").replace(",", "").strip()
    if not v:
        return None
    try:
        return money.to_minor(v, biz["currency"])
    except Exception:
        raise Invalid("check the amount", "amount") from None


def _owners(conn, business_id) -> list[str]:
    return [r[0] for r in conn.execute(select(db.users.c.email).join(db.memberships, db.memberships.c.user_id == db.users.c.id)
                                       .where(db.memberships.c.business_id == business_id, db.memberships.c.role == "owner"))]


def _tell_owners(biz: dict, project: dict, what: str) -> None:
    """The customer did something in their portal: one short email to the owners (when this server sends email)."""
    from ..services import mailer
    if not mailer.configured():
        return
    with db.tx() as conn:
        to = _owners(conn, biz["id"])
    lang = ui_lang(biz["language"])
    for o in to:
        mailer.send(o, t("st_mail_subject", lang).format(project=project["name"]),
                    t("st_mail_body", lang).format(project=project["name"], what=what,
                                                   link=f"{config.PUBLIC_URL()}/app/projects/{project['id']}"))


def portal_url(p: dict) -> str:
    return f"{config.PUBLIC_URL()}/portal/{p['token']}"


def _wa_link(phone: str | None, text: str) -> str:
    """wa.me with the message written: works from any phone with no WhatsApp API at all."""
    num = (phone or "").lstrip("+")
    return f"https://wa.me/{num}?text={urlquote(text)}"


async def _upload(request: Request) -> tuple[dict, list]:
    """A form with files: (fields, [(bytes, mime, name)])."""
    f = await _multi_form(request)
    fields, ups = {}, []
    for k, v in f.multi_items():
        if hasattr(v, "read"):
            raw = await v.read()
            if raw:
                ups.append((raw, v.content_type or "", v.filename or ""))
        else:
            fields[k] = v
    return fields, ups


async def _work_upload(request: Request) -> tuple[dict, list]:
    """A form that may carry design work files (up to 100 MB each): (fields, [UploadFile]) — each file is read only when
    it is saved, one at a time, so a form with several big files never holds them all in memory at once."""
    try:
        n = int(request.headers.get("content-length") or 0)
    except ValueError:
        n = 0
    if n > 20 * FL.MAX_IN:                                    # what one form could always send (20 files of 25 MB)
        raise Invalid("too much in one go; send the big files a few at a time", "file")
    f = await _multi_form(request)
    fields, ups = {}, []
    for k, v in f.multi_items():
        if hasattr(v, "read"):
            if v.filename or (v.size or 0):
                ups.append(v)
        else:
            fields[k] = v
    return fields, ups


async def _read_one(up) -> tuple[bytes, str, str]:
    """One uploaded file's bytes, refused before reading when it is plainly too big."""
    top = FL.MAX_WORK if FL.is_work_name(up.filename or "") else FL.MAX_IN
    if (up.size or 0) > top:
        raise Invalid("work files up to 100 MB" if top == FL.MAX_WORK else "files up to 25 MB", "file")
    return await up.read(), up.content_type or "", up.filename or ""


# ---------------------------------------------------------------- the list
@router.get("/app/projects")
@_guard
async def projects_page(request: Request):
    ctx = _pctx(request)
    biz = ctx["biz"]
    status = request.query_params.get("status") or "active"
    if status not in ("active", "done", "cancelled"):
        status = "active"
    from ..services import contacts as C
    with db.tx() as conn:
        rows = PR.search(conn, biz["id"], status=status)
        stats = ST.stats(conn, biz["id"])
        people = C.search(conn, biz["id"], limit=500)
        for r in rows:
            r["totals"] = PR.totals(conn, biz, r)
    return render(request, "projects.html", ctx, page="projects", rows=rows, stats=stats, status=status, people=people,
                  stages=PR._stages(biz))


@router.post("/app/projects")
@_guard
async def project_new(request: Request):
    ctx = _pctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        p = PR.create(conn, biz, ctx["actor"], {"name": f.get("name"), "contact_id": _uuid(f.get("contact_id")),
                                                 "budget_minor": _amount(biz, f.get("budget")), "style": f.get("style"),
                                                 "address": f.get("address"), "brief": f.get("brief"),
                                                 "fee_minor": _amount(biz, f.get("fee"))})
    return _go(f"/app/projects/{p['id']}")


# ---------------------------------------------------------------- one project
def _load(conn, biz: dict, pid: UUID, tab: str) -> dict:
    p = PR.get(conn, biz["id"], pid)
    d: dict = {"p": p, "rooms": PR.rooms(conn, biz["id"], pid), "totals": PR.totals(conn, biz, p)}
    if p["contact_id"]:
        from ..services import contacts as C
        d["client"] = C.get(conn, biz["id"], p["contact_id"])
    if tab == "overview":
        d["story"] = PR.story(conn, biz["id"], pid, limit=60)
        d["open_tasks"] = PR.tasks(conn, biz["id"], project_id=pid)[:6]
        d["waiting"] = [i for i in PR.items(conn, biz["id"], pid) if i["status"] == "proposed"]
    if tab in ("rooms", "design"):
        d["files"] = FL.with_previews(conn, biz["id"], FL.list_(conn, biz["id"], project_id=pid))
    if tab == "design":
        from ..services import canva as CV
        d["boards"] = ST.boards(conn, biz["id"], pid)
        d["canva_link"] = CV.link(conn, biz["id"]) if CV.configured() else None
        d["canva_designs"] = CV.designs(conn, biz["id"], pid)
    if tab in ("selections", "orders"):
        d["items"] = PR.items(conn, biz["id"], pid)
        d["suppliers"] = PR.suppliers(conn, biz["id"])
        d["library"] = PR.catalog(conn, biz["id"], limit=300)
        d["photos"] = FL.list_(conn, biz["id"], project_id=pid, kind=None)
        d["pos"] = PR.pos(conn, biz["id"], project_id=pid)
    if tab == "tasks":
        d["tasks"] = PR.tasks(conn, biz["id"], project_id=pid, open_only=False)
        d["hours"] = PR.hours(conn, biz["id"], pid)
        from ..services import team
        d["team"] = team.members(conn, biz["id"])
    if tab == "money":
        from ..services import payments as PY
        from ..services import quotes as QS
        d["quotes"] = QS.list_(conn, biz["id"], project_id=pid)
        d["payments"] = PY.list_(conn, biz["id"], project_id=pid)
        d["pay_accounts"] = PY.accounts(conn, biz["id"])
        d["pos"] = PR.pos(conn, biz["id"], project_id=pid)
    from ..services import inbox as X
    d["accounts"] = [a for a in X.accounts(conn, biz["id"]) if a.get("active", True)]
    return d


@router.get("/app/projects/{pid}")
@_guard
async def project_page(request: Request, pid: UUID):
    ctx = _pctx(request)
    biz = ctx["biz"]
    tab = request.query_params.get("tab") or "overview"
    if tab not in TABS:
        tab = "overview"
    with db.tx() as conn:
        d = _load(conn, biz, pid, tab)
        s = AI.settings(conn, biz["id"])
    d["portal"] = portal_url(d["p"])
    client = d.get("client")
    d["wa_portal"] = _wa_link(client and client.get("phone"),
                              t("st_wa_portal", _lang(request, biz)).format(name=(client or {}).get("name") or "",
                                                                            project=d["p"]["name"], link=d["portal"]))
    return render(request, "project.html", ctx, page="projects", tab=tab, tabs=TABS, stages=PR._stages(biz),
                  categories=PR.CATEGORIES, statuses=PR.ITEM_STATUS, styles=ST.STYLES, ai_on=bool(s and (s["api_key"] or s["provider"] == "demo")),
                  ai_draws=bool(s and s["provider"] in ("gemini", "openai", "demo")), studio_on=_blk(biz, "studio"),
                  assist=ST.ASSIST if ST.staff(biz).get("designer") else {}, **d)


def _blk(biz, name):
    from .. import blocks
    return blocks.on(biz, name)


def _back(pid, tab, anchor=""):
    return _go(f"/app/projects/{pid}?tab={tab}{anchor}")


@router.post("/app/projects/{pid}")
@_guard
async def project_save(request: Request, pid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    data: dict = {}
    for k in ("name", "address", "brief", "style", "notes", "stage", "status", "starts_on", "due_on"):
        if k in f:
            data[k] = f[k]
    for k, fk in (("budget_minor", "budget"), ("fee_minor", "fee")):
        if fk in f:
            data[k] = _amount(biz, f[fk])
    if "markup" in f:
        try:
            data["markup_bp"] = round(float(str(f["markup"] or 0).replace(",", ".")) * 100)
        except ValueError:
            raise Invalid("margin: a number, like 15", "markup") from None
    if "contact_id" in f:
        data["contact_id"] = _uuid(f["contact_id"])
    if "published" in f:
        data["published"] = f["published"] == "1"
    with db.tx() as conn:
        PR.patch(conn, biz, ctx["actor"], pid, data)
    _flash(request, "saved")
    return _back(pid, f.get("tab") or "overview")


@router.post("/app/projects/{pid}/link")
@_guard
async def project_new_link(request: Request, pid: UUID):
    ctx = _pctx(request)
    await _form(request)
    with db.tx() as conn:
        PR.new_link(conn, ctx["biz"], ctx["actor"], pid)
    _flash(request, "st_link_renewed")
    return _back(pid, "overview")


@router.post("/app/projects/{pid}/send")
@_guard
async def project_send(request: Request, pid: UUID):
    """A message to the client on a connected channel (WhatsApp first): the portal link, an update, an approval ask.
    lead27: «Send by WhatsApp» (via=whatsapp) — the business's OWN WhatsApp on the designer's phone, the message ready
    (wa.me): works with a personal or Business WhatsApp and nothing connected; the message goes into the project's story."""
    from ..services import inbox as X
    ctx = _pctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        p = PR.get(conn, biz["id"], pid)
    if not p["contact_id"]:
        raise Invalid("link a client to this project first", "contact_id")
    body = str(f.get("body") or "").strip()
    if f.get("with_link") == "1" and portal_url(p) not in body:
        body = (body + "\n\n" + portal_url(p)).strip()
    if not body:
        raise Invalid("write the message", "body")
    if f.get("via") == "whatsapp":
        with db.tx() as conn:
            phone = conn.execute(select(db.contacts.c.phone).where(db.contacts.c.business_id == biz["id"],
                                                                   db.contacts.c.id == p["contact_id"])).scalar()
            if not phone:
                raise Invalid("the client has no phone number", "phone")
            PR.note(conn, biz, ctx["actor"], pid, body, shared=True, kind="update")
        return RedirectResponse(_wa_link(phone, body), status_code=303)
    try:
        X.send(biz, ctx["actor"], contact_id=p["contact_id"], body=body, channel_account_id=_uuid(f.get("account_id")))
    except Blocked as b:
        _flash(request, f"r_{b.reason}", "warn")
        return _back(pid, f.get("tab") or "overview")
    with db.tx() as conn:
        PR.note(conn, biz, ctx["actor"], pid, body, shared=True, kind="update")
    _flash(request, "st_msg_sent")
    return _back(pid, f.get("tab") or "overview")


@router.post("/app/projects/{pid}/note")
@_guard
async def project_note(request: Request, pid: UUID):
    ctx = _pctx(request)
    f, ups = await _upload(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        fid = None
        if ups:
            raw, mime, name = ups[0]
            fid = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, project_id=pid,
                          shared=f.get("shared") == "1", kind="doc" if mime == "application/pdf" else "photo")["id"]
        PR.note(conn, biz, ctx["actor"], pid, str(f.get("body") or ""), shared=f.get("shared") == "1", file_id=fid)
    return _back(pid, "overview", "#story")


@router.post("/app/projects/{pid}/assist")
@_guard
async def project_assist(request: Request, pid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    try:
        ST.assist(ctx["biz"], ctx["actor"], pid, str(f.get("what") or ""), lang=_lang(request, ctx["biz"]))
        _flash(request, "st_ai_done")
    except AI.AIError as e:
        _flash(request, "error", "warn", raw=str(e))
    return _back(pid, "overview", "#story")


# ---------------------------------------------------------------- rooms, photos, renders
@router.post("/app/projects/{pid}/rooms")
@_guard
async def room_new(request: Request, pid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    with db.tx() as conn:
        PR.add_room(conn, ctx["biz"], ctx["actor"], pid, f)
    return _back(pid, "rooms")


@router.post("/app/rooms/{rid}")
@_guard
async def room_save(request: Request, rid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    with db.tx() as conn:
        r = PR.room(conn, ctx["biz"]["id"], rid)
        if f.get("act") == "delete":
            PR.remove_room(conn, ctx["biz"], ctx["actor"], rid)
        else:
            PR.patch_room(conn, ctx["biz"], ctx["actor"], rid,
                          {k: f[k] for k in ("name", "width_cm", "length_cm", "height_cm", "notes") if k in f})
    return _back(r["project_id"], "rooms")


@router.get("/app/rooms/{rid}/plan")
@_guard
async def plan_page(request: Request, rid: UUID):
    ctx = _pctx(request, "studio")
    with db.tx() as conn:
        r = PR.room(conn, ctx["biz"]["id"], rid)
        p = PR.get(conn, ctx["biz"]["id"], r["project_id"])
        its = [i for i in PR.items(conn, ctx["biz"]["id"], p["id"]) if i["room_id"] == rid and i["status"] != "rejected"]
    return render(request, "plan.html", ctx, page="projects", r=r, p=p, pieces=its)


@router.post("/app/rooms/{rid}/plan")
@_guard
async def plan_save(request: Request, rid: UUID):
    ctx = _pctx(request, "studio")
    f = await _form(request)
    try:
        plan = json.loads(str(f.get("plan") or "{}"))
    except ValueError:
        raise Invalid("the plan could not be read", "plan") from None
    with db.tx() as conn:
        PR.patch_room(conn, ctx["biz"], ctx["actor"], rid, {"plan": plan})
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    _flash(request, "saved")
    return _go(f"/app/rooms/{rid}/plan")


@router.post("/app/rooms/{rid}/palette")
@_guard
async def palette_save(request: Request, rid: UUID):
    ctx = _pctx(request, "studio")
    f = await _multi_form(request)
    pal = [{"hex": h, "name": n} for h, n in zip(f.getlist("hex"), f.getlist("pname")) if str(h).strip()]
    with db.tx() as conn:
        r = PR.room(conn, ctx["biz"]["id"], rid)
        if f.get("from_file"):
            raw, _m = FL.content(conn, ctx["biz"]["id"], _uuid(f["from_file"]))
            pal = [{"hex": h, "name": ""} for h in FL.palette(raw)] + pal
        PR.patch_room(conn, ctx["biz"], ctx["actor"], rid, {"palette": pal[:24]})
    return _back(r["project_id"], "rooms", f"#room-{rid}")


@router.post("/app/projects/{pid}/files")
@_guard
async def files_add(request: Request, pid: UUID):
    ctx = _pctx(request)
    f, ups = await _work_upload(request)
    if not ups:
        raise Invalid("choose a file", "file")
    kind = f.get("kind") or "photo"
    with db.tx() as conn:
        for up in ups[:20]:
            raw, mime, name = await _read_one(up)
            if not raw:
                continue
            FL.save(conn, ctx["biz"], ctx["actor"], raw=raw, mime=mime, name=name, project_id=pid,
                    room_id=_uuid(f.get("room_id")), kind="doc" if mime == "application/pdf" and kind in ("photo", "work") else
                    ("photo" if kind == "work" and not FL.is_work_name(name) else kind),
                    caption=str(f.get("caption") or ""), shared=f.get("shared") == "1", work=True)
            del raw
    _flash(request, "saved")
    return _back(pid, f.get("tab") or "rooms")


@router.post("/app/files/{fid}")
@_guard
async def file_save(request: Request, fid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    with db.tx() as conn:
        fl = FL.get(conn, ctx["biz"]["id"], fid)
        if f.get("act") == "delete":
            FL.remove(conn, ctx["biz"]["id"], ctx["actor"], fid)
        else:
            FL.patch(conn, ctx["biz"]["id"], ctx["actor"], fid,
                     caption=f.get("caption") if "caption" in f else None,
                     shared=(f.get("shared") == "1") if "shared" in f else None,
                     public=(f.get("public") == "1") if "public" in f else None,
                     room_id=_uuid(f.get("room_id")) if "room_id" in f else "keep",
                     client_download=(f.get("client_download") == "1") if "client_download" in f else None,
                     preview_id=_uuid(f.get("preview_id")) if "preview_id" in f else "keep")
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    return _back(fl["project_id"], f.get("tab") or "rooms") if fl["project_id"] else _go("/app/library")


@router.post("/app/files/{fid}/preview")
@_guard
async def file_preview(request: Request, fid: UUID):
    """A work file's preview: a picture or PDF uploaded for it (kept in the same project, like any other)."""
    ctx = _pctx(request)
    f, ups = await _upload(request)
    with db.tx() as conn:
        fl = FL.get(conn, ctx["biz"]["id"], fid)
        if fl["kind"] != "work":
            raise NotFound("file")
        if not ups:
            raise Invalid("choose a picture or a PDF", "file")
        raw, mime, name = ups[0]
        pv = FL.save(conn, ctx["biz"], ctx["actor"], raw=raw, mime=mime, name=name, project_id=fl["project_id"],
                     room_id=fl["room_id"], kind="doc" if mime == "application/pdf" else "photo", caption=fl["name"][:500])
        FL.patch(conn, ctx["biz"]["id"], ctx["actor"], fid, preview_id=pv["id"])
    _flash(request, "saved")
    return _back(fl["project_id"], f.get("tab") or "design", "#work") if fl["project_id"] else _go("/app/library")


@router.get("/app/files/{fid}")
@_guard
async def file_get(request: Request, fid: UUID):
    ctx = _ctx(request)
    with db.tx() as conn:
        fl = FL.get(conn, ctx["biz"]["id"], fid)
        if fl["kind"] == "work":
            if request.query_params.get("t") == "1":          # a work file's small picture is its preview's
                if not fl["preview_id"]:
                    return Response("Not found", status_code=404)
                data, mime = FL.content(conn, ctx["biz"]["id"], fl["preview_id"], thumb=True)
                return _file(data, mime, private=True)
            fl = FL.verified(conn, ctx["biz"]["id"], fid)
    if fl["kind"] == "work":
        return _work_file(fl)
    with db.tx() as conn:
        data, mime = FL.content(conn, ctx["biz"]["id"], fid, thumb=request.query_params.get("t") == "1")
    return _file(data, mime, private=True)


def _work_file(fl: dict) -> StreamingResponse:
    """A design work file, always as a download (never opened in the browser), sent in pieces, with its own name."""
    name = FL.download_name(fl)
    plain = "".join(c if c.isascii() and (c.isalnum() or c in "._- ()") else "_" for c in name)
    return StreamingResponse(FL.pieces(fl["business_id"], fl["id"], fl["size"]), media_type=fl["mime"], headers={
        "Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urlquote(name, safe='')}",
        "Content-Length": str(fl["size"]), "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


def _file(data: bytes, mime: str, private: bool) -> Response:
    return Response(data, media_type=mime, headers={"Cache-Control": ("private" if private else "public") + ", max-age=86400",
                                                    "X-Content-Type-Options": "nosniff",
                                                    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"})


@router.post("/app/files/{fid}/render")
@_guard
async def file_render(request: Request, fid: UUID):
    ctx = _pctx(request, "studio")
    f = await _form(request)
    with db.tx() as conn:
        fl = FL.get(conn, ctx["biz"]["id"], fid)
    try:
        ST.render_room(ctx["biz"], ctx["actor"], fid, style=str(f.get("style") or ""), notes=str(f.get("notes") or ""),
                       keep_layout=f.get("free") != "1")
        _flash(request, "st_render_done")
    except AI.AIError as e:
        _flash(request, "error", "warn", raw=str(e))
    return _back(fl["project_id"], "design", "#renders")


# ---------------------------------------------------------------- mood boards
@router.post("/app/projects/{pid}/boards")
@_guard
async def board_new(request: Request, pid: UUID):
    ctx = _pctx(request, "studio")
    f = await _form(request)
    with db.tx() as conn:
        b = ST.new_board(conn, ctx["biz"], ctx["actor"], pid, str(f.get("title") or ""), _uuid(f.get("room_id")))
    return _go(f"/app/boards/{b['id']}")


@router.get("/app/boards/{bid}")
@_guard
async def board_page(request: Request, bid: UUID):
    ctx = _pctx(request, "studio")
    with db.tx() as conn:
        b = ST.board(conn, ctx["biz"]["id"], bid)
        p = PR.get(conn, ctx["biz"]["id"], b["project_id"])
        pics = [x for x in FL.list_(conn, ctx["biz"]["id"], project_id=p["id"]) if x["mime"].startswith("image/")]
        rooms = PR.rooms(conn, ctx["biz"]["id"], p["id"])
    return render(request, "board.html", ctx, page="projects", b=b, p=p, pics=pics, rooms=rooms)


@router.post("/app/boards/{bid}")
@_guard
async def board_save(request: Request, bid: UUID):
    ctx = _pctx(request, "studio")
    f, ups = await _upload(request)
    with db.tx() as conn:
        b = ST.board(conn, ctx["biz"]["id"], bid)
        if f.get("act") == "delete":
            ST.remove_board(conn, ctx["biz"], ctx["actor"], bid)
            return _back(b["project_id"], "design")
        items = None
        if "items" in f:
            try:
                items = json.loads(str(f["items"]))
            except ValueError:
                raise Invalid("the board could not be read", "items") from None
        if ups:                                               # pictures dropped on the board: saved with the project, then placed
            items = list(items if items is not None else b["items"])
            for n, (raw, mime, name) in enumerate(ups[:20]):
                fl = FL.save(conn, ctx["biz"], ctx["actor"], raw=raw, mime=mime, name=name, project_id=b["project_id"],
                             room_id=b["room_id"], kind="canva" if f.get("canva") == "1" else "photo")
                items.append({"t": "img", "file": str(fl["id"]), "x": (n % 4) * 24 + 2, "y": (2 + (n // 4) * 32) % 90, "w": 22, "h": 30})
        ST.save_board(conn, ctx["biz"], ctx["actor"], bid, items=items, title=f.get("title"),
                      shared=(f.get("shared") == "1") if "shared" in f else None)
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    _flash(request, "saved")
    return _go(f"/app/boards/{bid}")


# ---------------------------------------------------------------- selections
def _item_form(biz: dict, f: dict) -> dict:
    d = {k: f[k] for k in ("name", "category", "description", "sku", "url", "finish", "width_cm", "depth_cm", "height_cm",
                           "lead_days", "qty") if k in f}
    for k, fk in (("cost_minor", "cost"), ("price_minor", "price")):
        if fk in f:
            d[k] = _amount(biz, f[fk])
    for k in ("supplier_id", "room_id", "catalog_id", "file_id"):
        if k in f:
            d[k] = _uuid(f[k])
    return d


@router.post("/app/projects/{pid}/items")
@_guard
async def item_new(request: Request, pid: UUID):
    ctx = _pctx(request)
    f, ups = await _upload(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        d = _item_form(biz, f)
        if ups:
            raw, mime, name = ups[0]
            d["file_id"] = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, project_id=pid,
                                   room_id=d.get("room_id"), shared=True)["id"]
        it = PR.add_item(conn, biz, ctx["actor"], pid, d)
        if f.get("to_library") == "1" and not d.get("catalog_id"):
            PR.save_to_catalog(conn, biz, ctx["actor"], it["id"])
    return _back(pid, "selections", "#items")


@router.post("/app/items/{iid}")
@_guard
async def item_save(request: Request, iid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        it = PR.item(conn, biz["id"], iid)
        act = f.get("act")
        if act == "delete":
            PR.remove_item(conn, biz, ctx["actor"], iid)
        elif act == "library":
            PR.save_to_catalog(conn, biz, ctx["actor"], iid)
            _flash(request, "st_in_library")
        else:
            d = _item_form(biz, f)
            if "status" in f:
                d["status"] = f["status"]
            PR.patch_item(conn, biz, ctx["actor"], iid, d)
    return _back(it["project_id"], "selections", f"#item-{iid}")


@router.post("/app/projects/{pid}/quote")
@_guard
async def project_quote(request: Request, pid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    with db.tx() as conn:
        q = PR.quote_from_selections(conn, ctx["biz"], ctx["actor"], pid, include=str(f.get("include") or "approved"),
                                     title=str(f.get("title") or "") or None)
    _flash(request, "st_quote_made")
    return _back(pid, "money", f"#q-{q['id']}")


@router.post("/app/projects/{pid}/payment")
@_guard
async def project_payment(request: Request, pid: UUID):
    from ..services import payments as PY
    ctx = _pctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    amount = _amount(biz, f.get("amount"))
    if not amount:
        raise Invalid("how much?", "amount")
    with db.tx() as conn:
        p = PR.get(conn, biz["id"], pid)
    desc = str(f.get("description") or "").strip() or p["name"]
    if f.get("received") == "1":
        PY.record_received(biz, ctx["actor"], amount_minor=amount, description=desc, contact_id=p["contact_id"], project_id=pid)
    else:
        try:
            PY.create(biz, ctx["actor"], amount_minor=amount, description=desc, contact_id=p["contact_id"], project_id=pid)
        except PY.PayError as e:
            _flash(request, "error", "warn", raw=str(e))
    with db.tx() as conn:
        PR.note(conn, biz, ctx["actor"], pid, desc, shared=True, kind="payment")
    return _back(pid, "money", "#payments")


# ---------------------------------------------------------------- orders
@router.post("/app/projects/{pid}/orders")
@_guard
async def order_new(request: Request, pid: UUID):
    ctx = _pctx(request)
    f = await _multi_form(request)
    ids = [_uuid(x) for x in f.getlist("item") if x]
    with db.tx() as conn:
        o = PR.order(conn, ctx["biz"], ctx["actor"], pid, _uuid(f.get("supplier_id")), ids, f.get("expected_on") or None,
                     str(f.get("notes") or ""))
    return _go(f"/app/orders/{o['id']}")


@router.get("/app/orders/{oid}")
@_guard
async def order_page(request: Request, oid: UUID):
    ctx = _pctx(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        o = PR.po(conn, biz["id"], oid)
        p = PR.get(conn, biz["id"], o["project_id"])
        lines = PR.po_items(conn, biz["id"], oid)
    text = t("st_po_wa", _lang(request, biz)).format(number=o["number"], business=biz["name"]) + "\n" + "\n".join(
        f"• {ln['qty']} × {ln['name']}" + (f" ({ln['sku']})" if ln["sku"] else "") + (f" — {ln['finish']}" if ln["finish"] else "")
        for ln in lines)
    return render(request, "order.html", ctx, page="projects", o=o, p=p, lines=lines,
                  wa=_wa_link(o.get("supplier_phone"), text) if o.get("supplier_phone") else None, wa_text=text)


@router.post("/app/orders/{oid}")
@_guard
async def order_save(request: Request, oid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    biz = ctx["biz"]
    data = {k: f[k] for k in ("status", "expected_on", "notes") if k in f}
    if "paid" in f:
        data["paid_minor"] = _amount(biz, f["paid"]) or 0
    with db.tx() as conn:
        PR.patch_po(conn, biz, ctx["actor"], oid, data)
    _flash(request, "saved")
    return _go(f"/app/orders/{oid}")


@router.post("/app/orders/{oid}/email")
@_guard
async def order_email(request: Request, oid: UUID):
    from ..services import mailer
    ctx = _pctx(request)
    await _form(request)
    biz = ctx["biz"]
    with db.tx() as conn:
        o = PR.po(conn, biz["id"], oid)
        lines = PR.po_items(conn, biz["id"], oid)
    if not o.get("supplier_email"):
        raise Invalid("add the supplier's email first", "email")
    if not mailer.configured():
        raise Invalid("this server doesn't send email; use WhatsApp or print it", "email")
    lang = _lang(request, biz)
    body = t("st_po_wa", lang).format(number=o["number"], business=biz["name"]) + "\n\n" + "\n".join(
        f"- {ln['qty']} x {ln['name']}" + (f" ({ln['sku']})" if ln["sku"] else "") + (f" - {ln['finish']}" if ln["finish"] else "")
        for ln in lines) + (f"\n\n{o['notes']}" if o["notes"] else "")
    ok = mailer.send(o["supplier_email"], f"PO #{o['number']} · {biz['name']}", body,
                     reply_to=(ctx["user"] or {}).get("email"))
    if ok:
        with db.tx() as conn:
            if o["status"] == "draft":
                PR.patch_po(conn, biz, ctx["actor"], oid, {"status": "sent"})
    _flash(request, "st_msg_sent" if ok else "st_not_sent", "ok" if ok else "warn")
    return _go(f"/app/orders/{oid}")


# ---------------------------------------------------------------- tasks and hours
# v0.16 (mon01): tasks are for every business (a recipe adds "reply to Ana" tasks), not only those with projects;
# a task can be about a customer or a deal, and the board shows To do / Doing / Done.
@router.post("/app/tasks")
@_guard
async def task_new(request: Request):
    ctx = _ctx(request)
    f, ups = await _upload(request)
    biz = ctx["biz"]
    pid = _uuid(f.get("project_id"))
    with db.tx() as conn:
        fid = None
        if ups and pid:
            raw, mime, name = ups[0]
            fid = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, project_id=pid)["id"]
        PR.add_task(conn, biz, ctx["actor"], {"title": f.get("title"), "kind": f.get("kind") or "task", "project_id": pid,
                                               "room_id": _uuid(f.get("room_id")), "assignee_id": _uuid(f.get("assignee_id")),
                                               "due_on": f.get("due_on") or None, "file_id": fid,
                                               "contact_id": _uuid(f.get("contact_id")), "deal_id": _uuid(f.get("deal_id"))})
    back = str(f.get("back") or "")
    return _go(back if back.startswith("/app/") else (f"/app/projects/{pid}?tab=tasks" if pid else "/app/tasks"))


@router.post("/app/tasks/{tid}")
@_guard
async def task_save(request: Request, tid: UUID):
    ctx = _ctx(request)
    f = await _form(request)
    fetch = request.headers.get("x-requested-with") == "fetch"
    try:
        with db.tx() as conn:
            if f.get("act") == "delete":
                PR.remove_task(conn, ctx["biz"], ctx["actor"], tid)
            elif f.get("status"):
                st = PR.move_task(conn, ctx["biz"], ctx["actor"], tid, f["status"])
                if fetch:
                    return JSONResponse({"ok": True, "status": st})
            else:
                PR.done_task(conn, ctx["biz"], ctx["actor"], tid, f.get("done") == "1")
    except (Invalid, NotFound) as e:
        if fetch:
            return JSONResponse({"ok": False, "error": str(e) or "not found"}, status_code=400)
        raise
    back = str(f.get("back") or "")
    return _go(back if back.startswith("/app/") else "/app/tasks")


@router.get("/app/tasks")
@_guard
async def tasks_page(request: Request):
    from datetime import datetime, timedelta, timezone
    ctx = _ctx(request)
    mine = request.query_params.get("mine") == "1" and ctx["user"]
    since = datetime.now(timezone.utc) - timedelta(days=14)            # the board's Done column: the last two weeks
    with db.tx() as conn:
        rows = PR.tasks(conn, ctx["biz"]["id"], assignee_id=ctx["user"]["id"] if mine else None, done_since=since)
        projects = PR.search(conn, ctx["biz"]["id"]) if blocks.on(ctx["biz"], "projects") else []
        from ..services import team
        people = team.members(conn, ctx["biz"]["id"])
    for r in rows:
        r["status"] = PR.task_status(r)
    cols = [{"key": k, "rows": [r for r in rows if r["status"] == k]} for k in PR.TASK_STATUSES]
    return render(request, "tasks.html", ctx, page="tasks", rows=[r for r in rows if r["status"] != "done"], cols=cols,
                  projects=projects, people=people, mine=bool(mine))


@router.post("/app/projects/{pid}/hours")
@_guard
async def hours_new(request: Request, pid: UUID):
    ctx = _pctx(request)
    f = await _form(request)
    try:
        h = float(str(f.get("hours") or "0").replace(",", "."))
    except ValueError:
        raise Invalid("hours, like 1.5", "hours") from None
    with db.tx() as conn:
        if f.get("act") == "delete":
            PR.remove_hours(conn, ctx["biz"], ctx["actor"], _uuid(f.get("entry_id")))
        else:
            PR.log_hours(conn, ctx["biz"], ctx["actor"], pid, ctx["user"]["id"] if ctx["user"] else None, round(h * 60),
                         f.get("on_date") or None, str(f.get("note") or ""))
    return _back(pid, "tasks", "#hours")


# ---------------------------------------------------------------- suppliers and the library
@router.get("/app/suppliers")
@_guard
async def suppliers_page(request: Request):
    ctx = _pctx(request)
    with db.tx() as conn:
        rows = PR.suppliers(conn, ctx["biz"]["id"])
        orders = PR.pos(conn, ctx["biz"]["id"], open_only=True)
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    return render(request, "suppliers.html", ctx, page="suppliers", rows=rows, orders=orders,
                  today_date=_dt.now(ZoneInfo(ctx["biz"]["timezone"])).date())


@router.post("/app/suppliers")
@_guard
async def supplier_new(request: Request):
    ctx = _pctx(request)
    f = await _form(request)
    sid = _uuid(f.get("id"))
    with db.tx() as conn:
        data = {k: f[k] for k in ("name", "kind", "contact_name", "phone", "email", "website", "lead_days", "notes") if k in f}
        if sid:
            if f.get("act") == "archive":
                data = {"archived": True}
            PR.patch_supplier(conn, ctx["biz"], ctx["actor"], sid, data)
        else:
            PR.add_supplier(conn, ctx["biz"], ctx["actor"], data)
    _flash(request, "saved")
    return _go("/app/suppliers")


@router.get("/app/library")
@_guard
async def library_page(request: Request):
    ctx = _pctx(request)
    q = request.query_params.get("q", "")
    cat = request.query_params.get("c") or None
    if cat not in PR.CATEGORIES:
        cat = None
    with db.tx() as conn:
        rows = PR.catalog(conn, ctx["biz"]["id"], q=q, category=cat)
        sup = PR.suppliers(conn, ctx["biz"]["id"])
    return render(request, "library.html", ctx, page="library", rows=rows, suppliers=sup, q=q, cat=cat, categories=PR.CATEGORIES)


@router.post("/app/library")
@_guard
async def library_save(request: Request):
    ctx = _pctx(request)
    f, ups = await _upload(request)
    biz = ctx["biz"]
    cid = _uuid(f.get("id"))
    with db.tx() as conn:
        d = _item_form(biz, f)
        d.pop("qty", None), d.pop("room_id", None), d.pop("catalog_id", None)
        if ups:
            raw, mime, name = ups[0]
            d["file_id"] = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name, kind="photo")["id"]
        if cid:
            if f.get("act") == "archive":
                d = {"archived": True}
            PR.patch_catalog(conn, biz, ctx["actor"], cid, d)
        else:
            PR.add_catalog(conn, biz, ctx["actor"], d)
    _flash(request, "saved")
    return _go("/app/library")


# ---------------------------------------------------------------- the AI team
@router.get("/app/ai-team")
@_guard
async def ai_team_page(request: Request):
    ctx = _own(request)
    with db.tx() as conn:
        s = AI.settings(conn, ctx["biz"]["id"])
    return render(request, "ai_team.html", ctx, page="ai_team", on=ST.staff(ctx["biz"]), ai=s)


@router.post("/app/ai-team")
@_guard
async def ai_team_save(request: Request):
    ctx = _own(request)
    f = await _multi_form(request)
    with db.tx() as conn:
        ST.set_staff(conn, ctx["biz"], ctx["actor"], [str(x) for x in f.getlist("on")])
    _flash(request, "saved")
    return _go("/app/ai-team")


# ---------------------------------------------------------------- print: presentation and specs
def _present_data(conn, biz: dict, p: dict, for_client: bool) -> dict:
    files = FL.list_(conn, biz["id"], project_id=p["id"], shared_only=for_client)
    bds = ST.boards(conn, biz["id"], p["id"], shared_only=for_client)
    return {"p": p, "rooms": PR.rooms(conn, biz["id"], p["id"]), "items": PR.items(conn, biz["id"], p["id"], for_client=for_client),
            "renders": [x for x in files if x["kind"] == "render"], "photos": [x for x in files if x["kind"] in ("photo", "canva")],
            "boards": bds, "totals": PR.totals(conn, biz, p)}


@router.get("/app/projects/{pid}/present")
@_guard
async def present_page(request: Request, pid: UUID):
    ctx = _pctx(request)
    with db.tx() as conn:
        d = _present_data(conn, ctx["biz"], PR.get(conn, ctx["biz"]["id"], pid), for_client=False)
    return render(request, "present.html", ctx, page="projects", fsrc=lambda fid, th=False: f"/app/files/{fid}" + ("?t=1" if th else ""),
                  team=True, **d)


@router.get("/app/projects/{pid}/specs")
@_guard
async def specs_page(request: Request, pid: UUID):
    ctx = _pctx(request)
    with db.tx() as conn:
        p = PR.get(conn, ctx["biz"]["id"], pid)
        rows = [i for i in PR.items(conn, ctx["biz"]["id"], pid) if i["status"] != "rejected"]
        rooms = PR.rooms(conn, ctx["biz"]["id"], pid)
    return render(request, "specs.html", ctx, page="projects", p=p, rows=rows, rooms=rooms)


# ---------------------------------------------------------------- the customer's portal
def _portal(conn, token: str) -> tuple[dict, dict]:
    p, biz = PR.by_token(conn, token)
    return p, biz


@router.get("/portal/{token}")
@_guard
async def portal_page(request: Request, token: str):
    from ..services import payments as PY
    from ..services import quotes as QS
    with db.tx() as conn:
        p, biz = _portal(conn, token)
        d = _present_data(conn, biz, p, for_client=True)
        d["story"] = PR.story(conn, biz["id"], p["id"], shared_only=True, limit=40)
        d["quotes"] = [q for q in QS.list_(conn, biz["id"], project_id=p["id"]) if q["status"] != "cancelled"]
        d["payments"] = [x for x in PY.list_(conn, biz["id"], project_id=p["id"]) if x["status"] in ("pending", "paid")]
        A = db.appointments
        d["visits"] = [dict(r) for r in conn.execute(select(A).where(A.c.business_id == biz["id"], A.c.contact_id == p["contact_id"],
                                                                     A.c.starts_at > func.now(), A.c.status.in_(("booked", "confirmed")))
                                                     .order_by(A.c.starts_at).limit(5)).mappings()] if p["contact_id"] else []
        shared = FL.list_(conn, biz["id"], project_id=p["id"], shared_only=True)
        d["docs"] = [x for x in shared if x["kind"] in ("doc", "plan")]
        d["work"] = FL.with_previews(conn, biz["id"], [x for x in shared if x["kind"] == "work"])   # des01: the preview; the file only when ticked
    ctx = {"biz": biz, "user": None, "actor": "customer", "role": None, "businesses": []}
    from .. import config as _c
    return render(request, "portal.html", ctx, token=token, stages=PR._stages(biz), public_url=_c.PUBLIC_URL(),
                  fsrc=lambda fid, th=False: f"/portal/{token}/f/{fid}" + ("?t=1" if th else ""), **d)


@router.get("/portal/{token}/present")
@_guard
async def portal_present(request: Request, token: str):
    with db.tx() as conn:
        p, biz = _portal(conn, token)
        d = _present_data(conn, biz, p, for_client=True)
    ctx = {"biz": biz, "user": None, "actor": "customer", "role": None, "businesses": []}
    return render(request, "present.html", ctx, fsrc=lambda fid, th=False: f"/portal/{token}/f/{fid}" + ("?t=1" if th else ""),
                  team=False, token=token, **d)


@router.get("/portal/{token}/f/{fid}")
async def portal_file(request: Request, token: str, fid: UUID):
    """A picture or document in the portal: only what the business shared, or what sits on a shared board or piece."""
    with db.tx() as conn:
        try:
            p, biz = _portal(conn, token)
            f = FL.get(conn, biz["id"], fid)
        except NotFound:
            return Response("Not found", status_code=404)
        if f["kind"] == "work":                               # des01: a work file only when the designer ticked "client can download"
            if not (f["project_id"] == p["id"] and f["shared"] and f["client_download"]) or request.query_params.get("t") == "1":
                return Response("Not found", status_code=404)
            try:
                f = FL.verified(conn, biz["id"], fid)
            except Invalid:
                return Response("Not found", status_code=404)
            return _work_file(f)
        ok = f["project_id"] == p["id"] and f["shared"]
        if not ok:                                            # the preview of a shared work file
            ok = bool(conn.execute(select(db.files.c.id).where(db.files.c.business_id == biz["id"], db.files.c.project_id == p["id"],
                                                               db.files.c.preview_id == fid, db.files.c.kind == "work",
                                                               db.files.c.shared.is_(True))).first())
        if not ok:
            ok = str(fid) in set().union(*[ST.board_file_ids(b) for b in ST.boards(conn, biz["id"], p["id"], shared_only=True)] or [set()])
        if not ok:                                            # the "before" photo of a shared render
            ok = bool(conn.execute(select(db.files.c.id).where(db.files.c.business_id == biz["id"], db.files.c.project_id == p["id"],
                                                               db.files.c.source_id == fid, db.files.c.shared.is_(True))).first())
        if not ok:
            ok = bool(conn.execute(select(db.project_items.c.id).where(db.project_items.c.business_id == biz["id"],
                                                                       db.project_items.c.project_id == p["id"],
                                                                       db.project_items.c.file_id == fid)).first())
        if not ok:
            return Response("Not found", status_code=404)
        data, mime = FL.content(conn, biz["id"], fid, thumb=request.query_params.get("t") == "1")
    return _file(data, mime, private=True)


@router.get("/portal/{token}/board/{bid}")
@_guard
async def portal_board(request: Request, token: str, bid: UUID):
    with db.tx() as conn:
        p, biz = _portal(conn, token)
        b = ST.board(conn, biz["id"], bid)
        if b["project_id"] != p["id"] or not b["shared"]:
            raise NotFound("board")
    ctx = {"biz": biz, "user": None, "actor": "customer", "role": None, "businesses": []}
    return render(request, "board_view.html", ctx, b=b, p=p, token=token,
                  fsrc=lambda fid, th=False: f"/portal/{token}/f/{fid}" + ("?t=1" if th else ""))


@router.post("/portal/{token}")
@_guard
async def portal_act(request: Request, token: str):
    f = await _form(request)
    if not _limit(_portal_hits, request, 120):
        raise Invalid("too many changes in a short time; try again in a while", "act")
    act = f.get("act")
    with db.tx() as conn:
        p, biz = _portal(conn, token)
        if act in ("approved", "rejected"):
            it = PR.decide(conn, token, _uuid(f.get("item_id")), act, str(f.get("note") or ""))
            what = f"{'✓' if act == 'approved' else '✗'} {it['name']}"
        elif act == "comment":
            PR.comment(conn, token, str(f.get("body") or ""))
            what = str(f.get("body") or "")[:300]
        elif act in ("like", "dislike", "clear"):
            PR.react(conn, token, str(f.get("what") or ""), _uuid(f.get("id")), None if act == "clear" else act,
                     str(f.get("note") or ""))
            what = {"like": "👍", "dislike": "👎", "clear": "·"}[act] + " " + str(f.get("note") or "")
        else:
            raise Invalid("unknown action", "act")
    _tell_owners(biz, p, what)
    _flash(request, "st_thanks")
    return _go(f"/portal/{token}{f.get('anchor') or ''}")


# ---------------------------------------------------------------- the website: finished work and "see your room"
@router.get("/s/{slug}/f/{fid}")
async def site_file(request: Request, slug: str, fid: UUID):
    with db.tx() as conn:
        bid = conn.execute(select(db.businesses.c.id).where(db.businesses.c.slug == slug, db.businesses.c.site_enabled.is_(True),
                                                            db.businesses.c.suspended_at.is_(None))).scalar()
        row = conn.execute(select(db.files.c.data, db.files.c.thumb, db.files.c.mime).where(
            db.files.c.business_id == bid, db.files.c.id == fid, db.files.c.public.is_(True),
            db.files.c.kind != "work")).first() if bid else None
    if not row:
        return Response("Not found", status_code=404)
    if row.mime.startswith("video/"):                       # page01: the page's video, in pieces (phones ask for byte ranges)
        return _video(request, bytes(row.data), row.mime)
    th = request.query_params.get("t") == "1" and row.thumb
    return _file(row.thumb if th else row.data, "image/jpeg" if th and row.mime != "image/png" else row.mime, private=False)


def _video(request: Request, data: bytes, mime: str) -> Response:
    """A video with byte ranges (Safari won't play one without them): 206 + Content-Range for «Range: bytes=a-b», else the whole."""
    import re as _re
    n = len(data)
    head = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
    m = _re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("range") or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return Response(data, media_type=mime, headers=head)
    if m.group(1):
        a, b = int(m.group(1)), min(int(m.group(2)) if m.group(2) else n - 1, n - 1)
    else:                                                   # «bytes=-500»: the last 500
        a, b = max(0, n - int(m.group(2))), n - 1
    if a >= n or a > b:
        return Response(status_code=416, headers={**head, "Content-Range": f"bytes */{n}"})
    return Response(data[a:b + 1], status_code=206, media_type=mime, headers={**head, "Content-Range": f"bytes {a}-{b}/{n}"})


@router.post("/s/{slug}/preview")
@_guard
async def site_preview(request: Request, slug: str):
    """A visitor uploads a photo of their room and sees it in the business's style. Uses the business's own AI key,
    so it is capped: 3 a day per visitor, and the business's daily picture limit."""
    from ..services import site as SI
    from .. import blocks
    f, ups = await _upload(request)
    with db.tx() as conn:
        biz = SI.public(conn, slug)
    if not blocks.on(biz, "studio") or not (biz.get("site") or {}).get("preview"):
        raise NotFound("page")
    if not ups:
        raise Invalid("choose a photo of your space", "file")
    if not _limit(_preview_hits, request, 3, 86400):
        _flash(request, "st_preview_limit", "warn")
        return _go(f"/s/{slug}#preview")
    raw, mime, name = ups[0]
    with db.tx() as conn:
        src = FL.save(conn, biz, "website", raw=raw, mime=mime, name=name, kind="site")
    try:
        out = ST.render_room(biz, "website", src["id"], style=str(f.get("style") or "") or "modern",
                             notes=str(f.get("notes") or "")[:200])
    except (AI.AIError, Invalid) as e:
        with db.tx() as conn:
            FL.remove(conn, biz["id"], "website", src["id"])
        _flash(request, "st_preview_failed", "warn")
        return _go(f"/s/{slug}#preview")
    key = secrets.token_urlsafe(12)
    with db.tx() as conn:
        conn.execute(db.files.update().where(db.files.c.id.in_((src["id"], out["id"]))).values(kind="site", public=True,
                                                                                                 caption=key))
    return _go(f"/s/{slug}/preview/{key}")


@router.get("/s/{slug}/preview/{key}")
@_guard
async def site_preview_result(request: Request, slug: str, key: str):
    from ..services import site as SI
    with db.tx() as conn:
        biz = SI.public(conn, slug)
        rows = conn.execute(select(db.files.c.id, db.files.c.source_id).where(
            db.files.c.business_id == biz["id"], db.files.c.kind == "site", db.files.c.caption == key)).all()
    if len(rows) != 2:
        raise NotFound("page")
    after = next(r.id for r in rows if r.source_id)
    before = next(r.id for r in rows if not r.source_id)
    ctx = {"biz": biz, "user": None, "actor": "website", "role": None, "businesses": []}
    return render(request, "site_preview.html", ctx, before=before, after=after, slug=slug)


# ---------------------------------------------------------------- the studio: its home, its own address, the door from the office
_HOST_OPEN = ("/static", "/login", "/logout", "/forgot", "/reset", "/lang", "/app/new-password", "/health", "/favicon.ico",
              "/studio/enter")
_HOST_ALSO = ("/app", "/portal/", "/p/", "/q/", "/b/", "/s/", "/w/", "/join/", "/r/", "/receipt/", "/sign/")   # the office and the business's own pages
_hosts: dict = {"at": 0.0, "map": {}}


def studio_business(host: str):
    """The business whose studio lives at this address (cached a minute), or None."""
    import time
    host = (host or "").split(":")[0].lower()
    if not host or host == (config.PUBLIC_URL().split("://")[-1].split("/")[0].split(":")[0].lower()):
        return None
    if time.time() - _hosts["at"] > 60:
        with db.tx() as conn:
            _hosts["map"] = {h.lower(): bid for h, bid in conn.execute(
                select(db.businesses.c.studio_host, db.businesses.c.id).where(db.businesses.c.studio_host.is_not(None)))}
        _hosts["at"] = time.time()
    return _hosts["map"].get(host)


async def studio_host_guard(request: Request, call_next):
    """At a studio address only the studio is served; anything else goes to the office (PUBLIC_URL)."""
    from fastapi.responses import RedirectResponse
    from . import STUDIO_PATHS
    bid = studio_business(request.headers.get("host", ""))
    request.state.studio_biz = bid
    if bid:                                                    # the business's own address: its office, its studio, its pages
        path = request.url.path
        if path == "/":
            return RedirectResponse("/app", status_code=302)
        if not (path.startswith(STUDIO_PATHS) or path.startswith(_HOST_OPEN) or path.startswith(_HOST_ALSO)):
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"{config.PUBLIC_URL()}{path}{q}", status_code=302)
    return await call_next(request)


@router.get("/studio")
@_guard
async def studio_home(request: Request):
    ctx = _pctx(request, "studio")
    biz = ctx["biz"]
    F, IT, P = db.files, db.project_items, db.projects
    with db.tx() as conn:
        rows = PR.search(conn, biz["id"])
        covers = {}
        for kind in ("render", "photo"):
            for pid, fid in conn.execute(select(F.c.project_id, F.c.id).where(F.c.business_id == biz["id"], F.c.kind == kind,
                                                                                F.c.project_id.in_([r["id"] for r in rows] or [None]))
                                         .order_by(F.c.created_at)):
                covers.setdefault(pid, fid)
        for r in rows:
            r["cover"] = covers.get(r["id"])
        waiting = [dict(x) for x in conn.execute(select(IT.c.id, IT.c.name, IT.c.project_id, P.c.name.label("project_name"))
                                                 .join(P, P.c.id == IT.c.project_id)
                                                 .where(IT.c.business_id == biz["id"], IT.c.status == "proposed", P.c.status == "active")
                                                 .order_by(IT.c.created_at.desc()).limit(12)).mappings()]
        renders = FL.list_(conn, biz["id"], kind="render", limit=8)
        stats = ST.stats(conn, biz["id"])
    return render(request, "studio_home.html", ctx, page="studio", rows=rows, waiting=waiting, renders=renders, stats=stats,
                  stages=PR._stages(biz))


def _enter_signer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(config.get("VO_SECRET_KEY", required=True), salt="studio-enter")


@router.get("/app/studio")
@_guard
async def studio_go(request: Request):
    """The office's Studio button: to the studio's own address, already signed in (a one-minute, one-time pass)."""
    ctx = _pctx(request, "studio")
    host = ctx["biz"].get("studio_host")
    if not host or not ctx["user"] or getattr(request.state, "studio_biz", None):
        return _go("/studio")
    tok = _enter_signer().dumps({"u": str(ctx["user"]["id"]), "b": str(ctx["biz"]["id"]), "n": secrets.token_urlsafe(12)})
    return _go(f"https://{host}/studio/enter?t={tok}")


@router.get("/studio/enter")
@_guard
async def studio_enter(request: Request):
    from itsdangerous import BadSignature
    from ..services import instance as I
    from ..services import users as U
    bid = getattr(request.state, "studio_biz", None)
    try:
        d = _enter_signer().loads(request.query_params.get("t", ""), max_age=60)
    except BadSignature:
        return _go("/login")
    if not bid or d.get("b") != str(bid):
        return _go("/login")
    with db.tx() as conn:
        key = f"studio_pass:{d['n']}"[:60]
        if I.kv_get(conn, key):                                # each pass works once
            return _go("/login")
        I.kv_set(conn, key, {"u": d["u"]})
        user = U.get(conn, UUID(d["u"]))
        if not user or not U.membership(conn, user["id"], bid):
            return _go("/login")
    request.session.clear()
    request.session.update(user_id=d["u"], business_id=d["b"])
    return _go("/studio")


# ---------------------------------------------------------------- Apps: her design software, and Canva connected
# the design tools designers already use: each opens in its own tab; what she makes there comes back into a project
# (key, name, open, sign up, paid) — every sign-up page offers "Continue with Google" (Photopea needs no account at all)
APPS = [("canva", "Canva", "https://www.canva.com/", "https://www.canva.com/signup", False),
        ("sketchup", "SketchUp", "https://app.sketchup.com/", "https://app.sketchup.com/", False),
        ("planner5d", "Planner 5D", "https://planner5d.com/editor", "https://planner5d.com/editor", False),
        ("homestyler", "Homestyler", "https://www.homestyler.com/", "https://www.homestyler.com/", False),
        ("coohom", "Coohom", "https://www.coohom.com/", "https://www.coohom.com/", False),
        ("floorplanner", "Floorplanner", "https://floorplanner.com/login", "https://floorplanner.com/signup", False),
        ("autocad", "AutoCAD Web", "https://web.autocad.com/", "https://web.autocad.com/", True),
        ("photoshop", "Photoshop (web)", "https://photoshop.adobe.com/", "https://photoshop.adobe.com/", True),
        ("photopea", "Photopea", "https://www.photopea.com/", "https://www.photopea.com/", False),
        ("pinterest", "Pinterest", "https://www.pinterest.com/", "https://www.pinterest.com/", False),
        ("houzz", "Houzz", "https://www.houzz.com/", "https://www.houzz.com/signup", False)]
APP_KEYS = {a[0] for a in APPS}


def _apps_ready(conn, business_id) -> list[str]:
    v = conn.execute(select(db.businesses.c.apps_ready).where(db.businesses.c.id == business_id)).scalar()
    return [k for k in (v or []) if k in APP_KEYS]


@router.get("/app/apps")
@_guard
async def apps_page(request: Request):
    from ..services import canva as CV
    ctx = _pctx(request, "studio")
    with db.tx() as conn:
        projects = PR.search(conn, ctx["biz"]["id"])
        cv = CV.link(conn, ctx["biz"]["id"])
        ready = _apps_ready(conn, ctx["biz"]["id"])
    return render(request, "apps.html", ctx, page="apps", apps=APPS, ready=ready, projects=projects, canva=cv,
                  canva_ready=CV.configured(), pick=request.query_params.get("p"))


@router.post("/app/apps/ready")
@_guard
async def apps_ready(request: Request):
    """She ticks an app as set up (her account works) — or un-ticks it to see the set-up steps again."""
    from sqlalchemy import update
    from ..services import log
    ctx = _pctx(request, "studio")
    f = await _form(request)
    key = f.get("app")
    if key not in APP_KEYS:
        raise Invalid("unknown app", "app")
    with db.tx() as conn:
        ready = set(_apps_ready(conn, ctx["biz"]["id"]))
        (ready.add if f.get("on") == "1" else ready.discard)(key)
        conn.execute(update(db.businesses).where(db.businesses.c.id == ctx["biz"]["id"]).values(apps_ready=sorted(ready)))
        log(conn, ctx["biz"]["id"], "apps.ready" if f.get("on") == "1" else "apps.unready", ctx["actor"], None, {"app": key})
    return _go(f"/app/apps#app-{key}")


@router.post("/app/apps/import")
@_guard
async def apps_import(request: Request):
    ctx = _pctx(request, "studio")
    f, ups = await _work_upload(request)
    pid = _uuid(f.get("project_id"))
    if not pid:
        raise Invalid("choose the project", "project_id")
    if not ups:
        raise Invalid("choose a file", "file")
    kind = f.get("kind") if f.get("kind") in ("render", "plan", "canva", "photo", "doc") else "canva"
    with db.tx() as conn:
        for up in ups[:20]:
            raw, mime, name = await _read_one(up)
            if not raw:
                continue
            FL.save(conn, ctx["biz"], ctx["actor"], raw=raw, mime=mime, name=name, project_id=pid,
                    kind="doc" if mime == "application/pdf" and kind == "photo" else kind,
                    caption=str(f.get("caption") or f.get("app") or "")[:500], work=True)
            del raw
    _flash(request, "saved")
    return _go(f"/app/projects/{pid}?tab=design")


@router.get("/app/canva/connect")
@_guard
async def canva_connect(request: Request):
    from ..services import canva as CV
    ctx = _pctx(request, "studio")
    host = request.headers.get("host", "")
    back = f"https://{host}/app/apps" if getattr(request.state, "studio_biz", None) else "/app/apps"
    with db.tx() as conn:
        url = CV.start(conn, ctx["biz"]["id"], ctx["user"]["id"] if ctx["user"] else None, request.query_params.get("back") or back)
    return _go(url)


@router.get("/app/canva/callback")
@_guard
async def canva_callback(request: Request):
    """Canva sends the designer back here (the server's main address) — no session needed: the state is the key."""
    from ..services import canva as CV
    if request.query_params.get("error"):
        _flash(request, "st_canva_failed", "warn")
        return _go("/app/apps")
    try:
        _bid, back = CV.finish(request.query_params.get("state", ""), request.query_params.get("code", ""))
    except (CV.CanvaError, httpx.HTTPError) as e:
        _flash(request, "error", "warn", raw=str(e))
        return _go("/app/apps")
    sep = "&" if "?" in back else "?"
    return _go(f"{back}{sep}canva=ok")


@router.post("/app/canva/disconnect")
@_guard
async def canva_disconnect(request: Request):
    from ..services import canva as CV
    ctx = _pctx(request, "studio")
    await _form(request)
    with db.tx() as conn:
        CV.disconnect(conn, ctx["biz"]["id"], ctx["actor"])
    return _go("/app/apps")


@router.post("/app/projects/{pid}/canva")
@_guard
async def canva_new(request: Request, pid: UUID):
    from ..services import canva as CV
    ctx = _pctx(request, "studio")
    f = await _form(request)
    try:
        d = CV.new_design(ctx["biz"], ctx["actor"], pid, str(f.get("kind") or "board"))
    except (CV.CanvaError, httpx.HTTPError) as e:
        _flash(request, "error", "warn", raw=str(e))
        return _back(pid, "design", "#canva")
    if d.get("edit_url"):
        return _go(d["edit_url"])
    return _back(pid, "design", "#canva")


@router.post("/app/canva/designs/{did}")
@_guard
async def canva_import(request: Request, did: UUID):
    from ..services import canva as CV
    ctx = _pctx(request, "studio")
    f = await _form(request)
    with db.tx() as conn:
        d = conn.execute(select(db.canva_designs).where(db.canva_designs.c.business_id == ctx["biz"]["id"],
                                                        db.canva_designs.c.id == did)).mappings().first()
    if not d:
        raise NotFound("design")
    try:
        CV.bring_back(ctx["biz"], ctx["actor"], did, fmt="pdf" if f.get("fmt") == "pdf" else "png")
        _flash(request, "st_canva_back")
    except (CV.CanvaError, httpx.HTTPError) as e:
        _flash(request, "error", "warn", raw=str(e))
    return _back(d["project_id"], "design", "#canva")


@router.post("/app/website/assistant-photo")
@_guard
async def assistant_photo(request: Request):
    """The face on the "Ask" chat bubble: a public picture of the business, shown on its site and any site with the bubble."""
    from ..services import businesses as B
    from ..services import site as SI
    ctx = _own(request)
    f, ups = await _upload(request)
    if not ups:
        raise Invalid("choose a photo", "file")
    raw, mime, name = ups[0]
    if not mime.startswith("image/"):
        raise Invalid("a photo (JPG or PNG)", "file")
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        old = (biz.get("site") or {}).get("assistant_photo")
        fl = FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=name or "assistant.jpg", kind="photo", caption="assistant")
        FL.patch(conn, biz["id"], ctx["actor"], fl["id"], public=True)
        SI.save(conn, biz, ctx["actor"], assistant_photo=str(fl["id"]))
        if old:
            try:
                FL.remove(conn, biz["id"], ctx["actor"], UUID(old))
            except (NotFound, ValueError):
                pass
    _flash(request, "saved")
    return _go("/app/website#assistant")


# ---------------------------------------------------------------- page01: «Mi página» — photos and video straight onto the page
from ..services.site import GALLERY  # noqa: E402  (the editor and the remove route name the gallery project)


@router.post("/app/website/media")
@_guard
async def website_media(request: Request):
    """Photos → the page's gallery (public, published); a video → the page's top video (the old one goes). One form, many files."""
    from ..services import businesses as B
    from ..services import site as SI
    ctx = _own(request)
    _need(request, ctx, "website")
    f, ups = await _upload(request)
    if not ups:
        raise Invalid("choose photos or a video", "file")
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        for raw, mime, name in ups[:12]:
            _, biz = SI.add_media(conn, biz, ctx["actor"], raw, mime, name)       # ponla01: same path as WhatsApp «ponla en mi página»
    _flash(request, "saved")
    return _go("/app/website#media")


@router.post("/app/website/media/{fid}/remove")
@_guard
async def website_media_remove(request: Request, fid: UUID):
    """Take a photo off the page's gallery, or the page's video off the page."""
    from ..services import businesses as B
    from ..services import site as SI
    ctx = _own(request)
    _need(request, ctx, "website")
    await _form(request)
    with db.tx() as conn:
        biz = B.get(conn, ctx["biz"]["id"])
        if (biz.get("site") or {}).get("hero_video") == str(fid):
            SI.save(conn, biz, ctx["actor"], hero_video="")
        else:
            gal = conn.execute(select(db.projects.c.id).where(db.projects.c.business_id == biz["id"],
                                                               db.projects.c.name.in_(list(GALLERY.values())))).scalars().all()
            f = FL.get(conn, biz["id"], fid)
            if f.get("project_id") not in gal:
                raise NotFound("file")
        FL.remove(conn, biz["id"], ctx["actor"], fid)
    _flash(request, "saved")
    return _go("/app/website#media")


# ---------------------------------------------------------------- Valeria, inside the office and the studio
@router.post("/app/assistant")
@_guard
async def copilot_chat(request: Request):
    from ..services import copilot as CP
    ctx = _ctx(request)
    if not secrets.compare_digest(request.headers.get("x-csrf", ""), str(request.session.get("csrf", "x"))):
        return JSONResponse({"error": "expired"}, status_code=403)
    if not _limit(_copilot_hits, request, 120):
        return JSONResponse({"error": "busy"}, status_code=429)
    try:
        d = json.loads(await request.body() or b"{}")
    except ValueError:
        d = {}
    from ..services import helpdesk as HD
    turns = d.get("turns") or []
    last = next((str(x.get("t") or "") for x in reversed(turns) if isinstance(x, dict) and x.get("r") == "user"), "")
    lang = _lang(request, ctx["biz"])
    try:
        out = CP.chat(ctx["biz"], ctx["user"], turns, str(d.get("page") or ""))
    except Invalid as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except AI.AIError as e:
        out = {"say": str(e), "actions": []}
    if out.get("reason") == "no_ai":                       # help01: no AI key → Valeria's built-in help + its wizards
        return JSONResponse(HD.answer(last, lang))
    out["guides"] = HD.match(last)                         # help01: with AI too, the wizards for what they asked about
    return JSONResponse(out)


@router.get("/app/help/catalog")
@_guard
async def help_catalog(request: Request):
    """help01: the built-in help and its wizards, in the person's language (the office page loads it once)."""
    from ..services import helpdesk as HD
    ctx = _ctx(request)
    return JSONResponse(HD.catalog(_lang(request, ctx["biz"])), headers={"Cache-Control": "private, max-age=600"})


@router.post("/app/help/forme")
@_guard
async def help_for_me(request: Request):
    """help01: «do it for me» — the job goes to valAI (owner ask → Tad's alert); we do it and tell them."""
    from ..services import helpdesk as HD
    ctx = _ctx(request)
    if not secrets.compare_digest(request.headers.get("x-csrf", ""), str(request.session.get("csrf", "x"))):
        return JSONResponse({"error": "expired"}, status_code=403)
    if not _limit(_forme_hits, request, 10):
        return JSONResponse({"error": "busy"}, status_code=429)
    try:
        d = json.loads(await request.body() or b"{}")
    except ValueError:
        d = {}
    with db.tx() as conn:
        HD.ask_for_me(conn, ctx["biz"], (ctx["user"] or {}).get("id"), str(d.get("task") or ""), str(d.get("page") or ""))
    return JSONResponse({"ok": True, "say": HD.TEXT[HD.lang_of(_lang(request, ctx["biz"]))]["asked"]})


_forme_hits: dict = {}


@router.post("/app/assistant/do")
@_guard
async def copilot_do(request: Request):
    from ..services import copilot as CP
    ctx = _ctx(request)
    if not secrets.compare_digest(request.headers.get("x-csrf", ""), str(request.session.get("csrf", "x"))):
        return JSONResponse({"error": "expired"}, status_code=403)
    try:
        d = json.loads(await request.body() or b"{}")
        done = CP.run_write(ctx["biz"], ctx["actor"], (ctx["user"] or {}).get("id"), str(d.get("token") or ""))
    except (Invalid, NotFound, Blocked) as e:
        return JSONResponse({"error": str(e) or "not found"}, status_code=400)
    except ValueError:
        return JSONResponse({"error": "bad request"}, status_code=400)
    return JSONResponse({"done": done})


_copilot_hits: dict = {}
