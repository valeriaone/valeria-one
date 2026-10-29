"""TikTok inbox: connect a business's own TikTok Business Account, and route TikTok's one webhook to it.

The operator of a Valeria One server registers ONE TikTok for Business developer app with the Business Messaging
API (scopes message.list.read, message.list.send, message.list.manage) and sets VO_TIKTOK_APP_ID /
VO_TIKTOK_APP_SECRET. Without them the feature stays off and the screens say "not connected yet". Each business
then signs in with its own TikTok Business Account (linked to TikTok for Business); messages arrive at
/hooks/tiktok, signed with the app secret, and land in the normal inbox, customers and AI auto-reply.

TikTok's rules, kept here and in the inbox: the customer writes first; answers within 48 hours of their last
message; the access token lasts 24 h and the refresh token 30 days — both renewed by themselves (before a send,
and by the job), the refresh token kept encrypted. Business Messaging is not offered in the EEA, Switzerland or
the UK: businesses there see that notice instead of the button. See docs/TIKTOK_SETUP.md.
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection

from .. import channels, config, crypto, db
from ..channels import BadSignature, HookRequest
from ..channels.tiktok import API, _data, app, check_signature, tokens_from
from . import Invalid, log
from . import inbox as X

A = db.channel_accounts
AUTH = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,message.list.read,message.list.send,message.list.manage"
# Business Messaging is not available for accounts in the EEA, Switzerland or the UK
NOT_HERE = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO CH GB".split())
STATE_MINUTES = 10
REFRESH_AHEAD = 2 * 3600                  # the job renews tokens with less than 2 hours left


class TikTokError(Exception):
    pass


def configured() -> bool:
    app_id, secret = app()
    return bool(app_id and secret) and not config.SANDBOX()


def available_in(country: str) -> bool:
    return (country or "").upper() not in NOT_HERE


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL()}/app/tiktok/callback"


def _http(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=20)


def _check(biz: dict) -> None:
    if not configured():
        raise Invalid("TikTok is not set up on this server yet", "tiktok")
    if biz["sandbox"]:
        raise Invalid("sandbox businesses can't connect a real TikTok account", "tiktok")
    if not available_in(biz["country"]):
        raise Invalid("TikTok doesn't offer business messages in the EEA, Switzerland or the UK", "tiktok")


# ---------------------------------------------------------------- connecting (TikTok's sign-in)
def start(conn: Connection, biz: dict, user_id: UUID | None) -> str:
    """-> TikTok's sign-in address. The state waits server-side (10 minutes, once)."""
    from . import instance as I
    _check(biz)
    state = secrets.token_urlsafe(24)
    I.kv_set(conn, f"tiktok:{state}"[:60], {"b": str(biz["id"]), "u": str(user_id) if user_id else None, "at": time.time()})
    return AUTH + "?" + urlencode({"client_key": app()[0], "scope": SCOPES, "response_type": "code",
                                   "redirect_uri": redirect_uri(), "state": state})


def finish(state: str, code: str, http=None) -> tuple[UUID, dict]:
    """The callback: code -> tokens -> a TikTok channel account (or the same one renewed). -> (business id, account)."""
    from . import instance as I
    with db.tx() as conn:
        key = f"tiktok:{state}"[:60]
        st = I.kv_get(conn, key) if state else None
        if st:
            conn.execute(delete(db.instance_kv).where(db.instance_kv.c.key == key))     # each state works once
    if not st or time.time() - st.get("at", 0) > STATE_MINUTES * 60 or not code:
        raise TikTokError("that TikTok sign-in expired; try again")
    app_id, secret = app()
    h = _http(http)
    try:
        d = _data(h.post(f"{API}/tt_user/oauth2/token/", json={"client_id": app_id, "client_secret": secret,
                                                               "grant_type": "authorization_code", "auth_code": code,
                                                               "redirect_uri": redirect_uri()}))
    except (ValueError, httpx.HTTPError) as e:
        raise TikTokError(str(e) if isinstance(e, ValueError) else "could not reach TikTok; try again") from None
    if not d.get("access_token") or not d.get("open_id"):
        raise TikTokError("TikTok did not give access — use a TikTok Business Account and allow messages")
    scopes = set(str(d.get("scope") or "").replace(" ", "").split(","))
    if not {"message.list.read", "message.list.send"} <= scopes:
        raise TikTokError("allow Valeria One to read and send your TikTok messages, then connect again")
    cfg = {"via": "tiktok_app", "business_id": str(d["open_id"]), **tokens_from(d)}
    try:
        info = _data(h.get(f"{API}/business/get/", headers={"Access-Token": d["access_token"]},
                           params={"business_id": cfg["business_id"], "fields": json.dumps(["username", "display_name"])}))
        cfg["username"] = str(info.get("username") or "")[:100]
        name = info.get("display_name") or info.get("username")
    except (ValueError, httpx.HTTPError):
        name = None
    bid = UUID(st["b"])
    actor = f"user:{st['u']}" if st.get("u") else "tiktok"
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
        _check(biz)
        acc = _save(conn, biz, actor, cfg, ("@" + cfg["username"]) if cfg.get("username") else (name or "TikTok"))
    return bid, acc


def _save(conn: Connection, biz: dict, actor: str, cfg: dict, name: str) -> dict:
    row = conn.execute(select(A).where(A.c.channel == "tiktok", A.c.active,
                                       A.c.config["business_id"].astext == cfg["business_id"])).mappings().first()
    if row and row["business_id"] != biz["id"]:
        raise TikTokError("this TikTok account is already connected to another business")
    if row:                                                    # connected again: same inbox, fresh tokens
        upd = conn.execute(A.update().where(A.c.id == row["id"]).values(config=crypto.seal(cfg), name=name[:100])
                           .returning(A)).mappings().one()
        log(conn, biz["id"], "channel.reconnected", actor, row["id"], {"channel": "tiktok"})
        return X.public_account(upd)
    row = conn.execute(insert(A).values(business_id=biz["id"], channel="tiktok", name=name[:100], address=cfg["business_id"],
                                        config=crypto.seal(cfg), hook_id=secrets.token_urlsafe(18)).returning(A)).mappings().one()
    log(conn, biz["id"], "channel.connected", actor, row["id"], {"channel": "tiktok", "via": "tiktok"})
    return X.public_account(row)


def account(conn: Connection, business_id: UUID) -> dict | None:
    r = conn.execute(select(A).where(A.c.business_id == business_id, A.c.channel == "tiktok", A.c.active)
                     .order_by(A.c.created_at)).mappings().first()
    if not r:
        return None
    cfg = r["config"] or {}
    return {"id": r["id"], "name": r["name"], "error": cfg.get("error"), "expires_at": cfg.get("expires_at"),
            "refresh_expires_at": cfg.get("refresh_expires_at")}


# ---------------------------------------------------------------- the one webhook for every business
def receive(req: HookRequest) -> dict:
    """Verify TikTok's signature once, then hand the message to the business whose account it is."""
    if not configured():
        raise BadSignature("not configured")
    check_signature(req)
    try:
        event = json.loads(req.body or b"{}")
    except ValueError:
        return {"routed": 0, "unknown": 0}
    stats = {"routed": 0, "unknown": 0, "ai": []}
    events = event if isinstance(event, list) else [event]
    for ev in events:
        if not isinstance(ev, dict):
            continue
        who = str(ev.get("user_openid") or "")
        with db.tx() as conn:
            acc = conn.execute(select(A).where(A.c.channel == "tiktok", A.c.active,
                                               A.c.config["business_id"].astext == who)).mappings().first() if who else None
        if not acc:
            stats["unknown"] += 1
            continue
        items = channels.adapter("tiktok").items(crypto.open_(acc["config"]), ev)
        got = X.receive_items(acc, items)
        stats["ai"].extend((acc["business_id"], cid) for cid in got.get("conversation_ids", []))
        stats["routed"] += 1
    return stats


# ---------------------------------------------------------------- the job: tokens renewed before they run out
def refresh_due(http=None, now: float | None = None) -> dict:
    """Renew every TikTok token with less than 2 hours left (so a quiet account never loses its 30-day refresh token).
    A token that can't be renewed is marked, and the screens say 'reconnect TikTok'. Safe to run any time."""
    if not configured():
        return {"accounts": 0}
    now = now or time.time()
    stats = {"accounts": 0, "renewed": 0, "failed": 0}
    with db.tx() as conn:
        rows = conn.execute(select(A).where(A.c.channel == "tiktok", A.c.active)).mappings().all()
    ad = channels.adapter("tiktok", http)
    for r in rows:
        cfg = crypto.open_(r["config"])
        stats["accounts"] += 1
        if int(cfg.get("expires_at") or 0) - now > REFRESH_AHEAD:
            continue
        try:
            fresh = ad.refresh({**cfg, "expires_at": 0})            # force: renew now, while the refresh token is alive
        except ValueError as e:
            if not cfg.get("error"):                              # told once; the next rounds keep trying quietly
                X.save_config(r["id"], {**cfg, "error": str(e)[:200]})
                with db.tx() as conn:
                    log(conn, r["business_id"], "tiktok.reconnect", "system", r["id"])
            stats["failed"] += 1
            continue
        if fresh:
            X.save_config(r["id"], fresh)
            stats["renewed"] += 1
    return stats
