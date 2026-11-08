"""Missed-call text-back, on the business's own Twilio number (the SMS channel).

When the number is connected, Valeria One points its calls here (channels/twilio_sms.register_hook). A call
rings the business's real phone (`forward_to`) for 20 seconds; if nobody answers — or there is no phone to
ring — the caller gets a text from the business number at once, and the conversation lands in the inbox
like any other. One text per caller per 30 minutes, so a caller trying again isn't flooded.

Config on the SMS channel: forward_to (optional, any format), text_back (on unless "off"), missed_text
(optional; else the default in the business's language, with {business}).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy import select

from .. import config, crypto, db, packs, phones
from ..channels import BadSignature, HookRequest
from ..channels.twilio_sms import signature
from . import Blocked, NotFound, log

logger = logging.getLogger("valeria_one.calls")
RING_SECONDS = 20
QUIET_MINUTES = 30
MISSED = ("no-answer", "busy", "failed", "canceled")
VM_SECONDS = 120                                              # close01: a voicemail up to 2 minutes
_VOICE = {"es": "es-MX", "en": "en-US", "pt": "pt-BR", "fr": "fr-FR", "de": "de-DE", "ja": "ja-JP", "hi": "hi-IN",
          "id": "id-ID", "zh": "cmn-CN", "ar": "arb", "fil": "fil-PH"}


def _params(req: HookRequest) -> dict:
    from urllib.parse import parse_qsl
    return req.form if req.form is not None else dict(parse_qsl((req.body or b"").decode()))


def _verified(acc, req: HookRequest) -> tuple[dict, dict]:
    cfg = crypto.open_(acc["config"])
    p = _params(req)
    if not crypto.same(req.headers.get("x-twilio-signature", ""), signature(cfg.get("auth_token", ""), req.url, p)):
        raise BadSignature("bad Twilio signature")
    return cfg, p


def _biz(business_id) -> dict:
    with db.tx() as conn:
        return dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())


def _twiml(inner: str = "") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'


def voice(acc, req: HookRequest) -> str:
    """Twilio asks what to do with a call -> TwiML."""
    from ..web.i18n import t, ui_lang
    cfg, p = _verified(acc, req)
    biz = _biz(acc["business_id"])
    from . import voice_agent as VA
    with db.tx() as conn:
        agent_on = VA.active(conn, biz, acc)
    if agent_on:                                             # v0.18: the phone agent answers (the business's own number + AI key)
        return VA.answer(acc, cfg, biz, p, req.url)
    caller = p.get("From") or ""
    fwd = _number(cfg.get("forward_to"), biz)
    if fwd and not biz.get("suspended_at"):
        done = req.url.split("?")[0] + "/done"
        return _twiml(f'<Dial timeout="{RING_SECONDS}" action={quoteattr(done)} method="POST" callerId={quoteattr(caller)}>'
                      f'<Number>{escape(fwd)}</Number></Dial>')
    sent = missed(acc, cfg, biz, caller)
    lang = ui_lang(biz["language"])
    say = t("missed_call_say" if sent else "missed_call_say_plain", lang).format(business=biz["name"])
    if cfg.get("voicemail") != "off" and not biz.get("suspended_at"):        # close01: they can leave a message
        return _twiml(f'<Say language="{_VOICE.get(lang, "en-US")}">{escape(say)}</Say>' + _record(req.url, lang))
    return _twiml(f'<Say language="{_VOICE.get(lang, "en-US")}">{escape(say)}</Say><Hangup/>')


def _record(url: str, lang: str) -> str:
    """close01: «leave your message after the tone» — up to 2 minutes, then voicemail() files it in the inbox."""
    from ..web.i18n import t
    action = url.split("?")[0].removesuffix("/done") + "/voicemail"
    return (f'<Say language="{_VOICE.get(lang, "en-US")}">{escape(t("vm_prompt", lang))}</Say>'
            f'<Record maxLength="{VM_SECONDS}" playBeep="true" timeout="5" trim="trim-silence" action={quoteattr(action)} method="POST"/>'
            '<Hangup/>')


def voice_done(acc, req: HookRequest) -> str:
    """After the business's phone rang: if nobody picked up, text the caller."""
    cfg, p = _verified(acc, req)
    biz = _biz(acc["business_id"])
    status = (p.get("DialCallStatus") or "").lower()
    if status in MISSED:
        missed(acc, cfg, biz, p.get("From") or "")
        if cfg.get("voicemail") != "off" and not biz.get("suspended_at"):    # close01: nobody picked up — leave a message
            from ..web.i18n import ui_lang
            return _twiml(_record(req.url, ui_lang(biz["language"])))
    else:
        with db.tx() as conn:
            log(conn, biz["id"], "call.answered", "webhook:sms", acc["id"], {"status": status or "completed"})
    return _twiml()


def _number(raw: str | None, biz: dict) -> str | None:
    if not raw:
        return None
    try:
        return phones.e164(raw, packs.country(biz["country"]))
    except phones.BadPhone:
        return None


def missed(acc, cfg: dict, biz: dict, caller: str, now: datetime | None = None) -> bool:
    """Text the caller back (once per 30 minutes). True when a text went out."""
    from . import contacts as C
    from . import inbox as X
    from ..web.i18n import t, ui_lang
    now = now or datetime.now(timezone.utc)
    phone = _number(caller, biz)
    if not phone:
        return False                                            # a hidden or invalid number: nobody to text
    E = db.events
    with db.tx() as conn:
        contact, _ = C.find_or_create_by_identity(conn, biz, "sms", phone, None, "missed call")
        recent = conn.execute(select(E.c.id).where(E.c.business_id == biz["id"], E.c.type == "call.missed",
                                                   E.c.subject_id == contact["id"],
                                                   E.c.created_at > now - timedelta(minutes=QUIET_MINUTES))).first()
        log(conn, biz["id"], "call.missed", "webhook:sms", contact["id"], {"texted": not recent and cfg.get("text_back") != "off"})
    if recent or cfg.get("text_back") == "off" or contact.get("unsubscribed_at"):
        return False
    text = (cfg.get("missed_text") or t("missed_call_text", ui_lang(biz["language"]))).replace("{business}", biz["name"])
    try:
        m = X.send(biz, "missed_call", contact_id=contact["id"], body=text, channel_account_id=acc["id"], kind="service")
    except Blocked as b:
        logger.info("missed-call text not sent: %s", b.reason)
        return False
    return m.get("status") not in ("failed", "blocked")


def agent_turn(acc, req: HookRequest) -> str:
    """v0.18: the caller spoke to the phone agent (services/voice_agent.py)."""
    from . import voice_agent as VA
    cfg, p = _verified(acc, req)
    return VA.turn(acc, cfg, _biz(acc["business_id"]), p, req.url)


def agent_recording(acc, req: HookRequest) -> str:
    from . import voice_agent as VA
    _, p = _verified(acc, req)
    VA.recording(acc, _biz(acc["business_id"]), p)
    return _twiml()


def voicemail(acc, req: HookRequest, http=None) -> str:
    """close01: the caller left a message. It goes into the inbox as a message from them: how long, what they said (written
    down with the business's OWN AI key when it has one) and a private link to listen (the recording stays in the business's
    own Twilio account). Twilio may retry: the recording's id keeps it to one message."""
    import httpx
    from ..channels import Inbound
    from . import ai as AI
    from . import inbox as X
    from ..web.i18n import t, ui_lang
    cfg, p = _verified(acc, req)
    biz = _biz(acc["business_id"])
    lang = ui_lang(biz["language"])
    rec, sid = (p.get("RecordingUrl") or "").strip(), (p.get("RecordingSid") or "").strip()
    phone = _number(p.get("From") or "", biz)
    try:
        secs = int(float(p.get("RecordingDuration") or 0))
    except ValueError:
        secs = 0
    if not (rec and re.fullmatch(r"RE[0-9a-f]{32}", sid) and phone and secs >= 1):
        return _twiml("<Hangup/>")                                # silence / hung up at the tone: nothing to file
    words = ""
    try:
        with db.tx() as conn:
            own = AI.own_key(conn, biz)
        if own:
            cl = http or httpx.Client(timeout=30)
            r = cl.get(rec + ".mp3", auth=(cfg.get("account_sid", ""), cfg.get("auth_token", "")))
            if r.status_code < 300 and r.content:
                words = AI.transcribe(biz, r.content, "audio/mpeg", http)
    except Exception as e:                                        # noqa: BLE001 — no words is fine: the link still works
        logger.info("voicemail not written down: %s", e)
    listen = f"{config.PUBLIC_URL()}/hooks/{acc['hook_id']}/voicemail/{sid}.mp3"
    body = t("vm_inbox", lang).format(seconds=secs) + (f"\n«{words.strip()[:1500]}»" if words.strip() else "") + f"\n{t('vm_listen', lang)}: {listen}"
    X.receive_items(acc, [Inbound(address=phone, body=body, provider_id=f"vm:{sid}")])
    with db.tx() as conn:
        log(conn, biz["id"], "call.voicemail", "webhook:sms", acc["id"], {"seconds": secs, "written": bool(words.strip())})
    return _twiml("<Hangup/>")


def voicemail_audio(acc, sid: str, http=None) -> tuple[bytes, str]:
    """close01: play a voicemail from the inbox link — fetched from the business's own Twilio account with its own keys."""
    import httpx
    if not re.fullmatch(r"RE[0-9a-f]{32}", sid or ""):
        raise NotFound("voicemail")
    cfg = crypto.open_(acc["config"])
    sid_acct = cfg.get("account_sid", "")
    if not re.fullmatch(r"AC[0-9a-f]{32}", sid_acct or ""):
        raise NotFound("voicemail")
    cl = http or httpx.Client(timeout=30)
    r = cl.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid_acct}/Recordings/{sid}.mp3", auth=(sid_acct, cfg.get("auth_token", "")))
    if r.status_code >= 300 or not r.content:
        raise NotFound("voicemail")
    return r.content, "audio/mpeg"
