"""Voice notes for leads (lead27, v0.27).

* The owner speaks: a recording made on the lead's page (the phone's microphone) is kept here and sent as a voice note
  through the business's own account. With the business's OWN AI key (Gemini or OpenAI) it is also written down — the
  words go into the message box, so the owner can send words, the voice, or both. No key: the voice still goes.
* Valeria speaks: a short voice note in HER OWN voice — a stock voice of the business's own AI account (OpenAI "nova",
  Gemini "Kore"), never a copy of anyone's voice: nothing here accepts a voice sample, and the owner's recordings are
  never sent to a speech service. She always says first who she is: "Hi Ana, this is Valeria, the assistant at <business>."
  Claude can't speak: with a Claude key there are no Valeria voice notes (the words still go).

The customer's app fetches a note from /lv/<token> (random, unguessable); erasing the customer deletes their notes.
"""
from __future__ import annotations

import base64
import secrets
import struct
from uuid import UUID

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db
from . import Invalid, NotFound, log

V = db.lead_voice
MAX_BYTES = 5 * 1024 * 1024
MAX_SCRIPT = 400                                             # Valeria's notes stay short
MIMES = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
         "audio/aac": "aac", "audio/x-m4a": "m4a", "audio/webm": "webm", "audio/wav": "wav", "audio/x-wav": "wav",
         "audio/amr": "amr"}
WHATSAPP_OK = ("audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3", "audio/mp4", "audio/aac", "audio/x-m4a", "audio/amr")
STOCK = {"openai": "nova", "gemini": "Kore"}                  # her own voice: one stock voice per provider, nothing else
TTS_MODEL = {"openai": "gpt-4o-mini-tts", "gemini": "gemini-2.5-flash-preview-tts"}


def _mime(m: str | None) -> str:
    m = (m or "").split(";")[0].strip().lower()
    if m not in MIMES:
        raise Invalid("a voice recording (ogg, mp3, m4a, aac, webm, wav)", "audio")
    return m


def url(row: dict) -> str:
    return f"{config.PUBLIC_URL()}/lv/{row['token']}.{MIMES[row['mime']]}"


def save(conn: Connection, biz: dict, actor: str, contact_id: UUID, raw: bytes, mime: str, who: str = "owner",
         transcript: str | None = None) -> dict:
    mime = _mime(mime)
    if not raw:
        raise Invalid("the recording is empty", "audio")
    if len(raw) > MAX_BYTES:
        raise Invalid("a voice note up to 5 MB (about 5 minutes)", "audio")
    row = conn.execute(insert(V).values(business_id=biz["id"], contact_id=contact_id, who=who, mime=mime, size=len(raw), data=raw,
                                        transcript=(transcript or None) and transcript[:4000], token=secrets.token_urlsafe(24),
                                        created_by=actor[:100]).returning(V.c.id, V.c.token, V.c.mime, V.c.who)).mappings().one()
    log(conn, biz["id"], "lead.voice_saved", actor, row["id"], {"who": who})
    return dict(row)


def record(biz: dict, actor: str, lead_id, raw: bytes, mime: str, http=None) -> dict:
    """The owner's recording for one lead -> {"id", "url", "transcript", "why"} (why = no_key | failed when not written down)."""
    from . import ai as AI
    from . import leads as LD
    with db.tx() as conn:
        lead = LD.get(conn, biz["id"], lead_id)
        row = save(conn, biz, actor, lead["contact_id"], raw, mime)
    text, why = None, None
    try:
        text = AI.transcribe(biz, raw, row["mime"], http)
    except AI.AIError as e:
        why = "no_key" if "no AI key" in str(e) else "failed"
    except Exception:                                          # noqa: BLE001 — the voice still goes
        why = "failed"
    if text:
        with db.tx() as conn:
            conn.execute(update(V).where(V.c.id == row["id"]).values(transcript=text[:4000]))
    return {"id": str(row["id"]), "url": url(row), "transcript": text, "why": why}


def media(conn: Connection, business_id: UUID, voice_id, contact_id: UUID) -> dict:
    """A kept note as message media — only this business's, and only for the customer it was made for."""
    try:
        vid = UUID(str(voice_id))
    except ValueError:
        raise NotFound("voice note") from None
    r = conn.execute(select(V.c.token, V.c.mime).where(V.c.business_id == business_id, V.c.id == vid,
                                                       V.c.contact_id == contact_id)).mappings().first()
    if not r:
        raise NotFound("voice note")
    return {"type": "audio", "url": url(dict(r)), "mime": r["mime"]}


def serve(token: str) -> tuple[bytes, str]:
    tok = (token or "").rsplit(".", 1)[0]
    if not tok or len(tok) > 40:
        raise NotFound("voice note")
    with db.tx() as conn:
        r = conn.execute(select(V.c.data, V.c.mime).where(V.c.token == tok)).first()
    if not r:
        raise NotFound("voice note")
    return bytes(r.data), r.mime


# ---------------------------------------------------------------- Valeria's own voice
def script(biz: dict, contact: dict, text: str, lang: str) -> str:
    """What she says: who she is first, always; then the message, short."""
    from .leads import _t
    first = (contact.get("name") or "").split(" ")[0]
    intro = _t("ld_voice_intro" if first else "ld_voice_intro_noname", lang).format(name=first, business=biz["name"])
    body = " ".join((text or "").split())
    for marker in ("http://", "https://"):                     # links are for reading, not for hearing
        body = " ".join(w for w in body.split() if not w.startswith(marker))
    return f"{intro} {body}"[:MAX_SCRIPT].strip()


def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    """16-bit mono PCM (what Gemini's speech returns) -> a .wav file."""
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def speak(biz: dict, words: str, http: httpx.Client | None = None) -> tuple[bytes, str]:
    """Her words -> (audio, mime), with the business's OWN key and a stock voice. Raises AIError with a plain reason."""
    from . import ai as AI
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
    if not creds:
        raise AI.AIError("no AI key")
    prov, key, _ = creds
    if prov not in STOCK:
        raise AI.AIError("Claude can't speak; a Google Gemini key (free) or an OpenAI key can")
    h = http or httpx.Client(timeout=40)
    try:
        if prov == "openai":
            r = h.post("https://api.openai.com/v1/audio/speech", headers={"Authorization": f"Bearer {key}"},
                       json={"model": TTS_MODEL["openai"], "voice": STOCK["openai"], "input": words, "response_format": "opus"})
            if r.status_code >= 300 or not r.content:
                raise AI.AIError(f"the AI service answered {r.status_code}")
            return r.content, "audio/ogg"
        r = h.post(f"https://generativelanguage.googleapis.com/v1beta/models/{TTS_MODEL['gemini']}:generateContent",
                   headers={"x-goog-api-key": key},
                   json={"contents": [{"parts": [{"text": words}]}],
                         "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                             "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": STOCK["gemini"]}}}}})
        j = AI._json(r)
        try:
            part = j["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError):
            raise AI.AIError("Gemini gave no audio") from None
        raw = base64.b64decode(part.get("data") or "")
        rate = 24000
        for bit in str(part.get("mimeType") or "").split(";"):
            if bit.strip().startswith("rate="):
                try:
                    rate = int(bit.split("=", 1)[1])
                except ValueError:
                    pass
        if not raw:
            raise AI.AIError("Gemini gave no audio")
        return _wav(raw, rate), "audio/wav"
    except httpx.HTTPError as e:
        raise AI.AIError(f"could not reach the AI service ({type(e).__name__})", busy=True) from None


def valeria_note(biz: dict, contact: dict, text: str, lang: str, http=None) -> dict:
    """Make and keep one short note in her voice for this customer -> message media. Raises AIError when she can't speak."""
    words = script(biz, contact, text, lang)
    raw, mime = speak(biz, words, http)
    with db.tx() as conn:
        row = save(conn, biz, "valeria", contact["id"], raw, mime, who="valeria", transcript=words)
    return {"type": "audio", "url": url(row), "mime": row["mime"], "id": str(row["id"])}
