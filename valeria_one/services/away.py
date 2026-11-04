"""The away message (wati01, v0.29): someone writes while the business is closed, and nothing else answered them.

The owner switches it on (Valeria › Settings) and may write it in their own words; {next} becomes when the business
opens again ("Monday 09:00"). It goes out only when:
  * it is switched on, and the business has opening hours (without hours there is no "closed");
  * it is outside those hours, in the business's own time zone;
  * nothing else answered: not a booking under way, not the AI, not the 24/7 rule answers ("Valeria, stop" stops it too);
  * the last message is the customer's, and it isn't an opt-out word;
  * this chat hasn't had one in the last GAP_HOURS hours (one per night, not one per message).
It is a service reply inside the 24-hour window the customer just opened, so it needs no template.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .. import db
from . import log

GAP_HOURS = 12
MAX_TEXT = 500


def _hm(v: str) -> time | None:
    try:
        h, m = str(v).split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def is_open(hours: dict, local: datetime) -> bool:
    """Inside the opening hours? An interval that ends before it starts runs past midnight (22:00–02:00)."""
    from .booking import DAYS
    now_t = local.time()
    today, yesterday = DAYS[local.weekday()], DAYS[(local.weekday() - 1) % 7]
    for a, b in hours.get(today) or []:
        ta, tb = _hm(a), _hm(b)
        if ta and tb and ((ta <= now_t < tb) if ta < tb else now_t >= ta):
            return True
    for a, b in hours.get(yesterday) or []:                     # last night's interval still running after midnight
        ta, tb = _hm(a), _hm(b)
        if ta and tb and tb < ta and now_t < tb:
            return True
    return False


def next_open(hours: dict, local: datetime) -> tuple[int, time] | None:
    """(days from today, opening time) of the next opening, or None (no hours)."""
    from .booking import DAYS
    for d in range(0, 8):
        day = local + timedelta(days=d)
        starts = sorted(t for t in (_hm(a) for a, _ in (hours.get(DAYS[day.weekday()]) or [])) if t)
        for s in starts:
            if d or s > local.time():
                return d, s
    return None


def when_text(hours: dict, local: datetime, lang: str) -> str:
    from .booking import DAYS
    from .owner import t
    n = next_open(hours, local)
    if not n:
        return ""
    d, s = n
    day = t("va_away_today", lang) if d == 0 else t("va_away_tomorrow", lang) if d == 1 else \
        t("d_" + DAYS[(local + timedelta(days=d)).weekday()], lang)
    if lang.split("_")[0] in ("es", "pt", "fr", "fil", "id"):   # "abrimos lunes 09:00" — weekdays are lowercase mid-sentence
        day = day.lower()
    return f"{day} {s.strftime('%H:%M')}"


def text_for(biz: dict, settings: dict, local: datetime, lang: str) -> str:
    from .owner import t
    nxt = when_text(biz.get("hours") or {}, local, lang)
    own = (settings.get("away_text") or "").strip()
    body = own or t("va_away_default", lang)
    return body.replace("{business}", biz["name"]).replace("{next}", nxt or "…")


def save(conn, biz: dict, actor: str, on: bool, text: str) -> None:
    from .owner import _upsert
    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        from . import Invalid
        raise Invalid(f"the away message: up to {MAX_TEXT} characters", "away_text")
    _upsert(conn, biz["id"], {"away": bool(on), "away_text": text})
    log(conn, biz["id"], "assistant.away", actor, biz["id"], {"on": bool(on)})


def respond(business_id: UUID, conversation_id: UUID, http=None, now: datetime | None = None) -> str:
    """-> off | open | not_last | already | sent | blocked."""
    from . import contacts as C
    from . import inbox as X
    from . import owner as OW
    from .ai import detect_language
    now = now or datetime.now(timezone.utc)
    M, CV, E = db.messages, db.conversations, db.events
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        s = OW.settings(conn, business_id)
        hours = biz.get("hours") or {}
        if not s.get("away") or s.get("paused_at") or not any(hours.get(k) for k in hours):
            return "off"
        local = now.astimezone(ZoneInfo(biz["timezone"]))
        if is_open(hours, local):
            return "open"
        conv = dict(conn.execute(select(CV).where(CV.c.id == conversation_id, CV.c.business_id == business_id)).mappings().one())
        if conv["status"] != "open":
            return "off"
        last = conn.execute(select(M).where(M.c.conversation_id == conversation_id).order_by(M.c.created_at.desc()).limit(1)).mappings().first()
        if not last or last["direction"] != "in" or C.is_stop(last["body"]):
            return "not_last"
        if conn.execute(select(E.c.id).where(E.c.business_id == business_id, E.c.type == "away.sent", E.c.subject_id == conversation_id,
                                             E.c.created_at > now - timedelta(hours=GAP_HOURS))).first():
            return "already"
        lang = detect_language(last["body"] or last["transcript"] or "") or biz["language"]
        body = text_for(biz, s, local, lang)
    try:
        msg = X.send(biz, "valeria", contact_id=conv["contact_id"], body=body, channel_account_id=conv["channel_account_id"],
                     http=http, now=now)
    except Exception:                                          # noqa: BLE001 — blocked (window, STOP): nothing more to do
        return "blocked"
    with db.tx() as conn:
        log(conn, business_id, "away.sent", "valeria", conversation_id, {"message_id": str(msg["id"])})
    return "sent"
