"""Invoices and subscriptions (v0.18) — the money goes straight to the business; Valeria One never holds it.

Invoice: line items, the country pack's tax (computed like quotes: IVA, VAT, GST; an external tax such as US sales
tax is left to the payment provider), numbered 1, 2, 3… per business (never reused: a mistake is voided, not
deleted), a customer page /inv/<token> wearing the business's look (print it or save it as PDF from the browser),
sent by WhatsApp or email with a pay link from the business's OWN payment account (services/payments.py) — no
account connected: the invoice still goes, without the link ("pay as usual"). Status: draft → sent → paid, or
overdue after its due day; void. A pay link paid (any provider's webhook, or the owner marking it received) marks
its invoice paid. An accepted quote becomes an invoice with one tap (the same lines and the same tax, frozen).
Reminders are Valeria's: an overdue invoice is a question to the owner ("Remind Ana about invoice #12?") — money
always asks first.

Subscriptions / memberships: a plan (an amount every week, month or year) and a customer on it. Each period the
job makes that period's invoice (UNIQUE(subscription, period_start): never two for one period, however often the
job runs) and, when the owner set it up so, sends it with its pay link. The payment providers wired in Valeria One
take one-off payments (Stripe Checkout in payment mode, Mercado Pago preferences, PayMongo links…), not their own
recurring billing, so a subscription here is "a recurring invoice + a pay link" — the same everywhere. Pause and
cancel stop the next invoices; MRR (monthly recurring revenue) is on Today.
"""
from __future__ import annotations

import calendar
import secrets
from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .. import config, db, money, packs
from . import Blocked, Invalid, NotFound, contacts, log

IV, PL, SB = db.invoices, db.plans, db.subscriptions
MAX_LINES = 30
INTERVALS = ("week", "month", "year")
REMIND_EVERY_DAYS = 7
STATUSES = ("draft", "sent", "overdue", "paid", "void")


def _t(key: str, lang: str | None) -> str:
    from ..web.i18n import t, ui_lang
    return t(key, ui_lang(lang))


def today_of(biz: dict, now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(biz["timezone"])).date()


def clean_lines(lines) -> list[dict]:
    clean = []
    for ln in (lines or [])[:MAX_LINES]:
        desc = str(ln.get("description") or "").strip()
        if not desc:
            continue
        qty, unit = ln.get("qty", 1), ln.get("unit_minor")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 100000:
            raise Invalid("quantity: a whole number from 1", "lines")
        if not isinstance(unit, int) or isinstance(unit, bool) or unit < 0:
            raise Invalid("price: a whole number in the smallest currency unit", "lines")
        clean.append({"description": desc[:200], "qty": qty, "unit_minor": unit})
    if not clean:
        raise Invalid("add at least one line", "lines")
    return clean


def _next_number(conn: Connection, business_id: UUID) -> int:
    conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("invoice:" + str(business_id)))))
    return conn.execute(select(func.coalesce(func.max(IV.c.number), 0) + 1).where(IV.c.business_id == business_id)).scalar()


def create(conn: Connection, biz: dict, actor: str, *, title: str, lines: list[dict], contact_id: UUID | None = None,
           due_days: int = 15, notes: str = "", quote_id: UUID | None = None, subscription_id: UUID | None = None,
           period_start: date | None = None, issued_on: date | None = None, totals: dict | None = None) -> dict:
    title = " ".join((title or "").split())[:200]
    if not title:
        raise Invalid("give the invoice a title", "title")
    clean = clean_lines(lines)
    if not 0 <= int(due_days) <= 365:
        raise Invalid("due in 0 to 365 days", "due_days")
    if contact_id:
        contacts.get(conn, biz["id"], contact_id)
    pack = packs.country(biz["country"])
    if totals:                                               # a quote's frozen numbers (what the customer signed)
        t = totals
    else:
        subtotal = sum(ln["qty"] * ln["unit_minor"] for ln in clean)
        p = money.price(subtotal, pack, includes_tax=False)
        t = {"subtotal_minor": subtotal, "tax_minor": p.tax_minor,
             "total_minor": p.gross_minor if p.gross_minor is not None else subtotal, "tax_name": pack.tax.name,
             "tax_rate_bp": pack.tax.rate_bp}
    issued = issued_on or today_of(biz)
    number = _next_number(conn, biz["id"])
    row = conn.execute(insert(IV).values(
        business_id=biz["id"], number=number, contact_id=contact_id, quote_id=quote_id, subscription_id=subscription_id,
        period_start=period_start, title=title, lines=clean, currency=biz["currency"], notes=(notes or "").strip()[:2000],
        issued_on=issued, due_on=issued + timedelta(days=int(due_days)), status="draft", token=secrets.token_urlsafe(16),
        created_by=actor, **t).returning(IV)).mappings().one()
    log(conn, biz["id"], "invoice.created", actor, row["id"], {"number": number, "total_minor": row["total_minor"]})
    return dict(row)


def get(conn: Connection, business_id: UUID, invoice_id: UUID) -> dict:
    r = conn.execute(select(IV).where(IV.c.business_id == business_id, IV.c.id == invoice_id)).mappings().first()
    if not r:
        raise NotFound("invoice")
    return dict(r)


def by_token(conn: Connection, token: str) -> tuple[dict, dict]:
    r = conn.execute(select(IV).where(IV.c.token == (token or ""))).mappings().first() if token else None
    if not r or r["status"] == "draft":
        raise NotFound("invoice")                            # a draft isn't the customer's yet
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business_id"])).mappings().one())
    return dict(r), biz


def list_(conn: Connection, business_id: UUID, *, status: str | None = None, contact_id: UUID | None = None,
          limit: int = 100) -> list[dict]:
    C = db.contacts
    q = select(IV, C.c.name.label("contact_name")).outerjoin(C, C.c.id == IV.c.contact_id).where(IV.c.business_id == business_id)
    if status:
        q = q.where(IV.c.status == status)
    if contact_id:
        q = q.where(IV.c.contact_id == contact_id)
    return [dict(r) for r in conn.execute(q.order_by(IV.c.number.desc()).limit(max(1, min(limit, 300)))).mappings()]


def totals(conn: Connection, business_id: UUID) -> dict:
    """Owed (sent + overdue) and paid in the last 30 days, in the smallest unit."""
    owed = conn.execute(select(func.coalesce(func.sum(IV.c.total_minor), 0), func.count()).where(
        IV.c.business_id == business_id, IV.c.status.in_(("sent", "overdue")))).one()
    paid = conn.execute(select(func.coalesce(func.sum(IV.c.total_minor), 0)).where(
        IV.c.business_id == business_id, IV.c.status == "paid", IV.c.paid_at > func.now() - timedelta(days=30))).scalar()
    overdue = conn.execute(select(func.count()).where(IV.c.business_id == business_id, IV.c.status == "overdue")).scalar()
    return {"owed_minor": int(owed[0]), "owed_count": owed[1], "paid30_minor": int(paid), "overdue": overdue}


def from_quote(conn: Connection, biz: dict, actor: str, quote_id: UUID) -> dict:
    """An accepted quote → its invoice: the same lines and the very numbers the customer signed. Once."""
    Q = db.quotes
    q = conn.execute(select(Q).where(Q.c.business_id == biz["id"], Q.c.id == quote_id)).mappings().first()
    if not q:
        raise NotFound("quote")
    if q["status"] != "accepted":
        raise Invalid(_t("iv_quote_not_accepted", biz["language"]), "quote")
    have = conn.execute(select(IV).where(IV.c.business_id == biz["id"], IV.c.quote_id == quote_id, IV.c.status != "void")).mappings().first()
    if have:
        return dict(have)
    return create(conn, biz, actor, title=q["title"], lines=q["lines"], contact_id=q["contact_id"], quote_id=quote_id,
                  notes=q["notes"], totals={k: q[k] for k in ("subtotal_minor", "tax_minor", "total_minor", "tax_name", "tax_rate_bp")})


def url(inv: dict) -> str:
    return f"{config.PUBLIC_URL()}/inv/{inv['token']}"


def message_text(biz: dict, inv: dict, contact: dict | None, pay_url: str | None) -> str:
    L = biz["language"]
    amount = money.fmt(inv["total_minor"], inv["currency"], packs.country(biz["country"]).locale)
    first = ((contact or {}).get("name") or "").split(" ")[0]
    text = _t("iv_msg", L).format(name=first, business=biz["name"], number=inv["number"], amount=amount, link=url(inv))
    return " ".join(text.split(" ")).replace(" ,", ",").strip()


def send(biz: dict, actor: str, invoice_id: UUID, *, channel_account_id: UUID | None = None, http=None,
         message: bool = True) -> dict:
    """Issue it: a pay link with the business's own account (when one is connected), then the message to the customer.
    The invoice counts as sent even if the message can't go (the owner can share its link); the reason is returned."""
    from . import payments as PM
    from . import inbox as X
    from .followups import _pick_account
    with db.tx() as conn:
        inv = get(conn, biz["id"], invoice_id)
        if inv["status"] in ("paid", "void"):
            raise Invalid(_t("iv_cant_send", biz["language"]), "status")
        has_pay = bool(PM.accounts(conn, biz["id"]))
        contact = contacts.get(conn, biz["id"], inv["contact_id"]) if inv["contact_id"] else None
    pay_url = None
    if has_pay and not inv["payment_id"] and inv["total_minor"] > 0:
        try:
            pay = PM.create(biz, actor, amount_minor=inv["total_minor"], description=f"{_t('iv_invoice', biz['language'])} #{inv['number']}",
                            contact_id=inv["contact_id"], http=http)
            pay_url = pay.get("url") or pay.get("page_url")
            with db.tx() as conn:
                conn.execute(update(IV).where(IV.c.id == inv["id"]).values(payment_id=pay["id"]))
        except (Invalid, PM.PayError):
            pay_url = None                                   # no link today: the invoice still goes ("pay as usual")
    with db.tx() as conn:
        conn.execute(update(IV).where(IV.c.id == inv["id"], IV.c.status == "draft").values(status="sent", sent_at=func.now(),
                                                                                           updated_at=func.now()))
        log(conn, biz["id"], "invoice.sent", actor, inv["id"], {"number": inv["number"]})
        inv = get(conn, biz["id"], invoice_id)
        acc = channel_account_id or (_pick_account(conn, biz, contact["id"]) if contact else None)
    why = None
    if message and contact and acc:
        try:
            m = X.send(biz, actor, contact_id=contact["id"], channel_account_id=acc, body=message_text(biz, inv, contact, pay_url),
                       http=http, kind="service")
            if m["status"] == "failed":
                why = m.get("error") or "failed"
        except Blocked as b:
            why = b.reason
    elif message:
        why = "no_channel"
    return {**inv, "message_problem": why}


def mark_paid(conn: Connection, biz: dict, actor: str, invoice_id: UUID) -> dict:
    """Money received another way (cash, a transfer): the owner marks it."""
    inv = get(conn, biz["id"], invoice_id)
    if inv["status"] not in ("sent", "overdue", "draft"):
        raise Invalid(_t("iv_cant_pay", biz["language"]), "status")
    conn.execute(update(IV).where(IV.c.id == invoice_id).values(status="paid", paid_at=func.now(), updated_at=func.now()))
    log(conn, biz["id"], "invoice.paid", actor, invoice_id, {"number": inv["number"], "how": "marked"})
    return get(conn, biz["id"], invoice_id)


def void(conn: Connection, biz: dict, actor: str, invoice_id: UUID) -> dict:
    inv = get(conn, biz["id"], invoice_id)
    if inv["status"] == "paid":
        raise Invalid(_t("iv_cant_void", biz["language"]), "status")
    conn.execute(update(IV).where(IV.c.id == invoice_id).values(status="void", updated_at=func.now()))
    if inv["payment_id"]:
        conn.execute(update(db.payments).where(db.payments.c.id == inv["payment_id"], db.payments.c.status == "pending")
                     .values(status="cancelled"))
    log(conn, biz["id"], "invoice.void", actor, invoice_id, {"number": inv["number"]})
    return get(conn, biz["id"], invoice_id)


def on_event(conn: Connection, business_id: UUID, type_: str, subject_id: UUID | None, data: dict) -> None:
    """A pay link was paid (a provider's webhook, a phone payment, the owner confirming a transfer): its invoice is paid."""
    if type_ != "payment.paid" or not subject_id:
        return
    rows = conn.execute(update(IV).where(IV.c.business_id == business_id, IV.c.payment_id == subject_id,
                                         IV.c.status.in_(("draft", "sent", "overdue")))
                        .values(status="paid", paid_at=func.now(), updated_at=func.now()).returning(IV.c.id, IV.c.number)).all()
    for iid, number in rows:
        log(conn, business_id, "invoice.paid", "payment", iid, {"number": number, "how": "link"})


# ---------------------------------------------------------------- plans and subscriptions
def plans(conn: Connection, business_id: UUID, active_only: bool = True) -> list[dict]:
    q = select(PL).where(PL.c.business_id == business_id)
    if active_only:
        q = q.where(PL.c.active)
    return [dict(r) for r in conn.execute(q.order_by(PL.c.created_at)).mappings()]


def create_plan(conn: Connection, biz: dict, actor: str, *, name: str, amount_minor: int, interval: str = "month",
                every: int = 1) -> dict:
    name = " ".join((name or "").split())[:120]
    if not name:
        raise Invalid("give the plan a name", "name")
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise Invalid("amount: more than 0", "amount")
    if interval not in INTERVALS:
        raise Invalid("every week, month or year", "interval")
    if not 1 <= int(every) <= 12:
        raise Invalid("every 1 to 12", "every")
    row = conn.execute(insert(PL).values(business_id=biz["id"], name=name, amount_minor=amount_minor, currency=biz["currency"],
                                         interval=interval, every=int(every)).returning(PL)).mappings().one()
    log(conn, biz["id"], "plan.created", actor, row["id"], {"amount_minor": amount_minor, "interval": interval})
    return dict(row)


def archive_plan(conn: Connection, biz: dict, actor: str, plan_id: UUID) -> None:
    if not conn.execute(update(PL).where(PL.c.business_id == biz["id"], PL.c.id == plan_id).values(active=False)).rowcount:
        raise NotFound("plan")
    log(conn, biz["id"], "plan.archived", actor, plan_id)


def add_period(d: date, interval: str, every: int, anchor_day: int | None = None) -> date:
    if interval == "week":
        return d + timedelta(weeks=every)
    months = every * (12 if interval == "year" else 1)
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    day = min(anchor_day or d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def subscribe(conn: Connection, biz: dict, actor: str, *, plan_id: UUID, contact_id: UUID, start_on: date | None = None,
              auto_send: bool = True) -> dict:
    p = conn.execute(select(PL).where(PL.c.business_id == biz["id"], PL.c.id == plan_id, PL.c.active)).mappings().first()
    if not p:
        raise NotFound("plan")
    contacts.get(conn, biz["id"], contact_id)
    start = start_on or today_of(biz)
    row = conn.execute(insert(SB).values(business_id=biz["id"], plan_id=plan_id, contact_id=contact_id, started_on=start,
                                         next_on=start, auto_send=bool(auto_send), created_by=actor).returning(SB)).mappings().one()
    log(conn, biz["id"], "subscription.created", actor, row["id"], {"plan_id": str(plan_id)})
    return dict(row)


def subscriptions(conn: Connection, business_id: UUID, *, contact_id: UUID | None = None, live_only: bool = False) -> list[dict]:
    C = db.contacts
    q = (select(SB, PL.c.name.label("plan_name"), PL.c.amount_minor, PL.c.currency, PL.c.interval, PL.c.every,
                C.c.name.label("contact_name"))
         .join(PL, PL.c.id == SB.c.plan_id).join(C, C.c.id == SB.c.contact_id).where(SB.c.business_id == business_id))
    if contact_id:
        q = q.where(SB.c.contact_id == contact_id)
    if live_only:
        q = q.where(SB.c.status != "cancelled")
    return [dict(r) for r in conn.execute(q.order_by(SB.c.created_at.desc())).mappings()]


def set_subscription(conn: Connection, biz: dict, actor: str, sub_id: UUID, act: str) -> dict:
    s = conn.execute(select(SB).where(SB.c.business_id == biz["id"], SB.c.id == sub_id)).mappings().first()
    if not s:
        raise NotFound("subscription")
    now = datetime.now(timezone.utc)
    if act == "pause" and s["status"] == "active":
        vals = {"status": "paused", "paused_at": now}
    elif act == "resume" and s["status"] == "paused":
        vals = {"status": "active", "paused_at": None, "next_on": max(s["next_on"], today_of(biz))}
    elif act == "cancel" and s["status"] != "cancelled":
        vals = {"status": "cancelled", "cancelled_at": now}
    else:
        raise Invalid(_t("sb_cant", biz["language"]), "status")
    conn.execute(update(SB).where(SB.c.id == sub_id).values(**vals, updated_at=func.now()))
    log(conn, biz["id"], f"subscription.{act}", actor, sub_id)
    return dict(conn.execute(select(SB).where(SB.c.id == sub_id)).mappings().one())


def mrr(conn: Connection, business_id: UUID) -> int:
    """Monthly recurring revenue of the active subscriptions, in the smallest unit (weeks: ×52/12; years: ÷12)."""
    total = 0.0
    for amount, interval, every in conn.execute(select(PL.c.amount_minor, PL.c.interval, PL.c.every).join(SB, SB.c.plan_id == PL.c.id)
                                                .where(SB.c.business_id == business_id, SB.c.status == "active")).all():
        per = {"week": 52 / 12, "month": 1, "year": 1 / 12}[interval] / every
        total += amount * per
    return int(round(total))


def _period_invoice(conn: Connection, biz: dict, s: dict, p: dict) -> dict | None:
    """This period's invoice (once: UNIQUE(subscription, period_start)), and the next period's day. None = already made."""
    L = biz["language"]
    nxt = add_period(s["next_on"], p["interval"], p["every"], s["started_on"].day)
    title = _t("sb_invoice_title", L).format(plan=p["name"], start=s["next_on"].isoformat(), end=(nxt - timedelta(days=1)).isoformat())
    try:
        with conn.begin_nested():
            inv = create(conn, biz, "subscription", title=title[:200], lines=[{"description": p["name"], "qty": 1, "unit_minor": p["amount_minor"]}],
                         contact_id=s["contact_id"], subscription_id=s["id"], period_start=s["next_on"], issued_on=s["next_on"],
                         due_days=7)
    except IntegrityError:
        inv = None
    conn.execute(update(SB).where(SB.c.id == s["id"]).values(next_on=nxt, updated_at=func.now()))
    return inv


def round_(now: datetime | None = None, http=None) -> dict:
    """The job: each subscription's invoice when its day comes; overdue invoices; Valeria asks about reminders."""
    from .. import blocks
    from . import owner as OW

    def blocks_ok(biz: dict, name: str) -> bool:
        return blocks.on(biz, name)
    now = now or datetime.now(timezone.utc)
    stats = {"made": 0, "sent": 0, "overdue": 0, "asked": 0}
    with db.tx() as conn:
        due = conn.execute(select(SB.c.id, SB.c.business_id).where(SB.c.status == "active", SB.c.next_on <= now.date() + timedelta(days=1))
                           .limit(500)).all()
    to_send = []
    for sid, bid in due:
        with db.tx() as conn:
            s = conn.execute(select(SB).where(SB.c.id == sid, SB.c.status == "active").with_for_update(skip_locked=True)).mappings().first()
            if not s:
                continue
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            if not blocks_ok(biz, "payments"):
                continue
            s = dict(s)
            p = dict(conn.execute(select(PL).where(PL.c.id == s["plan_id"])).mappings().one())
            for _ in range(3):                               # catch up at most a few periods per round
                if s["next_on"] > today_of(biz, now):
                    break
                inv = _period_invoice(conn, biz, s, p)
                s = dict(conn.execute(select(SB).where(SB.c.id == sid)).mappings().one())
                if inv:
                    stats["made"] += 1
                    if s["auto_send"]:
                        to_send.append((biz, inv["id"]))
    for biz, iid in to_send:
        try:
            send(biz, "subscription", iid, http=http)
            stats["sent"] += 1
        except (Invalid, NotFound):
            pass
    with db.tx() as conn:
        rows = conn.execute(select(IV, db.businesses.c.timezone).join(db.businesses, db.businesses.c.id == IV.c.business_id)
                            .where(IV.c.status == "sent", IV.c.due_on < now.date() + timedelta(days=1)).limit(1000)).mappings().all()
        for r in rows:
            if r["due_on"] < now.astimezone(ZoneInfo(r["timezone"])).date():
                conn.execute(update(IV).where(IV.c.id == r["id"], IV.c.status == "sent").values(status="overdue", updated_at=func.now()))
                log(conn, r["business_id"], "invoice.overdue", "system", r["id"], {"number": r["number"]})
                stats["overdue"] += 1
        late = conn.execute(select(IV).where(IV.c.status == "overdue", IV.c.contact_id.isnot(None),
                                             (IV.c.reminded_at.is_(None)) | (IV.c.reminded_at < now - timedelta(days=REMIND_EVERY_DAYS)))
                            .limit(500)).mappings().all()
        by_biz: dict = {}
        for r in late:
            by_biz.setdefault(r["business_id"], []).append(dict(r))
        for bid, invs in by_biz.items():
            biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == bid)).mappings().one())
            if not blocks_ok(biz, "payments"):
                continue
            L = biz["language"]
            summary = _t("iv_q_remind", L).format(n=len(invs), numbers=", ".join(f"#{i['number']}" for i in invs[:8]))
            week = now.strftime("%G-%V")
            got = OW.ask(conn, biz, "pay_reminder", summary, {"invoice_ids": [str(i["id"]) for i in invs]},
                         dedupe=f"invrem:{week}:{len(invs)}:{invs[0]['id']}", now=now)
            if got:
                stats["asked"] += 1
                conn.execute(update(IV).where(IV.c.id.in_([i["id"] for i in invs])).values(reminded_at=now))
    return stats
