"""A time picked in the chat lands in both calendars (sch01, v0.23).

Built on the in-chat booking (chatbook.py), the booking rules (booking.py: hours, notice, nothing double-booked, the
owner's Google busy time) and the owner's assistant (owner.py). What this adds:

* Offer mode: Valeria proposes only times that are really free — "When works for you? Today 5:00–7:00 PM or tomorrow
  10:00 AM–2:00 PM" — with up to OPTIONS tap-to-pick times (WhatsApp lists; numbers elsewhere). A Marketplace buyer gets it
  in the item's first answer (marketplace.py); anything else can start it with chatbook.start_offer().
* Times in plain words, by rules (no AI, the same answer every time), in the 11 languages: "today 6", "tomorrow at
  10", "viernes a las 6 de la tarde", "18h", "6:30pm", "明日の18時"… An hour that could be morning or evening and is open
  at both is asked ("morning or evening?"), never guessed; a time that isn't free gets the closest free ones.
* The customer's calendar: the confirmation carries a .ics file (Apple, Outlook, Google…) and a Google Calendar link, both
  behind a signature (no account, nothing beyond what they already know). The same UID for the life of the booking and
  SEQUENCE +1 on every move or cancel, so their calendar replaces the event.
* "Can we do 7 instead?" moves it (free time re-checked; reminders follow it), "cancel" frees it; both sides hear.
* The owner: the booking is in the Valeria One calendar, their phone's calendar (a private per-person feed link, see
  feed()) and their Google Calendar when connected (gcal.py, the business's own Google); a ping on their chat naming the
  thing and the person; a note OWNER_BEFORE minutes before; afterwards "Did Juan come?" — yes = done (and, for a
  Marketplace item, "Mark it sold?"), no = no-show.

Customer reminders are the existing ones (reminders.py: the owner switches them on); outside a chat's 24 h window a
reminder that can't go out is skipped and the owner is told (followups.py).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_time
from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from .. import blocks, config, db, packs
from . import booking as BK
from . import log

AP = db.appointments
PICKUP_MINUTES = 30                                          # a Marketplace pickup (no service to take the length from)
OFFER_HOURS = 24                                             # an offer of times stays open as long as the chat's reply window
OFFER_DAYS = 2                                               # "today … or tomorrow …": the first days with free time
OPTIONS = 8                                                  # tap-to-pick times
OWNER_BEFORE = 60                                            # minutes: the owner's "in 1 hour" note
AFTER_MINUTES = 30                                           # after the end: "did they come?"
AFTER_DAYS = 2                                               # …asked for bookings that ended in the last 2 days only


# ---------------------------------------------------------------- words (folded: lower case, accents off)
def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _fw(words) -> tuple[str, ...]:
    return tuple(fold(w) for w in words)


AFTER_TOMORROW = _fw(("day after tomorrow", "pasado mañana", "depois de amanhã", "après-demain", "apres demain", "übermorgen",
                      "sa makalawa", "makalawa", "lusa", "परसों", "بعد غد", "بعد بكرة", "明後日", "あさって", "后天", "後天"))
TOMORROW = _fw(("tomorrow", "tmrw", "mañana", "amanhã", "demain", "morgen", "bukas", "besok", "कल", "غدا", "غدًا", "بكرة",
                "明日", "あした", "明天"))
TODAY = _fw(("today", "tonight", "this afternoon", "this evening", "hoy", "esta tarde", "esta noche", "hoje", "aujourd'hui",
             "aujourd’hui", "ce soir", "heute", "ngayon", "mamaya", "hari ini", "nanti sore", "nanti malam", "आज", "اليوم",
             "الليلة", "今日", "きょう", "今晚", "今天"))
WEEKDAYS = (                                                 # Monday … Sunday
    _fw(("monday", "lunes", "segunda", "segunda-feira", "lundi", "montag", "senin", "सोमवार", "الاثنين", "الإثنين",
         "月曜", "周一", "星期一", "礼拜一")),
    _fw(("tuesday", "tue", "tues", "martes", "terça", "terca", "terça-feira", "mardi", "dienstag", "selasa", "मंगलवार",
         "الثلاثاء", "火曜", "周二", "星期二", "礼拜二")),
    _fw(("wednesday", "wed", "miércoles", "miercoles", "quarta", "quarta-feira", "mercredi", "mittwoch", "miyerkules", "rabu",
         "बुधवार", "الأربعاء", "الاربعاء", "水曜", "周三", "星期三", "礼拜三")),
    _fw(("thursday", "thu", "thur", "thurs", "jueves", "quinta", "quinta-feira", "jeudi", "donnerstag", "huwebes", "kamis",
         "गुरुवार", "الخميس", "木曜", "周四", "星期四", "礼拜四")),
    _fw(("friday", "fri", "viernes", "sexta", "sexta-feira", "vendredi", "freitag", "biyernes", "jumat", "jum'at", "शुक्रवार",
         "الجمعة", "金曜", "周五", "星期五", "礼拜五")),
    _fw(("saturday", "sat", "sábado", "sabado", "samedi", "samstag", "sabtu", "शनिवार", "السبت", "土曜", "周六", "星期六",
         "礼拜六")),
    _fw(("sunday", "domingo", "dimanche", "sonntag", "linggo", "minggu", "रविवार", "الأحد", "الاحد", "日曜", "周日",
         "周天", "星期日", "星期天", "礼拜天")),
)
# afternoon / evening (an hour 1–11 becomes 13–23) and morning
PM = _fw(("afternoon", "evening", "tonight", "de la tarde", "de la noche", "en la tarde", "en la noche",
          "por la tarde", "por la noche", "esta tarde", "esta noche", "da tarde", "da noite", "à tarde", "a tarde", "à noite",
          "de l'après-midi", "de l’après-midi", "du soir", "ce soir", "nachmittags", "abends", "am nachmittag", "am abend",
          "ng hapon", "ng gabi", "sore", "malam", "siang", "शाम", "रात", "दोपहर", "مساء", "مساءً", "ظهرا", "ظهرًا", "بعد الظهر",
          "午後", "夜", "夕方", "下午", "晚上", "傍晚"))
AM = _fw(("morning", "de la mañana", "en la mañana", "por la mañana", "da manhã", "de manhã",
          "pela manhã", "du matin", "le matin", "morgens", "vormittags", "am morgen", "heute morgen", "ng umaga", "pagi",
          "सुबह", "صباحا", "صباحًا", "الصبح", "午前", "朝", "上午", "早上"))
NOON = _fw(("noon", "midday", "mediodía", "mediodia", "meio-dia", "meio dia", "midi", "mittag", "tanghali", "tengah hari",
            "الظهر", "正午", "中午"))
CHANGE = _fw(("instead", "change", "move", "reschedule", "earlier", "later", "another time", "different time", "en vez",
              "en lugar", "mejor a las", "mejor el", "cambiar", "cambia", "cambio", "mover", "mueve", "reagendar", "reagenda",
              "más tarde", "mas tarde", "más temprano", "mas temprano", "em vez", "ao invés", "ao inves", "mudar", "remarcar",
              "mais tarde", "mais cedo", "plutôt", "plutot", "changer", "décaler", "decaler", "plus tard", "plus tôt",
              "stattdessen", "lieber", "verschieben", "ändern", "andern", "später", "spater", "früher", "fruher", "palitan",
              "ilipat", "imbes", "sa halip", "mamaya na lang", "ganti", "ubah", "pindah", "geser", "lebih awal", "lebih lambat",
              "बदल", "की जगह", "बजाय", "تغيير", "غير الموعد", "بدلا", "بدلًا", "تأجيل", "変更", "ずらし", "代わりに", "改到",
              "改成", "改期", "换个时间", "改时间", "推迟", "提前"))
CANCEL = _fw(("cancel", "can't make it", "cant make it", "cannot make it", "won't make it", "cancelar", "cancela", "cancelo",
              "no voy a poder", "no podré ir", "no podre ir", "não vou poder", "nao vou poder", "annuler", "annule", "absagen",
              "stornieren", "storniere", "kanselahin", "i-cancel", "hindi na ako", "batal", "batalkan", "tidak jadi", "gak jadi",
              "nggak jadi", "रद्द", "कैंसल", "إلغاء", "الغاء", "ألغي", "キャンセル", "取り消", "取消", "不去了"))
_CJK = re.compile(r"[؀-ۿऀ-ॿ぀-ヿ一-鿿]")


def _has(t: str, words: tuple[str, ...]) -> bool:
    """Whole words for alphabets with spaces; any place for scripts written without them (CJK) or with joined marks."""
    for w in words:
        if _CJK.search(w):
            if w in t:
                return True
        elif re.search(r"(?<![\w'])" + re.escape(w) + r"(?![\w'])", t):
            return True
    return False


def _strip(t: str, words: tuple[str, ...]) -> str:
    for w in sorted(words, key=len, reverse=True):
        t = t.replace(w, " ")
    return t


# times: "18:30", "6.30", "18h30", "6pm", "6 p.m.", "18h", "18 uhr", "6時半", "6点", "6 बजे", "a las 6", "às 18", "um 6", "at 6"
_HM = re.compile(r"(?<![\d$€£¥₱₹])(\d{1,2})\s*(?::|\.|h|時|点|点钟)\s*(\d{2})(?!\d)\s*(a\.?\s?m\.?|p\.?\s?m\.?)?(?![a-z])")
_AMPM = re.compile(r"(?<![\d$€£¥₱₹])(\d{1,2})\s*(a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z])")
_UNIT = re.compile(r"(?<![\d$€£¥₱₹])(\d{1,2})\s*(hrs|hs|h|uhr|時|点|" + fold("बजे") + r")(半)?(?![a-z\d])")
_AT = re.compile(r"(?:\ba las|\ba la|\bas|\bum|\balas|\bjam|\bpukul|\bat|@|الساعة|الساعه)\s*(\d{1,2})(?![\d:.,])")
_BARE = re.compile(r"(?<![\d$€£¥₱₹.,:/])(\d{1,2})(?![\d.,:/%])")
# "10 minutes late", "in 2 hours": how long, never what time
_SPAN = re.compile(r"\d+\s*(?:mins?|minutes?|minutos?|minuten|minuto|menit|" + fold("मिनट") + r"|دقيقة|دقائق|分|分钟|"
                   r"hours?|oras|jam\b|" + fold("घंटे") + r"|ساعات|時間|小时)", re.I)   # ("18 hrs", "18 horas": a time, kept)


def parse(text: str, today: date, loose: bool = False) -> dict | None:
    """What day and time a customer means, by rules -> {"day": date | None, "h": hour | None, "m": minute, "sure": bool}
    ("sure" = no doubt between morning and evening). None = no day and no time in it. loose: Valeria just asked for a
    time (or they are changing one), so a lone number in the message is the hour ("can we do 7 instead?")."""
    t = " " + _SPAN.sub(" ", fold(text).replace("’", "'")) + " "
    day = None
    am, pm = _has(t, AM), _has(t, PM)
    if _has(t, AFTER_TOMORROW):
        day = today + timedelta(days=2)
        t = _strip(t, AFTER_TOMORROW)
    t_day = _strip(t, AM + PM)                               # "de la mañana" (morning) is not "mañana" (tomorrow)
    if day is None and _has(t_day, TOMORROW):
        day = today + timedelta(days=1)
    elif day is None and _has(t_day, TODAY):
        day = today
    elif day is None:
        for i, words in enumerate(WEEKDAYS):
            if _has(t_day, words):
                day = today + timedelta(days=(i - today.weekday()) % 7)
                break
    h = m = None
    sure = False
    if (mm := _HM.search(t)):
        h, m = int(mm.group(1)), int(mm.group(2))
        sure = h >= 13 or h == 0 or mm.group(1).startswith("0")
        if mm.group(3):
            pm, am = mm.group(3).startswith("p"), mm.group(3).startswith("a")
    elif (mm := _AMPM.search(t)):
        h, m, sure = int(mm.group(1)), 0, True
        if mm.group(2).startswith("p"):
            pm, am = True, False
        else:
            am, pm = True, False
    elif (mm := _UNIT.search(t)):
        h, m = int(mm.group(1)), 30 if mm.group(3) else 0
        sure = h >= 13 or h == 0 or mm.group(1).startswith("0")
    elif (mm := _AT.search(t)):
        h, m = int(mm.group(1)), 0
        sure = h >= 13 or h == 0
    elif _has(t, NOON):
        h, m, sure = 12, 0, True
    else:
        rest = re.sub(r"\s+", " ", _strip(_strip(t, AFTER_TOMORROW + TOMORROW + TODAY + sum(WEEKDAYS, ()) + AM + PM), ("?", "!", ".", ","))).strip()
        mm = _BARE.fullmatch(rest) if rest else None        # "today 6", "mañana 10", or just "18": the number is the hour
        if not mm and loose:
            nums = _BARE.findall(t)
            mm = _BARE.search(t) if len(nums) == 1 else None
        if mm and (day is not None or am or pm or loose or int(mm.group(1)) >= 13):
            h, m = int(mm.group(1)), 0
            sure = h >= 13 or h == 0
    if h is not None:
        if not (0 <= h <= 23 and 0 <= (m or 0) <= 59):
            h = m = None
        elif pm and h < 12:
            h, sure = h + 12, True
        elif am and h <= 12:
            h, sure = (0 if h == 12 else h), True
        elif h == 12:
            sure = True                                      # "12" alone is midday
    if day is None and h is None:
        return None
    return {"day": day, "h": h, "m": m or 0, "sure": sure}


def is_cancel(text: str) -> bool:
    return _has(" " + fold(text).replace("’", "'") + " ", CANCEL)


def is_change(text: str) -> bool:
    return _has(" " + fold(text).replace("’", "'") + " ", CHANGE)


# ---------------------------------------------------------------- what is free
def _within_hours(biz: dict, start: datetime, minutes: int) -> bool:
    tz = ZoneInfo(biz["timezone"])
    local = start.astimezone(tz)
    for a, b in (biz["hours"] or {}).get(BK.DAYS[local.weekday()]) or []:
        if datetime.combine(local.date(), time.fromisoformat(a), tz) <= local and \
                local + timedelta(minutes=minutes) <= datetime.combine(local.date(), time.fromisoformat(b), tz):
            return True
    return False


def resolve(conn: Connection, biz: dict, minutes: int, said: dict | None, now: datetime, ignore: UUID | None = None):
    """What the customer's words mean for this business -> ("at", start) free | ("busy", start | None) | ("ampm", [am, pm]) |
    ("day", day) | None. Morning or evening is decided by the opening hours; open at both = ask."""
    if not said:
        return None
    tz = ZoneInfo(biz["timezone"])
    today = now.astimezone(tz).date()
    if said["h"] is None:
        return ("day", said["day"]) if said["day"] and said["day"] >= today else None
    hours = [said["h"]] if said["sure"] or said["h"] >= 12 else [said["h"], said["h"] + 12]
    days = [said["day"]] if said["day"] else [today, today + timedelta(days=1)]
    for d in days:
        cands = [datetime.combine(d, time(h, said["m"]), tz) for h in hours]
        cands = [c for c in cands if c > now]
        if not cands:
            continue                                         # "6" said at 7 pm: tomorrow's 6
        fit = [c for c in cands if _within_hours(biz, c, minutes)]
        if len(fit) > 1:
            return ("ampm", fit)
        pick = fit[0] if fit else cands[-1] if not said["sure"] else cands[0]
        if fit and BK.fits(conn, biz, minutes, pick, now, ignore):
            return ("at", pick)
        return ("busy", pick)
    return ("busy", None)


def _ranges(starts: list[datetime], minutes: int) -> list[tuple[datetime, datetime]]:
    """Free start times -> the stretches they make ("5:00–7:00 PM" = any start in it, ending by 7)."""
    out: list[list[datetime]] = []
    for s in starts:
        if out and s - out[-1][1] <= timedelta(minutes=BK.STEP_MINUTES):
            out[-1][1] = s
        else:
            out.append([s, s])
    return [(a, b + timedelta(minutes=minutes)) for a, b in out]


def free_days(conn: Connection, biz: dict, minutes: int, now: datetime, ignore: UUID | None = None, days: int = OFFER_DAYS,
              only: date | None = None) -> list[tuple[date, list[datetime]]]:
    """The first `days` days with free time (or just `only`), each with its free start times."""
    from . import gcal
    tz = ZoneInfo(biz["timezone"])
    d0 = now.astimezone(tz).date()
    start = datetime.combine(d0, time(0), tz)
    outside = gcal.busy(conn, biz["id"], start, start + timedelta(days=biz["booking_days_ahead"] + 2))
    out = []
    for i in range(biz["booking_days_ahead"] + 1):
        d = d0 + timedelta(days=i)
        if only and d != only:
            continue
        ts = BK.free(conn, biz, minutes, d, now, outside_busy=outside, ignore=ignore)
        if ts:
            out.append((d, ts))
            if len(out) >= days:
                break
    return out


# ---------------------------------------------------------------- how a day and a time read
def _loc(biz: dict, lang: str) -> str:
    pack = packs.country(biz["country"])
    return pack.locale if pack.locale.startswith(lang) else lang


def _t(key: str, lang: str) -> str:
    from .owner import t
    return t(key, lang)


def day_label(biz: dict, d: date, lang: str, now: datetime) -> str:
    today = now.astimezone(ZoneInfo(biz["timezone"])).date()
    if d == today:
        return _t("sc_today", lang)
    if d == today + timedelta(days=1):
        return _t("sc_tomorrow", lang)
    try:
        return format_date(d, "EEEE d MMM", locale=_loc(biz, lang))
    except Exception:                                          # noqa: BLE001 — an odd locale: the plain date
        return d.isoformat()


def hm(biz: dict, dt: datetime, lang: str) -> str:
    try:
        return format_time(dt.astimezone(ZoneInfo(biz["timezone"])), "short", locale=_loc(biz, lang))
    except Exception:                                          # noqa: BLE001
        return dt.astimezone(ZoneInfo(biz["timezone"])).strftime("%H:%M")


def when(biz: dict, dt: datetime, lang: str, now: datetime) -> str:
    """"today 6:00 PM", "tomorrow 10:00", "Friday 31 Oct 18:00" — the business's time."""
    return day_label(biz, dt.astimezone(ZoneInfo(biz["timezone"])).date(), lang, now) + " " + hm(biz, dt, lang)


def offer(conn: Connection, biz: dict, minutes: int, lang: str, now: datetime, ignore: UUID | None = None,
          only: date | None = None) -> tuple[str, list[dict]] | None:
    """"Today 5:00–7:00 PM or tomorrow 10:00 AM–2:00 PM" + up to OPTIONS times to tap. None = nothing free."""
    days = free_days(conn, biz, minutes, now, ignore, days=1 if only else OFFER_DAYS, only=only)
    if not days:
        return None
    parts = []
    for d, ts in days:
        spans = _ranges(ts, minutes)[:2]
        parts.append(day_label(biz, d, lang, now) + " " + ", ".join(f"{hm(biz, a, lang)}–{hm(biz, b, lang)}" for a, b in spans))
    text = (" " + _t("sc_or", lang) + " ").join(parts)
    picks: list[datetime] = []                                # a spread: on the hour where possible, each day its share
    per = max(1, OPTIONS // len(days))
    for _, ts in days:
        hourly = [s for s in ts if s.minute == 0] or ts
        step = max(1, -(-len(hourly) // per))
        picks += hourly[::step][:per]
    opts = [{"id": s.isoformat(), "title": when(biz, s, lang, now)} for s in sorted(picks)[:OPTIONS]]
    return text, opts


# ---------------------------------------------------------------- the customer's calendar
def sig(appt_id) -> str:
    """The signature in a customer's calendar links (only someone who got the link can open it)."""
    from ..crypto import hmac_sha256_hex
    return hmac_sha256_hex(config.get("VO_SECRET_KEY", required=True), f"ics:{appt_id}".encode())[:24]


def links(biz: dict, appt: dict) -> dict | None:
    """{"ics", "google"} — public, signed; None when the business has no booking link name (slug) yet."""
    if not biz.get("slug"):
        return None
    base = f"{config.PUBLIC_URL()}/b/{biz['slug']}"
    s = sig(appt["id"])
    return {"ics": f"{base}/ics/{appt['id']}/{s}", "google": f"{base}/gcal/{appt['id']}/{s}"}


def google_url(biz: dict, appt: dict) -> str:
    """Google Calendar's own "add this event" page, filled in (UTC times: right in any time zone)."""
    f = BK._ics_time
    q = {"action": "TEMPLATE", "text": f"{appt['title']} — {biz['name']}", "dates": f"{f(appt['starts_at'])}/{f(appt['ends_at'])}",
         "ctz": biz["timezone"], "details": biz["name"]}
    if appt.get("location"):
        q["location"] = appt["location"]
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


def add_lines(biz: dict, appt: dict, lang: str) -> str:
    ln = links(biz, appt)
    return _t("sc_add_cal", lang).format(ics=ln["ics"], google=ln["google"]) if ln else ""


def public_appointment(conn: Connection, slug: str, appt_id: UUID, s: str) -> tuple[dict, dict] | None:
    """The business and the booking behind a customer's calendar link — None (= 404) for a wrong signature, another
    business's booking or an unknown link name."""
    import secrets
    if not secrets.compare_digest(s or "", sig(appt_id)):
        return None
    biz = conn.execute(select(db.businesses).where(db.businesses.c.slug == (slug or "").lower())).mappings().first()
    if not biz:
        return None
    a = conn.execute(select(AP).where(AP.c.id == appt_id, AP.c.business_id == biz["id"])).mappings().first()
    return (dict(biz), dict(a)) if a else None


# ---------------------------------------------------------------- the owner's own calendar: a private link per person
def feed_sig(business_id, user_id, gen: int) -> str:
    from ..crypto import hmac_sha256_hex
    return hmac_sha256_hex(config.get("VO_SECRET_KEY", required=True), f"feed:{business_id}:{user_id}:{gen}".encode())[:32]


def feed_url(conn: Connection, business_id: UUID, user_id: UUID) -> str | None:
    gen = conn.execute(select(db.memberships.c.feed_gen).where(db.memberships.c.business_id == business_id,
                                                               db.memberships.c.user_id == user_id)).scalar()
    if gen is None:
        return None
    return f"{config.PUBLIC_URL()}/cal/{business_id}/{user_id}/{feed_sig(business_id, user_id, gen)}.ics"


def new_feed(conn: Connection, business_id: UUID, user_id: UUID) -> None:
    """A new link; the old one stops working at once."""
    M = db.memberships
    conn.execute(update(M).where(M.c.business_id == business_id, M.c.user_id == user_id).values(feed_gen=M.c.feed_gen + 1))
    log(conn, business_id, "calendar.feed_renewed", f"user:{user_id}", user_id)


def feed(conn: Connection, business_id: UUID, user_id: UUID, s: str, now: datetime | None = None) -> str | None:
    """Everything in the business's calendar (60 days back, a year ahead) as a calendar the person's phone subscribes to.
    Cancellations stay in it a while (STATUS:CANCELLED) so phones take them off. None = wrong or old link."""
    import secrets
    now = now or datetime.now(timezone.utc)
    M = db.memberships
    gen = conn.execute(select(M.c.feed_gen).where(M.c.business_id == business_id, M.c.user_id == user_id)).scalar()
    if gen is None or not secrets.compare_digest(s or "", feed_sig(business_id, user_id, gen)):
        return None
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
    C = db.contacts
    rows = conn.execute(select(AP, C.c.name.label("contact_name")).outerjoin(C, C.c.id == AP.c.contact_id)
                        .where(AP.c.business_id == business_id, AP.c.ends_at > now - timedelta(days=60),
                               AP.c.starts_at < now + timedelta(days=400))
                        .order_by(AP.c.starts_at).limit(2000)).mappings().all()
    events = [BK.vevent(biz, dict(a), summary=a["title"] + (f" — {a['contact_name']}" if a["contact_name"] else ""))
              for a in rows if a["status"] != "cancelled" or a["sequence"]]
    return BK.calendar(events, name=f"{biz['name']} · Valeria One")


# ---------------------------------------------------------------- booking, moving, cancelling from the chat
def book(conn: Connection, biz: dict, *, conv: dict, title: str, minutes: int, start: datetime, now: datetime,
         item_id=None, location: str | None = None) -> dict | None:
    """Into the calendar — only if the time is still free (checked under the business's lock). None = just taken."""
    from . import deals as D
    from sqlalchemy import func
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(biz["id"])))))
    if not BK.fits(conn, biz, minutes, start, now):
        return None
    a = D.book(conn, biz, "booking_chat", {"title": title, "contact_id": conv["contact_id"], "starts_at": start,
                                          "ends_at": start + timedelta(minutes=minutes), "conversation_id": conv["id"],
                                          "item_id": item_id, "location": location or None})
    log(conn, biz["id"], "booking.chat", "booking_chat", a["id"], {"conversation_id": str(conv["id"])})
    return a


def move(conn: Connection, biz: dict, appt: dict, start: datetime, actor: str) -> dict:
    """A booking to a new time: same UID, SEQUENCE +1, Google pushed again, the reminders follow it."""
    from sqlalchemy import func
    end = start + (appt["ends_at"] - appt["starts_at"])
    row = dict(conn.execute(update(AP).where(AP.c.id == appt["id"]).values(
        starts_at=start, ends_at=end, sequence=AP.c.sequence + 1, synced_at=None, updated_at=func.now()).returning(AP)).mappings().one())
    SM, AU = db.scheduled_messages, db.automations
    for r in conn.execute(select(SM.c.id, AU.c.minutes).join(AU, AU.c.id == SM.c.automation_id).where(
            SM.c.subject_id == appt["id"], SM.c.status == "pending", AU.c.trigger == "appointment.reminder")).all():
        conn.execute(update(SM).where(SM.c.id == r.id).values(send_at=start - timedelta(minutes=r.minutes)))
    log(conn, biz["id"], "appointment.moved", actor, appt["id"], {"from": appt["starts_at"].isoformat(), "to": start.isoformat()})
    return row


def upcoming(conn: Connection, business_id: UUID, conversation_id: UUID, now: datetime) -> dict | None:
    """The next booking made in this chat that is still to come."""
    r = conn.execute(select(AP).where(AP.c.business_id == business_id, AP.c.conversation_id == conversation_id,
                                      AP.c.status.in_(("booked", "confirmed")), AP.c.starts_at > now)
                     .order_by(AP.c.starts_at).limit(1)).mappings().first()
    return dict(r) if r else None


def name_of(conn: Connection, biz: dict, appt: dict, lang: str) -> str:
    """The customer's first name — or "Buyer #3" for a Marketplace buyer Messenger sent no name for."""
    n = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == appt["contact_id"])).scalar() if appt.get("contact_id") else None
    first = (n or "").split(" ")[0]
    if first:
        return first
    if appt.get("conversation_id"):
        from . import marketplace as MK
        b = MK.of_conversation(conn, biz["id"], appt["conversation_id"])
        if b:
            return MK.who(b, lang)
    return "—"


# ---------------------------------------------------------------- the owner hears it
def tell_owner(biz: dict, key: str, appt: dict, *, dedupe: str, old: datetime | None = None, http=None,
               now: datetime | None = None) -> bool:
    """A note on the owner's chat (and the installed app): kept as one of her actions, sent now, once per dedupe."""
    from . import owner as OW
    now = now or datetime.now(timezone.utc)
    lang = OW.lang_of(biz)
    with db.tx() as conn:
        text = _t(key, lang).format(what=appt["title"], name=name_of(conn, biz, appt, lang), when=when(biz, appt["starts_at"], lang, now),
                                    old=when(biz, old, lang, now) if old else "", minutes=OWNER_BEFORE)
        a = OW.ask(conn, biz, "notice", text, {"text": text, "appointment_id": str(appt["id"])}, contact_id=appt.get("contact_id"),
                   by="valeria", dedupe=dedupe, owner_says_go=True, now=now)
    if not a:
        return False
    OW.run_now(biz, a["id"], http=http, now=now)
    return True


def round_(now: datetime | None = None, http=None) -> dict:
    """The job step: the owner's "in 1 hour" note, and afterwards "did they come?" — for bookings made in a chat."""
    from . import owner as OW
    now = now or datetime.now(timezone.utc)
    stats = {"soon": 0, "asked": 0}
    with db.tx() as conn:
        if not conn.execute(select(AP.c.id).where(AP.c.conversation_id.isnot(None), AP.c.status.in_(("booked", "confirmed")),
                                                  AP.c.ends_at > now - timedelta(days=AFTER_DAYS)).limit(1)).first():
            return stats
        soon = [dict(r) for r in conn.execute(select(AP).where(
            AP.c.conversation_id.isnot(None), AP.c.status.in_(("booked", "confirmed")), AP.c.starts_at > now,
            AP.c.starts_at <= now + timedelta(minutes=OWNER_BEFORE))).mappings()]
        done = [dict(r) for r in conn.execute(select(AP).where(
            AP.c.conversation_id.isnot(None), AP.c.status.in_(("booked", "confirmed")),
            AP.c.ends_at <= now - timedelta(minutes=AFTER_MINUTES), AP.c.ends_at > now - timedelta(days=AFTER_DAYS))).mappings()]
        bizs = {r["id"]: dict(r) for r in conn.execute(select(db.businesses).where(
            db.businesses.c.id.in_({a["business_id"] for a in soon + done}))).mappings()}
    for a in soon:
        biz = bizs[a["business_id"]]
        if blocks.on(biz, "calendar") and tell_owner(biz, "sc_ping_soon", a, dedupe=f"sch:soon:{a['id']}:{a['sequence']}", http=http, now=now):
            stats["soon"] += 1
    for a in done:
        biz = bizs[a["business_id"]]
        if not blocks.on(biz, "calendar"):
            continue
        lang = OW.lang_of(biz)
        with db.tx() as conn:
            q = OW.ask(conn, biz, "came", _t("sc_q_came", lang).format(what=a["title"], name=name_of(conn, biz, a, lang),
                                                                       when=when(biz, a["starts_at"], lang, now)),
                       {"appointment_id": str(a["id"]), "item_id": str(a["item_id"]) if a["item_id"] else None},
                       contact_id=a["contact_id"], dedupe=f"sch:came:{a['id']}", now=now)
        stats["asked"] += 1 if q else 0
    return stats
