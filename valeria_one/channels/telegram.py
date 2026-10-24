"""Telegram Bot API. Config: bot_token, secret (set as secret_token when registering the webhook,
Telegram echoes it in X-Telegram-Bot-Api-Secret-Token)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..crypto import same
from . import Adapter, BadSignature, Inbound, Outbound, SendResult, register


@register
class Telegram(Adapter):
    channel = "telegram"
    address_kind = "id"
    needs = ("bot_token", "secret")

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
