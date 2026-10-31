"""agr01 (v0.25) on screen: our agreement with every business.

The business (/app/agreement): the owner reads the current version in their language, ticks "I agree, for <business>",
types their full name, draws the signature (the typed name alone when the pad can't be used) and signs — or taps "Later"
(while the grace lasts) or "I don't agree" (then: how to take their data and leave; nothing is deleted). Their signed copies
stay in Settings › Our agreement, printable. Team members see where it stands; only the owner signs.

The operator (/app/server-agreement, the Server page): who the provider is, the law and courts, the deletion days, the
sub-processors, extra terms, the grace; a preview in every language; Publish (frozen + SHA-256); every version; who signed
which version and who hasn't. Until the details are filled in and a version is published, nothing is asked of anyone."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import db
from ..services import agreement as AG
from . import _admin, _countries, _ctx, _flash, _form, _go, _guard, _lang, _own, render
from .i18n import UI_LANGS, ui_lang

router = APIRouter(include_in_schema=False)


def _pick(request: Request, biz: dict) -> str:
    """The operator's views: ?lang=<code> shows that language's text (the screen keeps its own language)."""
    want = request.query_params.get("lang")
    return ui_lang(want) if want in UI_LANGS else _lang(request, biz)


def _view(cur: dict, lang: str, business_name: str) -> dict:
    lg, text = AG.text_for(cur, lang, business_name)
    title, paras = AG.paragraphs(text)
    return {"text_lang": lg, "title": title, "paras": paras, "shown": AG.sha(text)}


# ---------------------------------------------------------------- the business
@router.get("/app/agreement")
@_guard
async def agreement_page(request: Request):
    ctx = _ctx(request)
    biz, owner = ctx["biz"], ctx["role"] == "owner" and bool(ctx["user"])
    lang = _lang(request, biz)
    with db.tx() as conn:
        cur = AG.current(conn)
        st = AG.state(conn, biz)
        if cur and st and st["need"] and owner:
            AG.asked(conn, biz, cur["id"])                         # the grace starts the first time the owner sees it
            st = AG.state(conn, biz)
        mine = AG.acceptances(conn, biz["id"]) if owner else []
    if cur and st and st["need"] and owner:
        request.session["agr_seen"] = str(cur["id"])               # asked once per sign-in
    view = _view(cur, lang, biz["name"]) if cur and st and st["need"] else {}
    return render(request, "agreement.html", ctx, page="agreement", cur=cur, st=st, mine=mine, owner=owner,
                  provider=(cur or {}).get("provider") or {}, declined=request.query_params.get("declined") == "1", **view)


@router.post("/app/agreement")
@_guard
async def agreement_post(request: Request):
    ctx = _ctx(request)
    f = await _form(request)
    if ctx["role"] != "owner" or not ctx["user"]:
        _flash(request, "ag_owner_signs", "warn")
        return _go("/app/agreement")
    biz, act = ctx["biz"], f.get("act")
    nxt = request.session.get("agr_next") or "/app"
    if act == "later":
        with db.tx() as conn:
            AG.later(conn, biz, ctx["actor"], f.get("agreement_id"))
        request.session.pop("agr_next", None)
        return _go(nxt)
    if act == "decline":
        with db.tx() as conn:
            AG.decline(conn, biz, ctx["actor"], f.get("agreement_id"))
        return _go("/app/agreement?declined=1")
    ip = request.client.host if request.client else ""
    with db.tx() as conn:
        cur = AG.current_meta(conn)
        had = bool(cur and AG.acceptance(conn, biz["id"], cur["id"]))
        row = AG.accept(conn, biz, ctx["user"], agreement_id=f.get("agreement_id"), lang=str(f.get("lang") or ""),
                        shown=str(f.get("shown") or ""), name=str(f.get("name") or ""), agree=f.get("agree") == "1",
                        signature=str(f.get("signature") or ""), ip=ip, ua=request.headers.get("user-agent", ""))
        provider = AG.version(conn, row["version"])["provider"]
    if not had:                                                    # the copy goes once, when it is signed
        AG.mail_copy(row, provider)
    request.session.pop("agr_next", None)
    _flash(request, "ag_signed")
    return _go(nxt if nxt != "/app/agreement" else "/app")


@router.get("/app/agreement/{aid}/print")
@_guard
async def agreement_print(request: Request, aid: str):
    ctx = _own(request)
    with db.tx() as conn:
        row = AG.signed(conn, ctx["biz"]["id"], aid)
    title, paras = AG.paragraphs(row["text"])
    return render(request, "agreement_print.html", ctx, row=row, title=title, paras=paras)


# ---------------------------------------------------------------- the operator (Server)
@router.get("/app/server-agreement")
@_guard
async def server_agreement(request: Request):
    ctx = _admin(request)
    with db.tx() as conn:
        s = AG.settings(conn)
        rep = AG.report(conn)
        vers = AG.versions(conn)
        changed = AG.changed(conn)
    return render(request, "server_agreement.html", ctx, page="server", s=s, missing=AG.missing(s), rep=rep, vers=vers,
                  changed=changed, countries=_countries(request))


@router.post("/app/server-agreement")
@_guard
async def server_agreement_save(request: Request):
    ctx = _admin(request)
    f = await _form(request)
    with db.tx() as conn:
        s = AG.save_settings(conn, ctx["actor"], f)
    _flash(request, "saved" if not AG.missing(s) else "ag_still_missing", "ok" if not AG.missing(s) else "warn")
    return _go("/app/server-agreement")


@router.post("/app/server-agreement/publish")
@_guard
async def server_agreement_publish(request: Request):
    ctx = _admin(request)
    await _form(request)
    with db.tx() as conn:
        a = AG.publish(conn, ctx["actor"])
    _flash(request, "ag_published", raw=None)
    return _go(f"/app/server-agreement#v{a['version']}")


@router.get("/app/server-agreement/preview")
@_guard
async def server_agreement_preview(request: Request):
    ctx = _admin(request)
    with db.tx() as conn:
        texts = AG.preview(conn)
    lang = _pick(request, ctx["biz"])
    return render(request, "agreement_view.html", ctx, page="server", draft=True, a=None,
                  **_view({"texts": texts}, lang, AG.BLANK))


@router.get("/app/server-agreement/v/{n}")
@_guard
async def server_agreement_version(request: Request, n: str):
    ctx = _admin(request)
    with db.tx() as conn:
        a = AG.version(conn, n)
    lang = _pick(request, ctx["biz"])
    return render(request, "agreement_view.html", ctx, page="server", draft=False, a=a, **_view(a, lang, AG.BLANK))
