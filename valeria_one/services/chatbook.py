"""Booking inside the chat — on WhatsApp, Messenger, Instagram, SMS, Telegram, anything.

A customer writes "quiero una cita" / "book" / "予約"… and the conversation walks them through it:
service -> day -> time -> (phone, if the channel doesn't give one) -> booked. WhatsApp shows tap-to-pick
lists; every other channel gets numbered options ("reply with a number"). Uses the same rules as the public
booking page (hours, notice, no double booking — checked again at the last second).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_time
from sqlalchemy import select, update

from .. import blocks, db, money, packs
from . import Blocked, Invalid, NotFound, log
from . import booking as BK
from .ai import detect_language

FLOW_MINUTES = 30
TRIGGERS = ("cita", "agendar", "reservar", "reservación", "reservacion", "turno", "book", "booking", "appointment",
            "reserve", "schedule", "agendamento", "marcar", "horário", "rendez-vous", "réserver", "reserver", "termin",
            "buchen", "mag-book", "magpa-book", "iskedyul", "janji", "pesan", "booking", "予約", "预约", "預約", "حجز", "موعد",
            "बुक", "अपॉइंटमेंट")
CANCEL = ("cancel", "cancelar", "salir", "stop", "parar", "annuler", "abbrechen", "kanselahin", "batal", "キャンセル",
          "取消", "إلغاء", "रद्द", "0")

P = {   # what the conversation says, per language (English fallback)
    "service": {"en": "Sure! What would you like to book?", "es": "¡Claro! ¿Qué te gustaría agendar?", "pt": "Claro! O que você quer agendar?",
                "fr": "Bien sûr ! Que souhaitez-vous réserver ?", "de": "Gern! Was möchten Sie buchen?", "fil": "Sige! Ano ang gusto mong i-book?",
                "id": "Tentu! Mau pesan apa?", "hi": "ज़रूर! आप क्या बुक करना चाहेंगे?", "ar": "بكل سرور! ماذا تودّ أن تحجز؟",
                "ja": "かしこまりました。ご予約内容をお選びください。", "zh": "好的！您想预约什么？"},
    "day": {"en": "Which day?", "es": "¿Qué día?", "pt": "Qual dia?", "fr": "Quel jour ?", "de": "Welcher Tag?", "fil": "Anong araw?",
            "id": "Hari apa?", "hi": "कौन सा दिन?", "ar": "أي يوم؟", "ja": "ご希望の日をお選びください。", "zh": "哪一天？"},
    "time": {"en": "What time?", "es": "¿A qué hora?", "pt": "Que horas?", "fr": "À quelle heure ?", "de": "Welche Uhrzeit?",
             "fil": "Anong oras?", "id": "Jam berapa?", "hi": "कितने बजे?", "ar": "في أي ساعة؟", "ja": "ご希望の時間をお選びください。", "zh": "几点？"},
    "phone": {"en": "Almost done — what's your phone number?", "es": "Ya casi: ¿cuál es tu número de teléfono?",
              "pt": "Quase lá — qual é o seu telefone?", "fr": "Presque fini — quel est votre numéro de téléphone ?",
              "de": "Fast geschafft — wie lautet Ihre Telefonnummer?", "fil": "Halos tapos na — ano ang phone number mo?",
              "id": "Hampir selesai — berapa nomor telepon Anda?", "hi": "बस हो गया — आपका फ़ोन नंबर क्या है?",
              "ar": "اقتربنا — ما رقم هاتفك؟", "ja": "最後に、お電話番号を教えてください。", "zh": "快好了——请告诉我您的电话号码。"},
    "booked": {"en": "Booked! {what} — {when}. See you then.", "es": "¡Listo! {what} — {when}. Te esperamos.",
               "pt": "Agendado! {what} — {when}. Até lá.", "fr": "C’est réservé ! {what} — {when}. À bientôt.",
               "de": "Gebucht! {what} — {when}. Bis dann.", "fil": "Naka-book na! {what} — {when}. Kita-kits.",
               "id": "Sudah dipesan! {what} — {when}. Sampai jumpa.", "hi": "बुक हो गया! {what} — {when}। मिलते हैं।",
               "ar": "تم الحجز! {what} — {when}. نراك قريبًا.", "ja": "ご予約が完了しました。{what} — {when}。お待ちしております。",
               "zh": "预约成功！{what} — {when}。到时见。"},
    "taken": {"en": "That time was just taken. Please pick another:", "es": "Esa hora se acaba de ocupar. Elige otra:",
              "pt": "Esse horário acabou de ser ocupado. Escolha outro:", "fr": "Ce créneau vient d’être pris. Choisissez-en un autre :",
              "de": "Diese Uhrzeit ist gerade vergeben worden. Bitte wählen Sie eine andere:", "fil": "Nakuha na ang oras na iyon. Pumili ng iba:",
              "id": "Jam itu baru saja terisi. Pilih yang lain:", "hi": "यह समय अभी-अभी बुक हो गया। कोई दूसरा चुनें:",
              "ar": "حُجز هذا الموعد للتو. اختر موعدًا آخر:", "ja": "その時間はちょうど埋まりました。別の時間をお選びください。",
              "zh": "该时间刚被预约，请选择其他时间："},
    "none": {"en": "There are no free times right now — someone from the team will write to you.",
             "es": "No hay horarios libres por ahora; alguien del equipo te escribirá.",
             "pt": "Não há horários livres agora — alguém da equipe vai te escrever.",
             "fr": "Aucun créneau libre pour le moment — quelqu’un de l’équipe vous écrira.",
             "de": "Gerade sind keine Zeiten frei — jemand aus dem Team meldet sich.",
             "fil": "Walang bakanteng oras ngayon — may susulat sa iyo mula sa team.",
             "id": "Belum ada jadwal kosong — tim kami akan menghubungi Anda.",
             "hi": "अभी कोई समय खाली नहीं है — टीम से कोई आपको लिखेगा।",
             "ar": "لا توجد مواعيد متاحة الآن — سيتواصل معك أحد أفراد الفريق.",
             "ja": "現在空きがありません。スタッフからご連絡いたします。", "zh": "目前没有空闲时间——团队成员会联系您。"},
    "again": {"en": "Sorry, I didn't get that — reply with one of the numbers:", "es": "Perdón, no te entendí; responde con uno de los números:",
              "pt": "Desculpe, não entendi — responda com um dos números:", "fr": "Désolé, je n’ai pas compris — répondez avec un des numéros :",
              "de": "Entschuldigung, das habe ich nicht verstanden — antworten Sie mit einer der Nummern:",
              "fil": "Pasensya, hindi ko nakuha — sumagot ng isa sa mga numero:", "id": "Maaf, saya kurang paham — balas dengan salah satu nomor:",
              "hi": "माफ़ कीजिए, समझ नहीं आया — किसी एक नंबर से जवाब दें:", "ar": "عذرًا، لم أفهم — ردّ برقم من الأرقام:",
              "ja": "すみません、番号でお答えください。", "zh": "抱歉，我没看懂——请回复其中一个数字："},
    "bad_phone": {"en": "That number doesn't look right — please send it again.", "es": "Ese número no parece correcto; envíalo de nuevo.",
                  "pt": "Esse número não parece certo — envie de novo.", "fr": "Ce numéro ne semble pas correct — renvoyez-le.",
                  "de": "Die Nummer scheint nicht zu stimmen — bitte erneut senden.", "fil": "Mukhang mali ang numero — pakipadala ulit.",
                  "id": "Nomor itu sepertinya salah — kirim lagi ya.", "hi": "यह नंबर सही नहीं लगता — कृपया फिर से भेजें।",
                  "ar": "يبدو أن الرقم غير صحيح — أرسله مرة أخرى.", "ja": "番号が正しくないようです。もう一度お送りください。",
                  "zh": "号码似乎不对——请重新发送。"},
    "cancelled": {"en": "No problem — cancelled. Write anytime.", "es": "Sin problema, cancelado. Escríbenos cuando quieras.",
                  "pt": "Sem problema — cancelado. Escreva quando quiser.", "fr": "Pas de souci — annulé. Écrivez quand vous voulez.",
                  "de": "Kein Problem — abgebrochen. Schreiben Sie jederzeit.", "fil": "Walang problema — kinansela. Sumulat kahit kailan.",
                  "id": "Tidak masalah — dibatalkan. Hubungi kapan saja.", "hi": "कोई बात नहीं — रद्द किया। कभी भी लिखें।",
                  "ar": "لا مشكلة — تم الإلغاء. راسلنا في أي وقت.", "ja": "承知しました。キャンセルしました。いつでもご連絡ください。",
                  "zh": "没问题——已取消。随时联系我们。"},
    "pick": {"en": "Choose", "es": "Elegir", "pt": "Escolher", "fr": "Choisir", "de": "Auswählen", "fil": "Pumili", "id": "Pilih",
             "hi": "चुनें", "ar": "اختر", "ja": "選ぶ", "zh": "选择"},
}


def say(key: str, lang: str, **kw) -> str:
    row = P[key]
    return (row.get(lang) or row["en"]).format(**kw)


def wants_booking(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in TRIGGERS)


def _numbered(lines: list[str]) -> str:
    return "\n".join(f"{i}) {x}" for i, x in enumerate(lines, 1))


def _choose(text: str, options: list[dict]) -> dict | None:
    """By number ("2"), by the tapped title, or by the start of a title."""
    t = (text or "").strip()
    m = re.match(r"^\s*(\d{1,2})\b", t)
    if m and 1 <= int(m.group(1)) <= len(options):
        return options[int(m.group(1)) - 1]
    low = t.lower()
    for o in options:
        if low and (low == o["title"].lower() or (len(low) >= 3 and o["title"].lower().startswith(low))):
            return o
    return None


def handle(business_id: UUID, conversation_id: UUID, now: datetime | None = None, http=None) -> str:
    """Called after a customer's message. Returns: off | not_booking | asked | booked | cancelled | none | failed."""
    from . import contacts as C
    from . import inbox as X
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        if not biz["booking_enabled"] or not blocks.on(biz, "calendar"):
            return "off"
        CV, M = db.conversations, db.messages
        conv = dict(conn.execute(select(CV).where(CV.c.id == conversation_id, CV.c.business_id == business_id)).mappings().one())
        last = conn.execute(select(M.c.direction, M.c.body).where(M.c.conversation_id == conversation_id)
                            .order_by(M.c.created_at.desc()).limit(1)).first()
        if not last or last.direction != "in":
            return "not_booking"
        text = last.body or ""
        flow = conv.get("flow") or None
        if flow and datetime.fromisoformat(flow["expires"]) < now:
            flow = None
        if not flow and not wants_booking(text):
            return "not_booking"
        if conv["needs_human"]:
            return "not_booking"
        contact = C.get(conn, business_id, conv["contact_id"])
        svcs = BK.services(conn, business_id, active_only=True)
    lang = (flow or {}).get("lang") or detect_language(text) or biz["language"]
    lang = lang if lang in P["service"] else "en"
    pack = packs.country(biz["country"])
    tz = ZoneInfo(biz["timezone"])
    loc = pack.locale if pack.locale.startswith(lang) else lang

    def reply(body: str, options: list[dict] | None = None, new_flow: dict | None = ...) -> str:
        if options:
            body = body + "\n\n" + _numbered([o["title"] + (f" · {o['description']}" if o.get("description") else "") for o in options])
            options = [{**o, "button": say("pick", lang)} for o in options]
        try:
            X.send(biz, "booking_chat", contact_id=conv["contact_id"], body=body, channel_account_id=conv["channel_account_id"],
                   options=options or None)
        except Blocked:
            return "failed"
        if new_flow is not ...:
            with db.tx() as conn:
                conn.execute(update(db.conversations).where(db.conversations.c.id == conversation_id).values(flow=new_flow))
        return "asked"

    def expires():
        return (now + timedelta(minutes=FLOW_MINUTES)).isoformat()

    if flow and text.strip().lower() in CANCEL:
        reply(say("cancelled", lang), new_flow=None)
        return "cancelled"
    if not svcs:
        return "not_booking"
    svc_opts = [{"id": str(s["id"]), "title": s["name"],
                 "description": (money.fmt(s["price_minor"], biz["currency"], pack.locale) + " · " if s["price_minor"] is not None else "")
                 + f"{s['minutes']} min"} for s in svcs][:10]

    def days_for(service_id: str):
        with db.tx() as conn:
            ds = BK.open_days(conn, biz, UUID(service_id), now, limit=10)
        return [{"id": d.isoformat(), "title": format_date(d, "EEE d MMM", locale=loc)} for d in ds]

    def times_for(service_id: str, day: str):
        with db.tx() as conn:
            ts = BK.slots(conn, biz, UUID(service_id), date.fromisoformat(day), now)
        step = max(1, len(ts) // 10 + (1 if len(ts) % 10 else 0)) if len(ts) > 10 else 1
        ts = ts[::step][:10]
        return [{"id": t.isoformat(), "title": format_time(t.astimezone(tz), "short", locale=loc)} for t in ts]

    if not flow:
        if len(svc_opts) == 1:
            flow = {"step": "day", "lang": lang, "service": svc_opts[0]["id"], "expires": expires()}
            opts = days_for(flow["service"])
            if not opts:
                return reply(say("none", lang), new_flow=None) and "none"
            flow["options"] = opts
            return reply(f"{svc_opts[0]['title']} — {say('day', lang)}", opts, flow)
        return reply(say("service", lang), svc_opts, {"step": "service", "lang": lang, "options": svc_opts, "expires": expires()})

    step, opts = flow["step"], flow.get("options", [])
    if step == "phone":
        from .. import phones
        try:
            e164 = phones.e164(text, pack)
        except phones.BadPhone:
            return reply(say("bad_phone", lang), new_flow={**flow, "expires": expires()})
        flow = {**flow, "phone": e164}
        return _finish(biz, conv, contact, flow, lang, loc, tz, reply, now)
    pick = _choose(text, opts)
    if not pick:
        return reply(say("again", lang), opts, {**flow, "expires": expires()})
    if step == "service":
        o = days_for(pick["id"])
        if not o:
            return reply(say("none", lang), new_flow=None) and "none"
        return reply(f"{pick['title']} — {say('day', lang)}", o, {"step": "day", "lang": lang, "service": pick["id"], "options": o, "expires": expires()})
    if step == "day":
        o = times_for(flow["service"], pick["id"])
        if not o:
            o2 = days_for(flow["service"])
            return reply(say("taken", lang), o2, {**flow, "options": o2, "expires": expires()})
        return reply(say("time", lang), o, {**flow, "step": "time", "day": pick["id"], "options": o, "expires": expires()})
    if step == "time":
        flow = {**flow, "start": pick["id"]}
        if not (contact.get("phone")):
            return reply(say("phone", lang), new_flow={**flow, "step": "phone", "options": [], "expires": expires()})
        return _finish(biz, conv, contact, flow, lang, loc, tz, reply, now)
    return "not_booking"


def _finish(biz, conv, contact, flow, lang, loc, tz, reply, now) -> str:
    try:
        with db.tx() as conn:
            res = BK.book(conn, biz, service_id=UUID(flow["service"]), starts_at=datetime.fromisoformat(flow["start"]),
                          name=contact.get("name") or "—", phone=flow.get("phone") or contact.get("phone") or "", now=now)
            log(conn, biz["id"], "booking.chat", "booking_chat", res["appointment"]["id"], {"conversation_id": str(conv["id"])})
    except Invalid as e:
        if e.field == "starts_at":
            with db.tx() as conn:
                ts = BK.slots(conn, biz, UUID(flow["service"]), date.fromisoformat(flow["day"]), now)[:10]
            o = [{"id": t.isoformat(), "title": format_time(t.astimezone(tz), "short", locale=loc)} for t in ts]
            reply(say("taken", lang), o, {**flow, "step": "time", "options": o})
            return "asked"
        reply(say("bad_phone", lang), new_flow={**flow, "step": "phone", "options": []})
        return "asked"
    except NotFound:
        reply(say("none", lang), new_flow=None)
        return "none"
    a = res["appointment"]
    when = format_date(a["starts_at"].astimezone(tz), "EEEE d MMMM", locale=loc) + " · " + format_time(a["starts_at"].astimezone(tz), "short", locale=loc)
    reply(say("booked", lang, what=res["service"]["name"], when=when), new_flow=None)
    return "booked"
