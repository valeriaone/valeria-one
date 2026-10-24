"""Email from the business's own mailbox (SMTP). Config: smtp_host, smtp_port, username, password, from_email.
Inbound mail arrives as a signed JSON POST (any mail-forwarding service): {"from", "name", "subject", "text", "message_id"}
with header X-VO-Signature = hex HMAC-SHA256 of the body using the account's `secret`."""
from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from ..crypto import hmac_sha256_hex, same
from . import Adapter, BadSignature, Inbound, Outbound, SendResult, register


@register
class Email(Adapter):
    channel = "email"
    address_kind = "email"
    needs = ("smtp_host", "username", "password", "from_email", "secret")
    smtp_factory = smtplib.SMTP                     # tests swap this

    def send(self, cfg, address, out: Outbound) -> SendResult:
        m = EmailMessage()
        m["From"] = f'{cfg.get("from_name", "")} <{cfg["from_email"]}>'.strip()
        m["To"] = address
        m["Subject"] = (out.template or {}).get("subject") or cfg.get("default_subject") or out.body.split("\n", 1)[0][:78]
        mid = make_msgid(domain=cfg["from_email"].split("@")[-1])
        m["Message-ID"] = mid
        if cfg.get("unsubscribe_url"):
            m["List-Unsubscribe"] = f"<{cfg['unsubscribe_url']}>"
        m.set_content(out.body)
        try:
            port = int(cfg.get("smtp_port") or 587)
            with self.smtp_factory(cfg["smtp_host"], port, timeout=20) as s:
                if port != 25:
                    s.starttls(context=ssl.create_default_context())
                s.login(cfg["username"], cfg["password"])
                s.send_message(m)
        except (OSError, smtplib.SMTPException) as e:
            return SendResult("failed", error=f"{type(e).__name__}: {e}"[:300])
        return SendResult("sent", provider_id=mid)

    def parse(self, cfg, req):
        if not same(req.headers.get("x-vo-signature", ""), hmac_sha256_hex(cfg["secret"], req.body)):
            raise BadSignature("bad signature")
        d = json.loads(req.body or b"{}")
        body = d.get("text", "")
        if d.get("subject"):
            body = f"{d['subject']}\n\n{body}".strip()
        return [Inbound(address=(d.get("from") or "").strip().lower(), body=body, name=d.get("name"),
                        provider_id=d.get("message_id"))]
