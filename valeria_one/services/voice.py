"""Voice notes: what people say, written down — with the business's OWN AI key.

A voice note arrives like any message (WhatsApp sends OGG/Opus, Telegram OGG/Opus voice, Twilio MMS/WhatsApp
AMR or OGG…). The file is fetched with the channel's own account (channels/*.download) and the business's AI
writes it down (ai.transcribe: Gemini takes audio on its free tier; OpenAI's speech-to-text works too; Claude
can't listen). The words are kept with the message (messages.transcript):

* a customer's note: the transcript shows in the inbox, the AI's suggested reply under it (suggest mode — in
  auto mode the AI simply answers, from the transcript), and booking in the chat understands it too;
* the owner's note: it is a command, like a typed one (services/commands.py).

No AI key, or an AI that can't listen: the message is still delivered and kept as a voice note, and the inbox
says "voice note (connect your free AI key to read it)". Nothing is ever sent to an operator's AI.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update

from .. import channels, crypto, db
from . import log
from . import ai as AI

AUDIO = ("audio", "voice")


def audio_of(media: list | None) -> dict | None:
    for m in media or []:
        if isinstance(m, dict) and (m.get("type") in AUDIO or str(m.get("mime") or "").startswith("audio/")):
            return m
    return None


def image_of(media: list | None) -> dict | None:
    for m in media or []:
        if isinstance(m, dict) and (m.get("type") == "image" or str(m.get("mime") or "").startswith("image/")):
            return m
    return None


def fetch(account_id: UUID, media: dict, http=None) -> tuple[bytes, str]:
    """The file, from the channel it came on. Raises ValueError with a reason."""
    with db.tx() as conn:
        acc = conn.execute(select(db.channel_accounts).where(db.channel_accounts.c.id == account_id)).mappings().first()
    if not acc:
        raise ValueError("the channel is gone")
    return channels.adapter(acc["channel"], http).download(crypto.open_(acc["config"]), media)


def listen(biz: dict, account_id: UUID, media: dict, http=None) -> str:
    """Fetch + write down. Raises AI.AIError (no key, can't listen) or ValueError (couldn't fetch)."""
    with db.tx() as conn:
        if not AI.own_key(conn, biz):
            raise AI.AIError("no AI key")
    raw, mime = fetch(account_id, media, http)
    return AI.transcribe(biz, raw, mime or media.get("mime") or "audio/ogg", http)


def _strip(media: list) -> list:
    """The sandbox carries the file inside the message: once heard, it isn't kept twice."""
    return [{k: v for k, v in m.items() if k != "data"} if isinstance(m, dict) else m for m in media or []]


def for_customer(business_id: UUID, conversation_id: UUID, http=None) -> str:
    """After a customer's message: if it is a voice note not written down yet, write it down (and, in suggest mode,
    the AI's suggested reply). -> none | done | no_ai | failed."""
    M, CV = db.messages, db.conversations
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        last = conn.execute(select(M).where(M.c.business_id == business_id, M.c.conversation_id == conversation_id)
                            .order_by(M.c.created_at.desc()).limit(1)).mappings().first()
        if not last or last["direction"] != "in" or last["transcript"] or not audio_of(last["media"]):
            return "none"
        acc_id = conn.execute(select(CV.c.channel_account_id).where(CV.c.id == conversation_id)).scalar()
    try:
        text = listen(biz, acc_id, audio_of(last["media"]), http)
    except AI.AIError as e:
        with db.tx() as conn:
            log(conn, business_id, "voice.unread", "valeria", last["id"], {"why": str(e)[:200]})
        return "no_ai"
    except Exception as e:                                   # noqa: BLE001 — could not fetch it: the note stays a note
        with db.tx() as conn:
            log(conn, business_id, "voice.failed", "valeria", last["id"], {"why": f"{type(e).__name__}"[:200]})
        return "failed"
    with db.tx() as conn:
        conn.execute(update(M).where(M.c.id == last["id"]).values(transcript=text, media=_strip(last["media"])))
        log(conn, business_id, "voice.transcribed", "valeria", last["id"], {"who": "customer"})
        s = AI.settings(conn, business_id)
    if s and s["mode"] == "suggest":
        try:
            sug, _ = AI.draft(biz, conversation_id, http)
            with db.tx() as conn:
                conn.execute(update(M).where(M.c.id == last["id"]).values(suggestion=sug[:1000]))
        except AI.AIError:
            pass
    return "done"


def answered(conn, business_id: UUID, since) -> int:
    """Customer voice notes written down since `since` that got an answer after them (weekly proof)."""
    from sqlalchemy import and_, exists, func
    M = db.messages
    later = M.alias("later")
    return conn.execute(select(func.count()).select_from(M).where(
        M.c.business_id == business_id, M.c.direction == "in", M.c.transcript.isnot(None), M.c.created_at >= since,
        exists().where(and_(later.c.conversation_id == M.c.conversation_id, later.c.direction == "out",
                            later.c.created_at > M.c.created_at, later.c.status.notin_(("failed", "blocked")))))).scalar()
