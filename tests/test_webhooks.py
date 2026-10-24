import json
import socket
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from conftest import make_business
from valeria_one import db
from valeria_one.crypto import hmac_sha256_hex
from valeria_one.services import webhooks as WH


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    real = socket.getaddrinfo

    def fake(host, port, *a, **kw):
        table = {"hooks.example.com": "93.184.215.14", "inside.example.com": "10.0.0.5", "localhost": "127.0.0.1"}
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
        return real(host, port, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_only_public_https(client):
    _, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    for bad in ("http://hooks.example.com/x", "https://localhost/x", "https://inside.example.com/x", "https://u:p@hooks.example.com/"):
        assert client.post("/v1/webhooks", headers=h, json={"url": bad}).status_code == 422, bad
    r = client.post("/v1/webhooks", headers=h, json={"url": "https://hooks.example.com/vo", "events": ["contact.created", "message.*"]})
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    assert "secret" not in client.get("/v1/webhooks", headers=h).json()[0]


def test_events_are_delivered_signed_and_filtered(client):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    secret = client.post("/v1/webhooks", headers=h, json={"url": "https://hooks.example.com/vo",
                                                          "events": ["contact.created", "deal.*"]}).json()["secret"]
    client.post("/v1/contacts", headers=h, json={"name": "Ana"})
    client.post("/v1/deals", headers=h, json={"title": "Paquete"})
    client.post("/v1/appointments", headers=h, json={"title": "x", "starts_at": "2026-12-01T10:00:00Z", "ends_at": "2026-12-01T11:00:00Z"})
    got = []

    def handler(req):
        body = req.content
        assert req.headers["x-vo-signature"] == hmac_sha256_hex(secret, body)
        got.append(json.loads(body)["type"])
        return httpx.Response(200)
    stats = WH.deliver_due(http=_mock(handler))
    assert got == ["contact.created", "deal.created"] and stats["delivered"] == 2      # appointment.booked not subscribed
    assert WH.deliver_due(http=_mock(handler))["delivered"] == 0                         # delivered once


def test_retry_backoff_and_give_up(client):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    client.post("/v1/webhooks", headers=h, json={"url": "https://hooks.example.com/vo"})
    client.post("/v1/contacts", headers=h, json={"name": "Ana"})
    down = _mock(lambda r: httpx.Response(500))
    now = datetime.now(timezone.utc)
    assert WH.deliver_due(http=down, now=now)["retry"] >= 1
    assert WH.deliver_due(http=down, now=now)["retry"] == 0                               # not due yet
    for i in range(len(WH.BACKOFF)):
        now += timedelta(seconds=WH.BACKOFF[i] + 1)
        WH.deliver_due(http=down, now=now)
    with db.tx() as conn:
        rows = conn.execute(db.webhook_deliveries.select()).mappings().all()
    assert all(r["given_up_at"] for r in rows) and all(r["attempts"] == len(WH.BACKOFF) + 1 for r in rows)
    ep = client.get("/v1/webhooks", headers=h).json()[0]
    assert ep["last_status"] == 500


def test_deleted_endpoint_and_test_ping(client):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    ep = client.post("/v1/webhooks", headers=h, json={"url": "https://hooks.example.com/vo"}).json()
    assert client.post(f"/v1/webhooks/{ep['id']}/test", headers=h).status_code == 202
    seen = []
    WH.deliver_due(http=_mock(lambda r: seen.append(json.loads(r.content)["type"]) or httpx.Response(204)))
    assert "webhook.test" in seen
    client.post("/v1/contacts", headers=h, json={"name": "Bo"})
    client.delete(f"/v1/webhooks/{ep['id']}", headers=h)
    assert WH.deliver_due(http=_mock(lambda r: httpx.Response(200)))["gave_up"] >= 1
    assert client.get(f"/v1/webhooks/{ep['id']}/deliveries", headers=h).status_code == 200


def test_no_event_no_delivery_when_transaction_fails(client):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    client.post("/v1/webhooks", headers=h, json={"url": "https://hooks.example.com/vo"})
    with db.tx() as conn:
        before = conn.execute(select(func.count()).select_from(db.webhook_deliveries)).scalar()
    assert client.post("/v1/contacts", headers=h, json={"name": "X", "phone": "123"}).status_code == 422
    with db.tx() as conn:
        assert conn.execute(select(func.count()).select_from(db.webhook_deliveries)).scalar() == before


def test_one_business_never_gets_anothers_events(client):
    a, ka = make_business("MX")
    b, kb = make_business("US")
    client.post("/v1/webhooks", headers={"Authorization": f"Bearer {ka}"}, json={"url": "https://hooks.example.com/a"})
    client.post("/v1/contacts", headers={"Authorization": f"Bearer {kb}"}, json={"name": "B only"})
    seen = []
    WH.deliver_due(http=_mock(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200)))
    assert all(e["business_id"] == str(a["id"]) for e in seen)
