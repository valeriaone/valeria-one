"""WhatsApp through the business's own Twilio account. Twilio is a Meta Business Solution Provider, so this
works without a Meta app of our own: the business registers its WhatsApp sender in Twilio, then connects here.
Config: account_sid, auth_token, and from_number (the WhatsApp-enabled number, E.164) or messaging_service_sid.
Outside the 24-hour window only an approved template goes out: the template name is its Twilio Content SID (HX…)."""
from __future__ import annotations

import json

from . import register
from .twilio_sms import TwilioSMS


def _bare(v: str) -> str:
    return (v or "").removeprefix("whatsapp:")


@register
class TwilioWhatsApp(TwilioSMS):
    channel = "whatsapp_twilio"
    reply_window_hours = 24

    def register_hook(self, cfg, webhook_url):
        return None                                           # WhatsApp senders' webhook is set on the sender in Twilio

    def _form(self, cfg, address, out):
        form = {"To": f"whatsapp:{_bare(address)}"}
        if cfg.get("messaging_service_sid"):
            form["MessagingServiceSid"] = cfg["messaging_service_sid"]
        else:
            form["From"] = f"whatsapp:{_bare(cfg['from_number'])}"
        if out.template:
            form["ContentSid"] = out.template["name"]
            if out.template.get("params"):
                form["ContentVariables"] = json.dumps({str(i + 1): str(v) for i, v in enumerate(out.template["params"])})
        else:
            form["Body"] = out.body
            if out.media:
                form["MediaUrl"] = out.media[0]["url"]
        if cfg.get("status_callback"):
            form["StatusCallback"] = cfg["status_callback"]
        return form

    def parse(self, cfg, req):
        items = super().parse(cfg, req)
        params = req.form if req.form is not None else {}
        for it in items:
            if hasattr(it, "address"):
                it.address = _bare(it.address)
                it.name = it.name or params.get("ProfileName") or None
        return items
