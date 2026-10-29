"""Grow: bring customers back, and let them bring their friends.

Win-back. A customer has "gone quiet" when nothing came from them in N days (60 unless the owner says
otherwise): no message, no visit or booking, no payment, no signed quote — and they are not new. Once a day
(the business's own day, from 09:00 there) each quiet customer who agreed to promotions and can be reached
gets ONE friendly message: the owner's optional offer line and the booking link (or "just reply"). Never again
within 180 days, at most 30 a day (both the owner's numbers too). It is a follow-up rule like any other
(trigger contact.quiet, preset winback) and goes through the same outbox, so every send rule applies there:
consent, unsubscribed / STOP, quiet hours, WhatsApp's 24 h window (an approved template, else skipped).
Results: sent, replied, and came back (a booking, a payment or a signed quote within 30 days of the message).

Bring a friend. Each customer can have a personal link /f/<code> (random, can't be guessed or counted
through). A friend who opens it sees the business, their reward and a way in (book, the website, or leave a
number); the phone remembers the link for 14 days. When the friend becomes a NEW customer — books, writes
through the website or its chat, or leaves their number on the friend page — the referral is recorded once,
with both rewards the owner set ("a free haircut", "10% off"). Valeria One handles no money: the owner gives
the rewards and ticks them off. A reward is never tied to a review (Google and the FTC forbid paying for
reviews), so no message carries both a review link and a friend link.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, db
from . import Invalid, NotFound, log

GS, RL, RF = db.growth_settings, db.referral_links, db.referrals
AU, SM = db.automations, db.scheduled_messages
DEFAULTS = {"quiet_days": 60, "gap_days": 180, "daily_cap": 30, "offer": "", "reward_referrer": "", "reward_friend": "",
            "ran_on": None}
LIMITS = {"quiet_days": (14, 365), "gap_days": (30, 730), "daily_cap": (1, 500)}
TEXTS = ("offer", "reward_referrer", "reward_friend")
SEND_FROM = 9                     # win-backs are picked from 09:00 in the business's time zone (quiet hours still apply)
CAME_BACK_DAYS = 30               # a booking, payment or signed quote this soon after the message counts as "came back"
RESULTS_DAYS = 90
SHARE_EVERY_DAYS = 90             # the friend link goes out automatically at most once every 90 days per customer
REMEMBER_DAYS = 14                # a friend's phone remembers the link this long (the session cookie's life)
SHARE_WHEN = {"appointment.done": 120, "payment.paid": 30}      # the share message: 2 h after a visit / 30 min after paying
HOWS = ("booking", "website", "chat", "form")
_CODE = re.compile(r"^[A-Za-z0-9_-]{8,16}$")


class Skip(Exception):
    """This message should not go out (the reason is kept on the outbox row)."""


def _t(key: str, biz: dict) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(biz["language"]))


# ---------------------------------------------------------------- settings
def settings(conn: Connection, business_id: UUID) -> dict:
    row = conn.execute(select(GS).where(GS.c.business_id == business_id)).mappings().first()
    return {**DEFAULTS, **(dict(row) if row else {}), "business_id": business_id}


def save_settings(conn: Connection, biz: dict, actor: str, **fields) -> dict:
    vals = {}
    for k, (lo, hi) in LIMITS.items():
        if fields.get(k) is None:
            continue
        try:
            v = int(fields[k])
        except (TypeError, ValueError):
            raise Invalid(f"{k}: a whole number from {lo} to {hi}", k) from None
        if not lo <= v <= hi:
            raise Invalid(f"{k}: a whole number from {lo} to {hi}", k)
        vals[k] = v
    for k in TEXTS:
        if fields.get(k) is not None:
            v = " ".join(str(fields[k]).split())
            if len(v) > 200:
                raise Invalid(f"{k}: at most 200 characters", k)
            vals[k] = v
    if vals:
        conn.execute(pg_insert(GS).values(business_id=biz["id"], **vals)
                     .on_conflict_do_update(index_elements=[GS.c.business_id], set_={**vals, "updated_at": func.now()}))
        if "quiet_days" in vals:                               # the win-back rule waits the same number of days
            conn.execute(update(AU).where(AU.c.business_id == biz["id"], AU.c.preset == "winback", AU.c.active)
                         .values(minutes=vals["quiet_days"] * 1440))
        log(conn, biz["id"], "growth.updated", actor, biz["id"], {"fields": sorted(vals)})
    return settings(conn, biz["id"])


def ready_made(conn: Connection, business_id: UUID, preset: str) -> dict | None:
    r = conn.execute(select(AU).where(AU.c.business_id == business_id, AU.c.preset == preset, AU.c.active)
                     .order_by(AU.c.created_at).limit(1)).mappings().first()
    return dict(r) if r else None


def _clean_message(text: str | None, needs: tuple[str, ...]) -> str | None:
    if text is None:
        return None
    text = (text or "").strip()
    if not text:
        return None
    missing = [p for p in needs if p not in text]
    if missing:
        raise Invalid(f"keep {', '.join(missing)} in the message", "message")
    return text[:1000]


def set_winback(conn: Connection, biz: dict, actor: str, on: bool, *, message: str | None = None,
                template: str | None = None) -> dict | None:
    """Switch the win-back on (one ready-made rule) or off. Its message and template can be changed while on."""
    from . import followups as FU
    rule = ready_made(conn, biz["id"], "winback")
    message = _clean_message(message, ())
    if not on:
        if rule:
            FU.delete_automation(conn, biz["id"], actor, rule["id"])
            log(conn, biz["id"], "winback.off", actor, rule["id"])
        return None
    if template is not None:
        tpl = template.strip()[:100] or None
    else:
        tpl = rule["template"] if rule else None
    if rule:
        vals = {"template": tpl}
        if message:
            vals["message"] = message
        conn.execute(update(AU).where(AU.c.id == rule["id"]).values(**vals))
        return ready_made(conn, biz["id"], "winback")
    s = settings(conn, biz["id"])
    rule = FU.create_automation(conn, biz, actor, trigger=FU.QUIET, minutes=s["quiet_days"] * 1440,
                                message=message or _t("gw_wb_msg", biz), template=tpl, kind="marketing", preset="winback")
    log(conn, biz["id"], "winback.on", actor, rule["id"])
    return rule


def set_share(conn: Connection, biz: dict, actor: str, on: bool, *, when: str | None = None,
              message: str | None = None) -> dict | None:
    """The bring-a-friend message after each visit (or payment): on, off, or moved to the other moment."""
    from . import followups as FU
    rule = ready_made(conn, biz["id"], "referral")
    message = _clean_message(message, ("{referral_link}",))
    if not on:
        if rule:
            FU.delete_automation(conn, biz["id"], actor, rule["id"])
            log(conn, biz["id"], "share.off", actor, rule["id"])
        return None
    when = when or (rule["trigger"] if rule else "appointment.done")
    if when not in SHARE_WHEN:
        raise Invalid(f"when: one of {', '.join(SHARE_WHEN)}", "when")
    s = settings(conn, biz["id"])
    if not (s["reward_friend"] or s["reward_referrer"]):
        raise Invalid("write the rewards first (what the friend gets, what your customer gets)", "reward_friend")
    if rule and rule["trigger"] == when:
        if message:
            conn.execute(update(AU).where(AU.c.id == rule["id"]).values(message=message))
        return ready_made(conn, biz["id"], "referral")
    if rule:                                                   # moved to the other moment: one rule, never two
        FU.delete_automation(conn, biz["id"], actor, rule["id"])
    rule = FU.create_automation(conn, biz, actor, trigger=when, minutes=SHARE_WHEN[when],
                                message=message or (rule or {}).get("message") or _t("gw_share_msg", biz),
                                kind="marketing", preset="referral")
    log(conn, biz["id"], "share.on", actor, rule["id"], {"when": when})
    return rule


# ---------------------------------------------------------------- who went quiet
def _seen(business_id: UUID):
    """When we last heard from or saw each customer: created, wrote, visited (or is booked), paid, signed."""
    C, M, CV, AP, PY, Q = db.contacts, db.messages, db.conversations, db.appointments, db.payments, db.quotes
    wrote = (select(func.max(M.c.created_at)).select_from(M.join(CV, CV.c.id == M.c.conversation_id))
             .where(CV.c.business_id == business_id, CV.c.contact_id == C.c.id, M.c.direction == "in").scalar_subquery())
    visit = (select(func.max(AP.c.starts_at)).where(AP.c.business_id == business_id, AP.c.contact_id == C.c.id,
                                                    AP.c.status != "cancelled").scalar_subquery())
    paid = (select(func.max(PY.c.paid_at)).where(PY.c.business_id == business_id, PY.c.contact_id == C.c.id,
                                                 PY.c.status == "paid").scalar_subquery())
    signed = (select(func.max(Q.c.signed_at)).where(Q.c.business_id == business_id, Q.c.contact_id == C.c.id,
                                                    Q.c.status == "accepted").scalar_subquery())
    return func.greatest(C.c.created_at, wrote, visit, paid, signed)        # GREATEST skips the NULLs


def avoid_channels(rule: dict | None) -> frozenset[str]:
    """A win-back without an approved template can't go on a channel with a reply window (WhatsApp, Messenger…): a quiet
    customer is always outside it. Those channels are passed over, so it goes where it can — or isn't queued at all."""
    if rule and rule.get("preset") == "winback" and not rule.get("template"):
        from .. import channels
        return frozenset(c for c in channels.available() if channels.adapter(c).reply_window_hours)
    return frozenset()


def _reachable(business_id: UUID, avoid: frozenset[str] = frozenset()):
    C, I, A = db.contacts, db.identities, db.channel_accounts
    chans = select(A.c.channel).where(A.c.business_id == business_id, A.c.active, A.c.channel.notin_(("webchat", *avoid)))
    return exists().where(I.c.business_id == business_id, I.c.contact_id == C.c.id, I.c.channel.in_(chans))


def _won_back_lately(business_id: UUID, since: datetime):
    C = db.contacts
    return exists().where(SM.c.business_id == business_id, SM.c.contact_id == C.c.id, SM.c.created_at > since,
                          SM.c.automation_id.in_(select(AU.c.id).where(AU.c.business_id == business_id, AU.c.preset == "winback")))


def quiet(conn: Connection, biz: dict, now: datetime | None = None, limit: int = 50) -> dict:
    """Who has gone quiet (newest first), how many, and whether each one can get the win-back — and if not, why."""
    now = now or datetime.now(timezone.utc)
    s = settings(conn, biz["id"])
    C = db.contacts
    avoid = avoid_channels(ready_made(conn, biz["id"], "winback") or {"preset": "winback"})   # as it is, or as it would be
    seen = _seen(biz["id"]).label("last_seen")
    anywhere = _reachable(biz["id"]).label("anywhere")
    reach = _reachable(biz["id"], avoid).label("reachable")
    lately = _won_back_lately(biz["id"], now - timedelta(days=s["gap_days"])).label("lately")
    base = select(C.c.id, C.c.name, C.c.marketing_consent, C.c.unsubscribed_at, seen, anywhere, reach, lately).where(
        C.c.business_id == biz["id"], C.c.deleted_at.is_(None)).subquery()
    gone = select(base).where(base.c.last_seen < now - timedelta(days=s["quiet_days"]))
    count = conn.execute(select(func.count()).select_from(gone.subquery())).scalar()
    rows = []
    for r in conn.execute(gone.order_by(base.c.last_seen.desc()).limit(max(1, min(limit, 200)))).mappings():
        why = ("unsubscribed" if r["unsubscribed_at"] else "no_consent" if not r["marketing_consent"]
               else "no_channel" if not r["anywhere"] else "needs_template" if not r["reachable"]
               else "messaged" if r["lately"] else None)
        rows.append({"id": r["id"], "name": r["name"], "last_seen": r["last_seen"], "can": why is None, "why": why,
                     "days": max(0, (now - r["last_seen"]).days)})
    ready = conn.execute(select(func.count()).select_from(
        gone.where(base.c.marketing_consent, base.c.unsubscribed_at.is_(None), base.c.reachable, base.c.lately.is_(False))
        .subquery())).scalar()
    return {"count": count, "ready": ready, "rows": rows, "quiet_days": s["quiet_days"]}


def _candidates(conn: Connection, biz: dict, s: dict, rule: dict, now: datetime) -> list[UUID]:
    C = db.contacts
    seen = _seen(biz["id"])
    q = (select(C.c.id).where(C.c.business_id == biz["id"], C.c.deleted_at.is_(None), C.c.marketing_consent,
                              C.c.unsubscribed_at.is_(None), seen < now - timedelta(days=s["quiet_days"]),
                              _reachable(biz["id"], avoid_channels(rule)),
                              ~_won_back_lately(biz["id"], now - timedelta(days=s["gap_days"])))
         .order_by(seen.desc()).limit(s["daily_cap"]))             # the ones who went quiet most recently are likeliest to return
    return [r[0] for r in conn.execute(q)]


def daily(now: datetime | None = None) -> dict:
    """Background job (every minute): once per business per local day, queue today's win-backs in the outbox."""
    from . import billing
    now = now or datetime.now(timezone.utc)
    stats = {"businesses": 0, "queued": 0}
    with db.tx() as conn:
        found = conn.execute(select(AU.c.business_id, AU.c.id).where(AU.c.preset == "winback", AU.c.active)
                             .order_by(AU.c.created_at)).all()
    rules = {}
    for business_id, rule_id in found:
        rules.setdefault(business_id, rule_id)                  # one win-back rule per business (the first, if ever two)
    for business_id, rule_id in rules.items():
        with db.tx() as conn:
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
            if not blocks.on(biz, "grow") or billing.paused(biz):
                continue
            local = now.astimezone(ZoneInfo(biz["timezone"]))
            if local.hour < SEND_FROM:
                continue
            conn.execute(pg_insert(GS).values(business_id=business_id).on_conflict_do_nothing(index_elements=[GS.c.business_id]))
            locked = conn.execute(select(GS.c.ran_on).where(GS.c.business_id == business_id)
                                  .with_for_update(skip_locked=True)).first()
            if not locked or locked.ran_on == local.date():          # another worker has it, or today is done
                continue
            rule = dict(conn.execute(select(AU).where(AU.c.id == rule_id)).mappings().one())
            ids = _candidates(conn, biz, settings(conn, business_id), rule, now)
            for cid in ids:
                conn.execute(insert(SM).values(business_id=business_id, automation_id=rule_id, contact_id=cid, send_at=now))
            conn.execute(update(GS).where(GS.c.business_id == business_id).values(ran_on=local.date()))
            if ids:
                log(conn, business_id, "winback.queued", "system", rule_id, {"count": len(ids)})
            stats["businesses"] += 1
            stats["queued"] += len(ids)
    return stats


def last_seen(conn: Connection, business_id: UUID, contact_id: UUID) -> datetime | None:
    C = db.contacts
    return conn.execute(select(_seen(business_id)).where(C.c.business_id == business_id, C.c.id == contact_id)).scalar()


# ---------------------------------------------------------------- the words that go out
def winback_text(biz: dict, s: dict, text: str, link: str) -> str:
    book = _t("gw_book_line", biz).format(link=link) if link else _t("gw_reply_line", biz)
    return _tidy(text.replace("{offer}", s["offer"]).replace("{book}", book))


def _tidy(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", text).replace(" \n", "\n").strip()


def prepare(conn: Connection, biz: dict, contact: dict, rule: dict, text: str, link: str, now: datetime) -> tuple[str, UUID | None]:
    """Fill Grow's own words into a message about to go out -> (text, the friend link it carries, if any). Raises Skip."""
    s = settings(conn, biz["id"])
    if rule.get("preset") == "winback":
        seen = last_seen(conn, biz["id"], contact["id"])
        if seen and seen > now - timedelta(days=s["quiet_days"]):
            raise Skip("came back before the message went out")
    if rule.get("preset") == "winback" or "{offer}" in text or "{book}" in text:
        text = winback_text(biz, s, text, link)
    if "{referral_link}" not in text:
        return text, None
    if "{review_link}" in text:
        raise Skip("a review request never carries a reward")
    if not (s["reward_friend"] or s["reward_referrer"]):
        raise Skip("no reward set")
    ln = link_for(conn, biz, contact["id"])
    if ln["last_sent_at"] and ln["last_sent_at"] > now - timedelta(days=SHARE_EVERY_DAYS):
        raise Skip(f"sent the friend link in the last {SHARE_EVERY_DAYS} days")
    return share_text(s, text, ln["code"]), ln["id"]


def share_text(s: dict, text: str, code: str) -> str:
    """{referral_link} {reward} (the customer's) {friend_reward}; one reward written = both get it."""
    return _tidy(text.replace("{referral_link}", url(code)).replace("{reward}", s["reward_referrer"] or s["reward_friend"])
                 .replace("{friend_reward}", s["reward_friend"] or s["reward_referrer"]))


def preview(conn: Connection, biz: dict, text: str, quiet_rows: list[dict] | None = None) -> str:
    """The win-back as the next quiet customer would get it (a sample name when nobody is quiet yet)."""
    from .followups import _fill
    rows = quiet(conn, biz)["rows"] if quiet_rows is None else quiet_rows
    name = next((r["name"] for r in rows if r["can"] and r["name"]), None) or _t("gw_sample_name", biz)
    link = f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("booking_enabled") and biz.get("slug") else ""
    return _fill(winback_text(biz, settings(conn, biz["id"]), text, link), name=name, business=biz["name"], link=link)


# ---------------------------------------------------------------- results
def results(conn: Connection, business_id: UUID, now: datetime | None = None) -> dict:
    """Win-backs sent in the last 90 days: how many, how many answered, how many came back (within 30 days)."""
    now = now or datetime.now(timezone.utc)
    M, CV, AP, PY, Q = db.messages, db.conversations, db.appointments, db.payments, db.quotes
    end = SM.c.send_at + timedelta(days=CAME_BACK_DAYS)
    replied = exists().where(M.c.business_id == business_id, M.c.direction == "in", M.c.created_at > SM.c.send_at,
                             M.c.created_at <= end, CV.c.id == M.c.conversation_id, CV.c.contact_id == SM.c.contact_id)
    booked = exists().where(AP.c.business_id == business_id, AP.c.contact_id == SM.c.contact_id, AP.c.status != "cancelled",
                            AP.c.created_at > SM.c.send_at, AP.c.created_at <= end)
    paid = exists().where(PY.c.business_id == business_id, PY.c.contact_id == SM.c.contact_id, PY.c.status == "paid",
                          PY.c.paid_at > SM.c.send_at, PY.c.paid_at <= end)
    signed = exists().where(Q.c.business_id == business_id, Q.c.contact_id == SM.c.contact_id, Q.c.status == "accepted",
                            Q.c.signed_at > SM.c.send_at, Q.c.signed_at <= end)
    rows = conn.execute(select(replied.label("replied"), (booked | paid | signed).label("back"))
                        .where(SM.c.business_id == business_id, SM.c.status == "sent", SM.c.send_at > now - timedelta(days=RESULTS_DAYS),
                               SM.c.automation_id.in_(select(AU.c.id).where(AU.c.business_id == business_id, AU.c.preset == "winback")))
                        ).all()
    return {"sent": len(rows), "replied": sum(1 for r in rows if r.replied), "came_back": sum(1 for r in rows if r.back),
            "days": RESULTS_DAYS, "within_days": CAME_BACK_DAYS}


# ---------------------------------------------------------------- bring a friend
def url(code: str) -> str:
    return f"{config.PUBLIC_URL()}/f/{code}"


def link_for(conn: Connection, biz: dict, contact_id: UUID) -> dict:
    """This customer's own link (made the first time it is needed; the same one after that)."""
    from . import contacts
    contacts.get(conn, biz["id"], contact_id)
    conn.execute(pg_insert(RL).values(business_id=biz["id"], contact_id=contact_id, code=secrets.token_urlsafe(9))
                 .on_conflict_do_nothing(constraint="uq_referral_link"))
    return dict(conn.execute(select(RL).where(RL.c.business_id == biz["id"], RL.c.contact_id == contact_id)).mappings().one())


def link_of(conn: Connection, business_id: UUID, contact_id: UUID) -> dict | None:
    r = conn.execute(select(RL).where(RL.c.business_id == business_id, RL.c.contact_id == contact_id)).mappings().first()
    return dict(r) if r else None


def link_sent(conn: Connection, link_id: UUID, now: datetime | None = None) -> None:
    conn.execute(update(RL).where(RL.c.id == link_id).values(last_sent_at=now or func.now()))


def open_link(code: str, count: bool = True) -> tuple[dict, dict, str]:
    """A friend opened /f/<code> -> (the link, the business, the first name of who sent it). Counted."""
    if not _CODE.match(code or ""):
        raise NotFound("link")
    C = db.contacts
    with db.tx() as conn:
        r = conn.execute(select(RL, C.c.name.label("referrer_name"), C.c.deleted_at.label("gone"))
                         .join(C, C.c.id == RL.c.contact_id).where(RL.c.code == code)).mappings().first()
        if not r or r["gone"]:
            raise NotFound("link")
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
        if not blocks.on(biz, "grow"):                          # switched off (or paused by its host): the link rests
            raise NotFound("link")
        if count:
            conn.execute(update(RL).where(RL.c.id == r["id"]).values(opens=RL.c.opens + 1))
    return dict(r), biz, (r["referrer_name"] or "").split(" ")[0]


def remember(link: dict, now: datetime | None = None) -> dict:
    """What the friend's session keeps: which link, which business, when it was opened."""
    return {"c": link["code"], "b": str(link["business_id"]), "at": (now or datetime.now(timezone.utc)).isoformat()}


def claim(conn: Connection, biz: dict, ref: dict | None, contact: dict, how: str) -> dict | None:
    """The friend just became a customer (in this transaction): record the referral once, with both rewards.
    Nothing happens for someone who was already a customer, for the customer's own link, or another business's."""
    if not ref or not isinstance(ref, dict) or ref.get("b") != str(biz["id"]) or how not in HOWS:
        return None
    if not blocks.on(biz, "grow"):
        return None
    try:
        opened = datetime.fromisoformat(str(ref.get("at")))
    except ValueError:
        return None
    if datetime.now(timezone.utc) - opened > timedelta(days=REMEMBER_DAYS):
        return None
    ln = conn.execute(select(RL).where(RL.c.business_id == biz["id"], RL.c.code == str(ref.get("c") or "")[:16])).mappings().first()
    if not ln or ln["contact_id"] == contact["id"]:            # no link, or their own link: no reward for referring yourself
        return None
    if contact.get("created_at") and contact["created_at"] < opened - timedelta(minutes=1):
        return None                                            # already a customer before the link: not a new friend
    C = db.contacts
    if not conn.execute(select(C.c.id).where(C.c.id == ln["contact_id"], C.c.deleted_at.is_(None))).first():
        return None
    s = settings(conn, biz["id"])
    row = conn.execute(pg_insert(RF).values(business_id=biz["id"], link_id=ln["id"], referrer_id=ln["contact_id"],
                                            friend_id=contact["id"], how=how, reward_referrer=s["reward_referrer"] or s["reward_friend"],
                                            reward_friend=s["reward_friend"] or s["reward_referrer"])
                       .on_conflict_do_nothing(constraint="uq_referral_friend").returning(RF)).mappings().first()
    if not row:
        return None
    log(conn, biz["id"], "referral.joined", "customer", row["id"], {"how": how, "referrer_id": str(ln["contact_id"]),
                                                                    "friend_id": str(contact["id"])})
    return dict(row)


def referrals(conn: Connection, business_id: UUID, limit: int = 100) -> list[dict]:
    C = db.contacts
    ref, fr = C.alias("referrer"), C.alias("friend")
    q = (select(RF, ref.c.name.label("referrer_name"), fr.c.name.label("friend_name"))
         .join(ref, ref.c.id == RF.c.referrer_id).join(fr, fr.c.id == RF.c.friend_id)
         .where(RF.c.business_id == business_id).order_by(RF.c.created_at.desc()).limit(max(1, min(limit, 500))))
    return [dict(r) for r in conn.execute(q).mappings()]


def mark_given(conn: Connection, business_id: UUID, actor: str, referral_id: UUID, who: str) -> dict:
    """The owner gave a reward (to the customer who shared, or to the friend). Once; the time is kept."""
    if who not in ("referrer", "friend"):
        raise Invalid("who: referrer or friend", "who")
    col = RF.c[f"{who}_given_at"]
    row = conn.execute(select(RF).where(RF.c.business_id == business_id, RF.c.id == referral_id)).mappings().first()
    if not row:
        raise NotFound("referral")
    if row[f"{who}_given_at"] is None:
        conn.execute(update(RF).where(RF.c.id == referral_id, col.is_(None)).values({col: func.now()}))
        log(conn, business_id, "referral.rewarded", actor, referral_id, {"who": who})
    return dict(conn.execute(select(RF).where(RF.c.id == referral_id)).mappings().one())


def referral_stats(conn: Connection, business_id: UUID) -> dict:
    links = conn.execute(select(func.count(), func.coalesce(func.sum(RL.c.opens), 0)).where(RL.c.business_id == business_id)).one()
    joined = conn.execute(select(func.count()).select_from(RF).where(RF.c.business_id == business_id)).scalar()
    pending = conn.execute(select(func.count()).select_from(RF).where(
        RF.c.business_id == business_id, (RF.c.referrer_given_at.is_(None)) | (RF.c.friend_given_at.is_(None)))).scalar()
    return {"links": links[0], "opens": int(links[1]), "friends": joined, "rewards_pending": pending}


def send_link(biz: dict, actor: str, contact_id: UUID, channel_account_id: UUID | None = None,
              now: datetime | None = None) -> dict:
    """One tap on a customer's page: their friend link, in the business's words. -> the message sent."""
    from . import inbox as X
    from .followups import _fill, _pick_account
    with db.tx() as conn:
        from . import contacts as C
        contact = C.get(conn, biz["id"], contact_id)
        s = settings(conn, biz["id"])
        if not (s["reward_friend"] or s["reward_referrer"]):
            raise Invalid("write the rewards first (Grow)", "reward_friend")
        if not channel_account_id:
            channel_account_id = _pick_account(conn, biz, contact_id)
            if not channel_account_id:
                raise Invalid("no channel to reach this customer", "channel")
        ln = link_for(conn, biz, contact_id)
        rule = ready_made(conn, biz["id"], "referral")
        text = share_text(s, (rule or {}).get("message") or _t("gw_share_msg", biz), ln["code"])
    body = _fill(text, name=contact["name"], business=biz["name"])
    msg = X.send(biz, actor, contact_id=contact_id, body=body, kind="marketing", channel_account_id=channel_account_id, now=now)
    if msg.get("status") not in ("failed", "blocked"):
        with db.tx() as conn:
            link_sent(conn, ln["id"], now)
            log(conn, biz["id"], "referral.link_sent", actor, contact_id, {"how": "manual"})
    return msg


# ---------------------------------------------------------------- one look at everything (the Grow page, GET /v1/growth)
def overview(conn: Connection, biz: dict, now: datetime | None = None) -> dict:
    wb, sh = ready_made(conn, biz["id"], "winback"), ready_made(conn, biz["id"], "referral")
    s = settings(conn, biz["id"])
    s.pop("business_id", None)
    return {"settings": s,
            "winback": {"on": bool(wb), "message": (wb or {}).get("message") or _t("gw_wb_msg", biz),
                        "template": (wb or {}).get("template"), "rule_id": (wb or {}).get("id")},
            "share": {"on": bool(sh), "when": (sh or {}).get("trigger") or "appointment.done",
                      "message": (sh or {}).get("message") or _t("gw_share_msg", biz), "rule_id": (sh or {}).get("id")},
            "quiet": quiet(conn, biz, now), "results": results(conn, biz["id"], now),
            "referrals": referral_stats(conn, biz["id"])}


def came_through(conn: Connection, business_id: UUID, contact_id: UUID) -> dict | None:
    """On a customer's page: the friend who brought them, if one did."""
    C = db.contacts
    r = conn.execute(select(RF, C.c.name.label("referrer_name")).join(C, C.c.id == RF.c.referrer_id)
                     .where(RF.c.business_id == business_id, RF.c.friend_id == contact_id)).mappings().first()
    return dict(r) if r else None


def brought(conn: Connection, business_id: UUID, contact_id: UUID) -> int:
    return conn.execute(select(func.count()).select_from(RF).where(RF.c.business_id == business_id,
                                                                   RF.c.referrer_id == contact_id)).scalar()

