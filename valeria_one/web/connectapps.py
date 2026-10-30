"""ars02 (v0.21) on screen: Connect other apps (/app/connect-apps) — in one place: the business's API keys (shown once,
kept hashed), webhooks OUT (pick the events in plain words, see every delivery and its answer, send one again) and
connections IN (an address other apps post a new customer or deal to: /hooks/in/<token>). How-to: docs/WEBHOOKS.md."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, db, packs
from ..services import Invalid, NotFound
from ..services import businesses as B
from ..services import inbound as IN
from ..services import webhooks as WH
from . import _go, _guard, _limit, _multi_form, _not_shared_demo, _own, render

router = APIRouter(include_in_schema=False)
_in_hits: dict = {}


def _uuid(v, what: str = "connection") -> UUID:
    try:
        return UUID(str(v))
    except ValueError:
        raise NotFound(what) from None


def _page(request: Request, ctx: dict, **once):
    biz = ctx["biz"]
    sel = request.query_params.get("hook")
    with db.tx() as conn:
        keys = B.keys(conn, biz["id"])
        hooks = WH.endpoints(conn, biz["id"])
        ins = IN.list_(conn, biz["id"])
        log = WH.deliveries(conn, biz["id"], _uuid(sel), 30) if sel and any(str(h["id"]) == sel for h in hooks) else None
    return render(request, "connect_apps.html", ctx, keys=keys, hooks=hooks, ins=ins, log=log, sel=sel, events=WH.EVENTS,
                  stages=packs.industry(biz["industry"]).stages, base=config.PUBLIC_URL(), page="connect_apps", **once)


@router.get("/app/connect-apps")
@_guard
async def connect_apps(request: Request):
    return _page(request, _own(request))


@router.post("/app/connect-apps")
@_guard
async def connect_apps_act(request: Request):
    """Every secret (a key, a webhook's signing secret, a connection's address) is shown once, in this answer only."""
    ctx = _own(request)
    _not_shared_demo(request, ctx)
    f = await _multi_form(request)
    act, biz = str(f.get("act") or ""), ctx["biz"]
    with db.tx() as conn:
        if act == "key":
            key, _ = B.new_key(conn, biz["id"], str(f.get("name") or "app").strip()[:100])
            out = {"new_key": key}
        elif act == "key_revoke":
            B.revoke_key(conn, biz["id"], ctx["actor"], _uuid(f.get("id"), "key"))
            return _go("/app/connect-apps#keys")
        elif act == "hook":
            evs = [str(e) for e in f.getlist("event") if str(e) in WH.EVENTS] or ["*"]
            _, secret = WH.create(conn, biz, ctx["actor"], str(f.get("url") or ""), evs)
            out = {"hook_secret": secret}
        elif act == "hook_delete":
            WH.delete(conn, biz["id"], ctx["actor"], _uuid(f.get("id"), "webhook"))
            return _go("/app/connect-apps#out")
        elif act == "hook_test":
            WH.test(conn, biz, ctx["actor"], _uuid(f.get("id"), "webhook"))
            return _go(f"/app/connect-apps?hook={f.get('id')}#log")
        elif act == "redeliver":
            try:
                did = int(str(f.get("delivery") or ""))
            except ValueError:
                raise NotFound("delivery") from None
            WH.redeliver(conn, biz["id"], ctx["actor"], did)
            return _go(f"/app/connect-apps?hook={f.get('id')}#log")
        elif act == "in":
            _, token = IN.create(conn, biz, ctx["actor"], name=str(f.get("name") or ""), makes=str(f.get("makes") or "contact"),
                                 stage=str(f.get("stage") or "") or None, tag=str(f.get("tag") or "") or None)
            out = {"in_url": f"{config.PUBLIC_URL()}/hooks/in/{token}"}
        elif act == "in_delete":
            IN.delete(conn, biz["id"], ctx["actor"], _uuid(f.get("id")))
            return _go("/app/connect-apps#in")
        else:
            raise Invalid("?", "act")
    return _page(request, ctx, **out)


@router.post("/hooks/in/{token}")
async def inbound(request: Request, token: str):
    """Another app sends a customer in: JSON or a form, {name, email, phone, message, tags, deal, amount_minor}."""
    if not _limit(_in_hits, request, 120, 60):
        return JSONResponse({"error": "too many requests; try again in a minute"}, status_code=429)
    raw = await request.body()
    if len(raw) > IN.MAX_BODY:
        return JSONResponse({"error": "too big"}, status_code=413)
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("not an object")
        else:
            data = {k: v for k, v in (await request.form()).multi_items() if isinstance(v, str)}
    except ValueError:
        return JSONResponse({"error": "send JSON (an object) or a form"}, status_code=400)
    try:
        out = IN.receive(token, data)
    except NotFound:
        return JSONResponse({"error": "unknown address"}, status_code=404)
    except Invalid as e:
        return JSONResponse({"error": str(e), "field": e.field}, status_code=422)
    return JSONResponse({"ok": True, **out}, status_code=201)
