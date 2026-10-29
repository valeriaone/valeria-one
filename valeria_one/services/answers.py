"""24/7 answers in the owner's style — even without AI, and never an invented fact.

With the business's AI on "auto", the AI answers (ai.py: only the owner's facts, the owner's style, a person when
unsure). Without AI the owner can switch on rule answers: a customer asking the price, the hours, where the
business is or how to book gets the answer from what the owner entered — services and prices, opening hours, the
website's address, the booking link — in the owner's style (no emoji for someone who uses none, their sign-off).
Anything else, or a fact the owner never gave: "Thanks! We'll get back to you soon." — once per conversation per
12 hours — and Valeria asks the owner, on their chat, what to answer. The same happens when the AI hands a chat to a
person. Out of hours it works the same (that is the point); the hours themselves are part of the answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select

from .. import blocks, config, db, money, packs
from . import log

TOPICS = {
    "price": ("price", "prices", "how much", "cost", "precio", "precios", "cuánto", "cuanto", "cuesta", "costo", "preço", "preco",
              "quanto", "custa", "prix", "combien", "tarif", "preis", "kostet", "wie viel", "magkano", "presyo", "harga", "berapa",
              "कितना", "कीमत", "दाम", "سعر", "كم", "値段", "料金", "いくら", "价格", "多少钱", "价钱"),
    "hours": ("hours", "open", "close", "opening", "horario", "abren", "abierto", "cierran", "horário", "abrem", "aberto",
              "fecham", "horaires", "ouvert", "ferme", "öffnungszeiten", "geöffnet", "offen", "oras", "bukas ba", "sarado",
              "jam buka", "buka", "tutup", "समय", "खुला", "بتفتح", "مفتوح", "ساعات", "営業時間", "何時", "开门", "营业时间", "几点"),
    "location": ("where", "address", "location", "dónde", "donde", "dirección", "direccion", "ubicación", "ubicacion", "onde",
                 "endereço", "endereco", "localização", "où", "adresse", "wo ", "adresse", "saan", "lokasyon", "dimana",
                 "di mana", "alamat", "lokasi", "कहाँ", "पता", "أين", "عنوان", "موقع", "どこ", "住所", "场所", "地址", "在哪"),
    "booking": ("book", "appointment", "reserve", "cita", "agendar", "reservar", "turno", "agendamento", "marcar", "rendez-vous",
                "réserver", "termin", "buchen", "mag-book", "iskedyul", "janji", "pesan", "booking", "बुक", "अपॉइंटमेंट", "حجز",
                "موعد", "予約", "预约", "預約"),
}
GAP_HOURS = 12                                                # "we'll get back to you" once per conversation per 12 h
MAX_PER_HOUR = 3


def topic(text: str) -> str | None:
    t = (text or "").lower()
    for k in ("price", "booking", "hours", "location"):
        if any(w in t for w in TOPICS[k]):
            return k
    return None


def _facts(conn, biz: dict, which: str, lang: str) -> str | None:
    """The answer from the owner's own facts — None when they never gave it (then a person answers)."""
    from . import booking as BK
    from .owner import t
    pack = packs.country(biz["country"])
    link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") and blocks.on(biz, "calendar") else ""
    if which == "price":
        svcs = [s for s in BK.services(conn, biz["id"], active_only=True) if s["price_minor"] is not None]
        if not svcs:
            return None
        lines = [f"· {s['name']}: {money.fmt(s['price_minor'], biz['currency'], pack.locale)}" for s in svcs[:8]]
        return t("va_ans_price", lang) + "\n" + "\n".join(lines) + (("\n" + t("va_ans_book", lang).format(link=link)) if link else "")
    if which == "hours":
        h = biz.get("hours") or {}
        if not h:
            return None
        days = [f"· {t('d_' + d, lang)}: " + ", ".join(f"{a}–{b}" for a, b in h[d]) for d in BK.DAYS if h.get(d)]
        return t("va_ans_hours", lang) + "\n" + "\n".join(days)
    if which == "location":
        addr = (biz.get("site") or {}).get("address")
        if not addr:
            return None
        from urllib.parse import quote
        return t("va_ans_where", lang).format(address=addr) + f"\nhttps://www.google.com/maps/search/?api=1&query={quote(addr)}"
    if which == "booking":
        return t("va_ans_book", lang).format(link=link) if link else None
    return None


def tell_owner(conn, biz: dict, conv: dict, question: str, now: datetime | None = None) -> dict | None:
    """A customer is waiting for something only the owner knows: one question on the owner's chat."""
    from . import owner as OW
    C = db.contacts
    name = conn.execute(select(C.c.name).where(C.c.id == conv["contact_id"])).scalar() or "—"
    first = name.split(" ")[0] or "—"
    return OW.ask(conn, biz, "reply", OW.t("va_q_customer_asked", biz["language"]).format(name=first, text=(question or "…")[:300]),
                  {"conversation_id": str(conv["id"]), "question": (question or "")[:500]}, contact_id=conv["contact_id"],
                  dedupe=f"waiting:{conv['id']}:{(now or datetime.now(timezone.utc)).strftime('%Y%m%d%H')}", now=now)


def respond(business_id: UUID, conversation_id: UUID, http=None, now: datetime | None = None) -> str:
    """After a customer's message, when the AI isn't answering: off | replied | asked_owner | quiet | limit."""
    from . import inbox as X
    from . import owner as OW
    from . import style
    from .ai import detect_language
    from .owner import t
    now = now or datetime.now(timezone.utc)
    M, CV, E = db.messages, db.conversations, db.events
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        s = OW.settings(conn, business_id)
        if not s["answers"] or s["paused_at"] or not blocks.on(biz, "assistant"):
            return "off"
        conv = dict(conn.execute(select(CV).where(CV.c.id == conversation_id, CV.c.business_id == business_id)).mappings().one())
        if conv["needs_human"] or conv["status"] != "open" or conv.get("flow"):
            return "off"
        last = conn.execute(select(M).where(M.c.conversation_id == conversation_id).order_by(M.c.created_at.desc()).limit(1)).mappings().first()
        if not last or last["direction"] != "in":
            return "off"
        from . import contacts as C
        if C.is_stop(last["body"]):
            return "off"
        text = last["body"] or last["transcript"] or ""
        recent = conn.execute(select(func.count()).select_from(E).where(
            E.c.business_id == business_id, E.c.type == "answers.replied", E.c.subject_id == conversation_id,
            E.c.created_at > now - timedelta(hours=1))).scalar()
        if recent >= MAX_PER_HOUR:
            return "limit"
        lang = detect_language(text) or biz["language"]
        which = topic(text)
        body = _facts(conn, biz, which, lang) if which else None
        unknown = body is None
        if unknown:
            told = conn.execute(select(E.c.id).where(E.c.business_id == business_id, E.c.type == "answers.later",
                                                     E.c.subject_id == conversation_id,
                                                     E.c.created_at > now - timedelta(hours=GAP_HOURS))).first()
            tell_owner(conn, biz, conv, text, now)
            if told:
                return "asked_owner"
            body = t("va_ans_later", lang)
        body = style.apply(style.get(conn, business_id), body)
    try:
        msg = X.send(biz, "valeria", contact_id=conv["contact_id"], body=body, channel_account_id=conv["channel_account_id"], http=http, now=now)
    except Exception:                                          # noqa: BLE001 — blocked (window, STOP): nothing more to do
        return "blocked"
    with db.tx() as conn:
        conn.execute(db.messages.update().where(db.messages.c.id == msg["id"]).values(by_ai=True))
        log(conn, business_id, "answers.later" if unknown else "answers.replied", "valeria", conversation_id, {"topic": which})
    return "asked_owner" if unknown else "replied"
