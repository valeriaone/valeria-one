"""Leads (lead27, v0.27): every source tracked, every lead worked — and the follow-up never stops by forgetting.

A business keeps every place people find it — its websites, Facebook Page, Instagram, Google profile, marketplaces,
flyers, signs, ads, platforms that only send emails. Each one becomes a SOURCE here with its own code, and the code rides
along whichever way the person comes in:
  * a short link /l/<code> (counted, remembered on their phone for 30 days, then the business's page),
  * a click-to-chat link (WhatsApp wa.me / Messenger m.me) whose first words end in "(ref <code>)" — when they write,
    the chat is that source's,
  * a snippet for ANY website: one <script> that draws a small form (name, phone or email, message, the "you may contact
    me about my request" box the country's privacy law needs, a separate box for offers, a link to the business's
    privacy notice) and the chat buttons; honeypot + time check + a per-visitor limit,
  * a forwarding address <code>@<server's lead domain> for platforms that only email (the server's operator turns it on),
  * a QR of the short link for paper.
Every person who reaches out FIRST becomes a LEAD (one list, with the source); someone typed in or imported never does.

Then Valeria works each lead:
  * SUGGEST (default): Today shows the next best thing to do for each lead — answer, follow up (day N), check in — with
    the message already written; one tap sends it from the business's own account, or opens the owner's own WhatsApp /
    SMS / email app with it filled in when there is no account to send from (or the chat's 24 h window is closed).
  * AUTOPILOT (the owner switches it on): she sends the follow-ups herself, through the business's own accounts, inside
    the rules; what she can't send (no account, closed window and no approved template) waits as a one-tap suggestion.
The plan: day 0 (only if nobody answered within a few minutes), 1, 3, 7, 14, 30 — about their own request, so the
request consent they gave by writing first covers it — then one message every 30 days, and only to people who said yes
to offers (marketing consent, its own box). It stops AT ONCE when they reply (then it's a conversation: Valeria or the
owner answers), say an opt-out word in any language (STOP, BAJA, ARRÊT…), book, are marked won or lost, or the owner
pauses them (or pauses everything). Quiet hours in the business's time zone (the country's, else 21:00–08:00); one
message a day at most; a daily cap for autopilot; every send from the business's own accounts, in its name. $0 to us.

round_(dry_run=True) (and VO_LEADS_DRY_RUN=1 for the job) says what would happen and writes or sends nothing at all.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, db, money, packs, phones
from . import Blocked, Invalid, NotFound, log

LS, SRC, L, T, V = db.lead_settings, db.lead_sources, db.leads, db.lead_touches, db.lead_voice
C, M, CV, A = db.contacts, db.messages, db.conversations, db.channel_accounts

KINDS = ("website", "facebook", "instagram", "google", "marketplace", "flyer", "sign", "ad", "email", "referral", "event", "other")
MODES = ("suggest", "autopilot")
DAYS = (0, 1, 3, 7, 14, 30)
OPEN = ("new", "talking")                                     # still being worked
CLOSED_WHY = ("optout", "erased")                             # never restarted
NOT_LEADS = ("user:", "demo_visitor", "import", "recipe:", "workflow:", "valeria", "seed", "api_key:", "partner:", "csv",
             "sequence:", "followup", "system")                # someone the business typed in, imported or made itself
FIRST_MINUTES = 5                                             # day 0: only when nobody answered within this long
MIN_GAP_HOURS = 20                                            # at most one follow-up a day
CHECKIN_HOURS = 48                                            # a talk that went quiet: "check in" after this long
CHECKIN_DAYS = 21                                             # ... for this long
REMEMBER_DAYS = 30                                            # a short link remembered on the visitor's phone
MAX_SOURCES = 100
AUDIO_CHANNELS = ("whatsapp", "whatsapp_twilio", "messenger", "telegram", "sandbox")
_ALPHA = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE = re.compile(r"^[a-z0-9]{6,12}$")
_REF = re.compile(r"\bref[\s:#-]{0,3}([a-z0-9]{8})\b", re.I)
_MESSENGER = re.compile(r"^[A-Za-z0-9.]{2,100}$")
DEFAULTS = {"mode": "suggest", "followup": True, "days": list(DAYS), "nurture": True, "nurture_days": 30, "daily_cap": 40,
            "template": None, "chat_number": None, "messenger": None, "greeting": "", "voice": False, "paused_at": None}


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


# ---------------------------------------------------------------- settings
def settings(conn: Connection, business_id: UUID) -> dict:
    r = conn.execute(select(LS).where(LS.c.business_id == business_id)).mappings().first()
    return {**DEFAULTS, **(dict(r) if r else {}), "business_id": business_id, "saved": bool(r)}


def clean_days(raw) -> list[int]:
    """"0, 1, 3, 7, 14, 30" -> [0, 1, 3, 7, 14, 30]: 1 to 12 days, each 0 to 365, in order, never twice."""
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[\s,;]+", raw) if x]
    try:
        days = sorted({int(x) for x in (raw or [])})
    except (TypeError, ValueError):
        raise Invalid("days: whole numbers, like 0, 1, 3, 7, 14, 30", "days") from None
    if not days or len(days) > 12 or days[0] < 0 or days[-1] > 365:
        raise Invalid("days: 1 to 12 days, from 0 to 365", "days")
    return days


def save_settings(conn: Connection, biz: dict, actor: str, **f) -> dict:
    vals: dict = {}
    if "mode" in f:
        if f["mode"] not in MODES:
            raise Invalid("suggest or autopilot", "mode")
        vals["mode"] = f["mode"]
    for k in ("followup", "nurture", "voice"):
        if k in f:
            vals[k] = bool(f[k])
    if "days" in f:
        vals["days"] = clean_days(f["days"])
    for k, lo, hi in (("nurture_days", 14, 90), ("daily_cap", 1, 500)):
        if k in f and f[k] not in (None, ""):
            try:
                n = int(f[k])
            except (TypeError, ValueError):
                raise Invalid(f"{k}: a whole number", k) from None
            if not lo <= n <= hi:
                raise Invalid(f"{k}: {lo} to {hi}", k)
            vals[k] = n
    if "template" in f:
        vals["template"] = re.sub(r"[^A-Za-z0-9_]", "", str(f["template"] or ""))[:100] or None
    if "chat_number" in f:
        raw = str(f["chat_number"] or "").strip()
        try:
            vals["chat_number"] = phones.e164(raw, packs.country(biz["country"])) if raw else None
        except phones.BadPhone:
            raise Invalid("chat number: check the number", "chat_number") from None
    if "messenger" in f:
        m = str(f["messenger"] or "").strip().rstrip("/").split("/")[-1].lstrip("@")
        if m and not _MESSENGER.match(m):
            raise Invalid("Facebook Page: just its username, like mybusiness", "messenger")
        vals["messenger"] = m or None
    if "greeting" in f:
        vals["greeting"] = " ".join(str(f["greeting"] or "").split())[:300]
    if "paused" in f:
        vals["paused_at"] = datetime.now(timezone.utc) if f["paused"] else None
    if not vals:
        return settings(conn, biz["id"])
    conn.execute(pg_insert(LS).values(business_id=biz["id"], **vals)
                 .on_conflict_do_update(index_elements=[LS.c.business_id], set_={**vals, "updated_at": func.now()}))
    log(conn, biz["id"], "lead.settings", actor, biz["id"], {"fields": sorted(vals)})
    return settings(conn, biz["id"])


def chat_number(biz: dict, st: dict) -> str | None:
    return st.get("chat_number") or (biz.get("site") or {}).get("whatsapp")


# ---------------------------------------------------------------- sources
def _code(conn: Connection) -> str:
    while True:
        c = "".join(secrets.choice(_ALPHA) for _ in range(8))
        if not conn.execute(select(SRC.c.id).where(SRC.c.code == c)).first():
            return c


def sources(conn: Connection, business_id: UUID, *, active_only: bool = False) -> list[dict]:
    s = select(SRC).where(SRC.c.business_id == business_id)
    if active_only:
        s = s.where(SRC.c.active)
    rows = [dict(r) for r in conn.execute(s.order_by(SRC.c.created_at)).mappings()]
    n = dict(conn.execute(select(L.c.source_id, func.count()).where(L.c.business_id == business_id, L.c.source_id.isnot(None))
                          .group_by(L.c.source_id)).all())
    for r in rows:
        r["leads"] = n.get(r["id"], 0)
    return rows


def source(conn: Connection, business_id: UUID, source_id) -> dict:
    try:
        sid = UUID(str(source_id))
    except ValueError:
        raise NotFound("source") from None
    r = conn.execute(select(SRC).where(SRC.c.business_id == business_id, SRC.c.id == sid)).mappings().first()
    if not r:
        raise NotFound("source")
    return dict(r)


def save_source(conn: Connection, biz: dict, actor: str, data: dict, source_id=None) -> dict:
    from .funnels import https
    kind = str(data.get("kind") or "other")
    if kind not in KINDS:
        raise Invalid("pick what kind of place it is", "kind")
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise Invalid("give it a name you'll recognise (My website, Facebook page, Flyer at the market…)", "name")
    vals = {"kind": kind, "name": name, "dest": https(data.get("dest"), "dest")}
    if "active" in data:
        vals["active"] = bool(data["active"])
    if source_id:
        cur = source(conn, biz["id"], source_id)
        conn.execute(update(SRC).where(SRC.c.id == cur["id"]).values(**vals, updated_at=func.now()))
        log(conn, biz["id"], "lead.source_updated", actor, cur["id"], {"kind": kind})
        return source(conn, biz["id"], cur["id"])
    if conn.execute(select(func.count()).select_from(SRC).where(SRC.c.business_id == biz["id"])).scalar() >= MAX_SOURCES:
        raise Invalid(f"at most {MAX_SOURCES} sources", "name")
    row = conn.execute(insert(SRC).values(business_id=biz["id"], code=_code(conn), created_by=actor[:100], **vals)
                       .returning(SRC)).mappings().one()
    log(conn, biz["id"], "lead.source_created", actor, row["id"], {"kind": kind})
    return dict(row)


def set_active(conn: Connection, biz: dict, actor: str, source_id, on: bool) -> dict:
    cur = source(conn, biz["id"], source_id)
    conn.execute(update(SRC).where(SRC.c.id == cur["id"]).values(active=bool(on), updated_at=func.now()))
    log(conn, biz["id"], "lead.source_on" if on else "lead.source_off", actor, cur["id"])
    return source(conn, biz["id"], cur["id"])


def short_link(src: dict) -> str:
    return f"{config.PUBLIC_URL()}/l/{src['code']}"


def embed(src: dict) -> str:
    """What the owner pastes into ANY website (any builder that takes HTML)."""
    return f'<script src="{short_link(src)}/embed.js" async></script>'


def mail_domain() -> str | None:
    """The server's own address for platforms that only email leads (set by whoever runs the server; off = None)."""
    d = (config.get("VO_LEADS_MAIL_DOMAIN") or "").strip().lower().lstrip("@")
    return d if d and config.get("VO_LEADS_MAIL_SECRET") and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d) else None


def mail_address(src: dict) -> str | None:
    d = mail_domain()
    return f"{src['code']}@{d}" if d else None


def chat_text(biz: dict, st: dict, src: dict | None, lang: str | None = None) -> str:
    """The first words of a chat link: the owner's own, else a plain hello; the source's code at the end."""
    hello = st.get("greeting") or _t("ld_chat_hello", lang or biz["language"])
    return f"{hello} (ref {src['code']})" if src else hello


def chat_links(biz: dict, st: dict, src: dict | None, lang: str | None = None) -> dict:
    """{whatsapp, messenger}: click-to-chat on the business's own WhatsApp number and Facebook Page (None = not set)."""
    out = {"whatsapp": None, "messenger": None}
    num = chat_number(biz, st)
    if num:
        out["whatsapp"] = f"https://wa.me/{num.lstrip('+')}?text={quote(chat_text(biz, st, src, lang))}"
    if st.get("messenger"):
        out["messenger"] = f"https://m.me/{st['messenger']}" + (f"?ref={src['code']}" if src else "")
    return out


def landing(conn: Connection, biz: dict, src: dict) -> str:
    """Where the short link opens: the source's own address, else the business's website, booking page, or its lead page."""
    from .funnels import https
    if src.get("dest"):
        return https(src["dest"])
    base = config.PUBLIC_URL()
    if biz.get("slug") and biz.get("site_enabled") and blocks.on(biz, "website"):
        return f"{base}/s/{biz['slug']}"
    if biz.get("slug") and biz.get("booking_enabled") and blocks.on(biz, "calendar"):
        return f"{base}/b/{biz['slug']}"
    return f"{short_link(src)}/hi"


def by_code(conn: Connection, code: str) -> tuple[dict, dict]:
    """A public code -> (the source, its business). NotFound when unknown, switched off, or the business is paused."""
    code = (code or "").strip().lower()
    if not _CODE.match(code):
        raise NotFound("source")
    s = conn.execute(select(SRC).where(SRC.c.code == code, SRC.c.active)).mappings().first()
    if not s:
        raise NotFound("source")
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == s["business_id"])).mappings().one())
    if biz["suspended_at"]:
        raise NotFound("source")
    from . import billing
    if billing.paused(biz):
        raise NotFound("source")
    return dict(s), biz


def open_link(code: str) -> tuple[dict, dict, str]:
    """/l/<code> opened: counted. -> (source, business, where to go)."""
    with db.tx() as conn:
        s, biz = by_code(conn, code)
        conn.execute(update(SRC).where(SRC.c.id == s["id"]).values(clicks=SRC.c.clicks + 1))
        return s, biz, landing(conn, biz, s)


def remember(src: dict, now: datetime | None = None) -> dict:
    return {"c": src["code"], "b": str(src["business_id"]), "at": _now(now).isoformat()}


def _set_source(conn: Connection, biz: dict, lead: dict, src_id: UUID) -> bool:
    r = conn.execute(update(L).where(L.c.id == lead["id"], L.c.source_id.is_(None)).values(source_id=src_id, updated_at=func.now()))
    return bool(r.rowcount)


def claim(conn: Connection, biz: dict, ref: dict | None, contact: dict) -> bool:
    """A NEW lead on a phone that opened one of the business's short links: that source is theirs. Never over another source,
    never for a customer older than the click."""
    if not isinstance(ref, dict) or ref.get("b") != str(biz["id"]):
        return False
    try:
        opened = datetime.fromisoformat(str(ref.get("at")))
    except ValueError:
        return False
    if datetime.now(timezone.utc) - opened > timedelta(days=REMEMBER_DAYS):
        return False
    if contact.get("created_at") and contact["created_at"] < opened - timedelta(minutes=1):
        return False
    s = conn.execute(select(SRC.c.id).where(SRC.c.business_id == biz["id"], SRC.c.code == str(ref.get("c") or "")[:12])).scalar()
    lead = lead_of(conn, biz["id"], contact["id"])
    return bool(s and lead and _set_source(conn, biz, lead, s))


# ---------------------------------------------------------------- leads come in (inside the event's transaction)
def lead_of(conn: Connection, business_id: UUID, contact_id) -> dict | None:
    r = conn.execute(select(L).where(L.c.business_id == business_id, L.c.contact_id == contact_id)).mappings().first()
    return dict(r) if r else None


def _consent_of(actor: str) -> str:
    a = actor or ""
    if a == "webhook:meta":                                   # a Facebook / Instagram lead ad: a form they filled in
        return "form"
    if a.startswith("webhook:"):
        return "wrote_first"
    if a.startswith("lead:widget"):
        return "widget"
    if a.startswith("lead:mail"):
        return "mail"
    return "form"


def is_lead_actor(actor: str | None) -> bool:
    return bool(actor) and not actor.startswith(NOT_LEADS)


def _stop(conn: Connection, lead: dict, status: str, why: str, now: datetime, **stamps) -> None:
    conn.execute(update(L).where(L.c.id == lead["id"]).values(status=status, stop_reason=why, next_at=None, blocked=None,
                                                              updated_at=func.now(), **stamps))


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict, actor: str) -> None:
    """Called by services.log() for every event: who becomes a lead, and what stops their follow-up. Nothing here sends
    anything or logs anything (the event that caused it is the record)."""
    if not subject_id or type_.startswith("lead."):
        return
    now = datetime.now(timezone.utc)
    if type_ == "contact.created":
        if not is_lead_actor(actor):
            return
        c = conn.execute(select(C.c.source, C.c.deleted_at).where(C.c.id == subject_id)).first()
        if not c or c.deleted_at:
            return
        conn.execute(pg_insert(L).values(business_id=business_id, contact_id=subject_id, origin=(c.source or "")[:60],
                                         consent=_consent_of(actor), step=0, next_at=now + timedelta(minutes=FIRST_MINUTES))
                     .on_conflict_do_nothing(constraint="uq_lead_contact"))
        return
    cid = None
    if type_ in ("contact.unsubscribed", "contact.erased"):
        cid = subject_id
    elif type_ == "message.received":
        cid = data.get("contact_id")
    elif type_ == "appointment.booked":
        cid = conn.execute(select(db.appointments.c.contact_id).where(db.appointments.c.id == subject_id)).scalar()
    elif type_ == "deal.stage_changed":
        d = conn.execute(select(db.deals.c.contact_id).where(db.deals.c.id == subject_id)).scalar()
        cid = d
    elif type_ == "payment.paid":
        cid = conn.execute(select(db.payments.c.contact_id).where(db.payments.c.id == subject_id)).scalar()
    elif type_ == "invoice.paid":
        cid = conn.execute(select(db.invoices.c.contact_id).where(db.invoices.c.id == subject_id)).scalar()
    elif type_ == "pos.paid":
        cid = conn.execute(select(db.pos_sales.c.contact_id).where(db.pos_sales.c.id == subject_id)).scalar()
    elif type_ == "ai.handed_off":
        cid = conn.execute(select(CV.c.contact_id).where(CV.c.id == subject_id)).scalar()
    if not cid:
        return
    lead = lead_of(conn, business_id, UUID(str(cid)))
    if not lead:
        return
    if type_ == "contact.erased":
        _stop(conn, lead, "stopped", "erased", now)
        conn.execute(V.delete().where(V.c.business_id == business_id, V.c.contact_id == lead["contact_id"]))
    elif type_ == "contact.unsubscribed":
        if lead["status"] != "stopped":
            _stop(conn, lead, "stopped", "optout", now)
    elif type_ == "message.received":
        _received(conn, lead, subject_id, now)
    elif type_ == "appointment.booked":
        if lead["status"] in (*OPEN, "quiet"):
            _stop(conn, lead, "booked", "booked", now, booked_at=func.coalesce(L.c.booked_at, now))
        elif not lead["booked_at"]:
            conn.execute(update(L).where(L.c.id == lead["id"]).values(booked_at=now))
    elif type_ == "deal.stage_changed":
        ind = conn.execute(select(db.businesses.c.industry).where(db.businesses.c.id == business_id)).scalar()
        st = packs.industry(ind).stage(data.get("to") or "")
        if st and st.outcome == "won" and lead["status"] != "stopped":
            _stop(conn, lead, "won", "won", now, won_at=func.coalesce(L.c.won_at, now))
        elif st and st.outcome == "lost" and lead["status"] in (*OPEN, "quiet", "booked"):
            _stop(conn, lead, "lost", "lost", now, lost_at=now)
    elif type_ in ("payment.paid", "invoice.paid", "pos.paid"):
        if lead["status"] != "stopped":
            _stop(conn, lead, "won", "won", now, won_at=func.coalesce(L.c.won_at, now))
    elif type_ == "ai.handed_off" and lead["status"] in OPEN and not lead["hot_at"]:
        conn.execute(update(L).where(L.c.id == lead["id"]).values(hot_at=now))    # Valeria tells the owner (round_)


def _received(conn: Connection, lead: dict, message_id: UUID, now: datetime) -> None:
    """They wrote. After the business has said something since they came in, it's a reply: the plan stops — it's a talk now."""
    from . import contacts as CT
    body = conn.execute(select(M.c.body).where(M.c.id == message_id)).scalar() or ""
    if CT.is_stop(body):
        return                                             # the opt-out itself (contact.unsubscribed stops it)
    if lead["status"] not in ("new", "quiet"):
        return
    ours = conn.execute(select(M.c.id).join(CV, CV.c.id == M.c.conversation_id).where(
        CV.c.business_id == lead["business_id"], CV.c.contact_id == lead["contact_id"], M.c.direction == "out",
        M.c.created_at >= lead["created_at"]).limit(1)).first()
    if not ours and lead["status"] == "new":
        return                                             # still their first words (they wrote twice): day 0 is still ours
    _stop(conn, lead, "talking", "replied", now, replied_at=func.coalesce(L.c.replied_at, now))


def on_inbound(conn: Connection, biz: dict, contact: dict, body: str, ref: dict | None) -> None:
    """A chat message stored (inbox._inbound): the source from "ref <code>" (or Messenger's m.me ref) for a new lead; a
    lead asking the price or to book is HOT (the owner hears it from round_). Nothing is sent here."""
    lead = lead_of(conn, biz["id"], contact["id"])
    if not lead:
        return
    now = datetime.now(timezone.utc)
    if not lead["source_id"] and now - lead["created_at"] < timedelta(hours=24):
        code = None
        m = _REF.search(body or "")
        if m:
            code = m.group(1).lower()
        elif isinstance(ref, dict) and ref.get("ref"):
            code = str(ref["ref"]).strip().lower()[:12]
        if code:
            sid = conn.execute(select(SRC.c.id).where(SRC.c.business_id == biz["id"], SRC.c.code == code)).scalar()
            if sid:
                _set_source(conn, biz, lead, sid)
    if lead["status"] in OPEN and not lead["hot_at"]:
        from .answers import topic
        if topic(body) in ("price", "booking"):
            conn.execute(update(L).where(L.c.id == lead["id"]).values(hot_at=now))


# ---------------------------------------------------------------- the plan
def due_at(lead: dict, st: dict, step: int) -> datetime | None:
    """When follow-up `step` is due: day N of the plan from when they came in; after the plan, every nurture_days."""
    days = list(st.get("days") or DAYS)
    if step < len(days):
        d = days[step]
        return lead["created_at"] + (timedelta(minutes=FIRST_MINUTES) if d == 0 else timedelta(days=d))
    if not st.get("nurture"):
        return None
    return (lead.get("last_touch_at") or lead["created_at"]) + timedelta(days=int(st.get("nurture_days") or 30))


def is_nurture(st: dict, step: int) -> bool:
    return step >= len(st.get("days") or DAYS)


def _next_values(lead: dict, st: dict, contact: dict | None, now: datetime, *, sent: bool) -> dict:
    """The lead after this step: the next step and when (never sooner than a day after a message that went out), or the
    plan's end — nurture only goes to people who said yes to offers."""
    nxt = lead["step"] + 1
    if is_nurture(st, nxt) and (not st.get("nurture") or not contact or not contact.get("marketing_consent")
                                or contact.get("unsubscribed_at")):
        return {"step": nxt, "next_at": None, "status": "quiet", "stop_reason": "done", "blocked": None}
    when = due_at({**lead, "last_touch_at": now if sent else lead.get("last_touch_at")}, st, nxt)
    if when is None:
        return {"step": nxt, "next_at": None, "status": "quiet", "stop_reason": "done", "blocked": None}
    if sent:
        when = max(when, now + timedelta(hours=MIN_GAP_HOURS))
    return {"step": nxt, "next_at": when, "blocked": None}


def _touch(conn: Connection, lead: dict, *, how: str, status: str, by: str, channel: str | None = None, message_id=None,
           note: str = "", advance: bool, st: dict | None = None, contact: dict | None = None, now: datetime) -> None:
    conn.execute(insert(T).values(business_id=lead["business_id"], lead_id=lead["id"], step=lead["step"], how=how, status=status,
                                  channel=channel, message_id=message_id, note=note[:200], by=by[:100]))
    vals: dict = {"updated_at": func.now()}
    if status in ("sent", "opened"):
        vals.update(touches=L.c.touches + 1, last_touch_at=now)
    if advance:
        vals.update(_next_values(lead, st or {}, contact, now, sent=status in ("sent", "opened")))
    conn.execute(update(L).where(L.c.id == lead["id"]).values(**vals))


# ---------------------------------------------------------------- the list, one lead, the owner's taps
def _base(business_id: UUID):
    return (select(L, C.c.name.label("name"), C.c.phone.label("phone"), C.c.email.label("email"),
                   C.c.marketing_consent.label("marketing_consent"), SRC.c.name.label("source_name"), SRC.c.kind.label("source_kind"))
            .join(C, C.c.id == L.c.contact_id).outerjoin(SRC, SRC.c.id == L.c.source_id)
            .where(L.c.business_id == business_id, C.c.deleted_at.is_(None)))


def list_(conn: Connection, business_id: UUID, *, status: str | None = None, source_id=None, q: str | None = None,
          limit: int = 100) -> list[dict]:
    s = _base(business_id)
    if status == "open":
        s = s.where(L.c.status.in_(OPEN))
    elif status:
        s = s.where(L.c.status == status)
    if source_id:
        s = s.where(L.c.source_id == source_id)
    if q:
        like = f"%{q.strip()}%"
        s = s.where(or_(C.c.name.ilike(like), C.c.phone.ilike(like), C.c.email.ilike(like)))
    return [dict(r) for r in conn.execute(s.order_by(L.c.created_at.desc()).limit(max(1, min(limit, 500)))).mappings()]


def get(conn: Connection, business_id: UUID, lead_id) -> dict:
    try:
        lid = UUID(str(lead_id))
    except ValueError:
        raise NotFound("lead") from None
    r = conn.execute(_base(business_id).where(L.c.id == lid)).mappings().first()
    if not r:
        raise NotFound("lead")
    d = dict(r)
    d["history"] = [dict(x) for x in conn.execute(select(T).where(T.c.lead_id == lid).order_by(T.c.created_at.desc()).limit(50)).mappings()]
    return d


def origin_family(origin: str | None) -> str:
    """contacts.source -> a family the screens name: chat:<channel>, website, form, ad, app, lead_ad, booking, other."""
    o = (origin or "").lower()
    for ch in ("whatsapp", "messenger", "instagram", "telegram", "viber", "sms", "email", "tiktok", "sandbox"):
        if o == ch or o == f"{ch}_twilio" or (ch == "whatsapp" and o.startswith("whatsapp")):
            return f"chat:{ch}"
    if o.startswith("form:"):
        return "form"
    if o.startswith("ad:"):
        return "ad"
    if o.startswith("app:"):
        return "app"
    if o.startswith("lead:"):
        return "source"
    if o in ("website", "directory", "funnel", "booking", "lead_ad", "webchat", "voice", "call"):
        return o
    return "other"


def label(lead: dict, lang: str) -> str:
    """Where a lead came from, in words."""
    if lead.get("source_name"):
        return lead["source_name"]
    fam = origin_family(lead.get("origin"))
    if fam.startswith("chat:"):
        return channel_name(fam[5:])
    return _t(f"ld_o_{fam}", lang)


NAMES = {"whatsapp": "WhatsApp", "whatsapp_twilio": "WhatsApp", "messenger": "Messenger", "instagram": "Instagram",
         "telegram": "Telegram", "viber": "Viber", "sms": "SMS", "email": "Email", "tiktok": "TikTok", "sandbox": "Sandbox",
         "webhook": "Gateway", "webchat": "Web"}


def channel_name(ch: str | None) -> str:
    return NAMES.get(ch or "", (ch or "").replace("_", " ").title())


def owner_action(conn: Connection, biz: dict, actor: str, lead_id, act: str, now: datetime | None = None) -> dict:
    """pause | resume | won | lost | restart | skip — the owner's taps on one lead."""
    now = _now(now)
    lead = get(conn, biz["id"], lead_id)
    st = settings(conn, biz["id"])
    if act == "pause":
        conn.execute(update(L).where(L.c.id == lead["id"]).values(paused=True, updated_at=func.now()))
    elif act == "resume":
        conn.execute(update(L).where(L.c.id == lead["id"]).values(paused=False, updated_at=func.now()))
    elif act == "won":
        _stop(conn, lead, "won", "won", now, won_at=func.coalesce(L.c.won_at, now))
    elif act == "lost":
        _stop(conn, lead, "lost", "lost", now, lost_at=now)
    elif act == "restart":
        if lead["stop_reason"] in CLOSED_WHY:
            raise Invalid(_t("ld_no_restart", biz["language"]), "lead")
        step = max(lead["step"], 1)
        contact = dict(conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().one())
        if is_nurture(st, step) and not (st["nurture"] and contact["marketing_consent"] and not contact["unsubscribed_at"]):
            step = max(0, len(st["days"]) - 1)
        conn.execute(update(L).where(L.c.id == lead["id"]).values(status="new", stop_reason=None, paused=False, step=step,
                                                                  next_at=now + timedelta(minutes=FIRST_MINUTES), blocked=None,
                                                                  updated_at=func.now()))
    elif act == "skip":
        if lead["status"] != "new" or lead["next_at"] is None:
            raise Invalid("nothing is due for this lead", "lead")
        contact = dict(conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().one())
        _touch(conn, lead, how="skip", status="skipped", by=actor, note="owner", advance=True, st=st, contact=contact, now=now)
    else:
        raise Invalid("unknown action", "act")
    log(conn, biz["id"], f"lead.{act}", actor, lead["id"])
    return get(conn, biz["id"], lead["id"])


# ---------------------------------------------------------------- Valeria suggests: the next best thing, per lead
def _last_msgs(conn: Connection, business_id: UUID, contact_ids: list) -> dict:
    """{contact_id: (direction, created_at, conversation_id)} — the last message with each of them, any channel."""
    if not contact_ids:
        return {}
    sub = (select(CV.c.contact_id, M.c.direction, M.c.created_at, M.c.conversation_id,
                  func.row_number().over(partition_by=CV.c.contact_id, order_by=M.c.created_at.desc()).label("rn"))
           .join(CV, CV.c.id == M.c.conversation_id)
           .where(M.c.business_id == business_id, CV.c.contact_id.in_(contact_ids), M.c.status != "blocked").subquery())
    return {r.contact_id: (r.direction, r.created_at, r.conversation_id)
            for r in conn.execute(select(sub).where(sub.c.rn == 1))}


def suggestions(conn: Connection, biz: dict, now: datetime | None = None, limit: int = 50) -> list[dict]:
    """Today's next best action per lead, most urgent first: answer (they wrote, nobody answered), follow up (day N of the
    plan is due; in autopilot only the ones she couldn't send herself), check in (a talk went quiet). One per lead."""
    now = _now(now)
    st = settings(conn, biz["id"])
    rows = [dict(r) for r in conn.execute(_base(biz["id"]).where(L.c.status.in_(OPEN), L.c.paused.is_(False))
                                          .order_by(L.c.created_at).limit(1000)).mappings()]
    last = _last_msgs(conn, biz["id"], [r["contact_id"] for r in rows])
    out = []
    for r in rows:
        lm = last.get(r["contact_id"])
        r["conversation_id"] = lm[2] if lm else None
        if lm and lm[0] == "in" and now - lm[1] >= timedelta(minutes=2):
            out.append({**r, "do": "answer", "since": lm[1], "rank": (0, lm[1])})
            continue
        if r["status"] == "new" and r["next_at"] and r["next_at"] <= now and st["followup"] and not st["paused_at"]:
            if st["mode"] == "suggest" or r["blocked"]:
                out.append({**r, "do": "followup", "since": r["next_at"], "rank": (1 if r["hot_at"] else 2, r["next_at"]),
                            "nurture": is_nurture(st, r["step"]), "day": (st["days"][r["step"]] if not is_nurture(st, r["step"]) else None)})
            continue
        if r["status"] == "talking" and lm and lm[0] == "out" and timedelta(hours=CHECKIN_HOURS) <= now - lm[1] <= timedelta(days=CHECKIN_DAYS):
            recent = conn.execute(select(T.c.id).where(T.c.lead_id == r["id"], T.c.note == "checkin",
                                                       T.c.created_at > now - timedelta(days=7)).limit(1)).first()
            if not recent:
                out.append({**r, "do": "checkin", "since": lm[1], "rank": (3, lm[1])})
    out.sort(key=lambda x: x["rank"])
    return out[:limit]


# ---------------------------------------------------------------- write: drafts, cards, links
STEP_KEYS = ("first", "fu1", "fu3", "fu7", "fu14", "fu30")


def step_key(st: dict, step: int) -> str:
    days = list(st.get("days") or DAYS)
    if step >= len(days):
        return "nurture"
    if days == list(DAYS):
        return STEP_KEYS[step]
    if step == 0:
        return "first" if days[0] == 0 else "fu1"
    if step == len(days) - 1:
        return "fu30"
    return ("fu1", "fu3", "fu7", "fu14")[(step - 1) % 4]


def booking_link(biz: dict) -> str | None:
    if biz.get("slug") and biz.get("booking_enabled") and blocks.on(biz, "calendar"):
        return f"{config.PUBLIC_URL()}/b/{biz['slug']}"
    return None


def lang_for(conn: Connection, biz: dict, contact: dict) -> str:
    """The language to write to them in: theirs if known, else what their last message was in, else the business's."""
    from ..web.i18n import ui_lang
    if contact.get("language"):
        return ui_lang(contact["language"])
    from .ai import detect_language
    body = conn.execute(select(M.c.body).join(CV, CV.c.id == M.c.conversation_id).where(
        CV.c.business_id == biz["id"], CV.c.contact_id == contact["id"], M.c.direction == "in", M.c.body != "")
        .order_by(M.c.created_at.desc()).limit(1)).scalar()
    return ui_lang(detect_language(body or "") or biz["language"])


def template_text(conn: Connection, biz: dict, contact: dict, do: str, key: str, lang: str) -> str:
    """The plain draft (no AI): the step's words in their language, the booking line, the offer (nurture)."""
    first = (contact.get("name") or "").split(" ")[0]
    k = {"answer": "ld_m_first", "checkin": "ld_m_checkin"}.get(do) or f"ld_m_{key}"
    text = _t(k if first else k + "_noname", lang).format(name=first, business=biz["name"])
    link = booking_link(biz)
    if link and do in ("followup", "checkin") and key not in ("fu30",):
        text += " " + _t("ld_m_book_line", lang).format(link=link)
    if key == "nurture":
        offer = conn.execute(select(db.growth_settings.c.offer).where(db.growth_settings.c.business_id == biz["id"])).scalar()
        if offer:
            text += " " + offer
    return text.strip()


def draft(biz: dict, lead_id, do: str = "followup", http=None) -> dict:
    """-> {"text", "by_ai", "lang"}: written by the business's OWN AI key from the chat so far (never the operator's), else the
    plain words for this step. Nothing is sent."""
    from . import ai as AI
    with db.tx() as conn:
        lead = get(conn, biz["id"], lead_id)
        contact = dict(conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().one())
        st = settings(conn, biz["id"])
        lang = lang_for(conn, biz, contact)
        key = step_key(st, lead["step"])
        plain = template_text(conn, biz, contact, do, key, lang)
        creds = AI.own_key(conn, biz)
        conv = conn.execute(select(CV.c.id).where(CV.c.business_id == biz["id"], CV.c.contact_id == contact["id"])
                            .order_by(CV.c.last_message_at.desc().nulls_last()).limit(1)).scalar()
        turns = AI._history(conn, biz["id"], conv) if conv else []
        facts = AI._facts(conn, biz) if creds else ""
        style = AI._style(conn, biz) if creds else ""
    if not creds or not turns:
        return {"text": plain, "by_ai": False, "lang": lang}
    prov, key_, model = creds
    goal = {"answer": "Answer their last message.", "checkin": "The chat went quiet after our last message: check in kindly.",
            "followup": f"They haven't answered for a while: write follow-up message number {lead['step'] + 1}. Never repeat an "
                        "earlier message; give one small reason to answer."}.get(do, "")
    system = (f"You write ONE short chat message (1-3 sentences) for {biz['name']}, on its behalf, to a person who contacted it "
              f"first. {goal} Use ONLY the facts below; never invent prices, availability or promises; no pressure, no "
              f"guilt. Write in {AI._NAMES.get(lang, 'the language of their messages')}. Only the message, nothing else.\n"
              f"{style}\nFACTS:\n{facts}" + (f"\nBooking link: {booking_link(biz)}" if booking_link(biz) else ""))
    try:
        text = AI._call(prov, key_, model.split(",")[0].strip(), system, turns + [("user", "(write the message now)")]
                        if turns[-1][0] == "assistant" else turns, AI._http(http), max_tokens=300)
    except Exception:                                          # noqa: BLE001 — the plain words, never nothing
        return {"text": plain, "by_ai": False, "lang": lang}
    text = (text or "").replace(AI.HANDOFF, "").strip()[:1000]
    return {"text": text or plain, "by_ai": bool(text), "lang": lang}


def cards(conn: Connection, biz: dict, lang: str) -> list[dict]:
    """One-line cards the owner drops into a message: services (with price and the booking link), products, the offer."""
    from . import booking as BK
    pack = packs.country(biz["country"])
    out = []
    link = booking_link(biz)
    for s in BK.services(conn, biz["id"], active_only=True)[:12]:
        price = money.fmt(s["price_minor"], biz["currency"], pack.locale) if s["price_minor"] is not None else ""
        bl = f"{link}/{s['id']}" if link else ""
        out.append({"kind": "service", "title": s["name"], "text": " — ".join(x for x in (s["name"], price, bl) if x)})
    CI = db.catalog_items
    for p in conn.execute(select(CI.c.name, CI.c.price_minor, CI.c.url).where(CI.c.business_id == biz["id"], CI.c.archived.is_(False),
                                                                             CI.c.price_minor.isnot(None))
                          .order_by(CI.c.name).limit(12)).all():
        out.append({"kind": "product", "title": p.name,
                    "text": " — ".join(x for x in (p.name, money.fmt(p.price_minor, biz["currency"], pack.locale), p.url or "") if x)})
    if link:
        out.append({"kind": "booking", "title": _t("ld_card_booking", lang), "text": _t("ld_m_book_line", lang).format(link=link)})
    offer = conn.execute(select(db.growth_settings.c.offer).where(db.growth_settings.c.business_id == biz["id"])).scalar()
    if offer:
        out.append({"kind": "offer", "title": _t("ld_card_offer", lang), "text": offer})
    return out


def deeplinks(contact: dict, text: str, subject: str = "") -> list[dict]:
    """The owner's OWN apps, the message filled in: WhatsApp, SMS, email — for when there is no account to send from."""
    out = []
    if contact.get("phone"):
        out.append({"kind": "whatsapp", "url": f"https://wa.me/{contact['phone'].lstrip('+')}?text={quote(text)}"})
        out.append({"kind": "sms", "url": f"sms:{contact['phone']}?body={quote(text)}"})
    if contact.get("email"):
        out.append({"kind": "email", "url": f"mailto:{contact['email']}?subject={quote(subject)}&body={quote(text)}"})
    return out


# ---------------------------------------------------------------- send (one tap, or autopilot)
# Autopilot sends only through an official business API the business connected itself (WhatsApp Business Cloud API or
# through Twilio, its Page, SMS, email, its bot…) — never a "generic gateway", which could be a personal WhatsApp bridge:
# a personal WhatsApp is never automated (the owner sends from it with one tap: wa.me, the share sheet).
AUTO_AVOID = frozenset({"webhook"})


def _account(conn: Connection, biz: dict, contact_id: UUID, *, auto: bool = False):
    from .followups import _pick_account
    aid = _pick_account(conn, biz, contact_id, AUTO_AVOID if auto else frozenset())
    if not aid:
        return None, None
    return aid, conn.execute(select(A.c.channel).where(A.c.id == aid)).scalar()


def _deliver(biz: dict, actor: str, contact: dict, account_id, channel: str, body: str, kind: str, st: dict, media=None,
             http=None, now: datetime | None = None) -> tuple[str, str | None, UUID | None]:
    """-> (sent | failed | blocked, why, message id). Outside a chat's 24 h window: the business's approved template, if it
    set one; else blocked (the owner sends it from their own app). Words and a voice note: the words first, then the note
    (WhatsApp shows no caption under a voice note)."""
    from . import inbox as X
    if media and body:
        res = _deliver(biz, actor, contact, account_id, channel, body, kind, st, None, http, now)
        if res[0] != "sent":
            return res
        _deliver(biz, actor, contact, account_id, channel, "", kind, st, media, http, now)
        return res
    try:
        msg = X.send(biz, actor, contact_id=contact["id"], body=body, media=media, kind=kind, channel_account_id=account_id,
                     http=http, now=now)
    except Blocked as b:
        if b.reason == "outside_reply_window" and st.get("template"):
            try:
                msg = X.send(biz, actor, contact_id=contact["id"], kind=kind, channel_account_id=account_id, http=http, now=now,
                             template={"name": st["template"], "language": biz["language"],
                                       "params": [p for p in ((contact.get("name") or "").split(" ")[0],) if p]})
            except Blocked as b2:
                return "blocked", b2.reason, None
        else:
            return "blocked", b.reason, None
    if msg["status"] == "failed":
        return "failed", msg.get("error"), msg["id"]
    return "sent", None, msg["id"]


def send(biz: dict, actor: str, lead_id, *, body: str, do: str = "followup", voice_id=None, http=None,
         now: datetime | None = None) -> dict:
    """The owner's one tap: the message (and their voice note, if recorded) from the business's own account. ->
    {"status", "why", "links"} — blocked comes back with the owner's own-app links (WhatsApp, SMS, email) instead."""
    now = _now(now)
    body = (body or "").strip()[:1600]
    with db.tx() as conn:
        lead = get(conn, biz["id"], lead_id)
        contact = dict(conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().one())
        st = settings(conn, biz["id"])
        media = None
        if voice_id:
            from . import leadvoice as LV
            media = [LV.media(conn, biz["id"], voice_id, contact["id"])]
        if not body and not media:
            raise Invalid("write the message", "body")
        aid, ch = _account(conn, biz, contact["id"])
    nurture = do == "followup" and is_nurture(st, lead["step"])
    if contact.get("unsubscribed_at") and nurture:
        return {"status": "blocked", "why": "unsubscribed", "links": []}
    if not aid:
        return {"status": "blocked", "why": "no_channel", "links": deeplinks(contact, body, biz["name"])}
    from .leadvoice import WHATSAPP_OK
    if media and (ch not in AUDIO_CHANNELS or (ch.startswith("whatsapp") and media[0]["mime"] not in WHATSAPP_OK)):
        body = (body + "\n" if body else "") + "🎧 " + media[0]["url"]
        media = None
    status, why, mid = _deliver(biz, actor, contact, aid, ch, body, "marketing" if nurture else "service", st, media, http, now)
    with db.tx() as conn:
        lead = get(conn, biz["id"], lead_id)
        if status == "blocked":
            return {"status": status, "why": why, "links": deeplinks(contact, body, biz["name"])}
        advance = do == "followup" and lead["status"] == "new" and lead["next_at"] is not None and status == "sent"
        _touch(conn, lead, how="voice" if media else "tap", status="sent" if status == "sent" else "failed", by=actor,
               channel=ch, message_id=mid, note=do if do != "followup" else "", advance=advance, st=st, contact=contact, now=now)
        log(conn, biz["id"], "lead.sent" if status == "sent" else "lead.failed", actor, lead["id"], {"do": do, "channel": ch})
    return {"status": status, "why": why, "links": []}


def opened_app(conn: Connection, biz: dict, actor: str, lead_id, do: str, via: str, now: datetime | None = None,
               voice: bool = False) -> None:
    """The owner tapped "open WhatsApp / SMS / email" with the message filled in: on record as done by them (we can't see their
    own app), and the plan moves on."""
    now = _now(now)
    lead = get(conn, biz["id"], lead_id)
    contact = dict(conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().one())
    st = settings(conn, biz["id"])
    advance = do == "followup" and lead["status"] == "new" and lead["next_at"] is not None
    _touch(conn, lead, how="voice" if voice else "link", status="opened", by=actor, channel=via[:20], note=do if do != "followup" else "",
           advance=advance, st=st, contact=contact, now=now)
    log(conn, biz["id"], "lead.opened_app", actor, lead["id"], {"do": do, "via": via[:20]})


# ---------------------------------------------------------------- talk 24/7: what the business's AI knows about a lead
def ai_facts(conn: Connection, biz: dict, conversation_id) -> str:
    """For the AI's answer (ai.draft): this person reached out first — qualify gently, offer to book, hand over when ready."""
    cid = conn.execute(select(CV.c.contact_id).where(CV.c.id == conversation_id, CV.c.business_id == biz["id"])).scalar()
    lead = lead_of(conn, biz["id"], cid) if cid else None
    if not lead or lead["status"] not in OPEN:
        return ""
    src = conn.execute(select(SRC.c.name).where(SRC.c.id == lead["source_id"])).scalar() if lead["source_id"] else None
    return ("\n\nLEAD: this person contacted the business first" + (f" (they came from: {src})" if src else "") + ". Answer from "
            "the facts, then ask ONE short question to understand what they need and when. When they are ready, offer to book"
            + (" with the booking link" if booking_link(biz) else "") + ". If they are ready to buy or want a price you don't "
            f"have, say a person will confirm and end with {_handoff()}.")


def _handoff() -> str:
    from .ai import HANDOFF
    return HANDOFF


# ---------------------------------------------------------------- the job: due follow-ups, hot leads told
def _quiet_until(biz: dict, now: datetime) -> datetime | None:
    from .campaigns import polite_until
    return polite_until(biz, now)


def _sent_today(conn: Connection, biz: dict, now: datetime) -> int:
    tz = ZoneInfo(biz["timezone"])
    start = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tz)
    return conn.execute(select(func.count()).select_from(T).where(T.c.business_id == biz["id"], T.c.how.in_(("auto", "voice")),
                                                                  T.c.by == "valeria", T.c.created_at >= start)).scalar()


def _stale(lead: dict, st: dict, now: datetime) -> bool:
    """This step's time passed and the next one's came: it was missed (not tapped, not sendable) — move on."""
    if is_nurture(st, lead["step"]) or lead["step"] + 1 >= len(st["days"]):
        return False
    nxt = due_at(lead, st, lead["step"] + 1)
    return bool(nxt and now >= nxt)


def round_(now: datetime | None = None, http=None, dry_run: bool | None = None, limit: int = 300, business_id=None) -> dict:
    """Every minute: each due follow-up — skipped when already answered or missed, ended when the plan is over, sent by
    Valeria in autopilot (inside the rules), left as a one-tap suggestion otherwise — and hot leads told to the owner.
    dry_run (or VO_LEADS_DRY_RUN=1): only says what would happen; nothing is written, nothing is sent."""
    now = _now(now)
    if dry_run is None:
        dry_run = config.get("VO_LEADS_DRY_RUN", "0") == "1"
    stats = {"sent": 0, "skipped": 0, "later": 0, "suggest": 0, "ended": 0, "blocked": 0, "told": 0, "dry_run": dry_run}
    plan: list[dict] = []
    B = db.businesses
    with db.tx() as conn:
        q = (select(L).join(B, B.c.id == L.c.business_id).outerjoin(LS, LS.c.business_id == L.c.business_id)
             .where(L.c.next_at <= now, L.c.status == "new", L.c.paused.is_(False), B.c.suspended_at.is_(None),
                    or_(LS.c.business_id.is_(None), and_(LS.c.followup, LS.c.paused_at.is_(None)))))
        if business_id:                                        # the owner's "what would Valeria do today?"
            q = q.where(L.c.business_id == business_id)
        due = [dict(r) for r in conn.execute(q.order_by(L.c.next_at).limit(limit)).mappings()]
    bizs: dict = {}
    for lead in due:
        res = _one(lead, bizs, now, http, dry_run)
        stats[res["do"]] += 1
        plan.append({"lead_id": str(lead["id"]), **res})
    stats["told"] = _tell_hot(now, http, dry_run, plan, business_id)
    if dry_run:
        stats["plan"] = plan
    return stats


def _one(lead: dict, bizs: dict, now: datetime, http, dry_run: bool) -> dict:
    from . import billing
    from . import owner as OW
    with db.tx() as conn:
        if lead["business_id"] not in bizs:
            b = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == lead["business_id"])).mappings().one())
            bizs[lead["business_id"]] = (b, settings(conn, b["id"]))
        biz, st = bizs[lead["business_id"]]
        contact = conn.execute(select(C).where(C.c.id == lead["contact_id"])).mappings().first()
        contact = dict(contact) if contact else None
        if not contact or contact["deleted_at"]:
            if not dry_run:
                _stop(conn, lead, "stopped", "erased", now)
            return {"do": "ended", "why": "erased"}
        if lead["step"] == 0:
            ours = conn.execute(select(M.c.id).join(CV, CV.c.id == M.c.conversation_id).where(
                CV.c.business_id == biz["id"], CV.c.contact_id == contact["id"], M.c.direction == "out",
                M.c.created_at >= lead["created_at"]).limit(1)).first()
            if ours:                                           # someone (Valeria, the owner) already answered: day 0 is done
                if not dry_run:
                    _touch(conn, lead, how="skip", status="skipped", by="valeria", note="answered", advance=True, st=st,
                           contact=contact, now=now)
                return {"do": "skipped", "why": "answered"}
        if _stale(lead, st, now):
            if not dry_run:
                _touch(conn, lead, how="skip", status="skipped", by="valeria", note="missed", advance=True, st=st, contact=contact,
                       now=now)
            return {"do": "skipped", "why": "missed"}
        nurture = is_nurture(st, lead["step"])
        if nurture and (not st["nurture"] or not contact["marketing_consent"] or contact["unsubscribed_at"]):
            if not dry_run:
                conn.execute(update(L).where(L.c.id == lead["id"]).values(status="quiet", stop_reason="done", next_at=None,
                                                                          updated_at=func.now()))
            return {"do": "ended", "why": "no_offers_consent"}
        auto = (st["mode"] == "autopilot" and blocks.on(biz, "followups") and not OW.paused(conn, biz["id"])
                and not billing.paused(biz))
        if not auto:
            return {"do": "suggest", "why": "suggest_mode" if st["mode"] == "suggest" else "autopilot_off"}
        if lead["blocked"]:
            return {"do": "suggest", "why": lead["blocked"]}
        if _sent_today(conn, biz, now) >= st["daily_cap"]:
            return {"do": "suggest", "why": "daily_cap"}
        wait = _quiet_until(biz, now)
        if wait:
            if not dry_run:
                conn.execute(update(L).where(L.c.id == lead["id"]).values(next_at=wait, updated_at=func.now()))
            return {"do": "later", "why": "quiet_hours", "at": wait.isoformat()}
        aid, ch = _account(conn, biz, contact["id"], auto=True)
        if not aid:
            if not dry_run:
                conn.execute(update(L).where(L.c.id == lead["id"]).values(blocked="no_channel", updated_at=func.now()))
            return {"do": "blocked", "why": "no_channel"}
        lang = lang_for(conn, biz, contact)
        text = template_text(conn, biz, contact, "followup", step_key(st, lead["step"]), lang)
    if dry_run:
        return {"do": "sent", "why": "would_send", "channel": ch, "text": text}
    with db.tx() as conn:
        from . import ai as AI
        has_ai = AI.own_key(conn, biz) is not None
    if has_ai:
        text = draft(biz, lead["id"], "followup", http=http)["text"]
    if ch == "sms":
        from .campaigns import stop_line
        line = stop_line(biz, "sms")
        if line and line not in text:
            text = f"{text}\n{line}"
    media = None
    if st["voice"] and lead["step"] == 1 and ch in AUDIO_CHANNELS:      # one short voice note, in HER OWN voice, on day 1
        from . import leadvoice as LV
        try:
            media = [LV.valeria_note(biz, contact, text, lang, http=http)]
            if ch.startswith("whatsapp") and media[0]["mime"] not in LV.WHATSAPP_OK:
                media = None                                   # WhatsApp takes no .wav: the words alone
        except Exception:                                      # noqa: BLE001 — no voice: the text still goes
            media = None
    kind = "marketing" if nurture else "service"
    status, why, mid = _deliver(biz, "valeria", contact, aid, ch, text, kind, st, media, http, now)
    if status == "blocked" and media:                          # a channel that refused the audio: the words alone
        status, why, mid = _deliver(biz, "valeria", contact, aid, ch, text, kind, st, None, http, now)
        media = None
    with db.tx() as conn:
        fresh = lead_of(conn, biz["id"], contact["id"])
        if not fresh or fresh["status"] != "new":
            return {"do": "skipped", "why": "changed"}
        if status == "blocked":
            if why in ("unsubscribed", "no_consent", "erased"):
                _stop(conn, fresh, "stopped" if why != "no_consent" else "quiet", "optout" if why == "unsubscribed" else "done", now)
                return {"do": "ended", "why": why}
            if why == "quiet_hours":
                conn.execute(update(L).where(L.c.id == fresh["id"]).values(next_at=_quiet_until(biz, now) or now + timedelta(hours=1)))
                return {"do": "later", "why": why}
            conn.execute(update(L).where(L.c.id == fresh["id"]).values(blocked=(why or "blocked")[:24], updated_at=func.now()))
            return {"do": "blocked", "why": why}
        _touch(conn, fresh, how="voice" if media else "auto", status="sent" if status == "sent" else "failed", by="valeria",
               channel=ch, message_id=mid, advance=status == "sent", st=st, contact=contact, now=now)
        log(conn, biz["id"], "lead.sent" if status == "sent" else "lead.failed", "valeria", fresh["id"],
            {"do": "followup", "channel": ch, "step": fresh["step"], "auto": True})
    return {"do": "sent" if status == "sent" else "blocked", "why": why, "channel": ch}


def _tell_hot(now: datetime, http, dry_run: bool, plan: list, business_id=None) -> int:
    """A lead who asked the price or to book, or whom the AI handed to a person: one note on the owner's chat, with who they
    are, where they came from and what they asked (Valeria's question queue — the owner's own chat, never the customer)."""
    from . import owner as OW
    n = 0
    with db.tx() as conn:
        q = (select(L, C.c.name, SRC.c.name.label("source_name")).join(C, C.c.id == L.c.contact_id)
             .outerjoin(SRC, SRC.c.id == L.c.source_id).where(L.c.hot_at > now - timedelta(hours=2), L.c.status.in_(OPEN)))
        if business_id:
            q = q.where(L.c.business_id == business_id)
        rows = [dict(r) for r in conn.execute(q).mappings()]
        for r in rows:
            if conn.execute(select(db.assistant_actions.c.id).where(db.assistant_actions.c.business_id == r["business_id"],
                                                                    db.assistant_actions.c.dedupe == f"lead:hot:{r['id']}")).first():
                continue
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
            if not blocks.on(biz, "assistant"):
                continue
            lang = OW.lang_of(biz)
            said = conn.execute(select(func.coalesce(func.nullif(M.c.body, ""), M.c.transcript, "")).join(CV, CV.c.id == M.c.conversation_id)
                                .where(CV.c.contact_id == r["contact_id"], M.c.direction == "in")
                                .order_by(M.c.created_at.desc()).limit(1)).scalar() or "…"
            text = _t("ld_hot", lang).format(name=(r["name"] or "—").split(" ")[0] or "—", source=label(r, lang),
                                             text=said[:200], n=r["touches"], link=f"{config.PUBLIC_URL()}/app/leads/{r['id']}")
            n += 1
            if dry_run:
                plan.append({"lead_id": str(r["id"]), "do": "tell", "text": text})
                continue
            OW.ask(conn, biz, "notice", text, {"text": text, "lead_id": str(r["id"])}, contact_id=r["contact_id"],
                   dedupe=f"lead:hot:{r['id']}", owner_says_go=True, now=now)
    return n


# ---------------------------------------------------------------- source -> result
def report(conn: Connection, biz: dict, days: int | None = 90, lang: str = "en") -> dict:
    """Per source: leads, replied (they answered back), booked, won (won, or paid anything), money paid since they came in."""
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    s = _base(biz["id"])
    if since:
        s = s.where(L.c.created_at >= since)
    rows = [dict(r) for r in conn.execute(s).mappings()]
    paid = _paid(conn, biz, [r["contact_id"] for r in rows])
    groups: dict = {}
    for r in rows:
        key = f"src:{r['source_id']}" if r["source_id"] else f"o:{origin_family(r['origin'])}"
        g = groups.setdefault(key, {"key": key, "label": label(r, lang), "kind": r["source_kind"], "leads": 0, "replied": 0,
                                    "booked": 0, "won": 0, "paid_minor": 0, "clicks": None, "source_id": r["source_id"]})
        g["leads"] += 1
        g["replied"] += int(bool(r["replied_at"]))
        g["booked"] += int(bool(r["booked_at"]))
        p = sum(v for (cid, at), v in paid.items() if cid == r["contact_id"] and at >= r["created_at"])
        g["won"] += int(bool(r["won_at"]) or p > 0)
        g["paid_minor"] += p
    for src in sources(conn, biz["id"]):
        g = groups.setdefault(f"src:{src['id']}", {"key": f"src:{src['id']}", "label": src["name"], "kind": src["kind"], "leads": 0,
                                                    "replied": 0, "booked": 0, "won": 0, "paid_minor": 0, "source_id": src["id"]})
        g["clicks"] = src["clicks"]
    out = sorted(groups.values(), key=lambda g: (-g["leads"], -(g.get("clicks") or 0), g["label"]))
    tot = {k: sum(g[k] for g in out) for k in ("leads", "replied", "booked", "won", "paid_minor")}
    return {"rows": out, "total": tot, "days": days}


def _paid(conn: Connection, biz: dict, contact_ids: list) -> dict:
    """{(contact_id, paid_at): amount} in the business's currency: pay links / received payments, and register sales and
    invoices paid without one (never twice)."""
    if not contact_ids:
        return {}
    P, S, I = db.payments, db.pos_sales, db.invoices
    out: dict = {}
    q = [select(P.c.contact_id, func.coalesce(P.c.paid_at, P.c.created_at), P.c.amount_minor).where(
            P.c.business_id == biz["id"], P.c.status == "paid", P.c.currency == biz["currency"], P.c.contact_id.in_(contact_ids)),
         select(S.c.contact_id, func.coalesce(S.c.paid_at, S.c.created_at), S.c.total_minor).where(
            S.c.business_id == biz["id"], S.c.status == "paid", S.c.payment_id.is_(None), S.c.contact_id.in_(contact_ids)),
         select(I.c.contact_id, I.c.created_at, I.c.total_minor).where(
            I.c.business_id == biz["id"], I.c.status == "paid", I.c.payment_id.is_(None), I.c.contact_id.in_(contact_ids))]
    for sq in q:
        for cid, at, amt in conn.execute(sq).all():
            out[(cid, at)] = out.get((cid, at), 0) + int(amt or 0)
    return out


def counts(conn: Connection, biz: dict, now: datetime | None = None) -> dict:
    """For Today: how many leads came in this week and how many want a tap today."""
    now = _now(now)
    week = conn.execute(select(func.count()).select_from(L).where(L.c.business_id == biz["id"],
                                                                  L.c.created_at > now - timedelta(days=7))).scalar()
    return {"week": week, "today": len(suggestions(conn, biz, now))}


# ---------------------------------------------------------------- platforms that only email: the forwarding address
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+|00)?\d[\d\s().-]{6,}\d")
_NAME = re.compile(r"^\s*(?:name|full name|nombre|nome|nom|vorname|pangalan|nama|नाम|الاسم|名前|姓名|氏名)\s*[:：]\s*(.{2,80})$",
                   re.I | re.M)


def mail_in(data: dict) -> dict:
    """A lead email forwarded to <code>@<lead domain> (already verified): the person in it -> a lead of that source, in the
    inbox with the email's words. -> {"ok", "contact_id"} | {"ok": False, "why"}. The same email twice = once."""
    from . import site as ST
    to = str(data.get("to") or "").strip().lower()
    code = to.split("@", 1)[0].split("+", 1)[0]
    mid = str(data.get("message_id") or "").strip()[:300]
    with db.tx() as conn:
        try:
            src, biz = by_code(conn, code)
        except NotFound:
            return {"ok": False, "why": "unknown_address"}
        if mid and conn.execute(select(db.events.c.id).where(db.events.c.business_id == biz["id"], db.events.c.type == "lead.mail",
                                                             db.events.c.data["message_id"].astext == mid)).first():
            return {"ok": True, "duplicate": True}
        sender = str(data.get("from") or "").strip().lower()
        text = str(data.get("text") or "")[:20000]
        subject = " ".join(str(data.get("subject") or "").split())[:200]
        emails = [e.lower() for e in _EMAIL.findall(text) if e.lower() not in (sender, to)]
        reply_to = str(data.get("reply_to") or "").strip().lower()
        email = reply_to if reply_to and reply_to not in (sender, to) else (emails[0] if emails else "")
        phone = ""
        pack = packs.country(biz["country"])
        for m in _PHONE.findall(text):
            try:
                phone = phones.e164(m, pack)
                break
            except phones.BadPhone:
                continue
        nm = _NAME.search(text)
        name = (nm.group(1).strip() if nm else "") or str(data.get("name") or "").strip() or email or phone
        if not (email or phone):
            log(conn, biz["id"], "lead.mail_unread", "lead:mail", src["id"], {"message_id": mid, "subject": subject})
            return {"ok": False, "why": "no_contact"}
        body = f"✉️ {src['name']}" + (f" · {subject}" if subject else "") + "\n" + " ".join(text.split())[:1800]
        res = ST.contact_form(conn, biz, name=name[:200], phone=phone, email=email, message=body,
                              source=f"lead:mail:{src['code']}"[:60], event="lead.mail_in")
        lead = lead_of(conn, biz["id"], res["contact"]["id"])
        if lead:
            _set_source(conn, biz, lead, src["id"])
        log(conn, biz["id"], "lead.mail", "lead:mail", res["contact"]["id"], {"message_id": mid, "source_id": str(src["id"])})
        return {"ok": True, "contact_id": str(res["contact"]["id"])}


def widget_in(code: str, d: dict, *, origin: str = "") -> dict:
    """The website snippet sent a lead (checks done by the caller: honeypot, time, rate). -> {"ok"} or Invalid."""
    from . import contacts as CT
    from . import site as ST
    if not d.get("consent"):
        raise Invalid("consent", "consent")
    with db.tx() as conn:
        src, biz = by_code(conn, code)
        contact_v = str(d.get("contact") or "").strip()[:200]
        phone, email = (str(d.get("phone") or "").strip()[:40], str(d.get("email") or "").strip()[:320])
        if contact_v and not (phone or email):
            phone, email = ("", contact_v) if "@" in contact_v else (contact_v, "")
        msg = str(d.get("message") or "").strip()[:1800] or "—"
        res = ST.contact_form(conn, biz, name=str(d.get("name") or "").strip()[:200], phone=phone, email=email,
                              message=f"🌐 {src['name']}\n{msg}", source=f"lead:widget:{src['code']}"[:60], event="lead.widget")
        contact = res["contact"]
        lead = lead_of(conn, biz["id"], contact["id"])
        if lead:
            _set_source(conn, biz, lead, src["id"])
        if d.get("offers") and not contact["marketing_consent"]:
            where = f"website snippet {src['code']}" + (f" on {origin[:80]}" if origin else "")
            CT.patch(conn, biz, f"lead:widget:{src['code']}", contact["id"], {"marketing_consent": True,
                                                                              "marketing_consent_source": where[:200]})
    return {"ok": True}
