"""Sandbox channel: nothing leaves the server. Every send succeeds and is marked delivered, so anyone can
try Valeria One end to end without a phone number, a Meta app or a Twilio account."""
from __future__ import annotations

import json
import uuid

from . import Adapter, Inbound, Outbound, SendResult, register


@register
class Sandbox(Adapter):
    channel = "sandbox"
    address_kind = "id"
    reply_window_hours = 24          # behaves like WhatsApp, so the reply-window rule can be tried safely

    def send(self, cfg, address, out: Outbound) -> SendResult:
        return SendResult("delivered", provider_id=f"sbx_{uuid.uuid4().hex[:16]}")

    def parse(self, cfg, req):
        d = json.loads(req.body or b"{}")
        return [Inbound(address=str(d.get("from", "")), body=d.get("body", ""), name=d.get("name"),
                        provider_id=d.get("id") or f"sbx_in_{uuid.uuid4().hex[:16]}")]
