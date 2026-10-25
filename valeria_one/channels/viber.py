"""Viber (REST Bot API, https://developers.viber.com/docs/api/rest-bot-api/). Config: auth_token (the business's
own Viber bot token, from partners.viber.com) and sender_name (shown on its messages). On connect we register our
webhook with Viber ourselves (set_webhook). Viber signs every callback: X-Viber-Content-Signature is the
HMAC-SHA256 of the raw body keyed with the bot's token. A bot can write to people who wrote to it or subscribed;
whatever Viber charges for business messages is billed to the business's own Viber account."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx

from ..crypto import same
from . import Adapter, BadSignature, Inbound, Outbound, SendResult, StatusUpdate, register

API = "https://chatapi.viber.com/pa"
_STATUS = {"delivered": "delivered", "seen": "read", "failed": "failed"}


@register
class Viber(Adapter):
    channel = "viber"
    address_kind = "id"
    needs = ("auth_token", "sender_name")

    def _post(self, cfg, path, body) -> dict:
        r = self.http.post(f"{API}/{path}", json=body, headers={"X-Viber-Auth-Token": cfg["auth_token"]})
        try:
            return r.json()
        except ValueError:
            return {"status": -1, "status_message": f"HTTP {r.status_code}"}

    def register_hook(self, cfg, webhook_url):
        if not webhook_url.startswith("https://"):
            raise ValueError("Viber only delivers to an HTTPS address: give this server a domain first "
                             "(valeria-one domain example.com), then connect again")
        try:
            j = self._post(cfg, "set_webhook", {"url": webhook_url, "send_name": True, "send_photo": False,
                                                 "event_types": ["delivered", "seen", "failed", "conversation_started"]})
        except httpx.HTTPError:
            raise ValueError("could not reach Viber; try again in a minute") from None
        if j.get("status") != 0:
            raise ValueError(f"Viber said: {j.get('status_message') or j.get('status')} (check the bot token)")

    def unregister_hook(self, cfg):
        try:
            self._post(cfg, "set_webhook", {"url": ""})
        except Exception:                                      # noqa: BLE001 — leaving is never blocked
            pass

    def send(self, cfg, address, out: Outbound) -> SendResult:
        msg = {"receiver": address, "min_api_version": 1, "sender": {"name": (cfg.get("sender_name") or "")[:28]}}
        if out.media:
            msg.update(type="url", media=out.media[0]["url"])
            if out.body:                                       # the words first, then the file's link
                self.send(cfg, address, Outbound(to=address, body=out.body))
        else:
            msg.update(type="text", text=out.body)
        try:
            j = self._post(cfg, "send_message", msg)
        except httpx.HTTPError as e:
            return SendResult("failed", error=type(e).__name__)
        if j.get("status") != 0:
            return SendResult("failed", error=str(j.get("status_message") or j.get("status"))[:200])
        return SendResult("sent", provider_id=str(j.get("message_token")))

    def parse(self, cfg, req):
        want = hmac.new(cfg["auth_token"].encode(), req.body or b"", hashlib.sha256).hexdigest()
        if not same(req.headers.get("x-viber-content-signature", ""), want):
            raise BadSignature("bad signature")
        e = json.loads(req.body or b"{}")
        ev = e.get("event")
        if ev in _STATUS and e.get("message_token") is not None:
            return [StatusUpdate(provider_id=str(e["message_token"]), status=_STATUS[ev], error=e.get("desc"))]
        if ev != "message":
            return []                                          # webhook check, subscribed, conversation_started…
        who, m = e.get("sender") or {}, e.get("message") or {}
        media = [{"type": m.get("type"), "url": m["media"]}] if m.get("media") else []
        ts = e.get("timestamp")
        return [Inbound(address=str(who.get("id")), body=m.get("text") or "", provider_id=str(e.get("message_token")),
                        name=who.get("name"), media=media, at=datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None)]
