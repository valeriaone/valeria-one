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
    "mpesa": ("consumer_key", "consumer_secret", "shortcode", "passkey"),          # Safaricom Daraja, STK push (KE)
    "mtn_momo": ("api_user", "api_key", "subscription_key", "target_environment"), # MTN MoMo Collection (UG, GH, …)
}
PUSH = ("mpesa", "mtn_momo")          # the customer gets a PIN prompt on their phone instead of a web link
BANK_IDS = ("iban", "swift", "routing", "sort_code", "ifsc", "bsb", "clabe", "branch")   # each country's own bank numbers
OPTIONAL = ("note", "environment", "base_url") + BANK_IDS
ALIASES = {"pix": "mercadopago", "upi": "razorpay", "sepa": "stripe"}     # country-pack names -> the provider that does it
_SECRET = ("secret_key", "access_token", "webhook_secret", "key_secret", "consumer_secret", "passkey", "api_key", "subscription_key")


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
    cfg = {k: str(v).strip() for k, v in (cfg or {}).items() if k in PROVIDERS[provider] + OPTIONAL and str(v).strip()}
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
           deal_id: UUID | None = None, provider: str | None = None, http=None, project_id: UUID | None = None) -> dict:
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
        if project_id:
            from . import projects
            projects.get(conn, biz["id"], project_id)
        token = secrets.token_urlsafe(16)
        pay = dict(conn.execute(insert(PY).values(business_id=biz["id"], account_id=acc["id"], provider=acc["provider"],
                                                  contact_id=contact_id, deal_id=deal_id, project_id=project_id, amount_minor=amount_minor,
                                                  currency=biz["currency"], description=description, token=token)
                                .returning(PY)).mappings().one())
    page = f"{config.PUBLIC_URL()}/p/{token}"
    cfg = _open(acc["config"])
    if acc["provider"] == "mpesa" and amount_minor % 100:
        with db.tx() as conn:
            conn.execute(PY.delete().where(PY.c.id == pay["id"]))
        raise Invalid("M-Pesa takes whole shillings", "amount_minor")
    if acc["provider"] == "bank_transfer" or acc["provider"] in PUSH:
        url, ref = page, None                          # push providers: the customer starts it on our page with their number
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


# ---------------------------------------------------------------- mobile money: PIN prompt on the customer's phone
def _mpesa_base(cfg) -> str:
    return "https://api.safaricom.co.ke" if cfg.get("environment") == "production" else "https://sandbox.safaricom.co.ke"


def _mpesa_token(cfg, h) -> str:
    r = h.get(f"{_mpesa_base(cfg)}/oauth/v1/generate", params={"grant_type": "client_credentials"},
              auth=(cfg["consumer_key"], cfg["consumer_secret"]))
    return _ok(r)["access_token"]


def _mpesa_password(cfg, ts: str) -> str:
    return base64.b64encode(f"{cfg['shortcode']}{cfg['passkey']}{ts}".encode()).decode()


def _momo_base(cfg) -> str:
    return cfg.get("base_url") or ("https://sandbox.momodeveloper.mtn.com" if cfg.get("target_environment") == "sandbox"
                                   else "https://proxy.momoapi.mtn.com")


def _momo_token(cfg, h) -> str:
    r = h.post(f"{_momo_base(cfg)}/collection/token/", auth=(cfg["api_user"], cfg["api_key"]),
               headers={"Ocp-Apim-Subscription-Key": cfg["subscription_key"]})
    return _ok(r)["access_token"]


def _status(provider: str, cfg: dict, ref: str, h) -> str:
    """Ask the provider itself: paid | failed | pending | unknown. Never trusts a callback on its own."""
    import time as _t
    try:
        if provider == "mpesa":
            ts = _t.strftime("%Y%m%d%H%M%S", _t.gmtime(_t.time() + 3 * 3600))
            r = h.post(f"{_mpesa_base(cfg)}/mpesa/stkpushquery/v1/query", headers={"Authorization": f"Bearer {_mpesa_token(cfg, h)}"},
                       json={"BusinessShortCode": cfg["shortcode"], "Password": _mpesa_password(cfg, ts), "Timestamp": ts,
                             "CheckoutRequestID": ref})
            j = r.json()
            if "ResultCode" not in j:
                return "pending"                     # Safaricom answers "still being processed" without a ResultCode
            return "paid" if str(j["ResultCode"]) == "0" else "failed"
        if provider == "mtn_momo":
            r = h.get(f"{_momo_base(cfg)}/collection/v1_0/requesttopay/{ref}",
                      headers={"Authorization": f"Bearer {_momo_token(cfg, h)}", "X-Target-Environment": cfg["target_environment"],
                               "Ocp-Apim-Subscription-Key": cfg["subscription_key"]})
            st = (r.json() or {}).get("status") if r.status_code < 300 else None
            return {"SUCCESSFUL": "paid", "PENDING": "pending"}.get(st, "failed" if st else "unknown")
    except (httpx.HTTPError, KeyError, ValueError):
        return "unknown"
    return "unknown"


def push(token: str, phone: str, http=None) -> dict:
    """The customer typed their number on /p/<token>: send the PIN prompt to their phone."""
    from .. import packs, phones
    import time as _t
    import uuid as _uuid
    with db.tx() as conn:
        pay, biz, _ = by_token(conn, token)
        acc = conn.execute(select(PA).where(PA.c.id == pay["account_id"])).mappings().first()
    if not acc or acc["provider"] not in PUSH:
        raise Invalid("this payment is not paid from the phone", "provider")
    if pay["status"] != "pending":
        raise Invalid("this payment is not pending", "status")
    try:
        msisdn = phones.e164(phone, packs.country(biz["country"])).lstrip("+")
    except phones.BadPhone:
        raise Invalid("check the phone number", "phone") from None
    cfg, h = _open(acc["config"]), _http(http)
    last = (pay["attempts"] or [])[-1] if pay["attempts"] else None
    if last:
        st = _status(acc["provider"], cfg, last["ref"], h)
        if st == "paid":
            _paid_by_ref(acc, last["ref"])
            raise Invalid("this payment has already been received", "status")
        from datetime import datetime as _dt, timezone as _tz
        if st == "pending" and (_dt.now(_tz.utc) - _dt.fromisoformat(last["at"])).total_seconds() < 120:
            raise Invalid("a payment request is already on your phone — approve it, or wait two minutes to send a new one", "phone")
    hook = f"{config.PUBLIC_URL()}/hooks/pay/{acc['hook_id']}"
    try:
        if acc["provider"] == "mpesa":
            ts = _t.strftime("%Y%m%d%H%M%S", _t.gmtime(_t.time() + 3 * 3600))      # Nairobi time
            j = _ok(h.post(f"{_mpesa_base(cfg)}/mpesa/stkpush/v1/processrequest",
                           headers={"Authorization": f"Bearer {_mpesa_token(cfg, h)}"},
                           json={"BusinessShortCode": cfg["shortcode"], "Password": _mpesa_password(cfg, ts), "Timestamp": ts,
                                 "TransactionType": "CustomerPayBillOnline", "Amount": pay["amount_minor"] // 100,
                                 "PartyA": msisdn, "PartyB": cfg["shortcode"], "PhoneNumber": msisdn, "CallBackURL": hook,
                                 "AccountReference": f"VO{pay['token'][:10]}", "TransactionDesc": pay["description"][:13]}))
            ref = j["CheckoutRequestID"]
        else:
            ref = str(_uuid.uuid4())
            r = h.post(f"{_momo_base(cfg)}/collection/v1_0/requesttopay",
                       headers={"Authorization": f"Bearer {_momo_token(cfg, h)}", "X-Reference-Id": ref,
                                "X-Target-Environment": cfg["target_environment"], "X-Callback-Url": hook,
                                "Ocp-Apim-Subscription-Key": cfg["subscription_key"]},
                       json={"amount": format(_major(pay["amount_minor"], pay["currency"]), "f"), "currency": pay["currency"],
                             "externalId": str(pay["id"]), "payer": {"partyIdType": "MSISDN", "partyId": msisdn},
                             "payerMessage": pay["description"][:160], "payeeNote": biz["name"][:160]})
            if r.status_code not in (200, 201, 202):
                raise ValueError(r.text[:200])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise PayError(f"{acc['provider']} could not send the prompt ({type(e).__name__})") from None
    from datetime import datetime as _dt, timezone as _tz
    attempt = {"ref": ref, "at": _dt.now(_tz.utc).isoformat()}
    with db.tx() as conn:
        conn.execute(update(PY).where(PY.c.id == pay["id"]).values(provider_ref=ref, attempts=PY.c.attempts + [attempt]))
        log(conn, pay["business_id"], "payment.prompted", "customer", pay["id"], {"provider": acc["provider"]})
    return {"provider": acc["provider"], "reference": ref}


# ---------------------------------------------------------------- money received by hand
def record_received(biz: dict, actor: str, *, amount_minor: int, description: str, contact_id: UUID | None = None,
                    deal_id: UUID | None = None, project_id: UUID | None = None) -> dict:
    """Cash, a transfer that arrived, a card machine: the owner writes down money already received. No provider
    and no link, so it works in every country from day one."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise Invalid("amount_minor: a whole number above 0 in the smallest currency unit", "amount_minor")
    description = (description or "").strip()[:300]
    if not description:
        raise Invalid("say what it is for", "description")
    with db.tx() as conn:
        if contact_id:
            contacts.get(conn, biz["id"], contact_id)
        if deal_id:
            from . import deals
            deals.get(conn, biz["id"], deal_id)
        if project_id:
            from . import projects
            projects.get(conn, biz["id"], project_id)
        row = dict(conn.execute(insert(PY).values(business_id=biz["id"], provider="cash", contact_id=contact_id, deal_id=deal_id,
                                                  project_id=project_id,
                                                  amount_minor=amount_minor, currency=biz["currency"], description=description,
                                                  token=secrets.token_urlsafe(16), status="paid", paid_at=func.now())
                                .returning(PY)).mappings().one())
        log(conn, biz["id"], "payment.paid", actor, row["id"], {"amount_minor": amount_minor, "provider": "cash"})
    return row


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


def list_(conn: Connection, business_id: UUID, *, contact_id=None, deal_id=None, project_id=None, limit: int = 50) -> list[dict]:
    q = select(PY).where(PY.c.business_id == business_id)
    if project_id:
        q = q.where(PY.c.project_id == project_id)
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


def webhook(hook_id: str, headers: dict, body: bytes, query: dict, http=None) -> str:  # noqa: C901
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
    if p in PUSH:
        # Neither Safaricom nor MTN signs its callbacks: a callback only tells us WHICH request to check,
        # and we believe the provider's own answer to a status query.
        data = json.loads(body or b"{}") or {}
        ref = None
        if p == "mpesa":
            ref = ((data.get("Body") or {}).get("stkCallback") or {}).get("CheckoutRequestID")
        else:
            ref = headers.get("x-reference-id") or data.get("referenceId")
            if not ref and data.get("externalId"):
                try:
                    with db.tx() as conn:
                        ref = conn.execute(select(PY.c.provider_ref).where(PY.c.id == UUID(str(data["externalId"])),
                                                                           PY.c.business_id == acc["business_id"])).scalar()
                except ValueError:
                    ref = None
        if not ref:
            return "unknown"
        st = _status(p, cfg, ref, _http(http))
        return _paid_by_ref(acc, ref) if st == "paid" else st
    if p == "razorpay":
        if not crypto.same(headers.get("x-razorpay-signature", ""), _hmac(cfg["webhook_secret"], body)):
            raise BadSig()
        ev = json.loads(body)
        if ev.get("event") != "payment_link.paid":
            return "ignored"
        ent = ev["payload"]["payment_link"]["entity"]
        return _paid(acc, ent.get("reference_id"), ent.get("id"))
    return "ignored"


def _paid_by_ref(acc, ref: str) -> str:
    """Any attempt that succeeded pays the payment — even an older one that confirmed late."""
    with db.tx() as conn:
        row = conn.execute(select(PY.c.id, PY.c.status, PY.c.provider_ref).where(
            PY.c.business_id == acc["business_id"],
            (PY.c.provider_ref == ref) | PY.c.attempts.contains([{"ref": ref}]))).first()
        if not row:
            return "unknown"
        if row.status == "paid" and row.provider_ref != ref:
            # two different prompts were both approved: tell the owner, so they can refund one
            log(conn, acc["business_id"], "payment.duplicate", f"webhook:{acc['provider']}", row.id, {"ref": ref})
            return "duplicate_charge"
    return _paid(acc, row.id, ref)


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


def reconcile(http=None, limit: int = 100) -> dict:
    """Every minute: for phone-prompt payments still pending (up to 24 h), ask the provider about every attempt.
    Catches confirmations that never arrived (timeouts, lost callbacks) and approvals that came late."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    stats = {"checked": 0, "paid": 0}
    with db.tx() as conn:
        rows = conn.execute(select(PY, PA.c.config.label("acc_config"), PA.c.business_id.label("acc_biz"), PA.c.provider.label("acc_provider"),
                                   PA.c.id.label("acc_id"))
                            .join(PA, PA.c.id == PY.c.account_id)
                            .where(PY.c.status == "pending", PY.c.provider.in_(PUSH), PY.c.attempts != [],
                                   PY.c.created_at > _dt.now(_tz.utc) - _td(hours=24)).limit(limit)).mappings().all()
    h = _http(http)
    for r in rows:
        cfg = _open(r["acc_config"])
        acc = {"business_id": r["acc_biz"], "provider": r["acc_provider"], "id": r["acc_id"]}
        for a in reversed(r["attempts"]):
            stats["checked"] += 1
            if _status(r["provider"], cfg, a["ref"], h) == "paid":
                if _paid_by_ref(acc, a["ref"]) == "paid":
                    stats["paid"] += 1
                break
    return stats


# ---------------------------------------------------------------- who owes me (Valeria, by chat)
def unpaid(conn: Connection, business_id: UUID, limit: int = 20) -> list[dict]:
    """Payment links still waiting, oldest first, with the customer's name (cash records are never 'owed')."""
    C = db.contacts
    rows = conn.execute(select(PY, C.c.name.label("contact_name")).join(C, C.c.id == PY.c.contact_id, isouter=True)
                        .where(PY.c.business_id == business_id, PY.c.status == "pending", PY.c.provider != "cash")
                        .order_by(PY.c.created_at).limit(max(1, min(limit, 200)))).mappings()
    return [dict(r) for r in rows]


def can_link(conn: Connection, business_id: UUID) -> bool:
    """Is the business's own payment account connected (so a link can be made)?"""
    return conn.execute(select(PA.c.id).where(PA.c.business_id == business_id, PA.c.active).limit(1)).first() is not None
