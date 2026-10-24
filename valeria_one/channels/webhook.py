"""Generic webhook channel: plug in ANY messaging system (a local telco gateway, Viber, a chat widget, Zapier/Make).

Outbound: we POST {"to", "body", "media", "template"} to the business's `url`, signed with X-VO-Signature
(hex HMAC-SHA256 of the body with `secret`). Their reply may include {"id": "<provider id>"}.
Inbound: they POST {"from", "body", "id", "name", "media"} (or a list of them) signed the same way."""
from __future__ import annotations

import json

from ..crypto import hmac_sha256_hex, same
from . import Adapter, BadSignature, Inbound, Outbound, SendResult, StatusUpdate, register


@register
class Webhook(Adapter):
    channel = "webhook"
    address_kind = "id"
    needs = ("url", "secret")

    def send(self, cfg, address, out: Outbound) -> SendResult:
        body = json.dumps({"to": address, "body": out.body, "media": out.media, "template": out.template}).encode()
        try:
            r = self.http.post(cfg["url"], content=body, headers={"Content-Type": "application/json",
                                                                  "X-VO-Signature": hmac_sha256_hex(cfg["secret"], body)})
        except Exception as e:  # network errors of any kind -> a failed send, never a crash
            return SendResult("failed", error=f"{type(e).__name__}")
        if r.status_code >= 300:
            return SendResult("failed", error=f"{r.status_code}")
        try:
            pid = r.json().get("id")
        except ValueError:
            pid = None
        return SendResult("sent", provider_id=pid)

    def parse(self, cfg, req):
        if not same(req.headers.get("x-vo-signature", ""), hmac_sha256_hex(cfg["secret"], req.body)):
            raise BadSignature("bad signature")
        d = json.loads(req.body or b"{}")
        items = d if isinstance(d, list) else [d]
        out: list = []
        for i in items:
            if i.get("status") and i.get("id") and not i.get("from"):
                out.append(StatusUpdate(provider_id=i["id"], status=i["status"], error=i.get("error")))
            else:
                out.append(Inbound(address=str(i.get("from", "")), body=i.get("body", ""), provider_id=i.get("id"),
                                   name=i.get("name"), media=i.get("media") or []))
        return out
