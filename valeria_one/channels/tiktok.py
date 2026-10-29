"""TikTok direct messages (TikTok Business Messaging API, business-api.tiktok.com/open_api/v1.3).

The business connects its OWN TikTok Business Account (linked to TikTok for Business) with TikTok's sign-in, from
Connect › TikTok (services/tiktok.py); it can't be connected by pasting settings. Config per channel account:
  business_id (the account's open_id), access_token (24 h), refresh_token (30 days; kept encrypted), expires_at,
  refresh_expires_at (unix seconds), username.

TikTok's rules: the customer writes first, and the business may answer within 48 hours of the customer's last
message — there are no templates to write outside that window (inbox._check_rules blocks it, with a clear reason).
A contact's address here is the TikTok conversation id (what /business/message/send/ answers to).

Messages arrive at the app's one webhook, /hooks/tiktok (services/tiktok.receive), signed by TikTok.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx

from .. import config
from ..crypto import hmac_sha256_hex, same
from . import Adapter, BadSignature, HookRequest, Inbound, Outbound, SendResult, register

API = "https://business-api.tiktok.com/open_api/v1.3"
REFRESH_BEFORE = 300                     # renew the access token when it has less than 5 minutes left
SIGNATURE_TOLERANCE = 300                # seconds between TikTok's timestamp and ours (replays older than this are refused)


def app() -> tuple[str | None, str | None]:
    return config.get("VO_TIKTOK_APP_ID"), config.get("VO_TIKTOK_APP_SECRET")


def check_signature(req: HookRequest, now: float | None = None) -> None:
    """TikTok-Signature: t=<unix seconds>,s=<hex HMAC-SHA256 of "<t>.<raw body>" keyed with the app secret>.

    As TikTok documents it for its webhooks (developers.tiktok.com/doc/webhooks-verification); the Business
    Messaging webhook uses the same scheme as far as is known. Kept in this one function so it is easy to adjust."""
    secret = app()[1]
    if not secret:
        raise BadSignature("not configured")
    parts = dict(p.split("=", 1) for p in req.headers.get("tiktok-signature", "").split(",") if "=" in p)
    ts, sig = parts.get("t", ""), parts.get("s", "")
    if not ts.isdigit() or not sig:
        raise BadSignature("missing signature")
    if abs((now or time.time()) - int(ts)) > SIGNATURE_TOLERANCE:
        raise BadSignature("stale signature")
    if not same(sig, hmac_sha256_hex(secret, ts.encode() + b"." + req.body)):
        raise BadSignature("bad signature")


def _data(r: httpx.Response) -> dict:
    """TikTok answers {"code": 0, "message": "OK", "data": {...}}; anything else is an error in its words."""
    try:
        j = r.json()
    except ValueError:
        raise ValueError(f"TikTok answered {r.status_code}") from None
    if r.status_code >= 300 or j.get("code") not in (0, None):
        raise ValueError(f"TikTok: {j.get('message') or r.status_code}")
    return j.get("data") or {}


def tokens_from(data: dict, now: float | None = None) -> dict:
    """TikTok's token answer -> the fields we keep (expiry as unix seconds)."""
    now = now or time.time()
    out = {"access_token": data["access_token"], "expires_at": int(now + int(data.get("expires_in") or 86400))}
    if data.get("refresh_token"):
        out["refresh_token"] = data["refresh_token"]
        out["refresh_expires_at"] = int(now + int(data.get("refresh_token_expires_in") or 30 * 86400))
    return out


@register
class TikTok(Adapter):
    channel = "tiktok"
    reply_window_hours = 48
    templates = False
    manual = False
    address_kind = "id"
    needs = ("business_id", "access_token")

    def refresh(self, cfg):
        if int(cfg.get("expires_at") or 0) - time.time() > REFRESH_BEFORE:
            return None
        app_id, secret = app()
        if not (app_id and secret and cfg.get("refresh_token")):
            raise ValueError("reconnect TikTok (Connect › TikTok)")
        if int(cfg.get("refresh_expires_at") or 0) and int(cfg["refresh_expires_at"]) < time.time():
            raise ValueError("TikTok's access ended after 30 days: reconnect TikTok (Connect › TikTok)")
        try:
            d = _data(self.http.post(f"{API}/tt_user/oauth2/refresh_token/", json={
                "client_id": app_id, "client_secret": secret, "grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]}))
        except httpx.HTTPError:
            raise ValueError("could not reach TikTok; try again in a minute") from None
        if not d.get("access_token"):
            raise ValueError("reconnect TikTok (Connect › TikTok)")
        return {**cfg, **tokens_from(d), "error": None}

    def send(self, cfg, address, out: Outbound) -> SendResult:
        body = out.body
        if out.media:                        # pictures need TikTok's own upload; a link to the file goes instead
            body = (body + "\n" + "\n".join(m["url"] for m in out.media if m.get("url"))).strip()
        r = self.http.post(f"{API}/business/message/send/", headers={"Access-Token": cfg["access_token"]},
                           json={"business_id": cfg["business_id"], "recipient_type": "CONVERSATION", "recipient": address,
                                 "message_type": "TEXT", "text": {"body": body[:6000]}})
        try:
            d = _data(r)
        except ValueError as e:
            return SendResult("failed", error=str(e)[:200])
        mid = d.get("message_id") or (d.get("message") or {}).get("message_id")
        return SendResult("sent", provider_id=str(mid) if mid else None)

    def parse(self, cfg, req):
        """Also usable at a per-account address (/hooks/<id>); the app-level /hooks/tiktok is the normal way in."""
        check_signature(req)
        return self.items(cfg, json.loads(req.body or b"{}"))

    def items(self, cfg, event: dict) -> list:
        """Customers' messages to THIS account from one verified event. The business's own messages (im_send_msg,
        sent from the TikTok app) and read receipts are not new messages here."""
        if event.get("event") != "im_receive_msg":
            return []
        try:
            c = json.loads(event.get("content") or "{}") if isinstance(event.get("content"), str) else (event.get("content") or {})
        except ValueError:
            return []
        me = str(cfg.get("business_id") or "")
        if str((c.get("to_user") or {}).get("id") or me) != me or str((c.get("from_user") or {}).get("id") or "") == me:
            return []
        conv = c.get("conversation_id")
        if not conv:
            return []
        kind = c.get("type") or "text"
        body = (c.get("text") or {}).get("body") or ""
        media = []
        if kind == "image":
            media = [{"type": "image", "provider_media_id": (c.get("image") or {}).get("media_id")}]
        elif kind == "share_post":
            body = body or (c.get("share_post") or {}).get("embed_url") or ""
        ts = c.get("timestamp")
        at = datetime.fromtimestamp(int(ts) / 1000 if int(ts) > 10**11 else int(ts), tz=timezone.utc) if str(ts or "").isdigit() else None
        return [Inbound(address=str(conv), body=body, provider_id=str(c["message_id"]) if c.get("message_id") else None,
                        name=c.get("from") or None, media=media, at=at)]
