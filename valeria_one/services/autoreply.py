"""What happens right after someone writes.

A customer: a voice note is written down first (with the business's own AI); an answer to a reminder ("1" / "2")
is handled next; then a booking under way in this chat (times offered, a question asked, "can we do 7 instead?" /
"cancel" for a booking made here — sch01); then a Facebook Marketplace buyer (services/marketplace.py: the item's own
answer, the owner told); then a request to book; otherwise the business's AI answers
if it is on 'auto' — and when it hands the chat to a person, the owner is asked on their chat. Without AI (or when
it fails), the owner's 24/7 rule answers, when switched on. "Valeria, stop" pauses the AI and the rule answers
(an in-chat booking a customer started still finishes).

The owner (a registered number): never a customer — what they wrote is a command for Valeria (respond_owner).
"""
from __future__ import annotations

from uuid import UUID

from . import ai, chatbook


def respond(business_id: UUID, conversation_id: UUID, http=None) -> str:
    from . import answers, reminders, voice
    from . import owner as OW
    from .. import db
    voice.for_customer(business_id, conversation_id, http)
    r = reminders.reply(business_id, conversation_id, http)
    if r:
        return "reminder:" + r
    r = chatbook.handle(business_id, conversation_id, http=http, ongoing=True)   # sch01: a time being picked, a booking moved
    if r not in ("off", "not_booking"):
        return "booking:" + r
    from . import marketplace
    r = marketplace.respond(business_id, conversation_id, http)   # mkt01: a Marketplace buyer: the item's own answer
    if r != "off":
        return "marketplace:" + r
    r = chatbook.handle(business_id, conversation_id, http=http)
    if r not in ("off", "not_booking"):
        return "booking:" + r
    with db.tx() as conn:
        stopped = OW.paused(conn, business_id)
    if stopped:
        return "paused"
    res = ai.auto_reply(business_id, conversation_id, http=http)
    if res == "handed_off":
        _tell_owner(business_id, conversation_id)
    if res in ("off", "failed"):
        got = answers.respond(business_id, conversation_id, http=http)
        if got != "off":
            return "answers:" + got
    return res


def _tell_owner(business_id: UUID, conversation_id: UUID) -> None:
    from sqlalchemy import select

    from .. import db
    from . import answers
    M, CV = db.messages, db.conversations
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        conv = dict(conn.execute(select(CV).where(CV.c.id == conversation_id)).mappings().one())
        q = conn.execute(select(M.c.body, M.c.transcript).where(M.c.conversation_id == conversation_id, M.c.direction == "in")
                         .order_by(M.c.created_at.desc()).limit(1)).first()
        answers.tell_owner(conn, biz, conv, (q.body or q.transcript or "") if q else "")


def respond_owner(business_id: UUID, message_id: UUID, http=None) -> str:
    """The owner wrote (or spoke, or sent a photo) to the business's chat: Valeria answers them there. Claimed once,
    so a replayed webhook is never answered twice. -> what happened (for logs and tests)."""
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from .. import db
    from . import commands, voice
    from . import owner as OW
    now = datetime.now(timezone.utc)
    AM = db.assistant_messages
    with db.tx() as conn:
        m = conn.execute(update(AM).where(AM.c.id == message_id, AM.c.business_id == business_id, AM.c.handled_at.is_(None))
                         .values(handled_at=now).returning(AM)).mappings().first()
        if not m:
            return "duplicate"
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        number = dict(conn.execute(select(db.owner_numbers).where(db.owner_numbers.c.id == m["number_id"])).mappings().one())
    lang = OW.lang_of(biz, number)
    if biz["suspended_at"]:
        return "suspended"
    if m["status"] == "paired":
        OW.say(biz, number, OW.t("va_paired", lang).format(business=biz["name"]), account_id=m["channel_account_id"], http=http)
        return "paired"
    if not number.get("channel"):
        return "unknown"
    text = m["body"] or ""
    audio, image = voice.audio_of(m["media"]), voice.image_of(m["media"])
    if image:
        try:
            raw, mime = voice.fetch(m["channel_account_id"], image, http)
        except Exception:                                      # noqa: BLE001 — couldn't fetch: tell them plainly
            OW.say(biz, number, OW.t("va_photo_failed", lang), account_id=m["channel_account_id"], http=http)
            return "photo_failed"
        caption = text
        if audio:                                              # a photo and a voice note in one go: the note is the caption
            try:
                caption = voice.listen(biz, m["channel_account_id"], audio, http)
            except Exception:                                  # noqa: BLE001
                pass
        out = commands.photo(biz, number, raw, mime, caption, http=http, now=now)
        OW.say(biz, number, out["text"], out.get("options"), account_id=m["channel_account_id"], http=http)
        return "photo"
    if audio and not text:
        try:
            text = voice.listen(biz, m["channel_account_id"], audio, http)
        except ai.AIError:
            OW.say(biz, number, OW.t("va_voice_no_ai", lang).format(link=_ai_link()), account_id=m["channel_account_id"], http=http)
            return "voice_no_ai"
        except Exception:                                      # noqa: BLE001
            OW.say(biz, number, OW.t("va_voice_failed", lang), account_id=m["channel_account_id"], http=http)
            return "voice_failed"
        with db.tx() as conn:
            conn.execute(update(AM).where(AM.c.id == m["id"]).values(transcript=text, media=voice._strip(m["media"])))
    out = commands.handle(biz, number, text, http=http, now=now)
    OW.say(biz, number, out["text"], out.get("options"), account_id=m["channel_account_id"], http=http)
    return "answered"


def _ai_link() -> str:
    from .. import config
    return f"{config.PUBLIC_URL()}/app/settings#ai"


def after_inbound(business_id: UUID, stats: dict, http=None) -> list:
    """Both kinds, for callers that run them in line (the sandbox, the API's simulate)."""
    out = [respond_owner(business_id, mid, http) for mid in (stats or {}).get("owner_ids", [])]
    out += [respond(business_id, cid, http) for cid in (stats or {}).get("conversation_ids", [])]
    return out
