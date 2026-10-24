"""Payment links with the business's OWN payment provider — money goes straight to the business; Valeria One
never holds it. One model for every country:

  stripe       (US, EU, UK, AU, JP, MX…)      Checkout Session
  mercadopago  (MX, BR incl. Pix, CO, AR…)    Checkout Pro preference
  paymongo     (PH: cards, GCash, Maya)       Link
  paystack     (NG, GH, ZA, KE)               Transaction initialize
  razorpay     (IN incl. UPI)                 Payment Link
  bank_transfer (anywhere)                    our page with the business's bank details; the owner confirms

Each provider's webhook arrives at /hooks/pay/<hook_id>, is verified with that account's secret, and marks
the payment paid (once). Every payment also has a public page /p/<token> the customer can open.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from uuid import UUID

import httpx
from babel.numbers import get_currency_precision
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, NotFound, contacts, log

PA, PY = db.payment_accounts, db.payments
PROVIDERS = {
    "stripe": ("secret_key", "webhook_secret"),
    "mercadopago": ("access_token", "webhook_secret"),
    "paymongo": ("secret_key", "webhook_secret"),
    "paystack": ("secret_key",),
    "razorpay": ("key_id", "key_secret", "webhook_secret"),
    "bank_transfer": ("bank", "holder", "account"),
}
ALIASES = {"pix": "mercadopago", "upi": "razorpay", "sepa": "stripe"}     # country-pack names -> the provider that does it
_SECRET = ("secret_key", "access_token", "webhook_secret", "key_secret")


class PayError(Exception):
    pass


def _http(http=None) -> httpx.Client:
    return http or httpx.Client(timeout=20)


def _major(amount_minor: int, currency: str) -> Decimal:
    return Decimal(amount_minor).scaleb(-get_currency_precision(currency))


# ---------------------------------------------------------------- accounts
def connect(conn: Connection, biz: dict, actor: str, provider: str, cfg: dict) -> dict:
    provider = ALIASES.get(provider, provider)
    if provider not in PROVIDERS:
        raise Invalid(f"unknown provider; available: {', '.join(PROVIDERS)}", "provider")
    if (biz["sandbox"] or config.SANDBOX()) and provider != "bank_transfer":
        raise Invalid("sandbox businesses can only use bank transfer (nothing real is charged here)", "provider")
    cfg = {k: str(v).strip() for k, v in (cfg or {}).items() if k in PROVIDERS[provider] + ("note", "clabe", "swift") and str(v).strip()}
    missing = [k for k in PROVIDERS[provider] if not cfg.get(k)]
    if missing:
        raise Invalid(f"missing: {', '.join(missing)}", "config")
    conn.execute(update(PA).where(PA.c.business_id == biz["id"], PA.c.provider == provider, PA.c.active).values(active=False))
    sealed = {k: ("enc:" + crypto._f().encrypt(v.encode()).decode() if k in _SECRET else v) for k, v in cfg.items()}
    row = conn.execute(insert(PA).values(business_id=biz["id"], provider=provider, config=sealed,
                                         hook_id=secrets.token_urlsafe(18)).returning(PA)).mappings().one()
    log(conn, biz["id"], "payments.connected", actor, row["id"], {"provider": provider})
    return public_account(row)


def _open(cfg: dict) -> dict:
    return {k: (crypto._f().decrypt(v[4:].encode()).decode() if isinstance(v, str) and v.startswith("enc:") else v)
            for k, v in cfg.items()}


def public_account(row) -> dict:
    d = dict(row)
    d["config"] = {k: ("••••" if k in _SECRET else v) for k, v in d["config"].items()}
    d["webhook_url"] = f"{config.PUBLIC_URL()}/hooks/pay/{d.pop('hook_id')}"
    return d


def accounts(conn: Connection, business_id: UUID) -> list[dict]:
    return [public_account(r) for r in conn.execute(select(PA).where(PA.c.business_id == business_id, PA.c.active)
                                                     .order_by(PA.c.created_at)).mappings()]


def disconnect(conn: Connection, business_id: UUID, actor: str, account_id: UUID) -> None:
    if not conn.execute(update(PA).where(PA.c.business_id == business_id, PA.c.id == account_id, PA.c.active)
                        .values(active=False)).rowcount:
        raise NotFound("payment account")
    log(conn, business_id, "payments.disconnected", actor, account_id)


# ---------------------------------------------------------------- links
def create(biz: dict, actor: str, *, amount_minor: int, description: str, contact_id: UUID | None = None,
           deal_id: UUID | None = None, provider: str | None = None, http=None) -> dict:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise Invalid("amount_minor: a whole number above 0 in the smallest currency unit", "amount_minor")
    description = (description or "").strip()[:300]
    if not description:
        raise Invalid("say what it is for", "description")
    with db.tx() as conn:
        q = select(PA).where(PA.c.business_id == biz["id"], PA.c.active)
        if provider:
            q = q.where(PA.c.provider == ALIASES.get(provider, provider))
        acc = conn.execute(q.order_by(PA.c.created_at)).mappings().first()
        if not acc:
            raise Invalid("connect a payment provider first", "provider")
        contact = contacts.get(conn, biz["id"], contact_id) if contact_id else None
        if deal_id:
            from . import deals
            deals.get(conn, biz["id"], deal_id)
        token = secrets.token_urlsafe(16)
        pay = dict(conn.execute(insert(PY).values(business_id=biz["id"], account_id=acc["id"], provider=acc["provider"],
                                                  contact_id=contact_id, deal_id=deal_id, amount_minor=amount_minor,
                                                  currency=biz["currency"], description=description, token=token)
                                .returning(PY)).mappings().one())
    page = f"{config.PUBLIC_URL()}/p/{token}"
    cfg = _open(acc["config"])
    if acc["provider"] == "bank_transfer":
        url, ref = page, None
    else:
        try:
            url, ref = _PROVIDER_CREATE[acc["provider"]](cfg, pay, biz, contact, page, acc, _http(http))
        except Invalid:
            with db.tx() as conn:                     # nothing half-made is left behind
                conn.execute(PY.delete().where(PY.c.id == pay["id"]))
            raise
        except (httpx.HTTPError, KeyError, ValueError) as e:
            with db.tx() as conn:
                conn.execute(update(PY).where(PY.c.id == pay["id"]).values(status="failed"))
            raise PayError(f"{acc['provider']} could not create the link ({type(e).__name__})") from None
    with db.tx() as conn:
        row = dict(conn.execute(update(PY).where(PY.c.id == pay["id"]).values(url=url, provider_ref=ref).returning(PY)).mappings().one())
        log(conn, biz["id"], "payment.requested", actor, pay["id"], {"provider": acc["provider"], "amount_minor": amount_minor})
    return {**row, "page_url": page}


def _ok(r: httpx.Response) -> dict:
    j = r.json()
    if r.status_code >= 300:
        raise ValueError(str(j)[:300])
    return j


def _stripe_create(cfg, pay, biz, contact, page, acc, h):
    form = {"mode": "payment", "success_url": page + "?done=1", "cancel_url": page,
            "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": pay["currency"].lower(),
            "line_items[0][price_data][unit_amount]": str(pay["amount_minor"]),
            "line_items[0][price_data][product_data][name]": pay["description"],
            "client_reference_id": str(pay["id"]), "metadata[vo_payment]": str(pay["id"])}
    if contact and contact.get("email"):
        form["customer_email"] = contact["email"]
    j = _ok(h.post("https://api.stripe.com/v1/checkout/sessions", data=form, headers={"Authorization": f"Bearer {cfg['secret_key']}"}))
    return j["url"], j["id"]


def _mercadopago_create(cfg, pay, biz, contact, page, acc, h):
    body = {"items": [{"title": pay["description"], "quantity": 1, "currency_id": pay["currency"],
                       "unit_price": float(_major(pay["amount_minor"], pay["currency"]))}],
            "external_reference": str(pay["id"]),
            "notification_url": f"{config.PUBLIC_URL()}/hooks/pay/{acc['hook_id']}",
            "back_urls": {"success": page + "?done=1", "pending": page, "failure": page}, "auto_return": "approved"}
    j = _ok(h.post("https://api.mercadopago.com/checkout/preferences", json=body,
                   headers={"Authorization": f"Bearer {cfg['access_token']}"}))
    return j["init_point"], j["id"]


def _paymongo_create(cfg, pay, biz, contact, page, acc, h):
    auth = base64.b64encode((cfg["secret_key"] + ":").encode()).decode()
    j = _ok(h.post("https://api.paymongo.com/v1/links", headers={"Authorization": f"Basic {auth}"},
                   json={"data": {"attributes": {"amount": pay["amount_minor"], "description": pay["description"],
                                                 "remarks": str(pay["id"])}}}))
    return j["data"]["attributes"]["checkout_url"], j["data"]["id"]


def _paystack_create(cfg, pay, biz, contact, page, acc, h):
    if not (contact and contact.get("email")):
        raise Invalid("Paystack needs the customer's email — add it to the customer first", "contact")
    j = _ok(h.post("https://api.paystack.co/transaction/initialize", headers={"Authorization": f"Bearer {cfg['secret_key']}"},
                   json={"email": contact["email"], "amount": pay["amount_minor"], "currency": pay["currency"],
                         "reference": f"vo_{pay['id'].hex}", "callback_url": page + "?done=1"}))
    return j["data"]["authorization_url"], f"vo_{pay['id'].hex}"


def _razorpay_create(cfg, pay, biz, contact, page, acc, h):
    body = {"amount": pay["amount_minor"], "currency": pay["currency"], "description": pay["description"],
            "reference_id": str(pay["id"])[:40], "callback_url": page + "?done=1", "callback_method": "get"}
    if contact:
        body["customer"] = {k: v for k, v in (("name", contact.get("name")), ("contact", contact.get("phone")),
                                              ("email", contact.get("email"))) if v}
    j = _ok(h.post("https://api.razorpay.com/v1/payment_links", json=body, auth=(cfg["key_id"], cfg["key_secret"])))
    return j["short_url"], j["id"]


_PROVIDER_CREATE = {"stripe": _stripe_create, "mercadopago": _mercadopago_create, "paymongo": _paymongo_create,
                    "paystack": _paystack_create, "razorpay": _razorpay_create}


# ---------------------------------------------------------------- paid
def mark_paid(conn: Connection, payment_id: UUID, actor: str, provider_ref: str | None = None) -> bool:
    """Once only: returns True the first time."""
    row = conn.execute(update(PY).where(PY.c.id == payment_id, PY.c.status == "pending")
                       .values(status="paid", paid_at=func.now(), **({"provider_ref": provider_ref} if provider_ref else {}))
                       .returning(PY)).mappings().first()
    if not row:
        return False
    log(conn, row["business_id"], "payment.paid", actor, row["id"], {"amount_minor": row["amount_minor"], "provider": row["provider"]})
    return True


def owner_confirms(conn: Connection, business_id: UUID, actor: str, payment_id: UUID) -> None:
    row = conn.execute(select(PY).where(PY.c.business_id == business_id, PY.c.id == payment_id)).mappings().first()
    if not row:
        raise NotFound("payment")
    if not mark_paid(conn, payment_id, actor):
        raise Invalid("this payment is not pending", "status")


def cancel(conn: Connection, business_id: UUID, actor: str, payment_id: UUID) -> None:
    n = conn.execute(update(PY).where(PY.c.business_id == business_id, PY.c.id == payment_id, PY.c.status == "pending")
                     .values(status="cancelled")).rowcount
    if not n:
        raise NotFound("pending payment")
    log(conn, business_id, "payment.cancelled", actor, payment_id)


def list_(conn: Connection, business_id: UUID, *, contact_id=None, deal_id=None, limit: int = 50) -> list[dict]:
    q = select(PY).where(PY.c.business_id == business_id)
    if contact_id:
        q = q.where(PY.c.contact_id == contact_id)
    if deal_id:
        q = q.where(PY.c.deal_id == deal_id)
    return [dict(r) for r in conn.execute(q.order_by(PY.c.created_at.desc()).limit(max(1, min(limit, 200)))).mappings()]


def by_token(conn: Connection, token: str) -> tuple[dict, dict, dict | None]:
    row = conn.execute(select(PY).where(PY.c.token == (token or ""))).mappings().first()
    if not row:
        raise NotFound("payment")
    biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == row["business_id"])).mappings().one())
    acc = conn.execute(select(PA).where(PA.c.id == row["account_id"])).mappings().first() if row["account_id"] else None
    bank = None
    if acc and acc["provider"] == "bank_transfer":
        bank = {k: v for k, v in acc["config"].items() if k not in _SECRET}
    return dict(row), biz, bank


# ---------------------------------------------------------------- provider webhooks
class BadSig(Exception):
    pass


def _hmac(secret: str, msg: bytes, algo=hashlib.sha256) -> str:
    return hmac.new(secret.encode(), msg, algo).hexdigest()


def webhook(hook_id: str, headers: dict, body: bytes, query: dict, http=None) -> str:
    """Verify, then mark paid. Returns what happened (for logs/tests)."""
    with db.tx() as conn:
        acc = conn.execute(select(PA).where(PA.c.hook_id == hook_id)).mappings().first()
    if not acc or acc["provider"] == "bank_transfer":
        raise NotFound("hook")
    cfg = _open(acc["config"])
    p = acc["provider"]
    if p == "stripe":
        parts = dict(x.split("=", 1) for x in headers.get("stripe-signature", "").split(",") if "=" in x)
        t, v1 = parts.get("t", ""), parts.get("v1", "")
        if not t.isdigit() or abs(time.time() - int(t)) > 300 or not crypto.same(v1, _hmac(cfg["webhook_secret"], f"{t}.".encode() + body)):
            raise BadSig()
        ev = json.loads(body)
        if ev.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            return "ignored"
        obj = ev["data"]["object"]
        if obj.get("payment_status") != "paid":
            return "not_paid"
        return _paid(acc, (obj.get("metadata") or {}).get("vo_payment") or obj.get("client_reference_id"), obj.get("id"))
    if p == "mercadopago":
        data_id = str((json.loads(body or b"{}").get("data") or {}).get("id") or query.get("data.id") or "")
        sig = dict(x.strip().split("=", 1) for x in headers.get("x-signature", "").split(",") if "=" in x)
        manifest = f"id:{data_id.lower()};request-id:{headers.get('x-request-id', '')};ts:{sig.get('ts', '')};"
        if not data_id or not crypto.same(sig.get("v1", ""), _hmac(cfg["webhook_secret"], manifest.encode())):
            raise BadSig()
        r = _http(http).get(f"https://api.mercadopago.com/v1/payments/{data_id}", headers={"Authorization": f"Bearer {cfg['access_token']}"})
        pay = r.json() if r.status_code < 300 else {}
        if pay.get("status") != "approved":
            return "not_paid"
        return _paid(acc, pay.get("external_reference"), str(pay.get("id")))
    if p == "paymongo":
        parts = dict(x.split("=", 1) for x in headers.get("paymongo-signature", "").split(",") if "=" in x)
        expected = _hmac(cfg["webhook_secret"], f"{parts.get('t', '')}.".encode() + body)
        if not (crypto.same(parts.get("li", ""), expected) or crypto.same(parts.get("te", ""), expected)):
            raise BadSig()
        ev = json.loads(body)["data"]["attributes"]
        if ev.get("type") != "link.payment.paid":
            return "ignored"
        link = ev["data"]
        return _paid(acc, (link.get("attributes") or {}).get("remarks"), link.get("id"))
    if p == "paystack":
        if not crypto.same(headers.get("x-paystack-signature", ""), _hmac(cfg["secret_key"], body, hashlib.sha512)):
            raise BadSig()
        ev = json.loads(body)
        if ev.get("event") != "charge.success":
            return "ignored"
        ref = (ev.get("data") or {}).get("reference", "")
        pid = ref[3:] if ref.startswith("vo_") else ""
        return _paid(acc, str(UUID(hex=pid)) if pid else None, ref)
    if p == "razorpay":
        if not crypto.same(headers.get("x-razorpay-signature", ""), _hmac(cfg["webhook_secret"], body)):
            raise BadSig()
        ev = json.loads(body)
        if ev.get("event") != "payment_link.paid":
            return "ignored"
        ent = ev["payload"]["payment_link"]["entity"]
        return _paid(acc, ent.get("reference_id"), ent.get("id"))
    return "ignored"


def _paid(acc, payment_id, ref) -> str:
    try:
        pid = UUID(str(payment_id))
    except (ValueError, TypeError):
        return "unknown"
    with db.tx() as conn:
        row = conn.execute(select(PY).where(PY.c.id == pid, PY.c.business_id == acc["business_id"])).first()
        if not row:
            return "unknown"
        return "paid" if mark_paid(conn, pid, f"webhook:{acc['provider']}", ref) else "duplicate"
