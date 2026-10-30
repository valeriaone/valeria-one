"""Campaigns (v0.18): one message to the business's OWN customers, by email, SMS or WhatsApp — built one question per
screen, sent with the business's own accounts. Drips (services/sequences.py) send their steps through the same doors.

The rules, on every message (a campaign is always marketing):
  * only customers who said yes to offers (marketing consent); the builder shows how many are left out and why
    (never said yes, unsubscribed, no address on that channel);
  * every message carries a way out: email → an unsubscribe link and the one-click List-Unsubscribe header
    (RFC 8058); SMS → "Reply STOP…"; WhatsApp → "Reply STOP…" (and STOP/BAJA/… is honoured on every channel);
  * quiet hours: the country's (inbox.send) and, where the country has none, polite hours 21:00–08:00 the
    business's time — a message due at night waits for the morning;
  * WhatsApp: outside the 24-hour window only the business's approved template goes; with no template, that
    customer is skipped (and the builder says so before sending);
  * frequency caps: at most FREQ_DAY marketing message per customer per day and FREQ_WEEK per 7 days, campaigns
    and drips together; one over the cap waits (up to a week), then is skipped;
  * throttled per channel (THROTTLE per minute): a big list goes out over minutes, not in one burst.

Nothing is sent twice: one outbox row per campaign and customer (UNIQUE), claimed once by the job (SKIP LOCKED).
Every row has the customer's own token: /u/<token> unsubscribes (GET asks, POST does — link scanners never
unsubscribe anyone), /c/<token>/<n> counts a click and goes to one of the campaign's own links (never anywhere
else), /o/<token>.gif counts an open — only when the owner switched opens on (off by default).
Numbers: sent, delivered, opened, clicked, replied, unsubscribed, and bookings / payments by those customers in
the 7 days after it went out.
"""
from __future__ import annotations

import html as _html
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, insert, or_, select, update
from sqlalchemy.engine import Connection

from .. import config, db, packs
from . import Blocked, Invalid, NotFound, log

CP, SM, C, I, A = db.campaigns, db.scheduled_messages, db.contacts, db.identities, db.channel_accounts
CHANNELS = {"email": ("email",), "sms": ("sms",), "whatsapp": ("whatsapp", "whatsapp_twilio")}
THROTTLE = {"email": 60, "sms": 30, "whatsapp": 60, None: 60}          # messages per minute, per campaign
FREQ_DAY, FREQ_WEEK = 1, 3
POLITE = (21, 8)                                                      # where the country sets no quiet hours
MAX_DEFER_DAYS = 7
ATTRIBUTION_DAYS = 7
BLOCK_TYPES = ("heading", "text", "image", "button")
MAX_BLOCKS = 20
SEEN_OPS = ("within", "over")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€\f")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


# ---------------------------------------------------------------- channels
def account_channels(biz: dict, channel: str | None) -> tuple[str, ...]:
    """The channel accounts a campaign channel goes out on (the sandbox stands in for every channel there)."""
    if biz.get("sandbox"):
        return ("sandbox",)
    return CHANNELS.get(channel or "", ())


def ready_channels(conn: Connection, biz: dict) -> dict[str, UUID | None]:
    """{email|sms|whatsapp: the business's own account for it, or None (connect it first)}."""
    rows = conn.execute(select(A.c.id, A.c.channel).where(A.c.business_id == biz["id"], A.c.active)
                        .order_by(A.c.created_at)).all()
    out = {}
    for ch in CHANNELS:
        want = account_channels(biz, ch)
        out[ch] = next((aid for aid, c in rows if c in want), None)
    return out


def sms_segments(text: str) -> dict:
    """How many SMS a text costs: GSM-7 (160, or 153 per part) or Unicode (70, or 67 per part)."""
    text = text or ""
    if all(ch in GSM7 or ch in GSM7_EXT for ch in text):
        n = sum(2 if ch in GSM7_EXT else 1 for ch in text)
        per, single, enc = 153, 160, "GSM-7"
    else:
        n = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)   # UTF-16 code units
        per, single, enc = 67, 70, "Unicode"
    parts = 1 if n <= single else -(-n // per)
    return {"chars": n, "segments": parts if n else 0, "encoding": enc, "per": single if parts <= 1 else per}


def stop_line(biz: dict, channel: str | None) -> str:
    return _t("cg_stop_line", biz["language"]) if channel in ("sms", "whatsapp", None) else ""


# ---------------------------------------------------------------- the email
def clean_blocks(raw) -> list[dict]:
    out = []
    for b in (raw or [])[:MAX_BLOCKS]:
        t = (b or {}).get("t")
        if t not in BLOCK_TYPES:
            continue
        text = str(b.get("text") or "").strip()[:4000]
        url = str(b.get("url") or "").strip()[:500]
        if url and not url.lower().startswith(("https://", "http://")):
            url = "https://" + url
        if t in ("heading", "text") and text:
            out.append({"t": t, "text": text})
        elif t == "image" and url:
            out.append({"t": t, "url": url, "text": text[:200]})
        elif t == "button" and text and url:
            out.append({"t": t, "text": text[:80], "url": url})
    return out


def links_of(content: dict) -> list[str]:
    """Every address a customer could click in this message, in order, once each (what /c/<token>/<n> may go to)."""
    seen: list[str] = []
    for b in content.get("blocks") or []:
        if b.get("t") == "button" and b.get("url") and b["url"] not in seen:
            seen.append(b["url"])
        if b.get("t") == "text":
            for u in _URL.findall(b.get("text") or ""):
                if u not in seen:
                    seen.append(u)
    for u in _URL.findall(content.get("text") or ""):
        if u not in seen:
            seen.append(u)
    return seen[:20]


def _track(url: str, links: list[str], token: str | None) -> str:
    if not token or url not in links:
        return url
    return f"{config.PUBLIC_URL()}/c/{token}/{links.index(url)}"


def _fill(text: str, biz: dict, contact: dict | None) -> str:
    first = ((contact or {}).get("name") or "").split(" ")[0]
    out = (text or "").replace("{business}", biz["name"]).replace("{name}", first)
    link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
    out = out.replace("{book_link}", link)
    return re.sub(r"[ \t]{2,}", " ", out).replace(" ,", ",").replace(" !", "!").strip()


def render_email(biz: dict, content: dict, contact: dict | None = None, token: str | None = None,
                 track_opens: bool = False) -> tuple[str, str]:
    """-> (html, plain text). Mobile-safe: one 600 px column of tables, inline styles, no scripts, no web fonts.
    The unsubscribe link is always in both parts."""
    color = ((biz.get("site") or {}).get("color") or "#4d5bff")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = "#4d5bff"
    links = content.get("links") or links_of(content)
    unsub = f"{config.PUBLIC_URL()}/u/{token}" if token else f"{config.PUBLIC_URL()}/u/preview"
    rows, plain = [], []
    blocks = content.get("blocks") or ([{"t": "text", "text": content.get("text") or ""}] if content.get("text") else [])
    for b in blocks:
        t = b["t"]
        if t == "heading":
            s = _fill(b["text"], biz, contact)
            rows.append(f'<tr><td style="padding:8px 24px 4px;font:700 22px/1.3 Arial,Helvetica,sans-serif;color:#111">'
                        f'{_html.escape(s)}</td></tr>')
            plain.append(s.upper())
        elif t == "text":
            s = _fill(b["text"], biz, contact)
            esc = _html.escape(s)
            for u in sorted(set(_URL.findall(s)), key=len, reverse=True):
                eu = _html.escape(u)
                esc = esc.replace(eu, f'<a href="{_html.escape(_track(u, links, token))}" style="color:{color}">{eu}</a>')
            rows.append(f'<tr><td style="padding:6px 24px;font:16px/1.55 Arial,Helvetica,sans-serif;color:#222">'
                        f'{esc.replace(chr(10), "<br>")}</td></tr>')
            plain.append(s)
        elif t == "image":
            alt = _html.escape(b.get("text") or "")
            rows.append(f'<tr><td style="padding:8px 24px"><img src="{_html.escape(b["url"])}" alt="{alt}" width="552" '
                        f'style="display:block;width:100%;max-width:552px;height:auto;border:0;border-radius:8px"></td></tr>')
        elif t == "button":
            s = _fill(b["text"], biz, contact)
            href = _html.escape(_track(b["url"], links, token))
            rows.append(f'<tr><td style="padding:12px 24px"><table role="presentation" cellspacing="0" cellpadding="0"><tr>'
                        f'<td style="background:{color};border-radius:8px"><a href="{href}" style="display:inline-block;'
                        f'padding:12px 22px;font:700 16px Arial,Helvetica,sans-serif;color:#fff;text-decoration:none">'
                        f'{_html.escape(s)}</a></td></tr></table></td></tr>')
            plain.append(f"{s}: {_track(b['url'], links, token)}")
    foot = _t("cg_email_foot", biz["language"]).format(business=biz["name"])
    out_word = _t("cg_unsubscribe", biz["language"])
    rows.append(f'<tr><td style="padding:22px 24px 8px;font:12px/1.5 Arial,Helvetica,sans-serif;color:#777">'
                f'{_html.escape(foot)}<br><a href="{_html.escape(unsub)}" style="color:#777">{_html.escape(out_word)}</a></td></tr>')
    pixel = (f'<img src="{config.PUBLIC_URL()}/o/{token}.gif" width="1" height="1" alt="" style="display:block;border:0">'
             if track_opens and token else "")
    subject = _html.escape(_fill(content.get("subject") or "", biz, contact))
    html = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{subject}</title></head><body style="margin:0;padding:0;background:#f4f4f6">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f4f6"><tr><td align="center" '
            'style="padding:16px 8px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="max-width:600px;background:#ffffff;border-radius:12px">'
            f'<tr><td style="padding:20px 24px 4px;font:700 14px Arial,Helvetica,sans-serif;color:{color}">{_html.escape(biz["name"])}</td></tr>'
            + "".join(rows) + f'</table>{pixel}</td></tr></table></body></html>')
    text = "\n\n".join(p for p in plain if p) + f"\n\n—\n{foot}\n{out_word}: {unsub}"
    return html, text


def body_for(biz: dict, content: dict, channel: str | None, contact: dict | None, token: str | None) -> str:
    """SMS / WhatsApp / a chat channel: the words, links counted, and the way out on its own line."""
    links = content.get("links") or links_of(content)
    s = _fill(content.get("text") or "", biz, contact)
    for u in sorted(set(_URL.findall(s)), key=len, reverse=True):
        s = s.replace(u, _track(u, links, token))
    line = stop_line(biz, channel)
    return f"{s}\n{line}".strip() if line and line not in s else s


# ---------------------------------------------------------------- the audience
def _seen():
    from .growth import _seen as seen
    return seen


def audience_where(conn: Connection, biz: dict, aud: dict) -> list:
    """WHERE for the customers a campaign is FOR (before the rules: consent, unsubscribed, address)."""
    from . import contacts as CT
    w = [C.c.business_id == biz["id"], C.c.deleted_at.is_(None)]
    tags = [str(x).strip().lower() for x in (aud.get("tags") or []) if str(x).strip()]
    if tags:
        w.append(C.c.tags.overlap(tags))
    f = aud.get("field") or {}
    if f.get("key"):
        cond = CT.field_filter(conn, biz["id"], C, "contact", f["key"], f.get("value"))
        if cond is not None:
            w.append(cond)
    if aud.get("stage"):
        D = db.deals
        w.append(exists().where(D.c.business_id == biz["id"], D.c.contact_id == C.c.id, D.c.stage == aud["stage"]))
    if aud.get("source"):
        w.append(C.c.source.ilike(f"%{str(aud['source']).strip()}%"))
    seen = aud.get("seen") or {}
    if seen.get("op") in SEEN_OPS and int(seen.get("days") or 0) > 0:
        last = _seen()(biz["id"])
        since = datetime.now(timezone.utc) - timedelta(days=int(seen["days"]))
        w.append(last >= since if seen["op"] == "within" else last < since)
    return w


def _reach(biz: dict, channel: str | None):
    chans = account_channels(biz, channel)
    return exists().where(I.c.business_id == biz["id"], I.c.contact_id == C.c.id, I.c.channel.in_(chans))


def audience(conn: Connection, biz: dict, aud: dict, channel: str | None) -> dict:
    """How many it is for, how many will get it, and who is left out and why."""
    w = audience_where(conn, biz, aud or {})
    reach = _reach(biz, channel)
    row = conn.execute(select(
        func.count(),
        func.count().filter(C.c.unsubscribed_at.isnot(None)),
        func.count().filter(and_(C.c.unsubscribed_at.is_(None), C.c.marketing_consent.is_(False))),
        func.count().filter(and_(C.c.unsubscribed_at.is_(None), C.c.marketing_consent, ~reach)),
        func.count().filter(and_(C.c.unsubscribed_at.is_(None), C.c.marketing_consent, reach)),
    ).where(*w)).one()
    total, unsub, no_consent, no_address, ready = row
    return {"total": total, "ready": ready, "excluded": total - ready,
            "why": {"no_consent": no_consent, "unsubscribed": unsub, "no_address": no_address}}


def ready_ids(conn: Connection, biz: dict, aud: dict, channel: str | None) -> list[UUID]:
    w = audience_where(conn, biz, aud or {})
    q = select(C.c.id).where(*w, C.c.marketing_consent, C.c.unsubscribed_at.is_(None), _reach(biz, channel)).order_by(C.c.created_at)
    return [r[0] for r in conn.execute(q)]


def clean_audience(conn: Connection, biz: dict, raw: dict) -> dict:
    from . import fields as CF
    out: dict = {}
    tags = sorted({str(x).strip().lower()[:60] for x in (raw.get("tags") or []) if str(x).strip()})
    if tags:
        out["tags"] = tags[:10]
    f = raw.get("field") or {}
    if f.get("key"):
        fl = next((x for x in CF.list_(conn, biz["id"], "contact") if x["key"] == f["key"]), None)
        if not fl:
            raise Invalid("pick one of your fields", "field")
        out["field"] = {"key": fl["key"], "value": str(f.get("value") or "").strip()[:60]}
    if raw.get("stage"):
        if not packs.industry(biz["industry"]).stage(str(raw["stage"])):
            raise Invalid("pick a stage", "stage")
        out["stage"] = str(raw["stage"])
    if str(raw.get("source") or "").strip():
        out["source"] = str(raw["source"]).strip()[:60]
    s = raw.get("seen") or {}
    if s.get("op") in SEEN_OPS:
        try:
            days = int(s.get("days") or 0)
        except ValueError:
            raise Invalid("days: a whole number", "seen") from None
        if not 1 <= days <= 3650:
            raise Invalid("days: 1 to 3650", "seen")
        out["seen"] = {"op": s["op"], "days": days}
    return out


# ---------------------------------------------------------------- the campaign
def get(conn: Connection, business_id: UUID, cid: UUID) -> dict:
    r = conn.execute(select(CP).where(CP.c.business_id == business_id, CP.c.id == cid)).mappings().first()
    if not r:
        raise NotFound("campaign")
    return dict(r)


def list_(conn: Connection, business_id: UUID) -> list[dict]:
    return [dict(r) for r in conn.execute(select(CP).where(CP.c.business_id == business_id, CP.c.channel.isnot(None))
                                          .order_by(CP.c.created_at.desc()).limit(100)).mappings()]


def start(conn: Connection, biz: dict, actor: str, channel: str) -> dict:
    """A new campaign begins as a draft on one channel."""
    if channel not in CHANNELS:
        raise Invalid("email, sms or whatsapp", "channel")
    name = _t("cg_default_name", biz["language"]).format(channel=_t(f"cg_ch_{channel}", biz["language"]))
    row = conn.execute(insert(CP).values(business_id=biz["id"], name=name[:120], message="", channel=channel, status="draft",
                                         send_at=datetime.now(timezone.utc), created_by=actor).returning(CP)).mappings().one()
    log(conn, biz["id"], "campaign.drafted", actor, row["id"], {"channel": channel})
    return dict(row)


def save(conn: Connection, biz: dict, actor: str, cid: UUID, data: dict) -> dict:
    cp = get(conn, biz["id"], cid)
    if cp["status"] != "draft":
        raise Invalid("this campaign has been scheduled; cancel it to change it", "status")
    vals: dict = {}
    if "name" in data:
        vals["name"] = (" ".join(str(data["name"] or "").split()) or cp["name"])[:120]
    if "audience" in data:
        vals["audience"] = clean_audience(conn, biz, data["audience"] or {})
    if "message" in data:
        vals["message"] = str(data["message"] or "").strip()[:1600]
    if "subject" in data:
        vals["subject"] = " ".join(str(data["subject"] or "").split())[:200] or None
    if "blocks" in data:
        vals["blocks"] = clean_blocks(data["blocks"])
    if "template" in data:
        vals["template"] = str(data["template"] or "").strip()[:100] or None
    if "track_opens" in data:
        vals["track_opens"] = bool(data["track_opens"]) and cp["channel"] == "email"
    if "by_ai" in data:
        vals["by_ai"] = bool(data["by_ai"])
    if vals:
        conn.execute(update(CP).where(CP.c.id == cid).values(**vals, updated_at=func.now()))
    return get(conn, biz["id"], cid)


def content_of(cp: dict) -> dict:
    return {"channel": cp.get("channel"), "text": cp.get("message") or "", "subject": cp.get("subject") or "",
            "blocks": cp.get("blocks") or [], "template": cp.get("template"), "links": cp.get("links") or []}


def problems(conn: Connection, biz: dict, cp: dict) -> list[str]:
    """What still stops it from going out (keys for the screen); [] = ready."""
    out = []
    if not ready_channels(conn, biz).get(cp["channel"]):
        out.append("cg_need_channel")
    if cp["channel"] == "email":
        if not cp.get("subject"):
            out.append("cg_need_subject")
        if not (cp.get("blocks") or cp.get("message")):
            out.append("cg_need_message")
    elif not (cp.get("message") or "").strip():
        out.append("cg_need_message")
    if cp["channel"] == "whatsapp" and not cp.get("template") and not biz.get("sandbox"):
        out.append("cg_need_template")
    return out


def schedule(conn: Connection, biz: dict, actor: str, cid: UUID, when: datetime | None = None) -> dict:
    cp = get(conn, biz["id"], cid)
    if cp["status"] != "draft":
        raise Invalid("already scheduled", "status")
    bad = problems(conn, biz, cp)
    if bad:
        raise Invalid(_t(bad[0], biz["language"]), "campaign")
    now = datetime.now(timezone.utc)
    when = when or now
    if when.tzinfo is None:
        raise Invalid("send_at needs a time zone", "send_at")
    if when < now - timedelta(minutes=5):
        raise Invalid(_t("cg_when_past", biz["language"]), "send_at")
    links = links_of(content_of(cp))
    conn.execute(update(CP).where(CP.c.id == cid).values(status="scheduled", send_at=max(when, now), links=links,
                                                         updated_at=func.now()))
    log(conn, biz["id"], "campaign.created", actor, cid, {"channel": cp["channel"], "when": when.isoformat()})
    return get(conn, biz["id"], cid)


def unschedule(conn: Connection, biz: dict, actor: str, cid: UUID) -> dict:
    """Back to a draft (only before it starts going out)."""
    if not conn.execute(update(CP).where(CP.c.business_id == biz["id"], CP.c.id == cid, CP.c.status == "scheduled")
                        .values(status="draft", updated_at=func.now())).rowcount:
        raise Invalid("it has already started going out", "status")
    log(conn, biz["id"], "campaign.unscheduled", actor, cid)
    return get(conn, biz["id"], cid)


def duplicate(conn: Connection, biz: dict, actor: str, cid: UUID) -> dict:
    cp = get(conn, biz["id"], cid)
    row = conn.execute(insert(CP).values(
        business_id=biz["id"], name=(cp["name"] + " (2)")[:120], message=cp["message"], channel=cp["channel"], status="draft",
        audience=cp["audience"], subject=cp["subject"], blocks=cp["blocks"], template=cp["template"],
        track_opens=cp["track_opens"], send_at=datetime.now(timezone.utc), created_by=actor).returning(CP)).mappings().one()
    return dict(row)


def delete_draft(conn: Connection, biz: dict, actor: str, cid: UUID) -> None:
    if not conn.execute(CP.delete().where(CP.c.business_id == biz["id"], CP.c.id == cid, CP.c.status == "draft")).rowcount:
        raise NotFound("campaign")
    log(conn, biz["id"], "campaign.deleted", actor, cid)


def new_token() -> str:
    return secrets.token_urlsafe(16)


def dispatch(conn: Connection, cp: dict, now: datetime) -> int:
    """Due campaign → one outbox row per customer who can get it, spread out by the channel's throttle. Inside the
    dispatcher's transaction (followups.dispatch_campaigns); UNIQUE(campaign, contact) keeps it to one each."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == cp["business_id"])).mappings().one())
    ids = ready_ids(conn, biz, cp["audience"] or {}, cp["channel"])
    per = THROTTLE.get(cp["channel"], 60)
    for i, cid in enumerate(ids):
        conn.execute(pg_insert(SM).values(business_id=cp["business_id"], campaign_id=cp["id"], contact_id=cid,
                                          send_at=now + timedelta(minutes=i // per), token=new_token(), channel=cp["channel"])
                     .on_conflict_do_nothing(constraint="uq_sched_campaign"))
    return len(ids)


# ---------------------------------------------------------------- sending one message (campaign rows and drip steps)
def polite_until(biz: dict, now: datetime) -> datetime | None:
    """Marketing at night waits: the country's quiet hours, else 21:00–08:00 local. -> when it may go, or None (now)."""
    qh = packs.country(biz["country"]).messaging.quiet_hours or POLITE
    tz = ZoneInfo(biz["timezone"])
    local = now.astimezone(tz)
    start, end = qh
    h = local.hour
    quiet = (h >= start or h < end) if start > end else (start <= h < end)
    if not quiet:
        return None
    target = datetime.combine(local.date(), time(end), tz)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _marketing_sent(conn: Connection, business_id: UUID, contact_id: UUID, since: datetime) -> list[datetime]:
    E, S = db.sequence_enrollments, db.sequences
    mk_seq = select(E.c.id).join(S, S.c.id == E.c.sequence_id).where(E.c.business_id == business_id, S.c.kind == "marketing")
    rows = conn.execute(select(SM.c.send_at).where(
        SM.c.business_id == business_id, SM.c.contact_id == contact_id, SM.c.status == "sent", SM.c.send_at > since,
        or_(SM.c.campaign_id.isnot(None), SM.c.enrollment_id.in_(mk_seq)))).all()
    return sorted(r[0] for r in rows)


def cap_until(conn: Connection, biz: dict, contact_id: UUID, now: datetime) -> datetime | None:
    """The frequency cap: None = may go now; else the earliest time it may."""
    week = _marketing_sent(conn, biz["id"], contact_id, now - timedelta(days=7))
    day = [t for t in week if t > now - timedelta(days=1)]
    waits = []
    if len(day) >= FREQ_DAY:
        waits.append(day[-FREQ_DAY] + timedelta(days=1))
    if len(week) >= FREQ_WEEK:
        waits.append(week[-FREQ_WEEK] + timedelta(days=7))
    return max(waits) if waits else None


def _account_for(conn: Connection, biz: dict, contact_id: UUID, channel: str | None) -> UUID | None:
    if not channel:
        from .followups import _pick_account
        return _pick_account(conn, biz, contact_id)
    chans = account_channels(biz, channel)
    have = {r[0] for r in conn.execute(select(I.c.channel).where(I.c.business_id == biz["id"], I.c.contact_id == contact_id))}
    for aid, ch in conn.execute(select(A.c.id, A.c.channel).where(A.c.business_id == biz["id"], A.c.active,
                                                                 A.c.channel.in_(chans)).order_by(A.c.created_at)).all():
        if ch in have:
            return aid
    return None


def deliver(row: dict, biz: dict, contact: dict, content: dict, *, kind: str, track_opens: bool, http, now: datetime,
            actor: str = "campaign") -> tuple[str, str | None, datetime | None, UUID | None]:
    """Send one outbox row. -> (sent | failed | skipped | later, why, when (later), message id)."""
    from . import inbox as X
    channel = content.get("channel")
    if kind == "marketing":
        if contact.get("unsubscribed_at"):
            return "skipped", "unsubscribed", None, None
        if not contact.get("marketing_consent"):
            return "skipped", "no_consent", None, None
        wait = polite_until(biz, now)
        if wait:
            return "later", "quiet hours", wait, None
        with db.tx() as conn:
            cap = cap_until(conn, biz, contact["id"], now)
        if cap:
            if cap > row["created_at"] + timedelta(days=MAX_DEFER_DAYS):
                return "skipped", "frequency cap", None, None
            return "later", "frequency cap", cap, None
    with db.tx() as conn:
        acc = _account_for(conn, biz, contact["id"], channel)
        acc_channel = conn.execute(select(A.c.channel).where(A.c.id == acc)).scalar() if acc else None
    if not acc:
        return "skipped", "no_address", None, None
    token = row.get("token")
    email = None
    template = None
    if acc_channel == "email":
        html, text = render_email(biz, content, contact, token, track_opens)
        unsub = f"{config.PUBLIC_URL()}/u/{token}" if token else None
        email = {"subject": _fill(content.get("subject") or "", biz, contact) or biz["name"], "html": html,
                 "headers": {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
                 if unsub else {}}
        body = text
    else:
        body = body_for(biz, content, channel or ("sms" if acc_channel == "sms" else "whatsapp"), contact, token)
    try:
        msg = X.send(biz, actor, contact_id=contact["id"], body=body, kind=kind, channel_account_id=acc, http=http, now=now,
                     email=email)
    except Blocked as b:
        if b.reason == "quiet_hours":
            from .followups import _next_allowed
            return "later", "quiet hours", _next_allowed(biz, now), None
        if b.reason == "outside_reply_window":
            if not content.get("template"):
                return "skipped", "needs_template", None, None
            template = {"name": content["template"], "language": biz["language"],
                        "params": [p for p in ((contact.get("name") or "").split(" ")[0],) if p]}
            try:
                msg = X.send(biz, actor, contact_id=contact["id"], kind=kind, channel_account_id=acc, http=http, now=now,
                             template=template)
            except Blocked as b2:
                return "skipped", b2.reason, None, None
        else:
            return "skipped", b.reason, None, None
    if msg["status"] == "failed":
        return "failed", msg.get("error"), None, msg["id"]
    return "sent", None, None, msg["id"]


def send_row(row: dict, cp: dict, biz: dict, contact: dict, now: datetime, http) -> tuple[str, str | None, datetime | None, UUID | None]:
    if cp["status"] == "cancelled":
        return "skipped", "cancelled", None, None
    return deliver(row, biz, contact, content_of(cp), kind="marketing", track_opens=cp["track_opens"], http=http, now=now)


# ---------------------------------------------------------------- "send me a test"
def send_test(biz: dict, actor: str, cp: dict, to: str, http=None) -> str:
    """The message as a customer will see it, to the owner's own address, through the business's own account.
    Never a customer row, never counted. -> sent | failed:<why>."""
    from .. import channels as CH
    from .. import crypto, phones
    to = (to or "").strip()
    if not to:
        raise Invalid(_t("cg_test_need_to", biz["language"]), "to")
    with db.tx() as conn:
        aid = ready_channels(conn, biz).get(cp["channel"])
        acc = conn.execute(select(A).where(A.c.id == aid)).mappings().first() if aid else None
    if not acc:
        raise Invalid(_t("cg_need_channel", biz["language"]), "channel")
    content = content_of(cp)
    content["links"] = links_of(content)
    fake = {"id": None, "name": _t("cg_test_name", biz["language"])}
    prefix = _t("cg_test_prefix", biz["language"])
    out = CH.Outbound(to=to)
    if acc["channel"] == "email":
        if "@" not in to:
            raise Invalid("not an email address", "to")
        html, text = render_email(biz, content, fake, None, False)
        out.body, out.html = text, html
        out.subject = f"{prefix} {_fill(content.get('subject') or '', biz, fake)}".strip()
    else:
        if acc["channel"] != "sandbox":
            try:
                to = phones.e164(to, packs.country(biz["country"]))
            except phones.BadPhone:
                raise Invalid("check the phone number", "to") from None
            out.to = to
        if cp["channel"] == "whatsapp" and content.get("template") and acc["channel"] != "sandbox":
            out.template = {"name": content["template"], "language": biz["language"], "params": [fake["name"]]}
        else:
            out.body = f"{prefix} " + body_for(biz, content, cp["channel"], fake, None)
    try:
        res = CH.adapter(acc["channel"], http).send(crypto.open_(acc["config"]), to, out)
    except Exception as e:                                   # noqa: BLE001 — a provider problem is shown, never a crash
        res = CH.SendResult("failed", error=type(e).__name__)
    with db.tx() as conn:
        log(conn, biz["id"], "campaign.test", actor, cp["id"], {"channel": acc["channel"], "ok": res.status != "failed"})
    return "sent" if res.status != "failed" else f"failed:{res.error or ''}"


# ---------------------------------------------------------------- the customer's links
def by_token(conn: Connection, token: str) -> dict:
    r = conn.execute(select(SM).where(SM.c.token == (token or "")[:24])).mappings().first() if token else None
    if not r:
        raise NotFound("link")
    return dict(r)


def unsubscribe(token: str, how: str = "link") -> dict:
    """The customer's own link: no more offers from this business (consent off, unsubscribed). Safe to repeat."""
    from . import contacts as CT
    with db.tx() as conn:
        row = by_token(conn, token)
        c = conn.execute(select(C).where(C.c.id == row["contact_id"])).mappings().first()
        if c and not c["unsubscribed_at"]:
            CT.unsubscribe(conn, row["business_id"], row["contact_id"], f"unsubscribe:{how}")
        if not row["unsubscribed_at"]:
            conn.execute(update(SM).where(SM.c.id == row["id"]).values(unsubscribed_at=func.now()))
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
    return biz


def opened(token: str) -> None:
    with db.tx() as conn:
        conn.execute(update(SM).where(SM.c.token == (token or "")[:24], SM.c.opened_at.is_(None)).values(opened_at=func.now()))


def clicked(token: str, n: int) -> str:
    """-> where to go. Only an address the message itself carried."""
    with db.tx() as conn:
        row = by_token(conn, token)
        links: list = []
        if row["campaign_id"]:
            links = conn.execute(select(CP.c.links).where(CP.c.id == row["campaign_id"])).scalar() or []
        elif row["enrollment_id"]:
            from . import sequences as SQ
            links = SQ.step_links(conn, row["enrollment_id"], row["step"])
        if not 0 <= n < len(links):
            raise NotFound("link")
        conn.execute(update(SM).where(SM.c.id == row["id"], SM.c.clicked_at.is_(None)).values(clicked_at=func.now()))
        return links[n]


# ---------------------------------------------------------------- the numbers
def stats(conn: Connection, business_id: UUID, *, campaign_id: UUID | None = None, sequence_id: UUID | None = None) -> dict:
    """sent, delivered, opened, clicked, replied, unsubscribed, bookings and payments within 7 days."""
    M, CV, AP, PY = db.messages, db.conversations, db.appointments, db.payments
    if campaign_id:
        base = [SM.c.business_id == business_id, SM.c.campaign_id == campaign_id]
    else:
        E = db.sequence_enrollments
        base = [SM.c.business_id == business_id, SM.c.enrollment_id.in_(select(E.c.id).where(E.c.sequence_id == sequence_id))]
    by = dict(conn.execute(select(SM.c.status, func.count()).where(*base).group_by(SM.c.status)).all())
    sent = and_(*base, SM.c.status == "sent")
    later = SM.c.send_at + timedelta(days=ATTRIBUTION_DAYS)
    delivered = conn.execute(select(func.count()).select_from(SM.join(M, M.c.id == SM.c.sent_message_id))
                             .where(sent, M.c.status.in_(("delivered", "read")))).scalar()
    read = conn.execute(select(func.count()).select_from(SM.join(M, M.c.id == SM.c.sent_message_id))
                        .where(sent, M.c.status == "read")).scalar()
    opened = conn.execute(select(func.count()).where(sent, SM.c.opened_at.isnot(None))).scalar()
    clicked = conn.execute(select(func.count()).where(sent, SM.c.clicked_at.isnot(None))).scalar()
    replied = conn.execute(select(func.count()).where(sent, exists().where(
        M.c.business_id == business_id, M.c.direction == "in", M.c.created_at > SM.c.send_at, M.c.created_at < later,
        M.c.conversation_id.in_(select(CV.c.id).where(CV.c.business_id == business_id, CV.c.contact_id == SM.c.contact_id))))).scalar()
    unsub = conn.execute(select(func.count()).where(*base, or_(SM.c.unsubscribed_at.isnot(None), exists().where(
        C.c.id == SM.c.contact_id, C.c.unsubscribed_at > SM.c.send_at, C.c.unsubscribed_at < later)), SM.c.status == "sent")).scalar()
    bookings = conn.execute(select(func.count()).where(sent, exists().where(
        AP.c.business_id == business_id, AP.c.contact_id == SM.c.contact_id, AP.c.created_at > SM.c.send_at,
        AP.c.created_at < later))).scalar()
    pay = conn.execute(select(func.count(PY.c.id.distinct()), func.coalesce(func.sum(PY.c.amount_minor), 0)).select_from(
        SM.join(PY, and_(PY.c.contact_id == SM.c.contact_id, PY.c.business_id == business_id, PY.c.status == "paid",
                         PY.c.paid_at > SM.c.send_at, PY.c.paid_at < later))).where(sent)).one()
    return {"queued": by.get("pending", 0), "sent": by.get("sent", 0), "skipped": by.get("skipped", 0),
            "failed": by.get("failed", 0), "delivered": delivered, "opened": max(opened, read) if campaign_id else opened,
            "clicked": clicked, "replied": replied, "unsubscribed": unsub, "bookings": bookings, "payments": pay[0],
            "paid_minor": int(pay[1] or 0)}


def skipped_why(conn: Connection, business_id: UUID, campaign_id: UUID) -> dict:
    return dict(conn.execute(select(SM.c.error, func.count()).where(SM.c.business_id == business_id, SM.c.campaign_id == campaign_id,
                                                                    SM.c.status == "skipped").group_by(SM.c.error)).all())


# ---------------------------------------------------------------- written by the AI, in the owner's style
def ai_write(biz: dict, idea: str, channel: str, http=None) -> dict:
    """A draft from the owner's idea, with the business's OWN AI key, in the owner's style. Without a key: a plain
    draft from the idea itself (styled the same way). -> {"text", "subject"?, "by_ai"}. Never sent by itself."""
    from . import ai as AI
    from . import style as STY
    idea = " ".join((idea or "").split())[:500]
    if not idea:
        raise Invalid(_t("cg_ai_need_idea", biz["language"]), "idea")
    with db.tx() as conn:
        creds = AI.own_key(conn, biz)
        prof = STY.get(conn, biz["id"])
        facts = AI._facts(conn, biz)
    if creds:
        prov, key, model = creds
        lang = AI._LANG.get(biz["language"], "the business's language")
        limit = {"sms": "at most 300 characters", "whatsapp": "at most 500 characters"}.get(channel, "80 to 160 words")
        system = (f"You write a short {channel} message from {biz['name']} to its own customers who agreed to hear about offers. "
                  f"Write in {lang}. {limit}. Start with a greeting that uses {{name}} (it becomes the customer's first name). "
                  "Use ONLY the owner's idea and the facts below; never invent prices, dates, discounts or promises. "
                  "No hashtags. Do not add an unsubscribe line (it is added for you). "
                  + ("For email, the first line is 'Subject: …' and then a blank line and the body. " if channel == "email" else "")
                  + "Output only the message.\n" + STY.prompt(prof) + f"\n\nFACTS:\n{facts}")
        try:
            out = AI._call(prov, key, model, system, [("user", idea)], AI._http(http), max_tokens=600).strip()
        except AI.AIError:
            out = ""
        if out:
            subject = None
            if channel == "email" and out.lower().startswith("subject:"):
                first, _, rest = out.partition("\n")
                subject, out = first.split(":", 1)[1].strip()[:200], rest.strip()
            return {"text": out[:1600], "subject": subject, "by_ai": True}
    text = STY.apply(prof, _t("cg_plain_draft", biz["language"]).replace("{idea}", idea).replace("{business}", biz["name"]))
    return {"text": text, "subject": idea[:80] if channel == "email" else None, "by_ai": False}
