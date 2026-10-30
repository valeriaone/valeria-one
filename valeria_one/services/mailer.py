"""The server's own email (password resets, billing notices, invitations), through any SMTP account:
VO_SMTP_HOST, VO_SMTP_PORT (587), VO_SMTP_USER, VO_SMTP_PASSWORD, VO_SMTP_FROM, VO_SMTP_FROM_NAME.

Not configured = nothing is sent: the message is kept in OUTBOX (for tests and development) and logged,
and the screens fall back to showing links instead. A mail problem never breaks a page.
Businesses' own email to their customers is a channel (channels/email_smtp.py), not this.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .. import config

log = logging.getLogger("valeria_one.mail")
OUTBOX: list[dict] = []                       # what would have gone out, when no SMTP is set (tests read it)


def configured() -> bool:
    return bool(config.get("VO_SMTP_HOST")) and config.get("VO_SMTP_HOST") != "off"


def send(to: str, subject: str, body: str, reply_to: str | None = None, from_name: str | None = None) -> bool:
    """True when the SMTP server accepted it. from_name: whose name shows (a business writing to its customer)."""
    item = {"to": to, "subject": subject, "body": body, "reply_to": reply_to, "from_name": from_name}
    if not configured():
        OUTBOX.append(item)
        del OUTBOX[:-200]
        log.info("mail not configured; kept for %s: %s", to, subject)
        return False
    msg = EmailMessage()
    sender = config.get("VO_SMTP_FROM") or config.get("VO_SMTP_USER")
    name = (from_name or config.get('VO_SMTP_FROM_NAME') or 'Valeria One').replace('"', "'").replace("\n", " ")[:80]
    msg["From"] = f'"{name}" <{sender}>'
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate()
    msg["Message-ID"] = make_msgid(domain=(sender or "localhost").split("@")[-1])
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(config.get("VO_SMTP_HOST"), int(config.get("VO_SMTP_PORT") or 587), timeout=20) as s:
            s.starttls()
            if config.get("VO_SMTP_USER"):
                s.login(config.get("VO_SMTP_USER"), config.get("VO_SMTP_PASSWORD") or "")
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        log.exception("could not send mail to %s", to)
        return False
