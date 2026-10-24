import base64
import hashlib
import hmac
import json
import re
import time

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import payments as PM


def sig256(secret, msg):
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class FakePSP:
    def __init__(self):
        self.calls = []

    def __call__(self, req: httpx.Request):
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if host == "api.stripe.com":
            return httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"})
        if host == "api.mercadopago.com" and path.endswith("/preferences"):
            return httpx.Response(201, json={"id": "pref_1", "init_point": "https://www.mercadopago.com.mx/checkout/v1/redirect?pref_id=pref_1"})
        if host == "api.mercadopago.com" and "/v1/payments/" in path:
            ext = self.mp_ref
            return httpx.Response(200, json={"id": 123, "status": "approved", "external_reference": ext})
        if host == "api.paymongo.com":
            return httpx.Response(200, json={"data": {"id": "link_1", "attributes": {"checkout_url": "https://pm.link/x"}}})
        if host == "api.paystack.co":
            return httpx.Response(200, json={"status": True, "data": {"authorization_url": "https://checkout.paystack.com/x"}})
        if host == "api.razorpay.com":
            return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/x"})
        return httpx.Response(404, json={})


@pytest.fixture
def psp(monkeypatch):
    f = FakePSP()
    c = httpx.Client(transport=httpx.MockTransport(f))
    monkeypatch.setattr(PM, "_http", lambda http=None: http or c)
    return f


def setup(country, provider, cfg, email=None):
    biz, key = make_business(country)
    with db.tx() as conn:
        acc = PM.connect(conn, biz, "t", provider, cfg)
        from valeria_one.services import contacts as C
        c = C.create(conn, biz, "t", {"name": "Cliente", "email": email} if email else {"name": "Cliente"})
    return biz, key, acc, c


def test_stripe_link_and_signed_webhook(client, psp):
    biz, key, acc, c = setup("US", "stripe", {"secret_key": "sk_test_X", "webhook_secret": "whsec_S"}, email="a@b.co")
    pay = PM.create(biz, "t", amount_minor=4500, description="Cleaning", contact_id=c["id"])
    assert pay["url"].startswith("https://checkout.stripe.com") and pay["provider_ref"] == "cs_test_1"
    req = psp.calls[-1]
    form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
    assert form["line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D"] == "4500" and req.headers["authorization"] == "Bearer sk_test_X"
    hook = acc["webhook_url"].replace("https://one.test", "")
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_1", "payment_status": "paid",
                                                                                 "metadata": {"vo_payment": str(pay["id"])}}}}).encode()
    t = str(int(time.time()))
    good = {"Stripe-Signature": f"t={t},v1={sig256('whsec_S', f'{t}.'.encode() + body)}", "Content-Type": "application/json"}
    assert client.post(hook, content=body, headers={**good, "Stripe-Signature": f"t={t},v1=bad"}).status_code == 403
    old = str(int(time.time()) - 3600)
    replay = {"Stripe-Signature": f"t={old},v1={sig256('whsec_S', f'{old}.'.encode() + body)}"}
    assert client.post(hook, content=body, headers=replay).status_code == 403                 # too old: replay refused
    assert client.post(hook, content=body, headers=good).status_code == 200
    assert client.post(hook, content=body, headers=good).status_code == 200                   # again: still paid once
    h = {"Authorization": f"Bearer {key}"}
    p = client.get("/v1/payments", headers=h).json()[0]
    assert p["status"] == "paid" and p["paid_at"]
    assert len([e for e in client.get("/v1/events", headers=h).json() if e["type"] == "payment.paid"]) == 1


def test_mercadopago_pix_link_and_webhook(client, psp):
    biz, key, acc, c = setup("BR", "pix", {"access_token": "APP_USR-1", "webhook_secret": "mpsecret"})
    assert acc["provider"] == "mercadopago"
    pay = PM.create(biz, "t", amount_minor=98000, description="Sinal do apartamento", contact_id=c["id"])
    sent = json.loads(psp.calls[-1].content)
    assert sent["items"][0]["unit_price"] == 980.0 and sent["items"][0]["currency_id"] == "BRL"
    assert sent["notification_url"] == acc["webhook_url"] and sent["external_reference"] == str(pay["id"])
    psp.mp_ref = str(pay["id"])
    hook = acc["webhook_url"].replace("https://one.test", "")
    ts, rid = "1761300000", "req-1"
    manifest = f"id:123;request-id:{rid};ts:{ts};"
    body = json.dumps({"type": "payment", "data": {"id": "123"}}).encode()
    assert client.post(hook, content=body, headers={"x-signature": f"ts={ts},v1=nope", "x-request-id": rid}).status_code == 403
    assert client.post(hook, content=body, headers={"x-signature": f"ts={ts},v1={sig256('mpsecret', manifest.encode())}",
                                                    "x-request-id": rid}).status_code == 200
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"}).json()[0]["status"] == "paid"


def test_paymongo_paystack_razorpay(client, psp):
    biz, key, acc, c = setup("PH", "paymongo", {"secret_key": "sk_test_pm", "webhook_secret": "pmsec"})
    pay = PM.create(biz, "t", amount_minor=150000, description="Aircon cleaning", contact_id=c["id"])
    assert pay["url"] == "https://pm.link/x"
    assert psp.calls[-1].headers["authorization"] == "Basic " + base64.b64encode(b"sk_test_pm:").decode()
    body = json.dumps({"data": {"attributes": {"type": "link.payment.paid",
                                               "data": {"id": "link_1", "attributes": {"remarks": str(pay["id"])}}}}}).encode()
    t = "1761300000"
    hook = acc["webhook_url"].replace("https://one.test", "")
    assert client.post(hook, content=body, headers={"Paymongo-Signature": f"t={t},te={sig256('pmsec', f'{t}.'.encode() + body)},li="}).status_code == 200
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"}).json()[0]["status"] == "paid"

    biz, key, acc, c = setup("NG", "paystack", {"secret_key": "sk_test_ps"})
    with pytest.raises(Invalid):
        PM.create(biz, "t", amount_minor=500000, description="Membership", contact_id=c["id"])   # Paystack needs an email
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"}).json() == []      # nothing half-made
    with db.tx() as conn:
        from valeria_one.services import contacts as C
        C.patch(conn, biz, "t", c["id"], {"email": "chidi@example.com"})
    pay = PM.create(biz, "t", amount_minor=500000, description="Membership", contact_id=c["id"])
    body = json.dumps({"event": "charge.success", "data": {"reference": f"vo_{pay['id'].hex}"}}).encode()
    sig = hmac.new(b"sk_test_ps", body, hashlib.sha512).hexdigest()
    hook = acc["webhook_url"].replace("https://one.test", "")
    assert client.post(hook, content=body, headers={"x-paystack-signature": sig}).status_code == 200
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"}).json()[0]["status"] == "paid"

    biz, key, acc, c = setup("IN", "upi", {"key_id": "rzp_test", "key_secret": "ks", "webhook_secret": "rzsec"})
    assert acc["provider"] == "razorpay"
    pay = PM.create(biz, "t", amount_minor=99900, description="Consultation", contact_id=c["id"])
    body = json.dumps({"event": "payment_link.paid", "payload": {"payment_link": {"entity": {"id": "plink_1", "reference_id": str(pay["id"])}}}}).encode()
    hook = acc["webhook_url"].replace("https://one.test", "")
    assert client.post(hook, content=body, headers={"x-razorpay-signature": sig256("rzsec", body)}).status_code == 200
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"}).json()[0]["status"] == "paid"


def test_bank_transfer_page_and_owner_confirms(client):
    biz, key, acc, c = setup("MX", "bank_transfer", {"bank": "BBVA", "holder": "Lupita Pérez", "account": "0123456789",
                                                     "clabe": "012180001234567891"})
    pay = PM.create(biz, "t", amount_minor=45000, description="Uñas acrílicas", contact_id=c["id"])
    page = client.get(pay["page_url"].replace("https://one.test", "")).text
    assert "$450.00" in page and "012180001234567891" in page and "Lupita Pérez" in page
    h = {"Authorization": f"Bearer {key}"}
    assert client.post(f"/v1/payments/{pay['id']}/confirm", headers=h).status_code == 200
    assert "pago recibido" in client.get(pay["page_url"].replace("https://one.test", "")).text.lower() or \
           "payment received" in client.get(pay["page_url"].replace("https://one.test", ""), headers={"Accept-Language": "en"}).text.lower()
    assert client.post(f"/v1/payments/{pay['id']}/confirm", headers=h).status_code == 422          # not pending anymore


def test_rules_secrets_and_isolation(client, psp):
    biz, key, acc, c = setup("US", "stripe", {"secret_key": "sk_live_SECRET", "webhook_secret": "whsec_SECRET"})
    assert acc["config"]["secret_key"] == "••••"
    with db.tx() as conn:
        raw = conn.execute(db.payment_accounts.select()).mappings().one()
    assert "SECRET" not in json.dumps(raw["config"])
    with pytest.raises(Invalid):
        PM.create(biz, "t", amount_minor=0, description="x")
    other, okey = make_business("MX")
    pay = PM.create(biz, "t", amount_minor=100, description="x")
    assert client.post(f"/v1/payments/{pay['id']}/confirm", headers={"Authorization": f"Bearer {okey}"}).status_code == 404
    sb, _ = make_business("MX", sandbox=True)
    with db.tx() as conn, pytest.raises(Invalid):
        PM.connect(conn, sb, "t", "stripe", {"secret_key": "x", "webhook_secret": "y"})


def test_request_payment_from_the_customer_screen(client):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Salón Pago", "country": "MX", "industry": "salon",
                                 "email": "pago@example.com", "password": "una-clave-larga"})
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/settings").text).group(1)
    client.post("/app/payments/connect", data={"csrf": tok, "provider": "bank_transfer", "cfg_bank": "BBVA",
                                               "cfg_holder": "Salón Pago", "cfg_account": "123"})
    r = client.post("/app/contacts", data={"csrf": tok, "name": "Mariana"}, follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    client.post(f"/app/contacts/{cid}/pay", data={"csrf": tok, "description": "Anticipo novia", "amount": "1,000.00", "account_id": ""})
    page = client.get(f"/app/contacts/{cid}").text
    assert "Anticipo novia" in page and "$1,000.00" in page and "Pendiente" in page
    pid = re.search(r'/app/payments/([0-9a-f-]{36})/confirm', page).group(1)
    client.post(f"/app/payments/{pid}/confirm", data={"csrf": tok, "back": f"/app/contacts/{cid}"})
    assert "Pagado" in client.get(f"/app/contacts/{cid}").text
