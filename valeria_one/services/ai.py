"""AI replies with the business's OWN key (Google Gemini — free tier —, Anthropic Claude or OpenAI).

* suggest: the owner taps "Suggest a reply" in the inbox and edits before sending.
* auto: a customer writes and the AI answers on its own, in the customer's language, using only what the
  business told it (services, prices, hours, booking link, its own notes). When it isn't sure, or the
  customer asks for a person, it hands the conversation to a human (needs_human) instead of guessing.

It is the business's assistant, not a general chatbot — which is also what Meta's WhatsApp rules allow.
The sandbox may use the operator's demo key (VO_DEMO_AI_PROVIDER / VO_DEMO_AI_KEY), capped per day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, crypto, db, money, packs
from . import Blocked, Invalid, log

AI = db.ai_settings
PROVIDERS = {"gemini": "gemini-3.5-flash-lite", "anthropic": "claude-haiku-5-5", "openai": "gpt-4.1-mini"}
HANDOFF = "[[HUMAN]]"
MAX_AUTO_PER_HOUR = 6
_LANG = {"es": "Spanish", "en": "English", "pt": "Portuguese", "fil": "Filipino", "ja": "Japanese", "de": "German", "hi": "Hindi",
         "fr": "French", "id": "Indonesian", "ar": "Arabic", "zh": "Chinese", "ko": "Korean", "th": "Thai"}


class AIError(Exception):
    def __init__(self, message: str, busy: bool = False):
        super().__init__(message)
        self.busy = busy                                       # overloaded / rate-limited: another model may answer


def _http(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=30)


# ---------------------------------------------------------------- settings
def settings(conn: Connection, business_id: UUID) -> dict | None:
    r = conn.execute(select(AI).where(AI.c.business_id == business_id)).mappings().first()
    return dict(r) if r else None


def public_settings(conn: Connection, business_id: UUID) -> dict:
    s = settings(conn, business_id)
    if not s:
        return {"provider": None, "model": None, "mode": "off", "instructions": "", "has_key": False}
    return {"provider": s["provider"], "model": s["model"], "mode": s["mode"], "instructions": s["instructions"],
            "has_key": bool(s["api_key"]) or s["provider"] == "demo"}


def save(conn: Connection, biz: dict, actor: str, *, provider: str | None = None, api_key: str | None = None,
         model: str | None = None, mode: str | None = None, instructions: str | None = None) -> dict:
    cur = settings(conn, biz["id"]) or {"provider": None, "api_key": None, "model": None, "mode": "off", "instructions": ""}
    prov = provider or cur["provider"] or ("demo" if biz["sandbox"] else None)
    if prov == "demo" and not biz["sandbox"]:
        raise Invalid("the demo AI is only for sandbox businesses — add your own key", "provider")
    if prov not in (*PROVIDERS, "demo"):
        raise Invalid("choose gemini, anthropic or openai", "provider")
    key = cur["api_key"]
    if api_key:                                             # empty = keep the stored key
        key = crypto.seal({"secret": api_key.strip()})["secret"]
    if provider and provider != cur["provider"] and not api_key and prov != "demo":
        key = None                                          # a key belongs to one provider
    m = mode or cur["mode"]
    if m not in ("off", "suggest", "auto"):
        raise Invalid("mode is off, suggest or auto", "mode")
    if m != "off" and not key and prov != "demo":
        raise Invalid("add your AI key first (Gemini has a free one: aistudio.google.com/apikey)", "api_key")
    vals = {"provider": prov, "api_key": key, "model": (model or "").strip() or cur["model"], "mode": m,
            "instructions": (instructions if instructions is not None else cur["instructions"])[:4000],
            "updated_at": func.now()}
    conn.execute(pg_insert(AI).values(business_id=biz["id"], **vals).on_conflict_do_update(index_elements=[AI.c.business_id], set_=vals))
    log(conn, biz["id"], "ai.updated", actor, biz["id"], {"provider": prov, "mode": m})
    return public_settings(conn, biz["id"])


# ---------------------------------------------------------------- what the AI knows
def _facts(conn: Connection, biz: dict) -> str:
    from . import booking as BK
    pack = packs.country(biz["country"])
    ind = packs.industry(biz["industry"])
    lines = [f"Business: {biz['name']} ({packs.text(ind.name, 'en')}), {pack.label('en')}. Time zone {biz['timezone']}."]
    svcs = BK.services(conn, biz["id"], active_only=True)
    for s in svcs:
        price = money.fmt(s["price_minor"], biz["currency"], pack.locale) if s["price_minor"] is not None else "price on request"
        lines.append(f"- Service: {s['name']}, {s['minutes']} min, {price}")
    if biz.get("hours"):
        lines.append("Opening hours: " + "; ".join(f"{d} " + ", ".join(f"{a}-{b}" for a, b in v) for d, v in biz["hours"].items()))
    if biz.get("booking_enabled") and biz.get("slug"):
        lines.append(f"Customers book here: {config.PUBLIC_URL()}/b/{biz['slug']}")
    return "\n".join(lines)


_WORDS = {
    "en": "the is are you how much what when where can do does have hi hello please thanks price open book want need my your",
    "es": "el la los las es son cuanto cuánto cuesta que qué cuando dónde donde puedo tienen hola gracias precio abren quiero necesito mi tu por para",
    "pt": "o a os as é são quanto custa que quando onde posso vocês tem têm olá oi obrigado obrigada preço abrem quero preciso meu sua para",
    "fil": "po ang ng mga magkano ba kayo saan kailan pwede salamat opo hindi meron wala",
    "de": "der die das ist sind wie viel was wann wo kann haben hallo danke preis offen möchte brauche mein ihr",
    "fr": "le la les est sont combien quoi quand où peux avez bonjour merci prix ouvert veux besoin mon votre",
    "id": "berapa harga apa kapan dimana bisa ada halo terima kasih buka saya mau butuh anda yang ini",
}
_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fil": "Filipino", "de": "German", "fr": "French", "id": "Indonesian",
          "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "hi": "Hindi", "ar": "Arabic", "th": "Thai"}


def detect_language(text: str) -> str | None:
    """Good enough to pick the reply language of a short chat message; None when unsure."""
    t = (text or "").strip()
    if not t:
        return None
    for code, lo, hi in (("ja", 0x3040, 0x30FF), ("ko", 0xAC00, 0xD7AF), ("zh", 0x4E00, 0x9FFF), ("hi", 0x0900, 0x097F),
                         ("ar", 0x0600, 0x06FF), ("th", 0x0E00, 0x0E7F)):
        if any(lo <= ord(c) <= hi for c in t):
            return code
    words = [w.strip("¿?¡!.,;:()\"'") for w in t.lower().split()]
    scores = {k: sum(w in set(v.split()) for w in words) for k, v in _WORDS.items()}
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    return best if ranked[0] >= 1 and ranked[0] > ranked[1] else None


def _system(conn: Connection, biz: dict, notes: str, customer_lang: str | None = None) -> str:
    lang = _LANG.get(biz["language"], "the customer's language")
    return (
        f"You answer customer messages for {biz['name']}, on their behalf, in a chat app. "
        f"Write like a friendly person at the front desk: short (1-3 sentences), warm, no lists unless asked. "
        + (f"The customer is writing in {_NAMES[customer_lang]}: reply ONLY in {_NAMES[customer_lang]}, translating the facts. "
           if customer_lang in _NAMES else f"Reply in the language of the customer's last message (the business usually speaks {lang}). ")
        + "\n"
        "Use ONLY the facts below. Never invent prices, availability, policies or promises. "
        "If you can't answer from the facts, if the customer is upset, wants a person, wants to cancel or complain, "
        f"or anything involves health, legal or money disputes, reply with a short note that a person will answer soon "
        f"and end with {HANDOFF}. To book, share the booking link when there is one.\n"
        "You are not a general assistant: politely decline anything unrelated to this business.\n"
        + _style(conn, biz) + "\n\n"
        f"FACTS:\n{_facts(conn, biz)}\n\nNOTES FROM THE OWNER:\n{(notes or '').strip() or '(none)'}"
    )


def _style(conn: Connection, biz: dict) -> str:
    """How the owner sounds (services/style.py), for every draft."""
    from . import style
    return style.prompt(style.get(conn, biz["id"]))


def _history(conn: Connection, business_id: UUID, conversation_id: UUID, n: int = 20) -> list[tuple[str, str]]:
    M = db.messages
    text = func.coalesce(func.nullif(M.c.body, ""), M.c.transcript, "")          # a voice note: what it said
    rows = conn.execute(select(M.c.direction, text).where(M.c.business_id == business_id, M.c.conversation_id == conversation_id,
                                                          text != "").order_by(M.c.created_at.desc()).limit(n)).all()
    return [("user" if d == "in" else "assistant", b) for d, b in reversed(rows)]


# ---------------------------------------------------------------- providers
def _call(provider: str, key: str, model: str, system: str, turns: list[tuple[str, str]], http: httpx.Client,
          max_tokens: int | None = None) -> str:
    # providers want the conversation to start with the customer
    while turns and turns[0][0] != "user":
        turns = turns[1:]
    if not turns:
        raise AIError("nothing to answer")
    if provider == "gemini":
        r = http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": key},
                      json={"systemInstruction": {"parts": [{"text": system}]},
                            "contents": [{"role": "user" if r_ == "user" else "model", "parts": [{"text": t}]} for r_, t in turns],
                            "generationConfig": {"maxOutputTokens": max_tokens or 1024, "temperature": 0.4}})
        j = _json(r)
        try:
            text = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"] if not p.get("thought")).strip()
        except (KeyError, IndexError):
            raise AIError("Gemini gave no answer") from None
        if not text:
            raise AIError("Gemini gave an empty answer")
        return text
    if provider == "anthropic":
        r = http.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                      json={"model": model, "max_tokens": max_tokens or 400, "system": system,
                            "messages": [{"role": r_, "content": t} for r_, t in turns]})
        j = _json(r)
        return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()
    if provider == "openai":
        r = http.post("https://api.openai.com/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "max_tokens": max_tokens or 400, "temperature": 0.4,
                            "messages": [{"role": "system", "content": system}] + [{"role": r_, "content": t} for r_, t in turns]})
        j = _json(r)
        try:
            return (j["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            raise AIError("OpenAI gave no answer") from None
    raise AIError(f"unknown provider {provider}")


def _json(r: httpx.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise AIError(f"the AI service answered {r.status_code}") from None
    if r.status_code >= 300:
        e = j.get("error") if isinstance(j, dict) else None
        msg = e.get("message") if isinstance(e, dict) else (e if isinstance(e, str) else None)
        raise AIError(f"the AI service said: {msg or r.status_code}", busy=r.status_code in (429, 500, 502, 503, 504, 529))
    return j


def _credentials(conn: Connection, biz: dict, s: dict) -> tuple[str, str, str]:
    if s["provider"] == "demo":
        prov, key = config.get("VO_DEMO_AI_PROVIDER"), config.get("VO_DEMO_AI_KEY")
        if not (biz["sandbox"] and config.SANDBOX() and prov in PROVIDERS and key):
            raise AIError("the demo AI is not available here")
        cap = int(config.get("VO_DEMO_AI_DAILY", "300"))
        E = db.events
        used = conn.execute(select(func.count()).select_from(E).where(
            E.c.type.in_(("ai.suggested", "ai.replied")), E.c.created_at > func.now() - timedelta(days=1),
            E.c.data["provider"].astext == "demo")).scalar()
        if used >= cap:
            raise AIError("the demo AI has had a busy day — try again tomorrow")
        return prov, key, config.get("VO_DEMO_AI_MODEL") or PROVIDERS[prov]
    if not s["api_key"]:
        raise AIError("no AI key")
    return s["provider"], crypto.open_({"secret": s["api_key"]})["secret"], s["model"] or PROVIDERS[s["provider"]]


def own_key(conn: Connection, biz: dict) -> tuple[str, str, str] | None:
    """The business's OWN key -> (provider, key, model), or None. Never the operator's demo key: what Valeria does for
    the owner (understanding commands, voice notes, learning their style) is paid by the business's own account or
    its free tier, or done by rules."""
    if not blocks.on(biz, "ai"):
        return None
    s = settings(conn, biz["id"])
    if not s or s["provider"] not in PROVIDERS or not s["api_key"]:
        return None
    return s["provider"], crypto.open_({"secret": s["api_key"]})["secret"], s["model"] or PROVIDERS[s["provider"]]


MAX_AUDIO = 10 * 1024 * 1024
LISTEN = ("Write down exactly what this voice note says, word for word, in the language it is spoken in. Only the words: "
          "no notes, no translation, no quotes.")


def transcribe(biz: dict, raw: bytes, mime: str, http: httpx.Client | None = None) -> str:
    """A voice note -> its words, with the business's own key (Gemini takes audio, free tier; OpenAI's speech-to-text).
    Raises AIError with a plain reason (no key, an AI that can't listen, too long)."""
    import base64
    with db.tx() as conn:
        creds = own_key(conn, biz)
    if not creds:
        raise AIError("no AI key")
    prov, key, model = creds
    if len(raw) > MAX_AUDIO:
        raise AIError("the voice note is too long")
    mime = (mime or "audio/ogg").split(";")[0].strip() or "audio/ogg"
    h = _http(http)
    try:
        if prov == "gemini":
            r = h.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model.split(',')[0].strip()}:generateContent",
                       headers={"x-goog-api-key": key},
                       json={"contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}},
                                                                     {"text": LISTEN}]}],
                             "generationConfig": {"maxOutputTokens": 1024, "temperature": 0}})
            j = _json(r)
            try:
                text = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"] if not p.get("thought")).strip()
            except (KeyError, IndexError):
                raise AIError("Gemini gave no answer") from None
        elif prov == "openai":
            ext = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "m4a",
                   "audio/amr": "amr", "audio/wav": "wav", "audio/webm": "webm"}.get(mime, "ogg")
            r = h.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                       data={"model": "whisper-1"}, files={"file": (f"voice.{ext}", raw, mime)})
            text = (_json(r).get("text") or "").strip()
        else:
            raise AIError("Claude can't listen to voice notes; a Google Gemini key (free) or an OpenAI key can")
    except httpx.HTTPError as e:
        raise AIError(f"could not reach the AI service ({type(e).__name__})", busy=True) from None
    if not text:
        raise AIError("the voice note had no words")
    return text[:4000]


def draft(biz: dict, conversation_id: UUID, http: httpx.Client | None = None) -> tuple[str, bool]:
    """-> (text, hand_to_human). Raises AIError with a message the owner can read."""
    with db.tx() as conn:
        s = settings(conn, biz["id"])
        if not s or s["mode"] == "off":
            raise AIError("turn on the AI in Settings first")
        prov, key, model = _credentials(conn, biz, s)
        fresh = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == biz["id"])).mappings().one())
        turns = _history(conn, biz["id"], conversation_id)
        last_in = next((t for r_, t in reversed(turns) if r_ == "user"), "")
        system = _system(conn, fresh, s["instructions"], detect_language(last_in))
        from . import marketplace
        system += marketplace.ai_facts(conn, fresh, conversation_id)    # mkt01: a Marketplace buyer: the item they ask about
    models = [m.strip() for m in model.split(",") if m.strip()]    # "model-a, model-b" = try b when a is busy
    out, last = None, None
    for m in models:
        try:
            out = _call(prov, key, m, system, turns, _http(http))
            break
        except httpx.HTTPError as e:
            last = AIError(f"could not reach the AI service ({type(e).__name__})", busy=True)
        except AIError as e:
            last = e
            if not e.busy:
                break
    if out is None:
        raise last or AIError("no model to ask")
    human = HANDOFF in out
    text = out.replace(HANDOFF, "").strip()[:1000]
    return text, human


def suggest(biz: dict, actor: str, conversation_id: UUID, http=None) -> dict:
    if not blocks.on(biz, "ai"):
        raise AIError("AI answers are switched off for this business")
    text, human = draft(biz, conversation_id, http)
    with db.tx() as conn:
        s = settings(conn, biz["id"])
        log(conn, biz["id"], "ai.suggested", actor, conversation_id, {"provider": s["provider"]})
    return {"text": text, "needs_human": human}


# ---------------------------------------------------------------- auto-reply after a customer writes
def auto_reply(business_id: UUID, conversation_id: UUID, http=None, now: datetime | None = None) -> str:
    """Called after an inbound message is stored. Returns what happened (for logs and tests)."""
    from . import inbox as X
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        s = settings(conn, business_id)
        if not s or s["mode"] != "auto" or not blocks.on(biz, "ai"):
            return "off"
        CV, M = db.conversations, db.messages
        conv = conn.execute(select(CV).where(CV.c.id == conversation_id, CV.c.business_id == business_id)).mappings().first()
        if not conv or conv["needs_human"] or conv["status"] != "open":
            return "human"
        last = conn.execute(select(M.c.direction, M.c.body).where(M.c.conversation_id == conversation_id)
                            .order_by(M.c.created_at.desc()).limit(1)).first()
        if not last or last.direction != "in":
            return "nothing_new"
        from . import contacts as C
        if C.is_stop(last.body):
            return "stop"
        recent = conn.execute(select(func.count()).select_from(M).where(
            M.c.conversation_id == conversation_id, M.c.by_ai, M.c.created_at > now - timedelta(hours=1))).scalar()
        if recent >= MAX_AUTO_PER_HOUR:
            conn.execute(update(CV).where(CV.c.id == conversation_id).values(needs_human=True))
            log(conn, business_id, "ai.handed_off", "ai", conversation_id, {"why": "too many automatic replies"})
            return "limit"
        first = not conn.execute(select(M.c.id).where(M.c.conversation_id == conversation_id, M.c.by_ai).limit(1)).first()
    try:
        text, human = draft(biz, conversation_id, http)
    except AIError as e:
        with db.tx() as conn:
            log(conn, business_id, "ai.failed", "ai", conversation_id, {"error": str(e)[:300]})
        return "failed"
    if text:
        if first and (line := powered_line(biz)):
            text = f"{text}\n\n{line}"
        try:
            msg = X.send(biz, "ai", contact_id=conv["contact_id"], body=text, channel_account_id=conv["channel_account_id"])
        except Blocked:
            return "blocked"
        with db.tx() as conn:
            conn.execute(update(db.messages).where(db.messages.c.id == msg["id"]).values(by_ai=True))
            log(conn, business_id, "ai.replied", "ai", conversation_id, {"provider": s["provider"]})
    if human:
        with db.tx() as conn:
            conn.execute(update(db.conversations).where(db.conversations.c.id == conversation_id).values(needs_human=True))
            log(conn, business_id, "ai.handed_off", "ai", conversation_id, {"why": "the AI asked for a person"})
        return "handed_off"
    return "replied"


def powered_line(biz: dict) -> str | None:
    """"— Valeria One · https://…/i/VO-XXXXX" under the AI's FIRST answer in a conversation: the people a business talks
    to see what answered them, and a business that signs up from it is that business's referral. Only on a server that
    charges (the link carries the referral code); the owner can switch it off in Settings › AI; never in the sandbox."""
    from . import billing
    if (not biz.get("powered_tag") or biz.get("sandbox") or not biz.get("billing_ref") or not billing.enabled()
            or (config.get("VO_BADGE") or "on").lower() == "off"):
        return None
    return f"— Valeria One · {config.PUBLIC_URL()}/i/{biz['billing_ref']}"


def clear_human(conn: Connection, business_id: UUID, actor: str, conversation_id: UUID) -> None:
    conn.execute(update(db.conversations).where(db.conversations.c.business_id == business_id,
                                                db.conversations.c.id == conversation_id).values(needs_human=False))
    log(conn, business_id, "ai.resumed", actor, conversation_id)
