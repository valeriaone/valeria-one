"""Booking inside the chat — on WhatsApp, Messenger, Instagram, SMS, Telegram, anything.

A customer writes "quiero una cita" / "book" / "予約"… and the conversation walks them through it:
service -> day -> time -> (phone, if the channel doesn't give one) -> booked. WhatsApp shows tap-to-pick
lists; every other channel gets numbered options ("reply with a number"). Uses the same rules as the public
booking page (hours, notice, no double booking — checked again at the last second).

bk01 (v0.26): with a team, "With whom?" (anyone available, or a person); a table or a class asks "For how many people?"
(digits in any script, numbers in words in several languages); a deposit's pay link and the booking's policy come with
the confirmation.

sch01 (v0.23, chatcal.py): a day or time can also be written ("tomorrow at 10"); offer mode ("When works for you? Today
5:00–7:00 PM or tomorrow …") for a Marketplace pickup or anything else; the confirmation carries "Add to my calendar"
links; "can we do 7 instead?" / "cancel" move or free a booking made in this chat; the owner hears each of them.
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
from . import chatcal as CC
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


_CJK_NUM = {"一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_WORD_NUM = {w: i for i, ws in enumerate((
    (), ("one", "uno", "una", "um", "uma", "un", "une", "eins", "ein", "eine", "isa", "satu", "एक", "واحد"),
    ("two", "dos", "dois", "duas", "deux", "zwei", "dalawa", "dua", "दो", "اثنين", "اثنان"),
    ("three", "tres", "três", "trois", "drei", "tatlo", "tiga", "तीन", "ثلاثة"),
    ("four", "cuatro", "quatro", "quatre", "vier", "apat", "empat", "चार", "أربعة", "اربعة"),
    ("five", "cinco", "cinq", "fünf", "funf", "lima", "पांच", "पाँच", "خمسة"),
    ("six", "seis", "sechs", "anim", "enam", "छह", "ستة"),
    ("seven", "siete", "sete", "sept", "sieben", "pito", "tujuh", "सात", "سبعة"),
    ("eight", "ocho", "oito", "huit", "acht", "walo", "delapan", "आठ", "ثمانية"),
    ("nine", "nueve", "nove", "neuf", "neun", "siyam", "sembilan", "नौ", "تسعة"),
    ("ten", "diez", "dez", "dix", "zehn", "sampu", "sepuluh", "दस", "عشرة"))) for w in ws}


def party_in(text: str) -> int | None:
    """How many people a customer wrote: "4", "para 4 personas", "٤", "४", "４人", "四人", "we are three"."""
    t = (text or "").strip().lower()
    m = re.search(r"\d+", t)                                  # any script's digits (Arabic-Indic, Devanagari, full-width…)
    if m:
        try:
            return int(m.group())
        except ValueError:
            return None
    for ch in t:
        if ch in _CJK_NUM:
            return _CJK_NUM[ch]
    for w in re.split(r"[\s,.!?¿¡;:()'\"]+", t):
        if w in _WORD_NUM:
            return _WORD_NUM[w]
    return None


def holds(flow: dict | None, now: datetime) -> bool:
    """A booking question is waiting for its answer (an open offer of times doesn't hold the chat: other answers run)."""
    return bool(flow) and flow.get("step") != "offer" and datetime.fromisoformat(flow["expires"]) >= now


def wants_booking(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in TRIGGERS)


def _numbered(lines: list[str]) -> str:
    return "\n".join(f"{i}) {x}" for i, x in enumerate(lines, 1))


def _choose(text: str, options: list[dict], prefix: bool = True) -> dict | None:
    """By number ("2"), by the tapped title, or by the start of a title (not for offered times: "tomorrow" is a day to
    look at, not the first time tomorrow)."""
    t = (text or "").strip()
    m = re.match(r"^\s*(\d{1,2})\b", t) if prefix else re.fullmatch(r"\s*(\d{1,2})\s*[.)]?\s*", t)
    if m and 1 <= int(m.group(1)) <= len(options):
        return options[int(m.group(1)) - 1]
    low = t.lower()
    for o in options:
        if low and (low in (o["title"].lower(), o["title"].lower()[:24])                    # a WhatsApp list tap: its title, cut at 24
                    or (prefix and len(low) >= 3 and o["title"].lower().startswith(low))):
            return o
    return None


def handle(business_id: UUID, conversation_id: UUID, now: datetime | None = None, http=None, ongoing: bool = False) -> str:
    """Called after a customer's message. Returns: off | not_booking | asked | booked | moved | cancelled | none | failed.
    ongoing: only a booking already under way in this chat (a question asked, times offered, a booking to move or
    cancel) — nothing new starts (autoreply runs this before the Marketplace answers)."""
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
        intent, ap = None, None                                # sch01: "can we do 7 instead?" / "cancel" a booking made here
        if not flow:
            ap = CC.upcoming(conn, business_id, conversation_id, now)
            if ap:                                             # "change" only with a day or time in it (else the usual answers)
                intent = "cancel" if CC.is_cancel(text) else "change" if CC.is_change(text) and \
                    CC.parse(text, now.astimezone(ZoneInfo(biz["timezone"])).date(), loose=True) else None
        if not flow and not intent and (ongoing or not wants_booking(text)):
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
                   options=options or None, http=http, now=now)
        except Blocked:
            return "failed"
        if new_flow is not ...:
            with db.tx() as conn:
                conn.execute(update(db.conversations).where(db.conversations.c.id == conversation_id).values(flow=new_flow))
        return "asked"

    def expires():
        return (now + timedelta(minutes=FLOW_MINUTES)).isoformat()

    if flow and flow.get("step") == "offer":
        return _offer_step(biz, conv, flow, text, lang, reply, now, http)
    if intent:
        return _change(biz, conv, ap, intent, text, lang, reply, now, http)
    if flow and text.strip().lower() in CANCEL:
        reply(say("cancelled", lang), new_flow=None)
        return "cancelled"
    if not svcs:
        return "not_booking"
    svc_opts = [{"id": str(s["id"]), "title": s["name"],
                 "description": (money.fmt(s["price_minor"], biz["currency"], pack.locale) + " · " if s["price_minor"] is not None else "")
                 + f"{s['minutes']} min"} for s in svcs][:10]

    def who(fl: dict | None) -> dict:                          # bk01: the person picked (anyone) and the party
        fl = fl or {}
        return {"staff": None if fl.get("staff") in (None, "any") else fl["staff"], "party": int(fl.get("party") or 1)}

    def days_for(service_id: str, fl: dict | None = None):
        with db.tx() as conn:
            ds = BK.open_days(conn, biz, UUID(service_id), now, limit=10, **who(fl))
        return [{"id": d.isoformat(), "title": format_date(d, "EEE d MMM", locale=loc)} for d in ds]

    def times_for(service_id: str, day: str, fl: dict | None = None):
        svc = next((x for x in svcs if str(x["id"]) == service_id), None)
        left = {}
        with db.tx() as conn:
            if svc and svc["capacity"] > 1:                    # bk01: a class / a table: "9:00 AM · 3 left"
                left, _ = BK.spots(conn, biz, UUID(service_id), date.fromisoformat(day), now, **who(fl))
                ts = list(left)
            else:
                ts = BK.slots(conn, biz, UUID(service_id), date.fromisoformat(day), now, **who(fl))
        step = max(1, len(ts) // 10 + (1 if len(ts) % 10 else 0)) if len(ts) > 10 else 1
        ts = ts[::step][:10]
        return [{"id": t.isoformat(), "title": format_time(t.astimezone(tz), "short", locale=loc)
                 + (" · " + CC._t("bkg_left", lang).format(n=left[t]) if t in left else "")} for t in ts]

    def day_prompt(service_id: str, title: str, fl: dict | None = None) -> str:
        """"Corte — Which day?" + (sch01) the free stretches of the first days, so a time can be written straight away."""
        svc = next((x for x in svcs if str(x["id"]) == service_id), None)
        with db.tx() as conn:
            got = CC.offer(conn, biz, svc["minutes"], lang, now, service=svc, **who(fl)) if svc else None
        return f"{title} — {say('day', lang)}" + ("\n" + CC._t("sc_free", lang).format(windows=got[0]) if got else "")

    def advance(fl: dict, title: str) -> str:
        """bk01: after the service — "With whom?" (a team: anyone, or a person), "For how many people?" (a table, a class),
        then the day (as before when neither is asked)."""
        svc = next((x for x in svcs if str(x["id"]) == fl["service"]), None)
        if svc and "staff" not in fl:
            with db.tx() as conn:
                people = BK.eligible(conn, biz, svc)
            if len(people) > 1:
                o = ([{"id": "any", "title": CC._t("bkg_anyone", lang)[:24]}]
                     + [{"id": str(x["id"]), "title": x["name"][:24]} for x in people][:9])
                return reply(f"{title} — {CC._t('bkg_who_q', lang)}", o, {**fl, "step": "staff", "options": o, "expires": expires()})
        if svc and svc["ask_party"] and "party" not in fl:
            return reply(CC._t("bkg_party_q", lang), new_flow={**fl, "step": "party", "options": [], "expires": expires()})
        o = days_for(fl["service"], fl)
        if not o:
            return reply(say("none", lang), new_flow=None) and "none"
        return reply(day_prompt(fl["service"], title, fl), o, {**fl, "step": "day", "options": o, "expires": expires()})

    if not flow:
        if len(svc_opts) == 1:
            return advance({"lang": lang, "service": svc_opts[0]["id"], "expires": expires()}, svc_opts[0]["title"])
        return reply(say("service", lang), svc_opts, {"step": "service", "lang": lang, "options": svc_opts, "expires": expires()})

    step, opts = flow["step"], flow.get("options", [])
    if step == "party":                                        # bk01: "for how many?"
        svc = next((x for x in svcs if str(x["id"]) == flow["service"]), None)
        n = party_in(text)
        try:
            n = BK.party_of(svc, n) if svc and n else None
        except Invalid:
            n = None
        if not n:
            top = min(int(svc["max_party"] or svc["capacity"]), int(svc["capacity"])) if svc else 1
            return reply(CC._t("bkg_party_again", lang).format(max=top), new_flow={**flow, "expires": expires()})
        return advance({**flow, "party": n}, svc["name"])
    if step == "phone":
        from .. import phones
        try:
            e164 = phones.e164(text, pack)
        except phones.BadPhone:
            return reply(say("bad_phone", lang), new_flow={**flow, "expires": expires()})
        flow = {**flow, "phone": e164}
        return _finish(biz, conv, contact, flow, lang, loc, tz, reply, now, http)
    pick = _choose(text, opts)
    if not pick and step in ("day", "time"):                   # sch01: "tomorrow at 10" instead of a number
        svc = next((s for s in svcs if str(s["id"]) == flow["service"]), None)
        said = CC.parse(text, now.astimezone(tz).date(), loose=step == "time") if svc else None
        if said and step == "time" and not said["day"]:
            said = {**said, "day": date.fromisoformat(flow["day"])}
        with db.tx() as conn:
            r = CC.resolve(conn, biz, svc["minutes"], said, now, service=svc, **who(flow)) if said else None
        if r and r[0] == "at":
            flow = {**flow, "day": r[1].astimezone(tz).date().isoformat(), "start": r[1].isoformat()}
            if not contact.get("phone"):
                return reply(say("phone", lang), new_flow={**flow, "step": "phone", "options": [], "expires": expires()})
            return _finish(biz, conv, contact, flow, lang, loc, tz, reply, now, http)
        if r and r[0] == "ampm":
            o = [{"id": x.isoformat(), "title": CC.when(biz, x, lang, now)} for x in r[1]]
            return reply(CC._t("sc_ampm", lang), o, {**flow, "step": "time", "day": r[1][0].astimezone(tz).date().isoformat(),
                                                     "options": o, "expires": expires()})
        if r and r[0] in ("day", "busy") and r[1]:
            d = r[1] if r[0] == "day" else r[1].astimezone(tz).date()
            o = times_for(flow["service"], d.isoformat(), flow)
            if o:
                head = say("time", lang) if r[0] == "day" else CC._t("sc_busy", lang).format(when=CC.when(biz, r[1], lang, now))
                return reply(head, o, {**flow, "step": "time", "day": d.isoformat(), "options": o, "expires": expires()})
    if not pick:
        return reply(say("again", lang), opts, {**flow, "expires": expires()})
    if step == "service":
        return advance({"lang": lang, "service": pick["id"], "expires": expires()}, pick["title"])
    if step == "staff":                                        # bk01: anyone, or this person
        svc = next((x for x in svcs if str(x["id"]) == flow["service"]), None)
        return advance({**flow, "staff": pick["id"]}, svc["name"] if svc else "")
    if step == "day":
        o = times_for(flow["service"], pick["id"], flow)
        if not o:
            o2 = days_for(flow["service"], flow)
            return reply(say("taken", lang), o2, {**flow, "options": o2, "expires": expires()})
        return reply(say("time", lang), o, {**flow, "step": "time", "day": pick["id"], "options": o, "expires": expires()})
    if step == "time":
        flow = {**flow, "start": pick["id"]}
        if not (contact.get("phone")):
            return reply(say("phone", lang), new_flow={**flow, "step": "phone", "options": [], "expires": expires()})
        return _finish(biz, conv, contact, flow, lang, loc, tz, reply, now, http)
    return "not_booking"


def _finish(biz, conv, contact, flow, lang, loc, tz, reply, now, http=None) -> str:
    staff = None if flow.get("staff") in (None, "any") else flow["staff"]
    party = int(flow.get("party") or 1)
    try:
        with db.tx() as conn:
            res = BK.book(conn, biz, service_id=UUID(flow["service"]), starts_at=datetime.fromisoformat(flow["start"]),
                          name=contact.get("name") or "—", phone=flow.get("phone") or contact.get("phone") or "", now=now,
                          conversation_id=conv["id"], staff_id=staff, party=party)
            log(conn, biz["id"], "booking.chat", "booking_chat", res["appointment"]["id"], {"conversation_id": str(conv["id"])})
    except Invalid as e:
        if e.field == "starts_at":
            with db.tx() as conn:
                ts = BK.slots(conn, biz, UUID(flow["service"]), date.fromisoformat(flow["day"]), now, staff=staff, party=party)[:10]
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
    what = res["service"]["name"]                              # bk01: "Corte · with Ana", "Mesa · for 4"
    if res.get("staff"):
        what += " · " + CC._t("bkg_with", lang).format(name=BK.first_name(res["staff"]))
    if a["party"] > 1:
        what += " · " + CC._t("bkg_for", lang).format(n=a["party"])
    from . import bookpay as BP
    money_lines = ""
    if a["deposit_minor"] or a["fee_minor"]:                   # bk01: the deposit's own pay link, and the policy in words
        got = BP.ask_deposit(biz, a["id"], http=http, now=now, lang=lang, actor="booking_chat")
        money_lines = "\n".join(x for x in [BP.deposit_words(biz, got, lang)] + BP.policy_lines(biz, a, lang) if x)
    tail = "\n\n".join(x for x in (money_lines, CC.add_lines(biz, a, lang), CC._t("sc_change_hint", lang)) if x)   # sch01: into their calendar
    reply(say("booked", lang, what=what, when=when) + ("\n\n" + tail if tail else ""), new_flow=None)
    CC.tell_owner(biz, "sc_ping_booked", a, dedupe=f"sch:booked:{a['id']}", http=http, now=now)
    return "booked"


# ---------------------------------------------------------------- sch01: offer mode, moving and cancelling in the chat
def offer_flow(conn, biz: dict, conversation_id: UUID, *, what: str, minutes: int, lang: str, now: datetime,
               item_id=None, location: str | None = None, move=None) -> tuple[str, list[dict]] | None:
    """Open an offer of free times in this chat (inside the caller's transaction) -> (the sentence, the tap options), or
    None when nothing is free. The caller sends it (marketplace.py puts it in the item's first answer)."""
    got = CC.offer(conn, biz, minutes, lang, now, ignore=UUID(str(move)) if move else None, **_want_for(conn, biz, move))
    if not got:
        return None
    text, opts = got
    flow = {"step": "offer", "lang": lang, "what": what, "minutes": minutes, "item": str(item_id) if item_id else None,
            "location": location, "move": str(move) if move else None, "options": opts,
            "expires": (now + timedelta(hours=CC.OFFER_HOURS)).isoformat()}
    conn.execute(update(db.conversations).where(db.conversations.c.id == conversation_id, db.conversations.c.business_id == biz["id"])
                 .values(flow=flow))
    return CC._t("sc_offer", lang).format(windows=text) + " " + CC._t("sc_offer_how", lang), opts


def start_offer(business_id: UUID, conversation_id: UUID, *, minutes: int, what: str | None = None, item_id=None,
                location: str | None = None, lang: str | None = None, http=None, now: datetime | None = None) -> str:
    """Offer mode for anything (a home to see, a sofa to pick up, an estimate): "When works for you? …" in the chat, the
    customer's answer books it. `what` names it in both calendars; without it, the kit's own word (Showing, Pickup,
    Visit, Appointment…). -> asked | none | off | failed."""
    from . import inbox as X
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        if not biz["booking_enabled"] or not blocks.on(biz, "calendar"):
            return "off"
        conv = dict(conn.execute(select(db.conversations).where(db.conversations.c.id == conversation_id,
                                                                db.conversations.c.business_id == business_id)).mappings().one())
        lang = lang if lang in P["service"] else biz["language"] if biz["language"] in P["service"] else "en"
        what = what or blocks.word(biz, "appointment", biz["language"])
        got = offer_flow(conn, biz, conversation_id, what=what, minutes=minutes, lang=lang, now=now, item_id=item_id, location=location)
    if not got:
        return "none"
    body, opts = got
    try:
        X.send(biz, "booking_chat", contact_id=conv["contact_id"], body=body + "\n\n" + _numbered([o["title"] for o in opts]),
               channel_account_id=conv["channel_account_id"], options=[{**o, "button": say("pick", lang)} for o in opts], http=http, now=now)
    except Blocked:
        return "failed"
    return "asked"


def _want_for(conn, biz: dict, appt_id) -> dict:
    """bk01: offering times to move a booking: its own person, service and party (nothing for a new offer)."""
    if not appt_id:
        return {}
    r = conn.execute(select(db.appointments).where(db.appointments.c.business_id == biz["id"],
                                                   db.appointments.c.id == UUID(str(appt_id)))).mappings().first()
    return CC.want_of(conn, biz, dict(r)) if r else {}


def _appt(business_id: UUID, appt_id) -> dict | None:
    AP = db.appointments
    with db.tx() as conn:
        r = conn.execute(select(AP).where(AP.c.business_id == business_id, AP.c.id == UUID(str(appt_id)),
                                          AP.c.status.in_(("booked", "confirmed")))).mappings().first()
    return dict(r) if r else None


def _offer_step(biz, conv, flow, text, lang, reply, now, http) -> str:
    """The customer answers an offer of times: a number / a tap, or a time in words. Anything else isn't about the time:
    the offer stays open and the usual answers run (a Marketplace buyer's question, an offer…)."""
    tz = ZoneInfo(biz["timezone"])
    moving = _appt(biz["id"], flow["move"]) if flow.get("move") else None
    if flow.get("move") and not moving:
        reply(say("cancelled", lang), new_flow=None)
        return "cancelled"
    if moving and CC.is_cancel(text):
        return _cancel(biz, conv, moving, lang, reply, now, http)
    if not moving and text.strip().lower() in CANCEL:
        reply(say("cancelled", lang), new_flow=None)
        return "cancelled"
    ignore = moving["id"] if moving else None
    pick = _choose(text, flow.get("options", []), prefix=False)
    if pick:
        start = datetime.fromisoformat(pick["id"])
    else:
        said = CC.parse(text, now.astimezone(tz).date(), loose=True)
        if said and not said["day"] and said["h"] is not None and (flow.get("day") or moving):   # "10am": the day just shown
            said = {**said, "day": date.fromisoformat(flow["day"]) if flow.get("day") else moving["starts_at"].astimezone(tz).date()}
        with db.tx() as conn:
            r = CC.resolve(conn, biz, flow["minutes"], said, now, ignore, **_want_for(conn, biz, ignore))
        if not r:
            return "not_booking"
        if r[0] == "ampm":
            o = [{"id": x.isoformat(), "title": CC.when(biz, x, lang, now)} for x in r[1]]
            reply(CC._t("sc_ampm", lang), o, {**flow, "options": o})
            return "asked"
        if r[0] != "at":
            return _reoffer(biz, flow, r, lang, reply, now, ignore)
        start = r[1]
    return _place(biz, conv, flow, start, lang, reply, now, http, moving)


def _reoffer(biz, flow, r, lang, reply, now, ignore) -> str:
    """Not free (or only a day was said): the free times of that day, else the next free ones."""
    tz = ZoneInfo(biz["timezone"])
    day = r[1] if r[0] == "day" else r[1].astimezone(tz).date() if r[1] else None
    with db.tx() as conn:
        want = _want_for(conn, biz, ignore)
        that_day = CC.offer(conn, biz, flow["minutes"], lang, now, ignore, only=day, **want) if day else None
        got = that_day or CC.offer(conn, biz, flow["minutes"], lang, now, ignore, **want)
    if not got:
        reply(CC._t("sc_none", lang), new_flow=None)
        return "none"
    text, opts = got
    if r[0] == "day" and that_day:
        head = CC._t("sc_which_time", lang).format(day=CC.day_label(biz, day, lang, now))
    elif r[0] == "day":
        head = CC._t("sc_busy", lang).format(when=CC.day_label(biz, day, lang, now)) + " " + CC._t("sc_offer", lang).format(windows=text)
    elif r[1]:
        head = CC._t("sc_busy", lang).format(when=CC.when(biz, r[1], lang, now))
    else:
        head = CC._t("sc_offer", lang).format(windows=text)
    reply(head + " " + CC._t("sc_offer_how", lang), opts, {**flow, "options": opts, "day": day.isoformat() if that_day else None,
                                                           "expires": (now + timedelta(hours=CC.OFFER_HOURS)).isoformat()})
    return "asked"


def _place(biz, conv, flow, start, lang, reply, now, http, moving=None) -> str:
    """Book it (or move the booking to it) — free time checked again at the last second."""
    from sqlalchemy import func
    with db.tx() as conn:
        if moving:
            conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(biz["id"])))))
            a = CC.move(conn, biz, moving, start, "booking_chat") if BK.fits(conn, biz, flow["minutes"], start, now, moving["id"],
                                                                             **CC.want_of(conn, biz, moving)) else None
        else:
            title = flow["what"]
            a = CC.book(conn, biz, conv=conv, title=title, minutes=flow["minutes"], start=start, now=now, item_id=flow.get("item"),
                        location=flow.get("location"))
        if a and a.get("item_id"):                               # a Marketplace buyer: "coming", with the time
            from . import marketplace as MK
            b = MK.of_conversation(conn, biz["id"], conv["id"])
            if b and b["status"] in MK.TAPS:
                conn.execute(update(db.marketplace_buyers).where(db.marketplace_buyers.c.id == b["id"]).values(
                    status="coming", coming=CC.when(biz, start, biz["language"], now)[:60], updated_at=func.now()))
    if not a:
        return _reoffer(biz, flow, ("busy", start), lang, reply, now, moving["id"] if moving else None)
    wh = CC.when(biz, a["starts_at"], lang, now)
    links = CC.add_lines(biz, a, lang)
    if moving:
        reply("\n\n".join(x for x in (CC._t("sc_moved", lang).format(what=a["title"], when=wh), links) if x), new_flow=None)
        CC.tell_owner(biz, "sc_ping_moved", a, old=moving["starts_at"], dedupe=f"sch:moved:{a['id']}:{a['sequence']}", http=http, now=now)
        return "moved"
    reply("\n\n".join(x for x in (CC._t("sc_booked", lang).format(what=a["title"], when=wh), links, CC._t("sc_change_hint", lang)) if x),
          new_flow=None)
    CC.tell_owner(biz, "sc_ping_booked", a, dedupe=f"sch:booked:{a['id']}", http=http, now=now)
    return "booked"


def _cancel(biz, conv, ap, lang, reply, now, http) -> str:
    from . import deals as D
    with db.tx() as conn:
        a = D.set_appointment_status(conn, biz["id"], "booking_chat", ap["id"], "cancelled")
    extra = ""                                                 # bk01: what becomes of a paid deposit, in their words
    if a.get("deposit_outcome") in ("kept", "refund"):
        from .bookpay import _money
        extra = "\n" + CC._t(f"bkg_cancel_{a['deposit_outcome']}", lang).format(amount=_money(biz, a["deposit_minor"]),
                                                                              hours=a["cancel_hours"] or "")
    reply(CC._t("sc_cancelled", lang).format(what=a["title"], when=CC.when(biz, a["starts_at"], lang, now)) + extra, new_flow=None)
    CC.tell_owner(biz, "sc_ping_cancelled", a, dedupe=f"sch:cancel:{a['id']}", http=http, now=now)
    return "cancelled"


def _change(biz, conv, ap, intent, text, lang, reply, now, http) -> str:
    """A booking made in this chat: "cancel" frees it; "can we do 7 instead?" moves it (or offers the free times)."""
    if intent == "cancel":
        return _cancel(biz, conv, ap, lang, reply, now, http)
    tz = ZoneInfo(biz["timezone"])
    minutes = int((ap["ends_at"] - ap["starts_at"]).total_seconds() // 60)
    flow = {"step": "offer", "lang": lang, "what": ap["title"], "minutes": minutes, "item": str(ap["item_id"]) if ap["item_id"] else None,
            "location": ap["location"], "move": str(ap["id"]), "options": [],
            "expires": (now + timedelta(hours=CC.OFFER_HOURS)).isoformat()}
    said = CC.parse(text, now.astimezone(tz).date(), loose=True)
    if said and not said["day"] and said["h"] is not None:
        said = {**said, "day": ap["starts_at"].astimezone(tz).date()}
    with db.tx() as conn:
        r = CC.resolve(conn, biz, minutes, said, now, ap["id"], **CC.want_of(conn, biz, ap))
    if r and r[0] == "at":
        return _place(biz, conv, flow, r[1], lang, reply, now, http, ap)
    if r and r[0] == "ampm":
        o = [{"id": x.isoformat(), "title": CC.when(biz, x, lang, now)} for x in r[1]]
        reply(CC._t("sc_ampm", lang), o, {**flow, "options": o})
        return "asked"
    return _reoffer(biz, flow, r or ("busy", None), lang, reply, now, ap["id"])
