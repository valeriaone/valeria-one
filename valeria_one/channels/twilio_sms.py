"""SMS through the business's own Twilio account. Config: account_sid, auth_token, and from_number
(E.164) or messaging_service_sid. Other SMS providers (Vonage, MessageBird, a local telco) = another file like this."""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

from ..crypto import same
from . import Adapter, BadSignature, HookRequest, Inbound, Outbound, SendResult, StatusUpdate, register

API = "https://api.twilio.com/2010-04-01"
_STATUS = {"queued": "queued", "accepted": "queued", "sending": "queued", "sent": "sent", "delivered": "delivered",
           "read": "read", "undelivered": "failed", "failed": "failed"}


def signature(token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@register
class TwilioSMS(Adapter):
    channel = "sms"
    address_kind = "phone"
    needs = ("account_sid", "auth_token")

    def check_config(self, cfg):
        missing = super().check_config(cfg)
        if not (cfg.get("from_number") or cfg.get("messaging_service_sid")):
            missing.append("from_number or messaging_service_sid")
        return missing

    def send(self, cfg, address, out: Outbound) -> SendResult:
        form = {"To": address, "Body": out.body}
        if cfg.get("messaging_service_sid"):
            form["MessagingServiceSid"] = cfg["messaging_service_sid"]
        else:
            form["From"] = cfg["from_number"]
        if out.media:
            form["MediaUrl"] = out.media[0]["url"]
        if cfg.get("status_callback"):
            form["StatusCallback"] = cfg["status_callback"]
        r = self.http.post(f"{API}/Accounts/{cfg['account_sid']}/Messages.json", data=form,
                           auth=(cfg["account_sid"], cfg["auth_token"]))
        if r.status_code >= 300:
            try:
                j = r.json()
                return SendResult("failed", error=f"{j.get('code')} {j.get('message')}")
            except ValueError:
                return SendResult("failed", error=f"{r.status_code}")
        j = r.json()
        return SendResult(_STATUS.get(j.get("status"), "queued"), provider_id=j.get("sid"))

    def parse(self, cfg, req: HookRequest):
        params = req.form if req.form is not None else dict(parse_qsl(req.body.decode()))
        if not same(req.headers.get("x-twilio-signature", ""), signature(cfg["auth_token"], req.url, params)):
            raise BadSignature("bad Twilio signature")
        if params.get("MessageStatus") and not params.get("Body"):
            return [StatusUpdate(provider_id=params.get("MessageSid"), status=_STATUS.get(params["MessageStatus"], "sent"),
                                 error=params.get("ErrorCode"))]
        media = [{"type": params.get(f"MediaContentType{i}", "file"), "url": params.get(f"MediaUrl{i}")}
                 for i in range(int(params.get("NumMedia") or 0))]
        return [Inbound(address=params.get("From", ""), body=params.get("Body", ""),
                        provider_id=params.get("MessageSid"), media=media)]
