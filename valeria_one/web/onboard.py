"""setup28 (v0.28) on screen: «Valeria sets it up» (/app/onboard) — she asks one question at a time which accounts the
business already has (chips, «I don't have it», «Later»; it picks up where it stopped) and sets each one up with what is
already here: the contact number and click-to-chat, the email channel (sign-in tested first), tracked sources with the
website snippet and bio links, Meta / TikTok / Google / calendar / payments connect screens. The end: «Ready: N of M»,
each pending item with one clear step. Offered right after sign-up, from Connect and from Today (owner only)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import config, db
from ..services import Invalid
from ..services import onboard as OBS
from . import _flash, _go, _guard, _lang, _multi_form, _not_shared_demo, _own, render
from .i18n import has

router = APIRouter(include_in_schema=False)


def _say(request: Request, e: Invalid) -> None:
    """A refusal in the reader's language (the service says it as a word key)."""
    k = str(e)
    if has(k):
        _flash(request, k, "warn")
    else:
        _flash(request, "error", "warn", raw=k)


@router.get("/app/onboard")
@_guard
async def onboard_page(request: Request, q: str | None = None):
    from ..services import meta as MT
    ctx = _own(request)
    biz = ctx["biz"]
    lang = _lang(request, biz)
    uid = ctx["user"]["id"] if ctx["user"] else None
    with db.tx() as conn:
        st = OBS.state(conn, biz, uid, lang=lang)
    order = st["order"]
    if q not in order:
        q = None if q == "summary" or not st["next"] else st["next"]
    it = st["qs"][q] if q else None
    i = order.index(q) if q else len(order)
    a = (it or {}).get("answer") or {}
    mail = None
    if q == "email":
        prov = a.get("provider")
        mail = {"provider": prov, "info": OBS.MAIL.get(prov) if prov else None, "custom": OBS.CUSTOM_HOSTS,
                "names": {k: v["name"] for k, v in OBS.MAIL.items()},
                "host": OBS.mail_host(prov, biz["country"], a.get("value") or "") if prov else None}
    tap = OBS.meta_tap(biz)
    meta = MT.settings() if (tap["wa"] or tap["pages"]) else None
    return render(request, "onboard.html", ctx, page="connect", st=st, q=q, it=it, a=a, pos=i, prev=order[i - 1] if i > 0 else None,
                  later=order[i + 1] if q and i + 1 < len(order) else None, tools=OBS.tools(biz),
                  chats=OBS.chat_options(biz), pays=OBS.payment_options(biz), pay_name=lambda p: OBS.pay_name(p, lang),
                  chat_names=OBS.CHAT_NAMES, mail=mail, tap=tap, meta=meta, graph_version=config.get("VO_GRAPH_VERSION", "v23.0"),
                  last=request.session.pop("ob_last", None), dry=request.session.pop("ob_dry", None),
                  wa_kinds=OBS.WA_KINDS, cal_kinds=OBS.CAL_KINDS)


@router.post("/app/onboard")
@_guard
async def onboard_answer(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    form = {k: v for k, v in f.multi_items() if isinstance(v, str)}
    form["pick"] = [str(x) for x in f.getlist("pick")]
    key = str(form.get("q") or "")
    biz = ctx["biz"]
    up = f.get("photo") if key == "prices" else None             # cfg01: a photo of their price list
    if up is not None and not isinstance(up, str) and getattr(up, "filename", ""):
        from ..services import files as FL
        raw = await up.read()
        if raw:
            mime = (up.content_type or "").lower()
            try:
                with db.tx() as conn:
                    form["photo_id"] = str(FL.save(conn, biz, ctx["actor"], raw=raw, mime=mime, name=up.filename or "precios",
                                                   kind="doc" if mime == "application/pdf" else "photo")["id"])
            except Invalid as e:
                _say(request, e)
                return _go("/app/onboard?q=prices")
            except Exception:                                       # noqa: BLE001 — a broken picture: a calm word, no error page
                _flash(request, "ob_prices_photo_bad", "warn")
                return _go("/app/onboard?q=prices")
    try:
        got = OBS.answer(biz, ctx["actor"], key, form)
    except Invalid as e:
        _say(request, e)
        return _go(f"/app/onboard?q={key}" if key in OBS.ORDER else "/app/onboard")
    if got["dry_run"]:
        request.session["ob_dry"] = {"key": key, "steps": got["steps"]}
        return _go(f"/app/onboard?q={key}")
    request.session["ob_last"] = key
    with db.tx() as conn:
        from ..services import businesses as B
        st = OBS.state(conn, B.get(conn, biz["id"]), links=False)
    step = (st["qs"][key].get("step") or {}).get("href", "")
    if got["has"] == "yes" and step.startswith(f"/app/onboard?q={key}"):
        return _go(f"/app/onboard?q={key}")                    # one more thing to give here (the app password, a link…)
    nxt = _next_after(st, key)
    return _go(f"/app/onboard?q={nxt}" if nxt else "/app/onboard?q=summary")


def _next_after(st: dict, key: str) -> str | None:
    """The next question nobody answered yet, after this one (then from the top): the talk moves forward."""
    order = st["order"]
    i = order.index(key) if key in order else -1
    todo = [k for k in order if not st["qs"][k]["has"]]
    return next((k for k in todo if order.index(k) > i), todo[0] if todo else None)


@router.post("/app/onboard/restart")
@_guard
async def onboard_restart(request: Request):
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    await _multi_form(request)
    with db.tx() as conn:
        OBS.reset(conn, ctx["biz"], ctx["actor"])
    _flash(request, "ob_restarted")
    return _go("/app/onboard")


def today_card(request: Request, conn, ctx: dict) -> dict | None:
    """Today's nudge (owner only) until the talk is over and nothing is left to do."""
    if ctx.get("role") != "owner":
        return None
    return OBS.nudge(conn, ctx["biz"])

