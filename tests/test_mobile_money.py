import json
import re

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import payments as PM


class FakeMM:
    def __init__(self, paid=True):
        self.calls, self.paid = [], paid

    def __call__(self, req: httpx.Request):
        self.calls.append(req)
        p = req.url.path
        if p.endswith("/oauth/v1/generate"):
            return httpx.Response(200, json={"access_token": "MPESA-AT", "expires_in": "3599"})
        if p.endswith("/stkpush/v1/processrequest"):
            return httpx.Response(200, json={"CheckoutRequestID": "ws_CO_1", "ResponseCode": "0"})
        if p.endswith("/stkpushquery/v1/query"):
            return httpx.Response(200, json={"ResultCode": "0" if self.paid else "1032"})
        if p.endswith("/collection/token/"):
            return httpx.Response(200, json={"access_token": "MOMO-AT"})
        if p.endswith("/collection/v1_0/requesttopay"):
            return httpx.Response(202)
        if "/collection/v1_0/requesttopay/" in p:
            return httpx.Response(200, json={"status": "SUCCESSFUL" if self.paid else "FAILED"})
        return httpx.Response(404, json={})


@pytest.fixture
def mm(monkeypatch):
    f = FakeMM()
    c = httpx.Client(transport=httpx.MockTransport(f))
    monkeypatch.setattr(PM, "_http", lambda http=None: http or c)
    return f


def setup(country, provider, cfg):
    biz, key = make_business(country)
    with db.tx() as conn:
        acc = PM.connect(conn, biz, "t", provider, cfg)
        c = C.create(conn, biz, "t", {"name": "Wanjiku", "phone": "0712 345 678" if country == "KE" else "0772 123 456"})
    return biz, key, acc, c


def test_mpesa_stk_push_and_verified_callback(client, mm):
    biz, key, acc, c = setup("KE", "mpesa", {"consumer_key": "ck", "consumer_secret": "cs", "shortcode": "174379", "passkey": "pk"})
    with pytest.raises(Invalid):
        PM.create(biz, "t", amount_minor=150050, description="Haircut", contact_id=c["id"])       # M-Pesa: whole shillings only
    pay = PM.create(biz, "t", amount_minor=150000, description="Haircut", contact_id=c["id"])
    page = client.get(f"/p/{pay['token']}").text
    assert "+254712345678" in page and "M-Pesa" in page
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post(f"/p/{pay['token']}/push", data={"csrf": tok, "phone": "0712 345 678"}, follow_redirects=True)
    assert "PIN" in r.text
    stk = json.loads([q for q in mm.calls if q.url.path.endswith("processrequest")][0].content)
    assert stk["Amount"] == 1500 and stk["PhoneNumber"] == "254712345678" and stk["CallBackURL"] == acc["webhook_url"]
    hook = acc["webhook_url"].replace("https://one.test", "")
    forged = {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": 0}}}
    mm.paid = False                                                       # a forged "paid" callback is checked with Safaricom
    client.post(hook, json=forged)
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/payments", headers=h).json()[0]["status"] == "pending"
    mm.paid = True
    client.post(hook, json=forged)
    assert client.get("/v1/payments", headers=h).json()[0]["status"] == "paid"


def test_mtn_momo_request_to_pay(client, mm):
    biz, key, acc, c = setup("UG", "mtn_momo", {"api_user": "u", "api_key": "k", "subscription_key": "s", "target_environment": "mtnuganda"})
    pay = PM.create(biz, "t", amount_minor=50000, description="Delivery", contact_id=c["id"])
    out = PM.push(pay["token"], "0772 123 456")
    req = [q for q in mm.calls if q.url.path.endswith("/requesttopay")][0]
    body = json.loads(req.content)
    assert body["amount"] == "50000" and body["currency"] == "UGX" and body["payer"]["partyId"] == "256772123456"
    assert req.headers["x-reference-id"] == out["reference"] and req.headers["x-target-environment"] == "mtnuganda"
    hook = acc["webhook_url"].replace("https://one.test", "")
    client.post(hook, json={"externalId": str(pay["id"]), "status": "SUCCESSFUL"})
    assert client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"}).json()[0]["status"] == "paid"
    client.post(hook, json={"externalId": "not-a-uuid", "status": "SUCCESSFUL"})          # junk never crashes


def test_secrets_hidden_and_amount_in_ugx():
    biz, key, acc, c = setup("UG", "mtn_momo", {"api_user": "u", "api_key": "SECRETKEY", "subscription_key": "SUBKEY", "target_environment": "sandbox"})
    assert acc["config"]["api_key"] == "••••" and acc["config"]["subscription_key"] == "••••"
    with db.tx() as conn:
        raw = conn.execute(db.payment_accounts.select()).mappings().one()
    assert "SECRETKEY" not in json.dumps(raw["config"])
