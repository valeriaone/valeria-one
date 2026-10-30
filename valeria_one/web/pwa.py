"""v0.18: Valeria One as an installable app — the web app manifest, the service worker (an offline page and Valeria
One's own files; NEVER a signed-in page, an API answer or anyone's data), Web Push for the owner's notifications
(services/push.py, VAPID: no Firebase) and Android's Digital Asset Links for the Play Store app (a Trusted Web
Activity, deploy/android/): /.well-known/assetlinks.json from VO_ANDROID_PACKAGE + VO_ANDROID_SHA256 — not set = 404."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import __version__, config, db
from ..services import Invalid
from ..services import push as PS
from . import _ctx, _guard, _lang, render
from .i18n import t

router = APIRouter(include_in_schema=False)
STATIC = Path(__file__).parent / "static"
_FP = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
SIZES = (48, 72, 96, 128, 144, 152, 192, 256, 384, 512)


@router.get("/manifest.webmanifest")
async def manifest(request: Request):
    lang = _lang(request)
    data = {
        "name": "Valeria One", "short_name": "Valeria One", "id": "/app", "start_url": "/app?source=app", "scope": "/",
        "display": "standalone", "orientation": "any", "background_color": "#05070d", "theme_color": "#05070d",
        "lang": lang, "dir": "rtl" if lang == "ar" else "ltr", "description": t("pw_description", lang),
        "categories": ["business", "productivity"],
        "icons": [{"src": f"/static/icons/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any"} for s in SIZES]
        + [{"src": f"/static/icons/maskable-{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "maskable"} for s in (192, 512)],
        "shortcuts": [{"name": t("inbox", lang), "url": "/app/inbox", "icons": [{"src": "/static/icons/icon-96.png", "sizes": "96x96"}]},
                      {"name": t("today", lang), "url": "/app", "icons": [{"src": "/static/icons/icon-96.png", "sizes": "96x96"}]}],
    }
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sw.js")
async def service_worker():
    """The service worker, from the site's root so it may serve the whole app; never cached itself (updates arrive)."""
    js = (STATIC / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", __version__)
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Service-Worker-Allowed": "/"})


@router.get("/offline")
async def offline(request: Request):
    return render(request, "offline.html", None)


@router.get("/.well-known/assetlinks.json")
async def assetlinks():
    """Proves to Android that the Play Store app (a Trusted Web Activity) and this site belong together."""
    pkg = (config.get("VO_ANDROID_PACKAGE") or "").strip()
    fps = [x.strip().upper() for x in (config.get("VO_ANDROID_SHA256") or "").split(",") if x.strip()]
    if not re.fullmatch(r"[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)+", pkg) or not fps or not all(_FP.match(x) for x in fps):
        return PlainTextResponse("not found", status_code=404)
    return JSONResponse([{"relation": ["delegate_permission/common.handle_all_urls"],
                          "target": {"namespace": "android_app", "package_name": pkg, "sha256_cert_fingerprints": fps}}])


# ---------------------------------------------------------------- notifications on this device (Web Push)
def _csrf_ok(request: Request) -> bool:
    return secrets.compare_digest(request.headers.get("x-csrf", ""), str(request.session.get("csrf", "x")))


@router.get("/app/push/key")
@_guard
async def push_key(request: Request):
    _ctx(request)
    pub, _ = PS.vapid()
    return JSONResponse({"key": pub})


@router.post("/app/push/subscribe")
@_guard
async def push_subscribe(request: Request):
    ctx = _ctx(request)
    if not _csrf_ok(request) or not ctx["user"]:
        return JSONResponse({"ok": False}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    try:
        with db.tx() as conn:
            PS.subscribe(conn, ctx["biz"], ctx["user"]["id"], body, request.headers.get("user-agent", ""))
    except Invalid:
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/app/push/unsubscribe")
@_guard
async def push_unsubscribe(request: Request):
    ctx = _ctx(request)
    if not _csrf_ok(request) or not ctx["user"]:
        return JSONResponse({"ok": False}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    with db.tx() as conn:
        PS.unsubscribe(conn, ctx["user"]["id"], str(body.get("endpoint") or ""))
    return JSONResponse({"ok": True})


@router.post("/app/push/test")
@_guard
async def push_test(request: Request):
    ctx = _ctx(request)
    if not _csrf_ok(request) or not ctx["user"]:
        return JSONResponse({"ok": False}, status_code=403)
    lang = _lang(request, ctx["biz"])
    n = PS.notify(ctx["biz"]["id"], ctx["biz"]["name"], t("pw_test_body", lang), "/app", user_id=ctx["user"]["id"])
    return JSONResponse({"ok": n > 0, "sent": n})


# face01 (Tad 2026-10-30 "put Valeria's face back"): the operator's default face for Valeria's button, for every business
# that hasn't uploaded its own. VO_ASSISTANT_PHOTO = "<business id>/<file id>" of a photo stored on THIS server, so the
# picture never lives in the public code. Unset = no default (the button shows "V").
def default_face() -> tuple[str, str] | None:
    v = (config.get("VO_ASSISTANT_PHOTO") or "").strip()
    if v.count("/") != 1:
        return None
    b, f = v.split("/")
    return (b, f) if b and f else None


@router.get("/assistant-face.jpg")
async def assistant_face():
    from uuid import UUID
    from ..services import files as FS
    from ..services import NotFound
    d = default_face()
    if not d:
        return Response(status_code=404)
    try:
        with db.tx() as conn:
            data, mime = FS.content(conn, UUID(d[0]), UUID(d[1]), thumb=True)
    except (ValueError, NotFound):
        return Response(status_code=404)
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

