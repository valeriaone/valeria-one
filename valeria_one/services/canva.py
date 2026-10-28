"""Canva, with the business's OWN Canva account (Canva Connect API, OAuth with PKCE).

From a project: start a Canva design (a mood board, a presentation, a post) that already has the project's renders
and photos in the designer's Canva uploads, edit it in Canva, then bring it back into the project with one tap
(exported as PNG pages, or one PDF). Off until the server has a Canva app (VO_CANVA_CLIENT_ID / _SECRET), which
the server's operator registers once at canva.com/developers — free; each business connects its own account.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from uuid import UUID

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, NotFound, log

AUTH = "https://www.canva.com/api/oauth/authorize"
API = "https://api.canva.com/rest/v1"
SCOPES = "asset:read asset:write design:content:read design:content:write design:meta:read profile:read"
KINDS = {"board": ("whiteboard", "Moodboard"), "presentation": ("presentation", "Presentación"), "doc": ("doc", "Documento")}
MAX_ASSETS = 10
AL, CD = db.app_links, db.canva_designs


class CanvaError(Exception):
    pass


def configured() -> bool:
    return bool(config.get("VO_CANVA_CLIENT_ID") and config.get("VO_CANVA_CLIENT_SECRET"))


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL()}/app/canva/callback"


def _http(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=30)


# ---------------------------------------------------------------- connecting
def start(conn: Connection, business_id: UUID, user_id: UUID | None, back: str) -> str:
    """-> the Canva sign-in URL. The PKCE verifier waits server-side (10 minutes, once) under the state."""
    if not configured():
        raise Invalid("Canva isn't set up on this server yet", "canva")
    from . import instance as I
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
    I.kv_set(conn, f"canva:{state}"[:60], {"b": str(business_id), "u": str(user_id) if user_id else None, "v": verifier,
                                             "back": back, "at": time.time()})
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return AUTH + "?" + urlencode({"code_challenge": challenge, "code_challenge_method": "s256", "scope": SCOPES,
                                   "response_type": "code", "client_id": config.get("VO_CANVA_CLIENT_ID"), "state": state,
                                   "redirect_uri": redirect_uri()})


def _basic() -> str:
    raw = f"{config.get('VO_CANVA_CLIENT_ID')}:{config.get('VO_CANVA_CLIENT_SECRET')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _json(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise CanvaError(f"Canva answered {r.status_code}") from None
    if r.status_code >= 300:
        msg = (j.get("message") or j.get("error_description") or j.get("error") or r.status_code) if isinstance(j, dict) else r.status_code
        raise CanvaError(f"Canva said: {msg}")
    return j


def finish(state: str, code: str, http=None) -> tuple[UUID, str]:
    """The callback: code -> tokens, saved for the business. -> (business_id, where to go back to)."""
    from . import instance as I
    with db.tx() as conn:
        key = f"canva:{state}"[:60]
        st = I.kv_get(conn, key) if state else None
        if st:
            conn.execute(delete(db.instance_kv).where(db.instance_kv.c.key == key))   # each state works once
    if not st or time.time() - st.get("at", 0) > 600 or not code:
        raise CanvaError("that Canva sign-in expired; try again")
    h = _http(http)
    tok = _json(h.post(f"{API}/oauth/token", headers={"Authorization": _basic()},
                       data={"grant_type": "authorization_code", "code": code, "code_verifier": st["v"], "redirect_uri": redirect_uri()}))
    name = ""
    try:
        name = (_json(h.get(f"{API}/users/me/profile", headers={"Authorization": f"Bearer {tok['access_token']}"}))
                .get("profile") or {}).get("display_name", "")
    except (CanvaError, httpx.HTTPError):
        pass
    bid = UUID(st["b"])
    with db.tx() as conn:
        _save(conn, bid, tok, name, f"user:{st['u']}" if st.get("u") else "canva")
        log(conn, bid, "canva.connected", f"user:{st['u']}" if st.get("u") else "canva", None, {"account": name[:100]})
    return bid, st.get("back") or "/app/apps"


def _save(conn: Connection, business_id: UUID, tok: dict, name: str | None, actor: str) -> None:
    vals = {"tokens": crypto.seal({"access_token": tok["access_token"], "secret": tok.get("refresh_token") or ""}),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in") or 3600) - 60),
            "updated_at": datetime.now(timezone.utc)}
    ins = pg_insert(AL).values(business_id=business_id, provider="canva", account_name=(name or "")[:200], connected_by=actor, **vals)
    upd = {**vals, **({"account_name": name[:200]} if name else {})}
    conn.execute(ins.on_conflict_do_update(constraint="uq_app_link", set_=upd))


def link(conn: Connection, business_id: UUID) -> dict | None:
    r = conn.execute(select(AL.c.account_name, AL.c.expires_at, AL.c.created_at).where(AL.c.business_id == business_id,
                                                                                      AL.c.provider == "canva")).mappings().first()
    return dict(r) if r else None


def disconnect(conn: Connection, business_id: UUID, actor: str) -> None:
    conn.execute(delete(AL).where(AL.c.business_id == business_id, AL.c.provider == "canva"))
    log(conn, business_id, "canva.disconnected", actor)


def _token(business_id: UUID, http=None) -> str:
    """A working access token, refreshed when it has run out (Canva's last 4 hours)."""
    with db.tx() as conn:
        row = conn.execute(select(AL).where(AL.c.business_id == business_id, AL.c.provider == "canva")).mappings().first()
    if not row:
        raise CanvaError("connect your Canva account first")
    t = crypto.open_(row["tokens"])
    if row["expires_at"] and row["expires_at"] > datetime.now(timezone.utc):
        return t["access_token"]
    if not t.get("secret"):
        raise CanvaError("connect your Canva account again")
    tok = _json(_http(http).post(f"{API}/oauth/token", headers={"Authorization": _basic()},
                                 data={"grant_type": "refresh_token", "refresh_token": t["secret"]}))
    with db.tx() as conn:
        _save(conn, business_id, tok, None, "canva")
    return tok["access_token"]


def _wait(h: httpx.Client, url: str, token: str, tries: int = 30) -> dict:
    for _ in range(tries):
        job = _json(h.get(url, headers={"Authorization": f"Bearer {token}"})).get("job") or {}
        if job.get("status") == "success":
            return job
        if job.get("status") == "failed":
            raise CanvaError((job.get("error") or {}).get("message") or "Canva couldn't finish it")
        time.sleep(1)
    raise CanvaError("Canva is taking long; try again in a minute")


# ---------------------------------------------------------------- designs from a project, and back
def new_design(biz: dict, actor: str, project_id: UUID, kind: str, http=None) -> dict:
    """Upload the project's renders and photos to the designer's Canva, start a design with them. -> the design row."""
    from . import files as FL
    from . import projects as PR
    if kind not in KINDS:
        raise Invalid("board, presentation or doc", "kind")
    with db.tx() as conn:
        p = PR.get(conn, biz["id"], project_id)
        pics = [f for f in FL.list_(conn, biz["id"], project_id=project_id) if f["mime"].startswith("image/")
                and f["kind"] in ("render", "photo", "canva")]
        pics = sorted(pics, key=lambda f: f["kind"] != "render")[:MAX_ASSETS]
        blobs = [(f["name"] or f"{p['name']}-{i + 1}.jpg", FL.content(conn, biz["id"], f["id"])) for i, f in enumerate(pics)]
    token, h = _token(biz["id"], http), _http(http)
    assets = []
    for name, (data, mime) in blobs:
        meta = json.dumps({"name_base64": base64.b64encode(name[:50].encode()).decode()})
        job = _json(h.post(f"{API}/asset-uploads", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream",
                                                            "Asset-Upload-Metadata": meta}, content=data)).get("job") or {}
        if job.get("status") != "success":
            job = _wait(h, f"{API}/asset-uploads/{job.get('id')}", token)
        if (job.get("asset") or {}).get("id"):
            assets.append(job["asset"]["id"])
    preset, word = KINDS[kind]
    title = f"{p['name']} — {word}"[:255]
    body = {"design_type": {"type": "preset", "name": preset}, "title": title}
    if assets:
        body["asset_id"] = assets[0]
    d = _json(h.post(f"{API}/designs", headers={"Authorization": f"Bearer {token}"}, json=body)).get("design") or {}
    if not d.get("id"):
        raise CanvaError("Canva didn't make the design")
    with db.tx() as conn:
        row = dict(conn.execute(pg_insert(CD).values(business_id=biz["id"], project_id=project_id, design_id=d["id"], title=d.get("title") or title,
                                                     edit_url=(d.get("urls") or {}).get("edit_url", ""))
                                .on_conflict_do_nothing().returning(CD)).mappings().first() or {})
        log(conn, biz["id"], "canva.design_created", actor, project_id, {"design": d["id"], "assets": len(assets)})
    return row or {"design_id": d["id"], "edit_url": (d.get("urls") or {}).get("edit_url", "")}


def designs(conn: Connection, business_id: UUID, project_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(CD).where(CD.c.business_id == business_id, CD.c.project_id == project_id)
                                          .order_by(CD.c.created_at.desc())).mappings()]


def _download(h: httpx.Client, url: str) -> bytes:
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or not (host == "canva.com" or host.endswith(".canva.com")):
        raise CanvaError("Canva sent a file from somewhere unexpected; nothing was saved")
    r = h.get(url)
    if r.status_code != 200 or len(r.content) > 25 * 1024 * 1024:
        raise CanvaError("couldn't download the design from Canva")
    return r.content


def bring_back(biz: dict, actor: str, row_id: UUID, fmt: str = "png", http=None) -> int:
    """Export the design (PNG pages, or one PDF) and save it into its project as Canva files. -> how many files."""
    from . import files as FL
    if fmt not in ("png", "pdf"):
        raise Invalid("png or pdf", "format")
    with db.tx() as conn:
        d = conn.execute(select(CD).where(CD.c.business_id == biz["id"], CD.c.id == row_id)).mappings().first()
    if not d:
        raise NotFound("design")
    token, h = _token(biz["id"], http), _http(http)
    job = _json(h.post(f"{API}/exports", headers={"Authorization": f"Bearer {token}"},
                       json={"design_id": d["design_id"], "format": {"type": fmt}})).get("job") or {}
    if job.get("status") != "success":
        job = _wait(h, f"{API}/exports/{job.get('id')}", token)
    urls = (job.get("urls") or [])[:30]
    if not urls:
        raise CanvaError("Canva sent nothing back")
    blobs = [_download(h, u) for u in urls]
    with db.tx() as conn:
        for n, raw in enumerate(blobs):
            FL.save(conn, biz, actor, raw=raw, mime="application/pdf" if fmt == "pdf" else "image/png", kind="canva",
                    project_id=d["project_id"], name=f"{d['title'][:150]}{f' ({n + 1})' if len(blobs) > 1 else ''}.{fmt}",
                    caption=d["title"][:500])
        conn.execute(update(CD).where(CD.c.id == row_id).values(imported_at=datetime.now(timezone.utc)))
        log(conn, biz["id"], "canva.imported", actor, d["project_id"], {"design": d["design_id"], "files": len(blobs)})
    return len(blobs)
