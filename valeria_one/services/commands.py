"""What the owner says to Valeria — typed, spoken (a voice note) or a photo — and what she answers.

Understanding: with the business's OWN AI key she reads any language mix into one intent (JSON); without one, or
when the AI is busy, keyword rules in the 11 languages of the screens do it. When she isn't sure she offers 2–3
taps instead of guessing. Answers are short, with one-tap choices (1 yes / 2 no / 3 later).

Intents: today · tomorrow · anything new · reply to a customer · who hasn't come back (win-back) · move or cancel a
booking · block a time · ask yesterday's customers for a review · post this photo · who owes me / remind them ·
send a payment link · change hours or info · what did you do today · how do I sound / be more formal · mark a
visit · help me set it up · stop · resume · undo.

Staff numbers only get what their permissions allow; hours, info, style, resume and helpers are the owner's.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .. import blocks, config, db, money, packs
from . import Invalid
from . import owner as OW

INTENTS = ("today", "tomorrow", "new", "reply", "winback", "move", "cancel", "block", "review", "post", "owes", "remind",
           "paylink", "hours", "info", "log", "style", "visit", "helpers", "stop", "resume", "undo", "help")
NEEDS = {"reply": "reply", "winback": "promos", "review": "promos", "post": "promos", "owes": "money", "remind": "money",
         "paylink": "money", "move": "bookings", "cancel": "bookings", "block": "bookings", "visit": "bookings",
         "hours": "owner", "info": "owner", "style": "owner", "resume": "owner", "helpers": "owner"}

# ---------------------------------------------------------------- words (whole message, after "Valeria," is dropped)
STOP = {"stop", "pause", "para", "alto", "detente", "pare", "parar", "arrête", "arrete", "stopp", "halt", "tigil", "hinto",
        "itigil", "berhenti", "stop dulu", "रुको", "बंद करो", "रोको", "توقف", "قف", "止めて", "ストップ", "停止", "停", "暂停"}
RESUME = {"resume", "continue", "start", "start again", "go on", "sigue", "continúa", "continua", "reanuda", "retoma", "volta",
          "continue", "reprends", "reprendre", "weiter", "fortsetzen", "tuloy", "ituloy", "sige na", "lanjut", "lanjutkan",
          "mulai lagi", "जारी रखो", "फिर से शुरू", "चालू करो", "استمر", "تابع", "再開", "続けて", "继续", "恢复"}
UNDO = {"undo", "deshacer", "deshaz", "desfaz", "desfazer", "annule ça", "annuler ça", "rückgängig", "ruckgangig", "bawiin",
        "ibalik", "urungkan", "batalkan itu", "पूर्ववत", "वापस लो", "वापस करो", "تراجع", "元に戻す", "取り消して", "撤销", "撤回"}
YES = {"1", "yes", "y", "ok", "okay", "sure", "send", "go", "si", "sí", "dale", "va", "claro", "manda", "envía", "envia", "sim",
       "pode", "manda ver", "oui", "d'accord", "vas-y", "ja", "klar", "los", "oo", "opo", "sige", "go na", "ya", "iya", "boleh",
       "kirim", "हाँ", "हां", "haan", "ji", "जी", "भेजो", "ठीक है", "نعم", "أجل", "ارسل", "أرسل", "はい", "お願い", "送って",
       "是", "好", "好的", "发送", "可以"}
NO = {"2", "no", "nope", "nah", "não", "nao", "non", "nein", "hindi", "huwag", "wag", "tidak", "jangan", "nggak", "gak",
      "नहीं", "नही", "nahi", "لا", "いいえ", "いらない", "不", "不要", "不用"}
LATER = {"3", "later", "not now", "luego", "después", "despues", "más tarde", "mas tarde", "depois", "mais tarde", "plus tard",
         "später", "spater", "mamaya", "saka na", "nanti", "nanti saja", "बाद में", "لاحقا", "لاحقًا", "فيما بعد", "後で", "あとで",
         "稍后", "以后", "待会"}

# words inside the message
W = {
    "log": ("what did you do", "what have you done", "what you did", "qué hiciste", "que hiciste", "qué has hecho", "que has hecho",
            "o que você fez", "o que voce fez", "o que fizeste", "qu'as-tu fait", "qu'est-ce que tu as fait", "was hast du gemacht",
            "was hast du heute", "ano ginawa mo", "anong ginawa mo", "ano'ng ginawa", "apa yang kamu lakukan", "kamu ngapain",
            "sudah ngapain", "तुमने क्या किया", "आज क्या किया", "ماذا فعلت", "ماذا عملت", "何をした", "何した", "今日何した",
            "你做了什么", "做了什么", "今天做了"),
    "style_ask": ("how do i sound", "how i sound", "my style", "cómo sueno", "como sueno", "mi estilo", "como eu soo", "meu estilo",
                  "comment je parle", "mon style", "wie klinge ich", "mein stil", "paano ako magsalita", "estilo ko", "gaya bahasa saya",
                  "gaya saya", "मैं कैसा लगता", "मेरी शैली", "كيف أبدو", "أسلوبي", "私の話し方", "話し方", "我的语气", "我的风格"),
    "pay_remind": ("remind", "recuérda", "recuerda", "recordar", "recordatorio", "lembra", "lembrar", "rappelle", "relance", "erinnere",
                   "erinnern", "paalalahanan", "paalala", "ingatkan", "याद दिला", "ذكره", "ذكرهم", "ذكّر", "リマインド", "催促", "提醒", "催"),
    "owes": ("who owes", "owes me", "owe me", "unpaid", "not paid", "hasn't paid", "quién me debe", "quien me debe", "me debe",
             "me deben", "deben", "sin pagar", "no ha pagado", "quem me deve", "me deve", "devem", "não pagou", "nao pagou",
             "qui me doit", "me doit", "impayé", "pas payé", "wer schuldet", "schuldet mir", "unbezahlt", "nicht bezahlt",
             "may utang", "sino ang may utang", "utang", "hindi pa nagbabayad", "siapa yang berutang", "hutang", "belum bayar",
             "बकाया", "उधार", "पैसे नहीं दिए", "مدين", "لم يدفع", "من عليه", "未払い", "払ってない", "欠款", "欠我", "没付", "未付"),
    "paylink": ("payment link", "pay link", "link to pay", "link de pago", "liga de pago", "enlace de pago", "link para pagar",
                "cóbrale", "cobrale", "cobrar a", "link de pagamento", "link pra pagar", "cobra o", "cobrar o", "lien de paiement",
                "zahlungslink", "bezahllink", "link pambayad", "link para magbayad", "tautan pembayaran", "link bayar",
                "link pembayaran", "भुगतान लिंक", "पेमेंट लिंक", "رابط دفع", "رابط الدفع", "支払いリンク", "決済リンク",
                "付款链接", "收款链接", "支付链接"),
    "review": ("review", "reviews", "reseña", "reseñas", "resena", "opinión", "opiniones", "avaliação", "avaliacao", "avaliações",
               "avis", "bewertung", "rebyu", "ulasan", "समीक्षा", "रिव्यू", "تقييم", "مراجعة", "レビュー", "口コミ", "评价", "好评"),
    "winback": ("hasn't come back", "haven't come back", "not come back", "didn't come back", "quiet customers", "win back",
                "winback", "promo", "promotion", "no han vuelto", "no ha vuelto", "no regresan", "no han regresado", "promoción",
                "promocion", "não voltaram", "nao voltaram", "sumiram", "promoção", "promocao", "pas revenus", "pas revenu",
                "nicht wiedergekommen", "nicht mehr gekommen", "angebot schicken", "hindi na bumalik", "di na bumalik",
                "belum kembali", "tidak kembali", "tidak datang lagi", "वापस नहीं आए", "प्रोमो", "ऑफ़र भेजो", "لم يعودوا", "لم يرجعوا",
                "عرض ترويجي", "来ていない", "戻ってこない", "キャンペーン", "没回来", "没再来", "优惠", "促销"),
    "cancel": ("cancel", "cancela", "cancelar", "anula", "annule", "annuler", "storniere", "absagen", "stornieren", "kanselahin",
               "i-cancel", "batalkan", "batal", "रद्द", "إلغاء", "الغ", "キャンセル", "取消"),
    "move": ("move", "reschedule", "change the time", "mueve", "mover", "cambia la cita", "cambiar la cita", "reagenda", "reagendar",
             "pasa a", "remarcar", "remarca", "mudar", "déplace", "décale", "deplace", "decale", "verschieb", "verleg", "ilipat",
             "palitan ang oras", "pindah", "pindahkan", "ganti jadwal", "समय बदलो", "शिफ्ट", "انقل", "غيّر الموعد", "غير الموعد",
             "移動", "ずらして", "変更して", "改到", "改期", "挪到"),
    "block": ("block", "bloquea", "bloquear", "bloqueia", "bloquear", "bloque", "blockier", "sperr", "i-block", "harangan",
              "blokir", "ब्लॉक", "احجب", "احجز لي وقت", "ブロック", "空けて", "屏蔽", "占用", "空出"),
    "reply": ("reply", "answer", "tell", "respond", "responde", "contesta", "dile", "dígale", "digale", "responda", "diga a",
              "diz a", "diz ao", "réponds", "reponds", "dis à", "dis a", "antworte", "sag ", "sagutin", "sabihin", "balas", "jawab",
              "bilang ke", "bilang sama", "जवाब", "बोलो", "बता दो", "رد على", "قل ل", "أخبر", "返信", "伝えて", "回复", "告诉"),
    "visit": ("came in", "visit", "visita", "vino", "veio", "visite", "est venu", "besuch", "war da", "bisita", "dumating",
              "kunjungan", "datang", "विज़िट", "आया", "आई", "زيارة", "زار", "来店", "来た", "到店", "来了"),
    "hours": ("hours", "opening", "we open", "we close", "closed on", "horario", "horarios", "abrimos", "cerramos", "cerrado",
              "horário", "fechamos", "fechado", "horaires", "nous ouvrons", "nous fermons", "fermé", "öffnungszeiten",
              "geschlossen", "oras ng", "bukas kami", "sarado", "jam buka", "jam tutup", "tutup", "समय", "खुला", "बंद रहेगा",
              "ساعات العمل", "مغلق", "نفتح", "営業時間", "休み", "营业时间", "休息", "关门"),
    "info": ("info", "information", "información", "informacion", "informação", "renseignement", "infos", "auskunft", "impormasyon",
             "informasi", "जानकारी", "معلومات", "情報", "信息"),
    "helpers": ("set it up", "set up for me", "setup for me", "someone to set", "help me set", "configúralo", "configuralo",
                "que alguien", "me lo configure", "configura por mim", "alguém configure", "configurer pour moi", "quelqu'un pour",
                "einrichten lassen", "jemand einrichten", "i-setup", "may mag-setup", "bantu atur", "bantu setel", "सेट अप कर",
                "कोई सेट", "إعداد لي", "يساعدني في الإعداد", "設定して", "設定を手伝", "帮我设置", "找人设置"),
    "new": ("anything new", "what's new", "whats new", "news", "algo nuevo", "qué hay de nuevo", "que hay de nuevo", "novedades",
            "alguma novidade", "novidades", "o que há de novo", "quoi de neuf", "du nouveau", "was gibt's neues", "was gibts neues",
            "neuigkeiten", "neues", "may bago", "ano'ng bago", "anong bago", "ada yang baru", "ada kabar", "kabar baru", "कुछ नया",
            "नया क्या", "جديد", "أي جديد", "何か新しい", "新着", "何かある", "有什么新", "新消息", "有新的"),
    "tomorrow": ("tomorrow", "mañana", "manana", "amanhã", "amanha", "demain", "morgen", "bukas", "besok", "कल", "غدا", "غدًا",
                 "بكرة", "明日", "明天"),
    "today": ("today", "summary", "my day", "hoy", "resumen", "mi día", "hoje", "resumo", "aujourd'hui", "aujourd", "résumé",
              "heute", "zusammenfassung", "ngayon", "buod", "hari ini", "ringkasan", "आज", "सारांश", "اليوم", "ملخص", "今日",
              "まとめ", "今天", "总结", "agenda"),
    "post": ("post", "publica", "publicar", "postea", "publique", "publie", "poste", "posten", "veröffentlich", "i-post",
             "unggah", "posting", "पोस्ट", "انشر", "نشر", "投稿", "发布", "发帖"),
    "help": ("help", "ayuda", "ajuda", "aide", "hilfe", "tulong", "bantuan", "मदद", "مساعدة", "ヘルプ", "助けて", "帮助"),
}
FOR = r"(?:\bfor\b|\bpor\b|\bpara\b|\bpour\b|\bfür\b|\bpara sa\b|\buntuk\b|के लिए|مقابل|の分|用于|作为)"


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"^(hey |hi |hola |oi |olá |ola )?valeria[\s,:!.\-]*", "", t)
    t = re.sub(r"[\s,:!.\-]*valeria[\s!.]*$", "", t)
    return " ".join(t.strip(".!¡?¿, ").split())


def _has(t: str, key: str) -> bool:
    return any(w in t for w in W[key])


def rules(text: str) -> dict:
    """Keyword rules -> {"intent": …} (the intent may be None: not sure)."""
    t = _norm(text)
    if t in STOP:
        return {"intent": "stop"}
    if t in RESUME:
        return {"intent": "resume"}
    if t in UNDO:
        return {"intent": "undo"}
    from . import style
    if _has(t, "log"):
        return {"intent": "log"}
    if _has(t, "style_ask"):
        return {"intent": "style"}
    ch = style.change_from(t)
    if ch and len(t.split()) <= 8:
        return {"intent": "style", "style": ch}
    if _has(t, "owes") and _has(t, "pay_remind"):
        return {"intent": "remind"}
    if _has(t, "paylink"):
        return {"intent": "paylink"}
    if _has(t, "owes"):
        return {"intent": "owes"}
    for k in ("helpers", "review", "winback", "cancel", "move", "block", "visit", "reply", "hours", "info", "new", "tomorrow",
              "today", "post", "help"):
        if _has(t, k):
            return {"intent": k}
    return {"intent": None}


# ---------------------------------------------------------------- the business's own AI reads it
def _ai_intent(biz: dict, text: str, http=None, now: datetime | None = None) -> dict | None:
    from . import ai as AI
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
    if not creds:
        return None
    prov, key, model = creds
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(biz["timezone"]))
    system = (
        f"You turn a small-business owner's message to their assistant into ONE command, as JSON only. Business: {biz['name']}. "
        f"Now: {local.strftime('%A %Y-%m-%d %H:%M')} ({biz['timezone']}). Currency {biz['currency']}. The message may mix languages.\n"
        "Intents: today, tomorrow, new (anything new?), reply (answer a customer), winback (customers who haven't come back / "
        "send a promo), move (a booking to another time), cancel (a booking), block (block time in the calendar), review "
        "(ask yesterday's customers for a review), post (post a photo), owes (who owes me), remind (remind people who owe), "
        "paylink (send someone a payment link), hours (change opening hours), info (a fact to tell customers), log (what did you "
        "do today), style (how do I sound / be more formal / no emojis), visit (a customer came in: count a visit), helpers "
        "(someone to set it up for me), stop, resume, undo, help, unsure.\n"
        'JSON: {"intent": "...", "who": "customer name or empty", "text": "the words to send (reply) or the fact (info)", '
        '"amount": number or null, "for": "what a payment is for", "when": "YYYY-MM-DDTHH:MM local or empty", "minutes": number '
        'or null, "hours": {"mon": [["09:00","18:00"]]} only the days that change ([] = closed), "style": {"tone": '
        '"formal|friendly|casual", "emoji": "none|some|lots"} only what changes}. Never invent names, amounts or times.')
    try:
        raw = AI._call(prov, key, model.split(",")[0].strip(), system, [("user", text[:2000])], AI._http(http), max_tokens=500)
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else None
    except Exception:                                         # noqa: BLE001 — busy or odd answer: the rules read it
        return None
    if not isinstance(d, dict):
        return None
    if d.get("intent") == "unsure":
        d["intent"] = None
    if d.get("intent") not in (*INTENTS, None):
        return None
    return d


def understand(biz: dict, text: str, http=None, now: datetime | None = None) -> dict:
    """The intent: whole-message words first (stop/undo are never left to an AI), then the AI, then the rules."""
    r = rules(text)
    if r["intent"] in ("stop", "resume", "undo"):
        return r
    d = _ai_intent(biz, text, http, now)
    if d and d.get("intent"):
        d["by_ai"] = True
        return d
    return r


# ---------------------------------------------------------------- helpers
def _lang(biz: dict, number: dict | None, text: str) -> str:
    """The owner's own language — unless what they wrote is clearly another one (a script, or 2+ telling words):
    a short command ("responde a Ana") must not flip her into Portuguese."""
    from ..web.i18n import ui_lang
    from .ai import _WORDS, detect_language
    got = detect_language(text or "")
    if got in ("ja", "zh", "hi", "ar", "ko", "th"):
        return ui_lang(got) if ui_lang(got) == got else OW.lang_of(biz, number)
    words = [w.strip("¿?¡!.,;:()\"'") for w in (text or "").lower().split()]
    scores = sorted(((sum(w in set(v.split()) for w in words), k) for k, v in _WORDS.items()), reverse=True)
    if scores and scores[0][0] >= 2 and scores[0][0] > scores[1][0] and ui_lang(scores[0][1]) == scores[0][1]:
        return scores[0][1]
    return OW.lang_of(biz, number)


def _who(number: dict | None, user: dict | None = None) -> str:
    if number and number.get("id"):
        return f"owner:{number['id']}"
    return f"user:{user['id']}" if user else "owner:web"


def _fmt(biz: dict, minor) -> str:
    return money.fmt(int(minor), biz["currency"], packs.country(biz["country"]).locale)


def _contacts(conn, biz: dict, text: str, d: dict | None = None) -> list[dict]:
    """The customers the owner means: the one picked from "which one?", the name the AI read, or any first name
    that appears in the message."""
    C = db.contacts
    d = d or {}
    if d.get("_contact"):
        r = conn.execute(select(C).where(C.c.business_id == biz["id"], C.c.id == UUID(d["_contact"]), C.c.deleted_at.is_(None))).mappings().first()
        return [dict(r)] if r else []
    who = d.get("who")
    if who:
        from . import contacts as CS
        return CS.search(conn, biz["id"], q=who.strip()[:60], limit=5)
    words = {w.strip(".,:;!?¿¡\"'()").lower() for w in (text or "").split()}
    words = [w for w in words if len(w) >= 2][:40]
    if not words:
        return []
    first = func.lower(func.split_part(C.c.name, " ", 1))
    return [dict(r) for r in conn.execute(select(C).where(C.c.business_id == biz["id"], C.c.deleted_at.is_(None), C.c.name != "",
                                                          first.in_(words)).order_by(C.c.updated_at.desc()).limit(5)).mappings()]


def _pick_or(conn, biz, number, intent: dict, found: list[dict], lang: str, now) -> dict | None:
    """Several customers with that name: 2–3 taps (a small question); none: say so."""
    if len(found) == 1:
        return None
    if not found:
        return {"text": OW.t("va_who", lang)}
    names = [f"{c['name']}" + (f" · {c['phone'][-4:]}" if c.get("phone") else "") for c in found[:3]]
    OW.ask(conn, biz, "pick", OW.t("va_which", lang), {"intent": intent, "ids": [str(c["id"]) for c in found[:3]], "names": names},
           number_id=(number or {}).get("id"), by=_who(number), prompted=True, now=now)
    return {"text": OW.t("va_which", lang), "options": names}


_TIME = re.compile(r"(?<![\d:])(\d{1,2})(?:[:h.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|hrs|h)?(?![\d])", re.I)


def _times(text: str) -> list[tuple[int, int]]:
    out = []
    for m in _TIME.finditer(text or ""):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and 1 <= h <= 7:                         # "a las 4" in a shop's day means the afternoon
            h += 12
        if 0 <= h <= 23 and 0 <= mi <= 59 and (m.group(2) or ap or len(m.group(1)) <= 2):
            out.append((h, mi))
    return out


def _when(biz: dict, text: str, base_day, now: datetime, ai: dict | None = None) -> datetime | None:
    tz = ZoneInfo(biz["timezone"])
    if ai and ai.get("when"):
        try:
            return datetime.fromisoformat(str(ai["when"])).replace(tzinfo=None).replace(tzinfo=tz)
        except ValueError:
            pass
    ts = _times(re.sub(r"\d[\d,.]*\s*(?:%|pesos|usd|mxn|php|inr)", "", text or "", flags=re.I))
    if not ts:
        return None
    day = base_day
    if _has(_norm(text), "tomorrow"):
        day = now.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(day, time(*ts[0]), tz)


def _amount(biz: dict, text: str, ai: dict | None) -> int | None:
    v = (ai or {}).get("amount")
    if v in (None, ""):
        m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", text or "")
        if not m:
            return None
        v = m.group(1).replace(",", "")
    try:
        n = money.to_minor(str(v), biz["currency"])
    except Exception:                                         # noqa: BLE001
        return None
    return n if n > 0 else None


def _next_appt(conn, biz: dict, contact_id, now: datetime) -> dict | None:
    AP = db.appointments
    r = conn.execute(select(AP).where(AP.c.business_id == biz["id"], AP.c.contact_id == contact_id, AP.c.status.in_(("booked", "confirmed")),
                                      AP.c.starts_at > now - timedelta(hours=1)).order_by(AP.c.starts_at).limit(1)).mappings().first()
    return dict(r) if r else None


def _confirm(conn, biz, number, kind: str, summary: str, payload: dict, lang: str, now, contact_id=None, user=None) -> dict:
    """A customer-facing action the owner asked for: the exact words and one tap — or, granted, straight to the queue."""
    s = OW.settings(conn, biz["id"])
    go = OW.alone(s, kind) or (kind in ("block_time",))
    a = OW.ask(conn, biz, kind, summary, payload, contact_id=contact_id, number_id=(number or {}).get("id"),
               by=_who(number, user), prompted=not go, now=now, owner_says_go=go)
    if go:
        return {"text": OW.t("va_queued", lang), "queued": str(a["id"])}
    from . import actions
    return {"text": summary, "options": [OW.t(f"va_c_{c}", lang) for c in actions.choices(kind)]}


# ---------------------------------------------------------------- the intents
def _summary(conn, biz: dict, days: int, lang: str, now: datetime) -> str:
    from . import deals as D
    from . import reminders as RM
    tz = ZoneInfo(biz["timezone"])
    day = now.astimezone(tz).date() + timedelta(days=days)
    start = datetime.combine(day, time(0), tz)
    appts = [a for a in D.appointments(conn, biz["id"], start=start, end=start + timedelta(days=1)) if a["status"] in ("booked", "confirmed")]
    lines = [OW.t("va_today_head" if days == 0 else "va_tomorrow_head", lang).format(n=len(appts))]
    for a in appts[:6]:
        lines.append(f"· {a['starts_at'].astimezone(tz).strftime('%H:%M')} {a['title']}" + (" ✓" if a["status"] == "confirmed" else ""))
    unc = RM.unconfirmed(conn, biz, day)
    if unc:
        lines.append(OW.t("va_unconfirmed", lang).format(n=len(unc), names=", ".join((u["name"] or "—").split(" ")[0] for u in unc[:4])))
    w = _waiting(conn, biz)
    if days == 0 and w:
        lines.append(OW.t("va_waiting", lang).format(n=len(w), names=", ".join((c["contact_name"] or "—").split(" ")[0] for c in w[:4])))
    return "\n".join(lines)


def _waiting(conn, biz: dict) -> list[dict]:
    from . import inbox as X
    M = db.messages
    out = []
    for c in X.conversations(conn, biz["id"], status="open", limit=100):
        last = conn.execute(select(M.c.direction).where(M.c.conversation_id == c["id"]).order_by(M.c.created_at.desc()).limit(1)).scalar()
        if last == "in":
            out.append(c)
    return out


def _new(conn, biz, number, lang, now) -> dict:
    w = _waiting(conn, biz)
    qs = OW.waiting(conn, biz["id"], number)
    if not w and not qs:
        return {"text": OW.t("va_nothing_new", lang)}
    lines = []
    if w:
        lines.append(OW.t("va_waiting", lang).format(n=len(w), names=", ".join((c["contact_name"] or "—").split(" ")[0] for c in w[:5])))
    if qs:
        lines.append(OW.t("va_open_questions", lang).format(n=len(qs)))
    return {"text": "\n".join(lines)}


def _log(conn, biz, lang, now) -> dict:
    """What did you do today? — honestly: what she sent, asked, and what is still waiting."""
    E, SM = db.events, db.scheduled_messages
    tz = ZoneInfo(biz["timezone"])
    start = datetime.combine(now.astimezone(tz).date(), time(0), tz)
    ev = dict(conn.execute(select(E.c.type, func.count()).where(E.c.business_id == biz["id"], E.c.created_at >= start,
                                                                E.c.type.in_(("ai.replied", "booking.chat", "answers.replied",
                                                                              "voice.transcribed", "appointment.confirmed")))
                           .group_by(E.c.type)).all())
    sent = conn.execute(select(func.count()).select_from(SM).where(SM.c.business_id == biz["id"], SM.c.status == "sent",
                                                                   SM.c.send_at >= start)).scalar()
    acts = OW.done_today(conn, biz, now)
    done = [a for a in acts if a["status"] == "done" and a["kind"] not in ("summary", "pick")]
    asked = [a for a in acts if a["status"] in ("asked", "later")]
    failed = [a for a in acts if a["status"] == "failed"]
    lines = [OW.t("va_log_head", lang)]
    pairs = (("ai.replied", "va_log_ai"), ("answers.replied", "va_log_answers"), ("booking.chat", "va_log_booked"),
             ("voice.transcribed", "va_log_voice"), ("appointment.confirmed", "va_log_confirmed"))
    for k, key in pairs:
        if ev.get(k):
            lines.append("· " + OW.t(key, lang).format(n=ev[k]))
    if sent:
        lines.append("· " + OW.t("va_log_followups", lang).format(n=sent))
    for a in done[:6]:
        lines.append("· ✓ " + ((a["result"] or {}).get("say") or a["kind"])[:120])
    for a in failed[:3]:
        lines.append("· ✗ " + ((a["result"] or {}).get("say") or a["kind"])[:120])
    if asked:
        lines.append("· " + OW.t("va_open_questions", lang).format(n=len(asked)))
    if len(lines) == 1:
        lines.append(OW.t("va_log_nothing", lang))
    return {"text": "\n".join(lines)}


def _help(lang: str) -> dict:
    return {"text": OW.t("va_help", lang)}


def _menu(conn, biz, number, lang, now, user=None) -> dict:
    """Not sure what they meant: three taps instead of a guess."""
    opts = ["today", "new", "help"]
    OW.ask(conn, biz, "pick", OW.t("va_unsure", lang), {"menu": opts, "names": [OW.t(f"va_m_{o}", lang) for o in opts]},
           number_id=(number or {}).get("id"), by=_who(number, user), prompted=True, now=now)
    return {"text": OW.t("va_unsure", lang), "options": [OW.t(f"va_m_{o}", lang) for o in opts]}


def _reply_cmd(conn, biz, number, d, text, lang, now, user=None, http=None) -> dict:
    from . import style
    found = _contacts(conn, biz, text, d)
    if not found:                                              # "reply to the one waiting": when only one is waiting
        w = _waiting(conn, biz)
        if len(w) == 1:
            found = [{"id": w[0]["contact_id"], "name": w[0]["contact_name"] or "—"}]
    pick = _pick_or(conn, biz, number, {**d, "intent": "reply", "_text": text}, found, lang, now)
    if pick:
        return pick
    c = found[0]
    words = (d.get("text") or "").strip()
    if not words:
        m = re.search(r"[:：]\s*(.+)$", text or "", re.S)
        words = m.group(1).strip() if m else ""
    CV = db.conversations
    conv = conn.execute(select(CV.c.id).where(CV.c.business_id == biz["id"], CV.c.contact_id == c["id"])
                        .order_by(CV.c.last_message_at.desc().nulls_last()).limit(1)).scalar()
    first = (c["name"] or "—").split(" ")[0]
    if not words:
        OW.ask(conn, biz, "reply", OW.t("va_q_reply_what", lang).format(name=first), {"conversation_id": str(conv) if conv else None},
               contact_id=c["id"], number_id=(number or {}).get("id"), by=_who(number, user), prompted=True, now=now)
        return {"text": OW.t("va_q_reply_what", lang).format(name=first)}
    p = style.get(conn, biz["id"])
    out = style.apply(p, words)
    return _confirm(conn, biz, number, "reply", OW.t("va_q_reply", lang).format(name=first, text=out),
                    {"conversation_id": str(conv) if conv else None, "text": out, "draft": out}, lang, now, contact_id=c["id"], user=user)


def _review_cmd(conn, biz, number, lang, now, user=None) -> dict:
    from . import reviews as RV
    if not biz.get("review_url"):
        return {"text": OW.t("va_need_review_link", lang).format(link=f"{config.PUBLIC_URL()}/app/followups#reviews")}
    tz = ZoneInfo(biz["timezone"])
    y = now.astimezone(tz).date() - timedelta(days=1)
    start = datetime.combine(y, time(0), tz)
    AP, C = db.appointments, db.contacts
    rows = conn.execute(select(C.c.id, C.c.name).join(AP, AP.c.contact_id == C.c.id).where(
        AP.c.business_id == biz["id"], AP.c.starts_at >= start, AP.c.starts_at < start + timedelta(days=1),
        AP.c.status.in_(("done", "confirmed", "booked")), C.c.deleted_at.is_(None), C.c.marketing_consent,
        C.c.unsubscribed_at.is_(None)).group_by(C.c.id, C.c.name)).all()
    ids = [r for r in rows if not RV.recently_asked(conn, biz["id"], r.id, now)]
    if not ids:
        return {"text": OW.t("va_no_review_people", lang)}
    names = ", ".join((r.name or "—").split(" ")[0] for r in ids[:5])
    return _confirm(conn, biz, number, "review_ask", OW.t("va_q_review", lang).format(n=len(ids), names=names),
                    {"contact_ids": [str(r.id) for r in ids]}, lang, now, user=user)


def _winback_cmd(conn, biz, number, lang, now, user=None) -> dict:
    from . import growth as GR
    if not blocks.on(biz, "grow"):
        return {"text": OW.t("va_block_off", lang).format(link=f"{config.PUBLIC_URL()}/app/start")}
    q = GR.quiet(conn, biz, now)
    if not q["count"]:
        return {"text": OW.t("va_no_quiet", lang)}
    names = ", ".join((r["name"] or "—").split(" ")[0] for r in q["rows"][:5])
    text = OW.t("va_quiet", lang).format(n=q["count"], days=q["quiet_days"], names=names, ready=q["ready"])
    if not q["ready"]:
        return {"text": text + "\n" + OW.t("va_quiet_none_ready", lang)}
    return _confirm(conn, biz, number, "promo", text + "\n" + OW.t("va_q_winback", lang).format(n=q["ready"]),
                    {"mode": "winback"}, lang, now, user=user)


def _owes_cmd(conn, biz, number, lang, now, remind: bool, user=None) -> dict:
    from . import payments as PM
    rows = PM.unpaid(conn, biz["id"])
    if not rows:
        return {"text": OW.t("va_nobody_owes", lang)}
    lines = [OW.t("va_owes_head", lang).format(n=len(rows))]
    for r in rows[:6]:
        lines.append(f"· {(r['contact_name'] or '—').split(' ')[0]}: {_fmt(biz, r['amount_minor'])} — {r['description'][:40]}")
    reach = [r for r in rows if r["contact_id"]]
    if not reach:
        return {"text": "\n".join(lines)}
    return _confirm(conn, biz, number, "pay_reminder", "\n".join(lines) + "\n" + OW.t("va_q_payrem", lang).format(n=len(reach)),
                    {"payment_ids": [str(r["id"]) for r in reach]}, lang, now, user=user)


def _paylink_cmd(conn, biz, number, d, text, lang, now, user=None) -> dict:
    from . import payments as PM
    if not blocks.on(biz, "payments"):
        return {"text": OW.t("va_block_off", lang).format(link=f"{config.PUBLIC_URL()}/app/start")}
    if not PM.can_link(conn, biz["id"]):
        return {"text": OW.t("va_need_payments", lang).format(link=f"{config.PUBLIC_URL()}/app/settings#payments")}
    amount = _amount(biz, text, d)
    found = _contacts(conn, biz, text, d)
    pick = _pick_or(conn, biz, number, {**d, "intent": "paylink", "_text": text}, found, lang, now)
    if pick:
        return pick
    if not amount:
        return {"text": OW.t("va_how_much", lang)}
    what = (d.get("for") or "").strip()
    if not what:
        m = re.search(FOR + r"\s+(.+)$", text or "", re.I)
        what = m.group(1).strip() if m else OW.t("va_pay_default", biz["language"])
    c = found[0]
    first = (c["name"] or "—").split(" ")[0]
    return _confirm(conn, biz, number, "paylink", OW.t("va_q_paylink", lang).format(name=first, amount=_fmt(biz, amount), what=what[:100]),
                    {"amount_minor": amount, "description": what[:300]}, lang, now, contact_id=c["id"], user=user)


def _booking_cmd(conn, biz, number, d, text, lang, now, which: str, user=None) -> dict:
    from . import reminders as RM
    if not blocks.on(biz, "calendar"):
        return {"text": OW.t("va_block_off", lang).format(link=f"{config.PUBLIC_URL()}/app/start")}
    found = _contacts(conn, biz, text, d)
    pick = _pick_or(conn, biz, number, {**d, "intent": which, "_text": text}, found, lang, now)
    if pick:
        return pick
    c = found[0]
    first = (c["name"] or "—").split(" ")[0]
    ap = _next_appt(conn, biz, c["id"], now)
    if not ap:
        return {"text": OW.t("va_no_booking", lang).format(name=first)}
    if which == "cancel":
        return _confirm(conn, biz, number, "booking_cancel", OW.t("va_q_cancel", lang).format(name=first, when=RM.when_text(biz, ap["starts_at"], lang)),
                        {"appointment_id": str(ap["id"]), "tell": True}, lang, now, contact_id=c["id"], user=user)
    st = _when(biz, text, ap["starts_at"].astimezone(ZoneInfo(biz["timezone"])).date(), now, d if d.get("by_ai") else None)
    if not st:
        return {"text": OW.t("va_what_time", lang)}
    return _confirm(conn, biz, number, "booking_move", OW.t("va_q_move", lang).format(
        name=first, old=RM.when_text(biz, ap["starts_at"], lang), new=RM.when_text(biz, st, lang)),
        {"appointment_id": str(ap["id"]), "starts_at": st.isoformat(), "tell": True}, lang, now, contact_id=c["id"], user=user)


def _block_cmd(conn, biz, number, d, text, lang, now, user=None) -> dict:
    if not blocks.on(biz, "calendar"):
        return {"text": OW.t("va_block_off", lang).format(link=f"{config.PUBLIC_URL()}/app/start")}
    tz = ZoneInfo(biz["timezone"])
    st = _when(biz, text, now.astimezone(tz).date(), now, d if d.get("by_ai") else None)
    if not st:
        return {"text": OW.t("va_what_time", lang)}
    minutes = d.get("minutes")
    ts = _times(text)
    if not minutes and len(ts) >= 2:
        end = datetime.combine(st.date(), time(*ts[1]), tz)
        minutes = int((end - st).total_seconds() // 60) if end > st else None
    minutes = int(minutes or 60)
    if not 5 <= minutes <= 1440:
        minutes = 60
    return _confirm(conn, biz, number, "block_time", "", {"starts_at": st.isoformat(), "minutes": minutes}, lang, now, user=user)


def _hours_cmd(conn, biz, number, d, text, lang, now, user=None) -> dict:
    from . import booking as BK
    hours = d.get("hours") if d.get("by_ai") and isinstance(d.get("hours"), dict) else None
    if hours:
        try:
            clean = BK.clean_hours({k: v for k, v in hours.items() if v})
        except Invalid:
            clean = None
        if clean is not None:
            closed = [k for k, v in hours.items() if not v and k in BK.DAYS]
            payload = {"hours": {**clean, **{k: [] for k in closed}}}
            line = "; ".join(f"{k} " + (", ".join(f"{a}-{b}" for a, b in v) if v else OW.t("va_closed", lang))
                             for k, v in payload["hours"].items())
            return _confirm(conn, biz, number, "hours", OW.t("va_q_hours", lang).format(hours=line), payload, lang, now, user=user)
    note = (d.get("text") or "").strip() or (text or "").strip()
    return _confirm(conn, biz, number, "info", OW.t("va_q_info", lang).format(text=note[:300]), {"text": note[:500]}, lang, now, user=user)


def _style_cmd(conn, biz, d, text, lang, user=None) -> dict:
    from . import style
    ch = d.get("style") if isinstance(d.get("style"), dict) else style.change_from(text)
    ch = {k: v for k, v in (ch or {}).items() if k in ("tone", "emoji") and v in (style.TONES + style.EMOJI)}
    if ch:
        p = style.save(conn, biz, _who(None, user) if user else "valeria", ch)
        return {"text": OW.t("va_style_changed", lang) + "\n" + style.describe(p, lang)}
    return {"text": OW.t("va_style_head", lang) + "\n" + style.describe(style.get(conn, biz["id"]), lang)
            + "\n" + OW.t("va_style_edit", lang).format(link=f"{config.PUBLIC_URL()}/app/valeria#style")}


def _visit_cmd(conn, biz, number, d, text, lang, now, user=None) -> dict:
    from . import loyalty as LY
    found = _contacts(conn, biz, text, d)
    pick = _pick_or(conn, biz, number, {**d, "intent": "visit", "_text": text}, found, lang, now)
    if pick:
        return pick
    r = LY.mark_visit(conn, biz, _who(number, user), found[0]["id"], now)
    first = (found[0]["name"] or "—").split(" ")[0]
    out = OW.t("va_visit_marked", lang).format(name=first, n=r["visits"])
    if r["reward"]:
        out += "\n" + OW.t("va_visit_reward", lang).format(reward=r["reward"]["reward"])
    return {"text": out}


# ---------------------------------------------------------------- answers to her questions
def _norm_ans(t: str) -> str:
    return _norm(t).strip(")")


def answer_of(text: str, kind: str, payload: dict) -> tuple[str, str | None] | None:
    """(yes|no|later, the choice) for the open question; None = not an answer."""
    from . import actions
    t = _norm_ans(text)
    if kind == "pick":                                         # "which one?": a number, or the name itself
        names = payload.get("names") or []
        if t.isdigit() and 1 <= int(t) <= len(names):
            return "yes", str(int(t) - 1)
        if t in NO:
            return "no", None
        if t in LATER:
            return "later", None
        for i, n in enumerate(names):
            if t and t == n.lower():
                return "yes", str(i)
        return None
    ch = actions.choices(kind)
    if kind == "came" and t in NO:                             # sch01: "did Juan come?" "no" = a no-show (still marked)
        return "yes", "no_show"
    if t.isdigit():
        i = int(t)
        if not 1 <= i <= len(ch):
            return None
        c = ch[i - 1]
        return ("no", None) if c == "no" else ("later", None) if c == "later" else ("yes", c)
    if t in YES:
        return "yes", ch[0]
    if t in NO:
        return "no", None
    if t in LATER:
        return "later", None
    return None


def _answered(conn, biz, number, q: dict, text: str, lang: str, now, user=None, http=None) -> dict | None:
    """The owner answered her open question (or gave the words she asked for)."""
    ans = answer_of(text, q["kind"], q["payload"] or {})
    by = _who(number, user)
    if q["kind"] == "pick":
        if not ans:
            return None
        conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.id == q["id"]).values(
            status="done" if ans[0] == "yes" else "declined", decided_at=now, done_at=now))
        if ans[0] != "yes":
            return {"text": OW.t("va_ok", lang)}
        p = q["payload"] or {}
        i = int(ans[1])
        if p.get("menu"):
            return {"redo": {"intent": p["menu"][i]}, "text": ""}
        intent = dict(p.get("intent") or {})
        intent["_contact"] = p["ids"][i]
        return {"redo": intent, "text": ""}
    if not ans:
        p = q["payload"] or {}
        asked_words = (q["kind"] == "reply" and not p.get("text")) or (q["kind"] == "post" and p.get("needs_text"))
        wants_text = q["kind"] == "reply" or asked_words
        # she asked for the words: anything but a one-word command is them; else only what isn't another command
        is_words = (asked_words and len(text.split()) > 1) or rules(text)["intent"] in (None, "reply")
        if wants_text and is_words and len(text.strip()) >= 2:
            from . import style
            words = text.strip()
            if q["kind"] == "post":
                from . import posts as PO
                post = PO.get(conn, biz["id"], UUID(q["payload"]["post_id"]))
                PO.save(conn, biz, by, post["id"], texts={n: words for n in post["networks"]})
                conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.id == q["id"]).values(
                    payload={**q["payload"], "needs_text": False}, summary=OW.t("va_q_post", lang).format(text=words[:600])))
                return {"text": OW.t("va_q_post", lang).format(text=words[:600]),
                        "options": [OW.t(f"va_c_{c}", lang) for c in ("now", "schedule", "edit")]}
            draft = (q["payload"] or {}).get("draft")
            if draft:                                          # her draft, changed by the owner: kept to learn their voice
                style.record(conn, biz["id"], "assistant", draft, words)
            out = words if draft else style.apply(style.get(conn, biz["id"]), words)
            first = _first_name(conn, q["contact_id"])
            conn.execute(db.assistant_actions.update().where(db.assistant_actions.c.id == q["id"]).values(
                payload={**(q["payload"] or {}), "text": out, "draft": draft or out},
                summary=OW.t("va_q_reply", lang).format(name=first, text=out)))
            return {"text": OW.t("va_q_reply", lang).format(name=first, text=out),
                    "options": [OW.t(f"va_c_{c}", lang) for c in ("yes", "no", "later")]}
        return None
    if q["kind"] == "reply" and ans[0] == "yes" and not (q["payload"] or {}).get("text"):
        return {"text": OW.t("va_q_reply_what", lang).format(name=_first_name(conn, q["contact_id"]))}
    a = OW.decide(conn, biz, q["id"], ans[0], by, choice=ans[1], now=now)
    if ans[0] == "no":
        return {"text": OW.t("va_ok_no", lang)}
    if ans[0] == "later":
        return {"text": OW.t("va_ok_later", lang)}
    if a["send_at"] and a["send_at"] > now:
        return {"text": OW.t("va_queued", lang)}
    return {"run": str(a["id"]), "text": ""}


def _first_name(conn, contact_id) -> str:
    if not contact_id:
        return "—"
    n = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == contact_id)).scalar()
    return (n or "—").split(" ")[0] or "—"


# ---------------------------------------------------------------- the one door
def handle(biz: dict, number: dict | None, text: str, *, user: dict | None = None, http=None, now: datetime | None = None,
           _depth: int = 0) -> dict:
    """What the owner said -> {"text", "options"?}. number None = the owner on the web app (signed in)."""
    now = now or datetime.now(timezone.utc)
    text = (text or "").strip()[:4000]
    lang = _lang(biz, number, text)
    if not text:
        return _help(lang)
    from . import marketplace as MK                            # mkt01: "#3 ok, 6pm works" — an answer for a Marketplace buyer
    got = MK.owner_reply(biz, number, text, user=user, http=http, now=now) if _depth == 0 else None
    if got is not None:
        return got
    with db.tx() as conn:
        q = OW.open_question(conn, biz["id"], number, now)
        r = rules(text)
        if q and r["intent"] not in ("stop", "undo", "resume"):
            got = _answered(conn, biz, number, q, text, lang, now, user, http)
        else:
            got = None
    if got is not None:
        if got.get("run"):
            say = OW.run_now(biz, UUID(got["run"]), http=http, now=now)
            return {"text": say or OW.t("va_done", lang)}
        if got.get("redo") and _depth < 2:
            return _intent(biz, number, got["redo"], got["redo"].get("_text") or text, lang, user=user, http=http, now=now)
        return got
    d = understand(biz, text, http, now)
    return _intent(biz, number, d, text, lang, user=user, http=http, now=now)


def _intent(biz, number, d: dict, text: str, lang: str, *, user=None, http=None, now=None) -> dict:
    intent = d.get("intent")
    if intent and not OW.may(number, NEEDS.get(intent, "view")):
        return {"text": OW.t("va_not_allowed", lang)}
    by = _who(number, user)
    with db.tx() as conn:
        if intent == "stop":
            n = OW.pause(conn, biz, by, now)
            return {"text": OW.t("va_stopped", lang) + (" " + OW.t("va_stopped_n", lang).format(n=n) if n else "")}
        if intent == "resume":
            OW.resume(conn, biz, by)
            return {"text": OW.t("va_resumed", lang)}
        if intent == "undo":
            rows = OW.undo(conn, biz, by, now)
            return {"text": OW.t("va_undone", lang).format(n=len(rows)) if rows else OW.t("va_nothing_to_undo", lang)}
        if intent in ("today", "tomorrow"):
            return {"text": _summary(conn, biz, 0 if intent == "today" else 1, lang, now)}
        if intent == "new":
            return _new(conn, biz, number, lang, now)
        if intent == "log":
            return _log(conn, biz, lang, now)
        if intent == "help":
            return _help(lang)
        if intent == "style":
            return _style_cmd(conn, biz, d, text, lang, user)
        if intent == "helpers":
            return {"text": OW.t("va_helpers_link", lang).format(link=f"{config.PUBLIC_URL()}/app/helpers")}
        if intent == "post":
            return {"text": OW.t("va_send_photo", lang)}
        if intent == "reply":
            return _reply_cmd(conn, biz, number, d, text, lang, now, user, http)
        if intent == "review":
            return _review_cmd(conn, biz, number, lang, now, user)
        if intent == "winback":
            return _winback_cmd(conn, biz, number, lang, now, user)
        if intent in ("owes", "remind"):
            return _owes_cmd(conn, biz, number, lang, now, intent == "remind", user)
        if intent == "paylink":
            return _paylink_cmd(conn, biz, number, d, text, lang, now, user)
        if intent in ("move", "cancel"):
            return _booking_cmd(conn, biz, number, d, text, lang, now, intent, user)
        if intent == "block":
            got = _block_cmd(conn, biz, number, d, text, lang, now, user)
        elif intent in ("hours", "info"):
            return _hours_cmd(conn, biz, number, d, text, lang, now, user)
        elif intent == "visit":
            if not blocks.on(biz, "loyalty"):
                return {"text": OW.t("va_block_off", lang).format(link=f"{config.PUBLIC_URL()}/app/start")}
            return _visit_cmd(conn, biz, number, d, text, lang, now, user)
        else:
            return _menu(conn, biz, number, lang, now, user)
    if got.get("queued"):                                      # a calendar block: done now, no undo wait needed
        say = OW.run_now(biz, UUID(got["queued"]), http=http, now=now)
        return {"text": say or OW.t("va_done", lang)}
    return got


# ---------------------------------------------------------------- photo -> post
def photo(biz: dict, number: dict | None, raw: bytes, mime: str, caption: str, *, user=None, http=None,
          now: datetime | None = None) -> dict:
    """The owner sent a picture: a post drafted in their style (Posts), then 1 publish now · 2 schedule · 3 edit."""
    from . import ai as AI
    from . import posts as PO
    from . import style
    now = now or datetime.now(timezone.utc)
    lang = _lang(biz, number, caption)
    if not OW.may(number, "post"):
        return {"text": OW.t("va_not_allowed", lang)}
    if not blocks.on(biz, "posts"):
        return {"text": OW.t("va_block_off", lang).format(link=f"{config.PUBLIC_URL()}/app/start")}
    by = _who(number, user)
    with db.tx() as conn:
        f = PO.save_picture(conn, biz, by, raw, mime or "image/jpeg", "chat.jpg")
        nets = [n["network"] for n in PO.networks(conn, biz) if n["connected"] and not n["reconnect"]] or ["facebook"]
    idea = (caption or "").strip() or OW.t("va_photo_idea", biz["language"])
    texts, by_ai = {}, False
    try:
        texts, by_ai = PO.draft(biz, by, kind="product" if caption else "idea", idea=idea, nets=nets, http=http), True
    except (AI.AIError, Invalid):
        if caption:
            with db.tx() as conn:
                texts = {n: style.apply(style.get(conn, biz["id"]), caption.strip()) for n in nets}
    with db.tx() as conn:
        post = PO.create(conn, biz, by, kind="idea", idea=idea, nets=nets, texts=texts, file_id=f["id"], by_ai=by_ai)
        if not texts:
            OW.ask(conn, biz, "post", OW.t("va_q_post_words", lang), {"post_id": str(post["id"]), "needs_text": True},
                   number_id=(number or {}).get("id"), by=by, prompted=True, now=now)
            return {"text": OW.t("va_q_post_words", lang)}
        first = next(iter(texts.values()))
        OW.ask(conn, biz, "post", OW.t("va_q_post", lang).format(text=first[:600]), {"post_id": str(post["id"])},
               number_id=(number or {}).get("id"), by=by, prompted=True, now=now)
        connected = any(n["connected"] for n in PO.networks(conn, biz))
    out = {"text": OW.t("va_q_post", lang).format(text=first[:600]), "options": [OW.t(f"va_c_{c}", lang) for c in ("now", "schedule", "edit")]}
    if not connected:
        out["text"] += "\n" + OW.t("va_connect_social", lang).format(link=f"{config.PUBLIC_URL()}/app/connect")
    return out
