"""SMS through the business's own Twilio account. Config: account_sid, auth_token, and from_number
(E.164) or messaging_service_sid; optional forward_to / text_back / missed_text for missed-call text-back
(services/calls.py). On connect we point the number's messages and calls at Valeria One ourselves.
Other SMS providers (Vonage, MessageBird, a local telco) = another file like this."""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

import httpx

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

    def register_hook(self, cfg, webhook_url):
        """Point the Twilio number's incoming texts and calls at Valeria One (no console work for the business)."""
        if not cfg.get("from_number"):
            return None                                       # a messaging service: its webhook is set in Twilio
        auth = (cfg["account_sid"], cfg["auth_token"])
        base = f"{API}/Accounts/{cfg['account_sid']}/IncomingPhoneNumbers"
        try:
            r = self.http.get(f"{base}.json", params={"PhoneNumber": cfg["from_number"]}, auth=auth)
            j = r.json()
        except (httpx.HTTPError, ValueError):
            raise ValueError("could not reach Twilio; try again in a minute") from None
        if r.status_code >= 300:
            raise ValueError(f"Twilio said: {j.get('message') or r.status_code} (check the Account SID and Auth Token)")
        nums = j.get("incoming_phone_numbers") or []
        if not nums:
            raise ValueError(f"{cfg['from_number']} is not a number in this Twilio account")
        try:
            r = self.http.post(f"{base}/{nums[0]['sid']}.json", auth=auth, data={
                "SmsUrl": webhook_url, "SmsMethod": "POST", "VoiceUrl": webhook_url + "/voice", "VoiceMethod": "POST"})
        except httpx.HTTPError:
            raise ValueError("could not reach Twilio; try again in a minute") from None
        if r.status_code >= 300:
            raise ValueError("Twilio did not let us set the number's webhooks")
        return None

    def _form(self, cfg, address, out: Outbound) -> dict:
        form = {"To": address, "Body": out.body}
        if cfg.get("messaging_service_sid"):
            form["MessagingServiceSid"] = cfg["messaging_service_sid"]
        else:
            form["From"] = cfg["from_number"]
        if out.media:
            form["MediaUrl"] = out.media[0]["url"]
        if cfg.get("status_callback"):
            form["StatusCallback"] = cfg["status_callback"]
        return form

    def send(self, cfg, address, out: Outbound) -> SendResult:
        form = self._form(cfg, address, out)
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

    def download(self, cfg, media):
        """MMS / WhatsApp-through-Twilio media: the URL Twilio gave, with the account's own credentials."""
        if not media.get("url"):
            raise ValueError("no media url")
        r = self.http.get(media["url"], auth=(cfg["account_sid"], cfg["auth_token"]), follow_redirects=True)
        if r.status_code >= 300:
            raise ValueError(f"{r.status_code}")
        return r.content, media.get("type") if "/" in str(media.get("type") or "") else r.headers.get("content-type", "")

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
