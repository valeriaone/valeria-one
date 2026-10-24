"""Telegram Bot API. Config: bot_token (from @BotFather). On connect we generate `secret` and register our
webhook with Telegram ourselves (setWebhook, secret_token = secret); Telegram echoes the secret in
X-Telegram-Bot-Api-Secret-Token, which is how we know a message really came from Telegram."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

import httpx

from ..crypto import same
from . import Adapter, BadSignature, Inbound, Outbound, SendResult, register


@register
class Telegram(Adapter):
    channel = "telegram"
    address_kind = "id"
    needs = ("bot_token",)

    def prepare(self, cfg):
        return {**cfg, "secret": cfg.get("secret") or secrets.token_urlsafe(24)}

    def register_hook(self, cfg, webhook_url):
        if not webhook_url.startswith("https://"):
            raise ValueError("Telegram only delivers to an HTTPS address: give this server a domain first "
                             "(valeria-one domain example.com), then connect again")
        try:
            r = self.http.post(f"https://api.telegram.org/bot{cfg['bot_token']}/setWebhook",
                               json={"url": webhook_url, "secret_token": cfg["secret"], "allowed_updates": ["message"]})
            j = r.json()
        except (httpx.HTTPError, ValueError):
            raise ValueError("could not reach Telegram; try again in a minute") from None
        if not j.get("ok"):
            raise ValueError(f"Telegram said: {j.get('description') or r.status_code} (check the bot token)")

    def unregister_hook(self, cfg):
        try:
            self.http.post(f"https://api.telegram.org/bot{cfg['bot_token']}/deleteWebhook", timeout=10)
        except Exception:                                      # noqa: BLE001 — leaving is never blocked
            pass

    def send(self, cfg, address, out: Outbound) -> SendResult:
        base = f"https://api.telegram.org/bot{cfg['bot_token']}"
        if out.media:
            r = self.http.post(f"{base}/sendDocument", json={"chat_id": address, "document": out.media[0]["url"], "caption": out.body})
        else:
            r = self.http.post(f"{base}/sendMessage", json={"chat_id": address, "text": out.body})
        try:
            j = r.json()
        except ValueError:
            return SendResult("failed", error=f"{r.status_code}")
        if not j.get("ok"):
            return SendResult("failed", error=str(j.get("description"))[:200])
        return SendResult("sent", provider_id=str(j["result"]["message_id"]))

    def parse(self, cfg, req):
        if not same(req.headers.get("x-telegram-bot-api-secret-token", ""), cfg["secret"]):
            raise BadSignature("bad secret token")
        u = json.loads(req.body or b"{}")
        m = u.get("message")
        if not m or not m.get("chat"):
            return []
        who = m.get("from") or {}
        name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or None
        return [Inbound(address=str(m["chat"]["id"]), body=m.get("text") or m.get("caption") or "",
                        provider_id=f"{m['chat']['id']}:{m['message_id']}", name=name,
                        at=datetime.fromtimestamp(m.get("date", 0), tz=timezone.utc) if m.get("date") else None)]
