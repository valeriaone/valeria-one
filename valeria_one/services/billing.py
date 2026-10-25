"""Billing, on a server that charges for Valeria One (VO_BILLING=on: the hosted service). A self-hosted
install never charges anybody: with billing off, every business has everything.

Plans: free (inbox and customers, 1 person) and Valeria One ("starter": everything, the whole team, no
per-person fees). "pro" stays in the engine for a future tier and is not offered. A new business gets a
30-day trial. When the trial or the paid time ends it falls back to free: nothing is
locked or deleted, and paying brings everything back at once.

Ways to pay: a bank transfer to the server's own account (the business sends it with its reference; the
admin confirms), or a card through Stripe, which renews by itself (on when VO_STRIPE_SECRET_KEY and
VO_STRIPE_WEBHOOK_SECRET are set). Price: USD 39, MXN 249, PHP 799 or INR 999 a month by the
business's country (Tadewan's decisions; USD everywhere else), changeable with VO_PRICES (JSON). Tax is added only on a domestic sale (the business is in VO_SELLER_COUNTRY and that
country has a fixed tax, e.g. IVA in Mexico).

Launch window (VO_LAUNCH_MONTHS, e.g. 12; 0 or unset = off): while it is open, every new business gets that
many free months instead of the 30-day trial, and is marked promo='launch'. It was told at sign-up what comes
after. When its free months end without payment it is PAUSED (VO_AFTER_PROMO=pause, the default): nothing is
sent, the screens show only Billing, Settings and the data export; messages still arrive and nothing is
deleted; paying opens everything at once. VO_AFTER_PROMO=free sends it to the free plan instead. Closing the
window (unset VO_LAUNCH_MONTHS) changes nothing for those already in it.

Referrals: every business has a code (its transfer reference); businesses that sign up with it are counted,
and the Server page shows what was paid by them and the commission (VO_REFERRAL_PERCENT, default 30).
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import blocks, config, crypto, db, money, packs
from . import Invalid, NotFound, log

B, BP, MB, U = db.businesses, db.billing_payments, db.memberships, db.users
PLANS = {"free": {"blocks": frozenset(), "people": 1},
         "starter": {"blocks": frozenset(blocks.BLOCKS), "people": None},        # the whole team
         "pro": {"blocks": frozenset(blocks.BLOCKS), "people": None},            # not offered yet
         "paused": {"blocks": frozenset(), "people": 1}}                         # launch months over, not paid
PAID = ("starter", "pro")
OFFERED = ("starter",)                                     # what the screens sell: one plan, everything
# Tadewan's decisions: USD 39 (the rest of the world), MXN 249 (2026-10-22), PHP 799 and INR 999 (2026-10-25)
DEFAULT_PRICES = {"starter": {"USD": 3900, "MXN": 24900, "PHP": 79900, "INR": 99900},
                  "pro": {"USD": 7900, "MXN": 49900, "PHP": 159900, "INR": 199900}}
TRIAL_DAYS = 30
MONTHS = (1, 12)
_REF_ABC = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"              # no 0/O, 1/I: easy to read out over the phone


class BillingError(Exception):
    pass


# ---------------------------------------------------------------- settings
def enabled() -> bool:
    return (config.get("VO_BILLING") or "off").lower() == "on" and not config.SANDBOX()


def launch_months() -> int:
    try:
        n = int(config.get("VO_LAUNCH_MONTHS") or 0)
    except ValueError:
        return 0
    return n if 0 < n <= 36 else 0


def after_promo() -> str:
    return "free" if (config.get("VO_AFTER_PROMO") or "").lower() == "free" else "pause"


def add_months(d: datetime, n: int) -> datetime:
    import calendar
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def prices() -> dict:
    p = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    try:
        for plan, cur in json.loads(config.get("VO_PRICES") or "{}").items():
            if plan in PAID:
                p[plan].update({c.upper(): int(v) for c, v in cur.items()})
    except (ValueError, AttributeError, TypeError):
        pass
    return p


def currency_for(biz: dict) -> str:
    """The plan is priced by the business's COUNTRY (its local price where one is set, else USD), never by the
    currency it chose for its own quotes: a business can't pick a cheaper country's price."""
    try:
        cur = packs.country(biz["country"]).currency
    except KeyError:
        return "USD"
    return cur if cur in prices()["starter"] else "USD"


def quote(biz: dict, plan: str, months: int = 1) -> dict:
    if plan not in PAID:
        raise Invalid("choose starter or pro", "plan")
    if months not in MONTHS:
        raise Invalid("pay for 1 or 12 months", "months")
    cur = currency_for(biz)
    price = prices()[plan][cur] * months
    tax_name, tax = None, 0
    seller = (config.get("VO_SELLER_COUNTRY") or "").upper()
    if seller and biz["country"] == seller:
        t = packs.country(seller).tax
        if t.mode == "fixed" and cur == packs.country(seller).currency:
            tax_name, tax = t.name, price * t.rate_bp // 10_000
    return {"plan": plan, "months": months, "currency": cur, "price_minor": price, "tax_minor": tax,
            "tax_name": tax_name, "amount_minor": price + tax}


# ---------------------------------------------------------------- who gets what
def effective(biz: dict, now: datetime | None = None) -> str:
    """unlimited (billing off) | pro | starter | free | paused (launch months over, not paid)."""
    if not enabled():
        return "unlimited"
    if biz.get("billing_exempt") or biz.get("sandbox"):
        return "pro"
    now = now or datetime.now(timezone.utc)
    plan = biz.get("plan") if biz.get("plan") in PAID else "starter"
    if not biz.get("paid_until") and not biz.get("trial_ends_at"):
        return plan                       # made before this server charged: its trial starts now (see start_trials)
    if (biz.get("paid_until") and biz["paid_until"] > now) or (biz.get("trial_ends_at") and biz["trial_ends_at"] > now):
        return plan
    return "paused" if biz.get("promo") == "launch" and after_promo() == "pause" else "free"


def paused(biz: dict, now: datetime | None = None) -> bool:
    return effective(biz, now) == "paused"


def start_trials(conn: Connection, now: datetime | None = None) -> int:
    """Businesses that existed before charging was switched on get their full trial from today, never a
    sudden downgrade."""
    now = now or datetime.now(timezone.utc)
    return conn.execute(update(B).where(B.c.sandbox.is_(False), B.c.trial_ends_at.is_(None), B.c.paid_until.is_(None))
                        .values(trial_ends_at=now + timedelta(days=TRIAL_DAYS))).rowcount


def allows(biz: dict, block: str) -> bool:
    p = effective(biz)
    return p == "unlimited" or block in PLANS[p]["blocks"]


def people_limit(biz: dict) -> int | None:
    p = effective(biz)
    return None if p == "unlimited" else PLANS[p]["people"]   # None = no limit


def status(biz: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    eff = effective(biz, now)
    trial = bool(biz.get("trial_ends_at") and biz["trial_ends_at"] > now and not (biz.get("paid_until") and biz["paid_until"] > now))
    until = biz.get("paid_until") if biz.get("paid_until") and biz["paid_until"] > now else (biz.get("trial_ends_at") if trial else None)
    return {"enabled": enabled(), "effective": eff, "plan": biz.get("plan"), "on_trial": trial and not biz.get("billing_exempt"),
            "until": until, "days_left": max(0, (until - now).days) if until else None, "exempt": bool(biz.get("billing_exempt")),
            "card": bool(biz.get("stripe_subscription_id")), "ref": biz.get("billing_ref"),
            "promo": biz.get("promo"), "paused": eff == "paused"}


def new_ref(conn: Connection) -> str:
    for _ in range(20):
        ref = "VO-" + "".join(secrets.choice(_REF_ABC) for _ in range(5))
        if not conn.execute(select(B.c.id).where(B.c.billing_ref == ref)).first():
            return ref
    raise RuntimeError("could not make a unique reference")


def on_create(conn: Connection, business_id: UUID, sandbox: bool) -> None:
    """Called for every new business: a reference always; a trial when this server charges."""
    vals = {"billing_ref": new_ref(conn)}
    if enabled() and not sandbox:
        now, months = datetime.now(timezone.utc), launch_months()
        if months:                                         # the launch window is open: free months, then pay
            vals.update(plan="starter", trial_ends_at=add_months(now, months), promo="launch")
        else:
            vals.update(plan="starter", trial_ends_at=now + timedelta(days=TRIAL_DAYS))
    conn.execute(update(B).where(B.c.id == business_id).values(**vals))


def set_exempt(conn: Connection, business_id: UUID, actor: str, exempt: bool) -> None:
    if not conn.execute(update(B).where(B.c.id == business_id).values(billing_exempt=exempt)).rowcount:
        raise NotFound("business")
    log(conn, business_id, "billing.exempt" if exempt else "billing.charged", actor, business_id)


def referrer_by_ref(conn: Connection, ref: str | None) -> UUID | None:
    if not ref:
        return None
    return conn.execute(select(B.c.id).where(B.c.billing_ref == ref.strip().upper()[:12])).scalar()


# ---------------------------------------------------------------- the server's own account (transfers)
def account(conn: Connection) -> dict | None:
    from . import instance
    a = instance.kv_get(conn, "billing_account")
    return a if a and a.get("account") else None


def set_account(conn: Connection, admin: dict, password: str, data: dict) -> dict:
    """Where businesses send transfers. Changing it asks for the admin's password again: a hijacked
    session can't quietly redirect everyone's payments."""
    from . import instance, partner, users
    if not users._verify(admin["password_hash"], password or ""):
        raise Invalid("wrong password", "password")
    a = {k: " ".join(str(data.get(k) or "").split())[:120] for k in ("bank", "holder", "account", "swift", "currency")}
    if not (a["bank"] and a["holder"] and a["account"]):
        raise Invalid("bank, account holder and account number are needed", "account")
    a["currency"] = (a["currency"] or "MXN").upper()[:3]
    instance.kv_set(conn, "billing_account", a)
    partner.audit(conn, "billing.account_changed", f"user:{admin['id']}", {"bank": a["bank"], "last4": a["account"][-4:]})
    return a


def _add_months(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + n
    y, m = dt.year + m // 12, m % 12 + 1
    from calendar import monthrange
    return dt.replace(year=y, month=m, day=min(dt.day, monthrange(y, m)[1]))


def request_transfer(conn: Connection, biz: dict, actor: str, plan: str, months: int, note: str = "") -> dict:
    """The business says it sent (or is sending) a transfer. One pending request at a time."""
    if not enabled():
        raise Invalid("this server does not charge", "billing")
    acc = account(conn)
    q = quote(biz, plan, months)
    if not acc or acc["currency"] != q["currency"]:
        raise Invalid("paying by transfer is not available for your currency; pay by card", "method")
    pend = conn.execute(select(BP).where(BP.c.business_id == biz["id"], BP.c.status == "pending", BP.c.method == "transfer")).mappings().first()
    if pend:
        return dict(pend)
    n = conn.execute(select(func.count()).select_from(BP).where(BP.c.business_id == biz["id"])).scalar() + 1
    row = conn.execute(insert(BP).values(business_id=biz["id"], plan=plan, months=months, amount_minor=q["amount_minor"],
                                         tax_minor=q["tax_minor"], currency=q["currency"], method="transfer",
                                         reference=f"{biz['billing_ref']}-{n}", note=(note or "").strip()[:300] or None)
                       .returning(BP)).mappings().one()
    log(conn, biz["id"], "billing.transfer_sent", actor, row["id"], {"plan": plan, "months": months})
    return dict(row)


def _extend(conn: Connection, business_id: UUID, plan: str, until: datetime) -> None:
    cur = conn.execute(select(B.c.paid_until).where(B.c.id == business_id)).scalar()
    conn.execute(update(B).where(B.c.id == business_id).values(plan=plan, paid_until=max(until, cur) if cur else until))


def decide(conn: Connection, payment_id: UUID, actor: str, approve: bool) -> dict:
    p = conn.execute(select(BP).where(BP.c.id == payment_id).with_for_update()).mappings().first()
    if not p:
        raise NotFound("payment")
    if p["status"] != "pending":
        return dict(p)                                          # already decided: deciding twice changes nothing
    now = datetime.now(timezone.utc)
    conn.execute(update(BP).where(BP.c.id == payment_id).values(status="paid" if approve else "rejected",
                                                                decided_by=actor, decided_at=now))
    if approve:
        cur = conn.execute(select(B.c.paid_until).where(B.c.id == p["business_id"])).scalar()
        _extend(conn, p["business_id"], p["plan"], _add_months(max(now, cur) if cur else now, p["months"]))
    log(conn, p["business_id"], "billing.paid" if approve else "billing.rejected", actor, payment_id,
        {"plan": p["plan"], "months": p["months"]})
    return dict(conn.execute(select(BP).where(BP.c.id == payment_id)).mappings().one())


def pending(conn: Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(select(BP, B.c.name.label("business_name"), B.c.billing_ref)
                                          .join(B, B.c.id == BP.c.business_id).where(BP.c.status == "pending")
                                          .order_by(BP.c.created_at)).mappings()]


def history(conn: Connection, business_id: UUID, limit: int = 24) -> list[dict]:
    return [dict(r) for r in conn.execute(select(BP).where(BP.c.business_id == business_id)
                                          .order_by(BP.c.created_at.desc()).limit(limit)).mappings()]


# ---------------------------------------------------------------- cards (Stripe, renews by itself)
def stripe_on() -> bool:
    return bool(config.get("VO_STRIPE_SECRET_KEY") and config.get("VO_STRIPE_WEBHOOK_SECRET"))


def _stripe(path: str, data: dict, http=None) -> dict:
    h = http or httpx.Client(timeout=20)
    r = h.post(f"https://api.stripe.com/v1/{path}", data=data,
               headers={"Authorization": f"Bearer {config.get('VO_STRIPE_SECRET_KEY')}"})
    try:
        j = r.json()
    except ValueError:
        raise BillingError(f"card payments are not answering ({r.status_code}); try again") from None
    if r.status_code >= 300:
        raise BillingError((j.get("error") or {}).get("message") or "the card payment could not start")
    return j


def checkout(biz: dict, email: str | None, plan: str, back: str, http=None) -> str:
    """A Stripe Checkout page for a monthly subscription. -> its URL."""
    if not (enabled() and stripe_on()):
        raise Invalid("card payments are not on for this server", "method")
    q = quote(biz, plan, 1)
    form = {"mode": "subscription", "success_url": back + "?paid=1", "cancel_url": back,
            "client_reference_id": str(biz["id"]),
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": q["currency"].lower(),
            "line_items[0][price_data][unit_amount]": str(q["amount_minor"]),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][price_data][product_data][name]": f"Valeria One {plan.title()} — {biz['name']}"[:250],
            "metadata[vo_business]": str(biz["id"]), "metadata[vo_plan]": plan,
            "subscription_data[metadata][vo_business]": str(biz["id"]), "subscription_data[metadata][vo_plan]": plan}
    if biz.get("stripe_customer_id"):
        form["customer"] = biz["stripe_customer_id"]
    elif email:
        form["customer_email"] = email
    return _stripe("checkout/sessions", form, http)["url"]


def portal(biz: dict, back: str, http=None) -> str:
    """Stripe's own page to change the card or cancel."""
    if not (stripe_on() and biz.get("stripe_customer_id")):
        raise Invalid("no card on file", "method")
    return _stripe("billing_portal/sessions", {"customer": biz["stripe_customer_id"], "return_url": back}, http)["url"]


def _meta(obj: dict) -> dict:
    for path in (("metadata",), ("subscription_details", "metadata"), ("parent", "subscription_details", "metadata")):
        cur = obj
        for k in path:
            cur = (cur or {}).get(k) or {}
        if cur.get("vo_business"):
            return cur
    for line in ((obj.get("lines") or {}).get("data") or []):
        if (line.get("metadata") or {}).get("vo_business"):
            return line["metadata"]
    return {}


def stripe_webhook(headers: dict, body: bytes) -> str:
    """Verify Stripe's signature, then apply the event once. Returns what happened."""
    secret = config.get("VO_STRIPE_WEBHOOK_SECRET")
    parts = dict(x.split("=", 1) for x in (headers.get("stripe-signature") or "").split(",") if "=" in x)
    t, v1 = parts.get("t", ""), parts.get("v1", "")
    if not secret or not t.isdigit() or abs(time.time() - int(t)) > 300 or \
            not crypto.same(v1, crypto.hmac_sha256_hex(secret, f"{t}.".encode() + body)):
        raise BillingError("bad signature")
    ev = json.loads(body)
    obj = (ev.get("data") or {}).get("object") or {}
    md = _meta(obj)
    try:
        bid = UUID(md.get("vo_business") or obj.get("client_reference_id") or "")
    except ValueError:
        return "ignored"
    plan = md.get("vo_plan") if md.get("vo_plan") in PAID else "starter"
    with db.tx() as conn:
        if not conn.execute(select(B.c.id).where(B.c.id == bid)).first():
            return "ignored"
        if ev.get("type") == "checkout.session.completed" and obj.get("mode") == "subscription":
            conn.execute(update(B).where(B.c.id == bid).values(stripe_customer_id=obj.get("customer"),
                                                               stripe_subscription_id=obj.get("subscription"), plan=plan))
            log(conn, bid, "billing.card_added", "stripe", bid, {"plan": plan})
            return "subscribed"
        if ev.get("type") == "invoice.paid":
            lines = (obj.get("lines") or {}).get("data") or [{}]
            end = (lines[0].get("period") or {}).get("end") or obj.get("period_end")
            if not end:
                return "ignored"
            ins = conn.execute(pg_insert(BP).values(
                business_id=bid, plan=plan, months=1, amount_minor=int(obj.get("amount_paid") or 0), tax_minor=0,
                currency=(obj.get("currency") or "usd").upper(), method="stripe", status="paid",
                reference=str(obj.get("number") or obj.get("id"))[:24], provider_id=obj.get("id"), decided_by="stripe",
                decided_at=func.now()).on_conflict_do_nothing(index_elements=[BP.c.provider_id]).returning(BP.c.id)).first()
            if not ins:
                return "duplicate"
            _extend(conn, bid, plan, datetime.fromtimestamp(int(end), timezone.utc))
            conn.execute(update(B).where(B.c.id == bid, B.c.stripe_customer_id.is_(None)).values(stripe_customer_id=obj.get("customer")))
            log(conn, bid, "billing.paid", "stripe", ins.id, {"plan": plan, "months": 1})
            return "paid"
        if ev.get("type") == "customer.subscription.deleted":
            conn.execute(update(B).where(B.c.id == bid).values(stripe_subscription_id=None))
            log(conn, bid, "billing.card_cancelled", "stripe", bid)
            return "cancelled"
    return "ignored"


# ---------------------------------------------------------------- referrals and money in
def referral_percent() -> int:
    try:
        return max(0, min(100, int(config.get("VO_REFERRAL_PERCENT") or 30)))
    except ValueError:
        return 30


def report(conn: Connection, month: str | None = None) -> dict:
    """Money received this month (by currency) and what each referrer earned."""
    from .partner import _month
    start, end, label = _month(month)
    R = B.alias("referred")
    got = conn.execute(select(BP.c.currency, func.sum(BP.c.amount_minor), func.count()).where(
        BP.c.status == "paid", BP.c.decided_at >= start, BP.c.decided_at < end).group_by(BP.c.currency)).all()
    rows = conn.execute(select(B.c.id, B.c.name, B.c.billing_ref, BP.c.currency, func.sum(BP.c.amount_minor).label("paid"),
                               func.count(func.distinct(R.c.id)).label("paying"))
                        .select_from(R.join(B, B.c.id == R.c.referred_by).join(BP, BP.c.business_id == R.c.id))
                        .where(BP.c.status == "paid", BP.c.decided_at >= start, BP.c.decided_at < end)
                        .group_by(B.c.id, B.c.name, B.c.billing_ref, BP.c.currency)).mappings().all()
    counts = dict(conn.execute(select(R.c.referred_by, func.count()).where(R.c.referred_by.isnot(None))
                               .group_by(R.c.referred_by)).all())
    pct = referral_percent()
    refs = [{"id": r["id"], "name": r["name"], "ref": r["billing_ref"], "currency": r["currency"], "paid_minor": int(r["paid"]),
             "paying": r["paying"], "referred": counts.get(r["id"], 0), "commission_minor": int(r["paid"]) * pct // 100}
            for r in rows]
    return {"month": label, "percent": pct, "received": [{"currency": c, "amount_minor": int(a), "count": n} for c, a, n in got],
            "referrers": refs}


def fmt(minor: int, currency: str) -> str:
    """'699.00 MXN', '39.00 USD': the code always, never an ambiguous '$'."""
    from babel.numbers import format_decimal
    from babel.numbers import get_currency_precision
    d = get_currency_precision(currency)
    major = int(minor) / (10 ** d)
    return f"{format_decimal(major, format='#,##0.' + '0' * d if d else '#,##0', locale='en_US')} {currency}"


# ---------------------------------------------------------------- notices (run by the jobs)
def notices(now: datetime | None = None) -> dict:
    """Tell owners before the trial or paid time ends (3 days; 30 for launch months) and once when it ends
    (falls to free, or pauses when its launch months ran out)."""
    if not enabled():
        return {"skipped": "billing off"}
    from . import mailer
    from ..web.i18n import t, ui_lang
    now = now or datetime.now(timezone.utc)
    E = db.events
    sent = {"ending": 0, "ended": 0}
    with db.tx() as conn:
        sent["trials_started"] = start_trials(conn, now)
        rows = conn.execute(select(B).where(B.c.sandbox.is_(False), B.c.billing_exempt.is_(False), B.c.suspended_at.is_(None),
                                            B.c.stripe_subscription_id.is_(None))).mappings().all()
        for b in rows:
            b = dict(b)
            end = max([d for d in (b["paid_until"], b["trial_ends_at"]) if d], default=None)
            if not end:
                continue
            ahead = timedelta(days=30 if b.get("promo") == "launch" else 3)
            kind = "ending" if now < end <= now + ahead else ("ended" if end <= now < end + timedelta(days=7) else None)
            if not kind:
                continue
            key = f"billing.{kind}"
            if conn.execute(select(E.c.id).where(E.c.business_id == b["id"], E.c.type == key,
                                                 E.c.created_at >= end - ahead - timedelta(days=1))).first():
                continue
            if kind == "ended" and effective(b, now) == "paused":
                kind = "paused"                            # its launch months ran out: paused until it pays
            if kind == "ending" and b.get("promo") == "launch":
                kind = "launch_ending"                     # 30 days ahead, says what happens after
            lang = ui_lang(b["language"])
            link = f"{config.PUBLIC_URL()}/app/billing"
            for (email,) in conn.execute(select(U.c.email).join(MB, MB.c.user_id == U.c.id)
                                         .where(MB.c.business_id == b["id"], MB.c.role == "owner")):
                mailer.send(email, t(f"mail_{kind}_subject", lang).format(business=b["name"]),
                            t(f"mail_{kind}_body", lang).format(business=b["name"], link=link))
            log(conn, b["id"], key, "system", b["id"])
            sent[kind] = sent.get(kind, 0) + 1
    return sent
