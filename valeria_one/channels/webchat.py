"""Messages left on the business's website (contact form / web chat). They arrive in the inbox like any
other; the business answers on a channel the person gave (WhatsApp, SMS, email)."""
from __future__ import annotations

from . import Adapter, Outbound, SendResult, register


@register
class WebChat(Adapter):
    channel = "webchat"
    address_kind = "id"

    def send(self, cfg, address, out: Outbound) -> SendResult:
        return SendResult("failed", error="this person wrote from your website — answer them by WhatsApp, SMS or email")

    def parse(self, cfg, req):
        return []                                  # only our own website form writes here, never an outside webhook
