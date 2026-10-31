"""Money on a booking and seats that free up (bk01, v0.26): a deposit to hold the time, a no-show / late-cancel fee, a
cancellation window — and the waitlist of a full class.

* Per service (booking_services): a deposit (a fixed amount, or a share of the price × the party), a no-show fee, and
  the window in hours: "Deposit ₱200 to confirm. Kept if you cancel less than 24 h before." The words go on the booking
  page and in the chat's confirmation; the booking keeps the policy as it was when it was made (appointments).
* At booking, Valeria sends the business's OWN pay link (payments.py: Stripe, Mercado Pago, PayMongo, Razorpay, bank
  transfer…). The booking waits for it ("booked", not yet "confirmed"); its time is held for the business's
  deposit_hold_minutes (30) from the moment the link went out, then released and the customer told (the link stays
  open: a payment that still arrives is recorded, and the owner is told to book them again or refund). Paid -> confirmed by
  itself (when a signed contract is also asked for, both have to be in: contracts.gate). No payment account connected:
  the owner marks the deposit received by hand (a "received" payment, so it is in Books like any other money in).
* Cancelled by the customer inside the window: the deposit is kept (or, with no deposit, the late-cancel fee can be
  sent); otherwise, or when the business cancels, the deposit is to be returned — the owner is told (refunds are done
  in their own payment account). No-show: the owner taps it and the fee's pay link goes to the customer (less a kept
  deposit). There is no card kept on file: every charge is a link the customer pays.
* A seat frees up (a cancellation, a released hold): the first person on that time's waitlist is told on their channel
  (the same rules as every service message: the 24 h window, another channel if that one is closed) — first come,
  first served, with the link to book it; if they can't be reached, the next one.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, db, money, packs, phones
from . import Invalid, NotFound, log

AP, PY, WL = db.appointments, db.payments, db.booking_waitlist
ACTIVE = ("booked", "confirmed")
CUSTOMER = ("booking_chat", "customer")                      # a cancellation the customer made themselves
_log = logging.getLogger(__name__)


def _t(key: str, lang: str | None) -> str:
    from .owner import t
    return t(key, lang)


def _money(biz: dict, minor: int) -> str:
    return money.fmt(int(minor), biz["currency"], packs.country(biz["country"]).locale)


# ---------------------------------------------------------------- the policy
def deposit_of(svc: dict, party: int = 1) -> int | None:
    """The deposit a booking of this service asks: the fixed amount, or its share of the price for the whole party."""
    if svc.get("deposit_minor"):
        return int(svc["deposit_minor"])
    if svc.get("deposit_bp") and svc.get("price_minor"):
        return money.part(int(svc["price_minor"]) * max(1, int(party or 1)), int(svc["deposit_bp"])) or None
    return None


def policy_of(svc: dict, party: int = 1) -> dict:
    """What a new booking keeps of the service's policy (nothing set = nothing kept: the booking is as before)."""
    out = {}
    dep = deposit_of(svc, party)
    if dep:
        out["deposit_minor"] = dep
    if svc.get("fee_minor"):
        out["fee_minor"] = int(svc["fee_minor"])
    if svc.get("cancel_hours") and out:
        out["cancel_hours"] = int(svc["cancel_hours"])
    return out


def policy_lines(biz: dict, pol: dict, lang: str, *, hold: bool = False) -> list[str]:
    """The policy in plain words for the customer (booking page, chat confirmation, API)."""
    out = []
    dep, fee, hours = pol.get("deposit_minor"), pol.get("fee_minor"), pol.get("cancel_hours")
    if dep:
        out.append(_t("bkg_pol_deposit", lang).format(amount=_money(biz, dep)))
        if hours:
            out.append(_t("bkg_pol_kept", lang).format(hours=hours))
        if hold:
            out.append(_t("bkg_pol_hold", lang).format(minutes=biz.get("deposit_hold_minutes") or 30))
    if fee:
        out.append((_t("bkg_pol_fee_late", lang).format(hours=hours, amount=_money(biz, fee)) if hours
                    else _t("bkg_pol_fee", lang).format(amount=_money(biz, fee))))
    return out


def service_policy(conn: Connection, biz: dict, svc: dict, lang: str, party: int = 1) -> list[str]:
    from . import payments as PM
    return policy_lines(biz, policy_of(svc, party), lang, hold=PM.can_link(conn, biz["id"]))


# ---------------------------------------------------------------- the deposit
def _appt(conn: Connection, business_id: UUID, appt_id, lock: bool = False) -> dict:
    q = select(AP).where(AP.c.business_id == business_id, AP.c.id == appt_id)
    r = conn.execute(q.with_for_update() if lock else q).mappings().first()
    if not r:
        raise NotFound("appointment")
    return dict(r)


def paid(conn: Connection, a: dict) -> bool:
    if a.get("deposit_paid_at"):
        return True
    return bool(a.get("deposit_payment_id")) and conn.execute(select(PY.c.status).where(PY.c.id == a["deposit_payment_id"])).scalar() == "paid"


def waiting(conn: Connection, business_id: UUID, appointment_id) -> bool:
    """This booking asks a deposit that hasn't come in (contracts.gate: it isn't confirmed until it has)."""
    a = conn.execute(select(AP).where(AP.c.business_id == business_id, AP.c.id == appointment_id)).mappings().first()
    return bool(a and a["deposit_minor"] and not paid(conn, dict(a)))


def _reach(conn: Connection, a: dict) -> tuple:
    """Who to write to about a booking, and where: the chat it was made in (its customer, its channel), else the
    booking's customer on the channel they use. -> (contact_id, channel account id | None)."""
    if a.get("conversation_id"):
        cv = conn.execute(select(db.conversations.c.contact_id, db.conversations.c.channel_account_id).where(
            db.conversations.c.id == a["conversation_id"])).first()
        if cv and cv.contact_id:
            return cv.contact_id, cv.channel_account_id
    return a.get("contact_id"), None


def pay_url(p: dict) -> str:
    return p.get("url") or f"{config.PUBLIC_URL()}/p/{p['token']}"


def _when(biz: dict, a: dict, lang: str) -> str:
    from .reminders import when_text
    return when_text(biz, a["starts_at"], lang)


def _what(conn: Connection, biz: dict, a: dict, lang: str) -> str:
    from . import booking as BK
    who = BK.staff_name(conn, biz["id"], a.get("staff_id"))
    return a["title"] + (" · " + _t("bkg_with", lang).format(name=BK.first_name(who)) if who else "")


def ask_deposit(biz: dict, appt_id, *, http=None, now: datetime | None = None, message: bool = False, lang: str | None = None,
                actor: str = "booking_page") -> dict | None:
    """The deposit's pay link on the business's own account, once per booking; the time is held from now for the
    business's hold minutes. -> {"amount_minor", "url", "due_at", "manual"} | None (nothing to pay).
    manual = no payment account (or the provider failed): the owner marks it received by hand; nothing is released."""
    from . import payments as PM
    now = now or datetime.now(timezone.utc)
    lang = lang or biz["language"]
    with db.tx() as conn:
        a = _appt(conn, biz["id"], appt_id)
        if not a["deposit_minor"] or a["status"] not in ACTIVE or paid(conn, a):
            return None
        if a["deposit_payment_id"]:
            p = dict(conn.execute(select(PY).where(PY.c.id == a["deposit_payment_id"])).mappings().one())
            if p["status"] == "pending":
                return {"amount_minor": a["deposit_minor"], "url": pay_url(p), "due_at": a["deposit_due_at"], "manual": False}
        can = PM.can_link(conn, biz["id"])
        what = _what(conn, biz, a, lang)
        to, acc = _reach(conn, a)
    out = {"amount_minor": a["deposit_minor"], "url": None, "due_at": None, "manual": True}
    if not can:
        return out
    try:
        p = PM.create(biz, actor, amount_minor=int(a["deposit_minor"]), contact_id=a["contact_id"], http=http,
                      description=f"{_t('bkg_deposit', biz['language'])} · {a['title']} · {_when(biz, a, biz['language'])}"[:300])
    except (Invalid, PM.PayError) as e:
        _log.warning("booking %s: no deposit link (%s)", a["id"], e)
        return out
    due = now + timedelta(minutes=int(biz.get("deposit_hold_minutes") or 30))
    with db.tx() as conn:
        took = conn.execute(update(AP).where(AP.c.id == a["id"], AP.c.deposit_payment_id.is_(None), AP.c.deposit_paid_at.is_(None))
                            .values(deposit_payment_id=p["id"], deposit_due_at=due, updated_at=func.now())).rowcount
        if not took:                                           # two at once: the other link stands, this one goes
            PM.cancel(conn, biz["id"], actor, p["id"])
            a = _appt(conn, biz["id"], a["id"])
            q = dict(conn.execute(select(PY).where(PY.c.id == a["deposit_payment_id"])).mappings().one()) if a["deposit_payment_id"] else None
            return {"amount_minor": a["deposit_minor"], "url": pay_url(q) if q else None, "due_at": a["deposit_due_at"], "manual": not q}
        log(conn, biz["id"], "appointment.deposit_asked", actor, a["id"], {"payment_id": str(p["id"]), "amount_minor": a["deposit_minor"]})
    out = {"amount_minor": a["deposit_minor"], "url": pay_url(p), "due_at": due, "manual": False}
    if message and to:
        from .contracts import _deliver
        text = _t("bkg_deposit_msg", lang).format(business=biz["name"], what=what, when=_when(biz, a, lang),
                                                  amount=_money(biz, a["deposit_minor"]), link=out["url"],
                                                  minutes=int(biz.get("deposit_hold_minutes") or 30))
        _deliver(biz, actor, to, text, account_id=acc, http=http, now=now)
    return out


def deposit_words(biz: dict, got: dict | None, lang: str) -> str:
    """The line for the customer after booking: pay here within N minutes — or the business will tell you how."""
    if not got:
        return ""
    if got.get("url"):
        return _t("bkg_pay_here", lang).format(amount=_money(biz, got["amount_minor"]), link=got["url"],
                                               minutes=int(biz.get("deposit_hold_minutes") or 30))
    return _t("bkg_pay_manual", lang).format(amount=_money(biz, got["amount_minor"]), business=biz["name"])


def mark_deposit(conn: Connection, biz: dict, actor: str, appt_id) -> dict:
    """The owner got the deposit (cash, a transfer): its pending link is marked paid, else a "received" payment is written
    (so Books has it). The booking is confirmed when nothing else is missing."""
    from . import payments as PM
    a = _appt(conn, biz["id"], appt_id, lock=True)
    if not a["deposit_minor"] or paid(conn, a):
        raise Invalid(_t("bkg_nothing_to_mark", biz["language"]), "deposit")
    if a["deposit_payment_id"] and conn.execute(select(PY.c.status).where(PY.c.id == a["deposit_payment_id"])).scalar() == "pending":
        PM.owner_confirms(conn, biz["id"], actor, a["deposit_payment_id"])
    else:
        p = dict(conn.execute(insert(PY).values(business_id=biz["id"], provider="cash", contact_id=a["contact_id"],
                                                amount_minor=int(a["deposit_minor"]), currency=biz["currency"],
                                                description=f"{_t('bkg_deposit', biz['language'])} · {a['title']}"[:300],
                                                token=secrets.token_urlsafe(16), status="paid", paid_at=func.now())
                                .returning(PY)).mappings().one())
        conn.execute(update(AP).where(AP.c.id == a["id"]).values(deposit_payment_id=p["id"]))
        log(conn, biz["id"], "payment.paid", actor, p["id"], {"amount_minor": p["amount_minor"], "provider": "cash"})
    return _appt(conn, biz["id"], a["id"])


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """From services.log, in the payment's own transaction: a deposit paid -> the booking is confirmed when nothing else
    is missing (a signed contract when one is asked for). Paid after its hold ran out -> the owner is told."""
    if type_ != "payment.paid" or not subject_id:
        return
    rows = [dict(r) for r in conn.execute(update(AP).where(AP.c.business_id == business_id, AP.c.deposit_payment_id == subject_id,
                                                          AP.c.deposit_paid_at.is_(None))
                                          .values(deposit_paid_at=func.now(), deposit_due_at=None, updated_at=func.now())
                                          .returning(AP)).mappings()]
    if not rows:
        fee = conn.execute(select(AP.c.id).where(AP.c.business_id == business_id, AP.c.fee_payment_id == subject_id)).scalar()
        if fee:
            log(conn, business_id, "appointment.fee_paid", "payment", fee, {"payment_id": str(subject_id)})
        return
    from . import contracts as CT
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
    for a in rows:
        log(conn, business_id, "appointment.deposit_paid", "payment", a["id"], {"payment_id": str(subject_id)})
        if a["status"] == "booked" and not CT.gate(conn, business_id, a["id"]):
            conn.execute(update(AP).where(AP.c.id == a["id"], AP.c.status == "booked").values(status="confirmed", updated_at=func.now()))
            log(conn, business_id, "appointment.confirmed", "deposit", a["id"], {"via": "deposit"})
        elif a["status"] == "cancelled":
            _notice(conn, biz, "bkg_ping_paid_late", a, dedupe=f"bk:late:{a['id']}")


def _notice(conn: Connection, biz: dict, key: str, a: dict, *, dedupe: str, **more) -> None:
    """A note on the owner's chat (sent by the assistant round)."""
    from . import chatcal as CC
    from . import owner as OW
    lang = OW.lang_of(biz)
    text = _t(key, lang).format(name=CC.name_of(conn, biz, a, lang), what=a["title"], when=_when(biz, a, lang), **more)
    OW.ask(conn, biz, "notice", text, {"text": text, "appointment_id": str(a["id"])}, contact_id=a.get("contact_id"), by="valeria",
           dedupe=dedupe, owner_says_go=True)


# ---------------------------------------------------------------- cancelled, no-show
def on_cancelled(conn: Connection, business_id: UUID, before: dict, after: dict, actor: str,
                 now: datetime | None = None) -> dict | None:
    """In the cancellation's own transaction: what becomes of the deposit (kept | refund | late) — an unpaid link is left
    open on purpose (money that still comes in is recorded and the owner told) — and the first person waiting for that
    seat is put in line to hear it."""
    now = now or datetime.now(timezone.utc)
    vals = {}
    biz = None
    if before["deposit_minor"] or before["fee_minor"]:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        customer = actor in CUSTOMER
        late = bool(before["cancel_hours"]) and before["starts_at"] - now < timedelta(hours=int(before["cancel_hours"]))
        got = paid(conn, before) if before["deposit_minor"] else False
        if before["deposit_minor"] and not got:               # its pay link stays open: a late payment is never lost —
            vals["deposit_due_at"] = None                     # it is recorded and the owner is told (on_event)
        if got:
            vals["deposit_outcome"] = "kept" if customer and late else "refund"
        elif customer and late and before["fee_minor"]:
            vals["deposit_outcome"] = "late"
        if vals.get("deposit_outcome"):
            log(conn, business_id, f"appointment.deposit_{vals['deposit_outcome']}", actor, before["id"],
                {"amount_minor": before["deposit_minor"] if got else before["fee_minor"]})
        if vals.get("deposit_outcome") == "refund":
            _notice(conn, biz, "bkg_ping_refund", before, dedupe=f"bk:refund:{before['id']}", amount=_money(biz, before["deposit_minor"]))
    row = None
    if vals:
        row = dict(conn.execute(update(AP).where(AP.c.id == before["id"]).values(**vals).returning(AP)).mappings().one())
    if before["service_id"] and before["starts_at"] > now:
        seat_freed(conn, business_id, before)
    return row


def fee_due(conn: Connection, a: dict) -> int:
    """The no-show / late-cancel fee still to ask: the fee less a deposit the business keeps."""
    if not a.get("fee_minor") or a.get("fee_payment_id"):
        return 0
    if a["status"] == "cancelled" and a.get("deposit_outcome") != "late":
        return 0
    if a["status"] not in ("no_show", "cancelled"):
        return 0
    kept = int(a["deposit_minor"] or 0) if a.get("deposit_minor") and paid(conn, a) else 0
    return max(0, int(a["fee_minor"]) - kept)


def send_fee(biz: dict, appt_id, actor: str, *, http=None, now: datetime | None = None, lang: str | None = None) -> dict | None:
    """The owner tapped No-show (or the customer cancelled too late): the fee's pay link on the business's own account,
    to the customer by their channel. -> {"amount_minor", "url", "channel"} | None (no fee to ask)."""
    from . import payments as PM
    from .contracts import _deliver
    now = now or datetime.now(timezone.utc)
    with db.tx() as conn:
        a = _appt(conn, biz["id"], appt_id)
        amount = fee_due(conn, a)
        if not amount or not a["contact_id"]:
            return None
        if not PM.can_link(conn, biz["id"]):
            raise Invalid(_t("bkg_fee_no_pay", biz["language"]), "provider")
        what = _what(conn, biz, a, lang or biz["language"])
        to, acc = _reach(conn, a)
    p = PM.create(biz, actor, amount_minor=amount, contact_id=a["contact_id"], http=http,
                  description=f"{_t('bkg_fee', biz['language'])} · {a['title']} · {_when(biz, a, biz['language'])}"[:300])
    with db.tx() as conn:
        if not conn.execute(update(AP).where(AP.c.id == a["id"], AP.c.fee_payment_id.is_(None)).values(fee_payment_id=p["id"])).rowcount:
            PM.cancel(conn, biz["id"], actor, p["id"])
            return None
        log(conn, biz["id"], "appointment.fee_asked", actor, a["id"], {"payment_id": str(p["id"]), "amount_minor": amount})
    lang = lang or biz["language"]
    text = _t("bkg_fee_msg", lang).format(business=biz["name"], what=what, when=_when(biz, a, lang), amount=_money(biz, amount),
                                          link=pay_url(p))
    ch, _why = _deliver(biz, actor, to, text, account_id=acc, http=http, now=now)
    return {"amount_minor": amount, "url": pay_url(p), "channel": ch}


def after_status(biz: dict, appt_id, status: str, actor: str, *, http=None) -> str | None:
    """After the owner marks a booking: a no-show with a fee -> its link goes out. -> a flash key for the screen, or None."""
    if status != "no_show":
        return None
    try:
        got = send_fee(biz, appt_id, actor, http=http)
    except Invalid:
        return "bkg_fee_no_pay"
    except Exception:                                          # noqa: BLE001 — the no-show is on record either way
        _log.exception("no-show fee for %s", appt_id)
        return "bkg_fee_failed"
    return "bkg_fee_sent" if got else None


# ---------------------------------------------------------------- the hold runs out
def expire_holds(now: datetime | None = None, http=None) -> dict:
    """Bookings whose deposit didn't come within the hold: released (cancelled), the customer told, a waiting seat
    offered. Each one is claimed under its row lock, so a payment arriving at the same moment wins or loses cleanly."""
    from . import deals as D
    from .contracts import _deliver
    now = now or datetime.now(timezone.utc)
    stats = {"released": 0, "told": 0}
    with db.tx() as conn:
        ids = [r[0] for r in conn.execute(select(AP.c.id).where(AP.c.deposit_due_at <= now, AP.c.deposit_paid_at.is_(None),
                                                                AP.c.status == "booked").limit(200))]
    for aid in ids:
        with db.tx() as conn:
            a = conn.execute(select(AP).where(AP.c.id == aid).with_for_update(skip_locked=True)).mappings().first()
            if not a or a["status"] != "booked" or a["deposit_paid_at"] or not a["deposit_due_at"] or a["deposit_due_at"] > now:
                continue
            a = dict(a)
            if paid(conn, a):
                continue
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == a["business_id"])).mappings().one())
            D.set_appointment_status(conn, biz["id"], "deposit_hold", a["id"], "cancelled")
            stats["released"] += 1
            lang = biz["language"]
            text = _t("bkg_hold_gone", lang).format(business=biz["name"], what=_what(conn, biz, a, lang), when=_when(biz, a, lang),
                                                    link=f"{config.PUBLIC_URL()}/b/{biz['slug']}" if biz.get("slug") else "").strip()
            to, acc = _reach(conn, a)
        if to and not biz.get("suspended_at"):
            ch, _ = _deliver(biz, "deposit_hold", to, text, account_id=acc, http=http, now=now)
            stats["told"] += 1 if ch else 0
    return stats


# ---------------------------------------------------------------- the waitlist
def join_waitlist(conn: Connection, biz: dict, *, service_id: UUID, starts_at: datetime, name: str, phone: str, party: int = 1,
                  staff_id=None, email: str | None = None, now: datetime | None = None) -> dict:
    """"Tell me if a spot opens": only for a service with its waitlist on, and a time that is full right now."""
    from . import booking as BK
    now = now or datetime.now(timezone.utc)
    if not (name or "").strip():
        raise Invalid("tell us your name", "name")
    try:
        e164 = phones.e164(phone, packs.country(biz["country"]))
    except phones.BadPhone:
        raise Invalid("check the phone number", "phone") from None
    svc = BK._service(conn, biz, service_id)
    if not svc["waitlist"]:
        raise NotFound("waitlist")
    party = BK.party_of(svc, party)
    staff = BK._uid(staff_id)
    from zoneinfo import ZoneInfo
    local = starts_at.astimezone(ZoneInfo(biz["timezone"]))
    _, full = BK.spots(conn, biz, service_id, local.date(), now, staff=staff, party=party)
    if local not in full:
        raise Invalid(_t("bkg_wl_not_full", biz["language"]), "starts_at")
    contact = BK.customer(conn, biz, name=name, e164=e164, email=email)
    have = conn.execute(select(WL).where(WL.c.business_id == biz["id"], WL.c.service_id == service_id, WL.c.starts_at == local,
                                         WL.c.contact_id == contact["id"], WL.c.status.in_(("waiting", "notify")))).mappings().first()
    if have:
        return dict(have)
    row = dict(conn.execute(insert(WL).values(business_id=biz["id"], service_id=service_id, staff_id=staff if isinstance(staff, UUID) else None,
                                              starts_at=local, party=party, contact_id=contact["id"]).returning(WL)).mappings().one())
    log(conn, biz["id"], "booking.waitlisted", "booking_page", row["id"], {"service": svc["name"]})
    return row


def waitlist(conn: Connection, business_id: UUID, service_id: UUID | None = None, starts_at: datetime | None = None,
             live: bool = True) -> list[dict]:
    C = db.contacts
    q = select(WL, C.c.name.label("contact_name")).join(C, C.c.id == WL.c.contact_id).where(WL.c.business_id == business_id)
    if service_id:
        q = q.where(WL.c.service_id == service_id)
    if starts_at:
        q = q.where(WL.c.starts_at == starts_at)
    if live:
        q = q.where(WL.c.status.in_(("waiting", "notify")))
    return [dict(r) for r in conn.execute(q.order_by(WL.c.starts_at, WL.c.created_at).limit(500)).mappings()]


def remove_waiting(conn: Connection, business_id: UUID, actor: str, entry_id) -> None:
    if not conn.execute(update(WL).where(WL.c.business_id == business_id, WL.c.id == entry_id,
                                         WL.c.status.in_(("waiting", "notify"))).values(status="removed")).rowcount:
        raise NotFound("waitlist")
    log(conn, business_id, "booking.waitlist_removed", actor, entry_id)


def seat_freed(conn: Connection, business_id: UUID, a: dict) -> bool:
    """A seat on this time freed up: the first one waiting (for that person, or for anyone) is put in line to be told."""
    q = select(WL.c.id).where(WL.c.business_id == business_id, WL.c.service_id == a["service_id"], WL.c.starts_at == a["starts_at"],
                              WL.c.status == "waiting")
    if a.get("staff_id"):
        q = q.where(WL.c.staff_id.is_(None) | (WL.c.staff_id == a["staff_id"]))
    first = conn.execute(q.order_by(WL.c.created_at).limit(1).with_for_update(skip_locked=True)).scalar()
    if not first:
        return False
    conn.execute(update(WL).where(WL.c.id == first).values(status="notify"))
    return True


def send_offers(now: datetime | None = None, http=None) -> dict:
    """Tell the people put in line: "a spot opened — book it here". Can't be reached -> the next one waiting."""
    from .contracts import _deliver
    now = now or datetime.now(timezone.utc)
    stats = {"told": 0, "unreachable": 0}
    for _ in range(200):
        with db.tx() as conn:
            r = conn.execute(select(WL).where(WL.c.status == "notify").order_by(WL.c.created_at).limit(1)
                             .with_for_update(skip_locked=True)).mappings().first()
            if not r:
                break
            r = dict(r)
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
            svc = conn.execute(select(db.booking_services).where(db.booking_services.c.id == r["service_id"])).mappings().first()
            if r["starts_at"] <= now or not svc or not svc["active"] or biz.get("suspended_at"):
                conn.execute(update(WL).where(WL.c.id == r["id"]).values(status="removed"))
                continue
            conn.execute(update(WL).where(WL.c.id == r["id"]).values(status="notified", notified_at=now))
        from zoneinfo import ZoneInfo
        lang = biz["language"]
        day = r["starts_at"].astimezone(ZoneInfo(biz["timezone"])).date().isoformat()
        link = f"{config.PUBLIC_URL()}/b/{biz['slug']}/{r['service_id']}?d={day}" + (f"&p={r['staff_id']}" if r["staff_id"] else "")
        name = ""
        with db.tx() as conn:
            name = conn.execute(select(db.contacts.c.name).where(db.contacts.c.id == r["contact_id"])).scalar() or ""
        from .reminders import when_text
        text = _t("bkg_wl_open", lang).format(name=name.split(" ")[0] if name else "", business=biz["name"], what=svc["name"],
                                              when=when_text(biz, r["starts_at"], lang), link=link)
        ch, _why = _deliver(biz, "waitlist", r["contact_id"], " ".join(text.split(" ")).strip(), http=http, now=now)
        with db.tx() as conn:
            if ch:
                conn.execute(update(WL).where(WL.c.id == r["id"]).values(channel=ch))
                log(conn, biz["id"], "booking.waitlist_told", "waitlist", r["id"], {"channel": ch})
                stats["told"] += 1
            else:
                conn.execute(update(WL).where(WL.c.id == r["id"]).values(status="unreachable"))
                stats["unreachable"] += 1
                seat_freed(conn, biz["id"], {"service_id": r["service_id"], "starts_at": r["starts_at"], "staff_id": r["staff_id"]})
    return stats


def round_(now: datetime | None = None, http=None) -> dict:
    """The jobs step: holds that ran out, then the waitlist's offers."""
    return {**expire_holds(now, http), "waitlist": send_offers(now, http)}
