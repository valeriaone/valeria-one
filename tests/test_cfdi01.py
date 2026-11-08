"""cfdi01 (v0.52.0): Mexican CFDI 4.0 through the business's OWN Facturapi account — connect (key encrypted, readiness shown),
stamp an invoice once (its own lines and IVA; the customer's fiscal data remembered; público en general with the global
period), totals checked, PDF/XML from their account, email, SAT cancellation, re-stamp after a cancel keeps the history.
Facturapi is faked: nothing leaves the machine."""
import json

import httpx
import pytest
from sqlalchemy import select

from _va import csrf, owner_session, refuse_network
from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import cfdi as CF
from valeria_one.services import contacts as C
from valeria_one.services import invoices as IVS


class FakeFacturapi:
    def __init__(self, ready=True):
        self.calls, self.ready, self.n, self.total = [], ready, 0, None

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, dict(req.url.params), body, req.headers.get("authorization")))
        p = req.url.path
        if p.endswith("/organizations/me"):
            return httpx.Response(200, json={"legal": {"name": "Decora con Hugo", "tax_id": "DEHU800101AB1", "address": {"zip": "97000"}},
                                             "is_production_ready": self.ready,
                                             "pending_steps": [] if self.ready else [{"type": "certificate", "description": "Sube tu CSD"}]})
        if p.endswith("/v2/invoices") and req.method == "POST":
            self.n += 1
            if body["customer"].get("tax_id") == "BAD010101AAA":
                return httpx.Response(400, json={"ok": False, "message": "El RFC no está en la lista del SAT", "code": "tax_id_not_found",
                                                 "path": "customer.tax_id"})
            tot = self.total
            if tot is None:
                sub = sum(i["quantity"] * i["product"]["price"] for i in body["items"])
                rate = (body["items"][0]["product"]["taxes"] or [{"rate": 0}])[0]["rate"]
                tot = round(sub * (1 + rate), 2)
            return httpx.Response(200, json={"id": f"inv_{self.n}", "uuid": f"UUID-{self.n}", "status": "valid", "livemode": False,
                                             "series": "F", "folio_number": 100 + self.n, "total": tot, "verification_url": "https://verificacfdi.sat"})
        if p.endswith("/pdf"):
            return httpx.Response(200, content=b"%PDF-fake")
        if p.endswith("/xml"):
            return httpx.Response(200, content=b"<cfdi/>")
        if p.endswith("/email"):
            return httpx.Response(200, json={"ok": True})
        if req.method == "DELETE":
            return httpx.Response(200, json={"status": "valid", "cancellation_status": "pending"})
        if req.method == "GET" and "/v2/invoices/" in p:
            return httpx.Response(200, json={"status": "canceled", "cancellation_status": "accepted"})
        return httpx.Response(404, json={"message": "unexpected " + p})


@pytest.fixture
def fake(monkeypatch):
    refuse_network(monkeypatch)
    f = FakeFacturapi()
    real = httpx.Client
    monkeypatch.setattr(CF.httpx, "Client", lambda *a, **k: real(transport=httpx.MockTransport(f)))
    return f


def shop():
    biz, _ = make_business("MX", "design", name="Decora con Hugo")
    with db.tx() as conn:
        c = C.create(conn, biz, "t", {"name": "Ana Ruiz", "email": "ana@x.mx", "phone": "+529991112233"})
        inv = IVS.create(conn, biz, "t", title="Cabecera queen", lines=[{"description": "Cabecera queen", "qty": 1, "unit_minor": 280000},
                                                                       {"description": "Instalación", "qty": 2, "unit_minor": 25000}],
                         contact_id=c["id"])
    return biz, c, inv


RECEIVER = {"tax_id": "ruia800101xy9", "legal_name": "Comercial Ruiz, S.A. de C.V.", "tax_system": "601", "zip": "97130", "use": "G03",
            "email": "facturas@ruiz.mx", "payment_form": "03", "payment_method": "PUE"}


def test_connect_keeps_the_key_encrypted_and_shows_readiness(fake):
    biz, _, _ = shop()
    with db.tx() as conn:
        with pytest.raises(Invalid):
            CF.connect(conn, biz, "t", "not-a-key")
        acc = CF.connect(conn, biz, "t", "sk_test_ABCdef12345678")
        raw = conn.execute(select(db.cfdi_accounts.c.secret)).scalar()
    assert acc["livemode"] is False and acc["org"]["name"] == "Decora con Hugo" and acc["org"]["ready"] is True and "secret" not in acc
    assert raw.startswith("enc:") and "ABCdef" not in raw
    assert fake.calls[-1][4] == "Bearer sk_test_ABCdef12345678" and fake.calls[-1][1] == "/v2/organizations/me"
    fake.ready = False
    with db.tx() as conn:
        acc = CF.connect(conn, biz, "t", "sk_live_ABCdef12345678")
    assert acc["livemode"] is True and acc["org"]["ready"] is False and acc["org"]["pending"] == ["Sube tu CSD"]


def test_stamp_once_with_their_lines_and_iva_and_remember_the_customer(fake):
    biz, c, inv = shop()
    with db.tx() as conn:
        CF.connect(conn, biz, "t", "sk_test_ABCdef12345678")
    got = CF.stamp(biz, "t", inv["id"], RECEIVER)
    method, path, _, body, _ = fake.calls[-1]
    assert (method, path) == ("POST", "/v2/invoices")
    assert body["customer"] == {"legal_name": "COMERCIAL RUIZ", "tax_id": "RUIA800101XY9", "tax_system": "601", "address": {"zip": "97130"},
                                "email": "facturas@ruiz.mx"}
    assert body["use"] == "G03" and body["payment_form"] == "03" and body["payment_method"] == "PUE" and body["currency"] == "MXN"
    assert [(i["quantity"], i["product"]["price"], i["product"]["tax_included"]) for i in body["items"]] == [(1, 2800.0, False), (2, 250.0, False)]
    rate = inv["tax_rate_bp"] / 10000 if inv["tax_minor"] is not None else None
    assert body["items"][0]["product"]["taxes"] == ([{"type": "IVA", "rate": rate}] if rate is not None else [])
    assert body["items"][0]["product"]["product_key"] == "01010101" and body["idempotency_key"] == f"vo-{inv['id']}-0"
    assert got["uuid"] == "UUID-1" and got["status"] == "valid" and got["total_mismatch"] is False
    with db.tx() as conn:
        assert conn.execute(select(db.invoices.c.cfdi).where(db.invoices.c.id == inv["id"])).scalar()["uuid"] == "UUID-1"
        f = conn.execute(select(db.contacts.c.fiscal).where(db.contacts.c.id == c["id"])).scalar()
    assert f == {"tax_id": "RUIA800101XY9", "legal_name": "COMERCIAL RUIZ", "tax_system": "601", "zip": "97130", "use": "G03"}
    with pytest.raises(Invalid) as e:
        CF.stamp(biz, "t", inv["id"], RECEIVER)
    assert str(e.value) == "cfdi_already" and fake.n == 1                                # never twice


def test_publico_en_general_uses_the_global_period(fake):
    biz, _, inv = shop()
    with db.tx() as conn:
        CF.connect(conn, biz, "t", "sk_test_ABCdef12345678")
    CF.stamp(biz, "t", inv["id"], {"publico": "1", "payment_form": "01"})
    body = fake.calls[-1][3]
    assert body["customer"] == {"legal_name": "PUBLICO EN GENERAL", "tax_id": "XAXX010101000", "tax_system": "616", "address": {"zip": "97000"}}
    assert body["use"] == "S01" and body["global"] == {"periodicity": "month", "months": f"{inv['issued_on'].month:02d}", "year": inv["issued_on"].year}


def test_checks_errors_mismatch_ppd(fake):
    biz, _, inv = shop()
    with pytest.raises(Invalid) as e:
        CF.stamp(biz, "t", inv["id"], RECEIVER)
    assert str(e.value) == "cfdi_not_connected"
    with db.tx() as conn:
        CF.connect(conn, biz, "t", "sk_test_ABCdef12345678")
    for bad, err in (({"tax_id": "123"}, "cfdi_bad_rfc"), ({"tax_system": "999"}, "cfdi_bad_tax_system"), ({"zip": "12"}, "cfdi_bad_zip"),
                     ({"use": "ZZZ"}, "cfdi_bad_use"), ({"payment_form": "77"}, "cfdi_bad_payment_form")):
        with pytest.raises(Invalid) as e:
            CF.stamp(biz, "t", inv["id"], {**RECEIVER, **bad})
        assert str(e.value) == err
    with pytest.raises(Invalid) as e:
        CF.stamp(biz, "t", inv["id"], {**RECEIVER, "tax_id": "BAD010101AAA"})
    assert "El RFC no está en la lista del SAT" in str(e.value)                          # Facturapi's own words reach the owner
    fake.total = 1.0
    got = CF.stamp(biz, "t", inv["id"], {**RECEIVER, "payment_method": "PPD"})
    assert got["total_mismatch"] is True and fake.calls[-1][3]["payment_form"] == "99"   # PPD: «por definir»


def test_files_email_cancel_and_restamp_keeps_history(fake):
    biz, _, inv = shop()
    with db.tx() as conn:
        CF.connect(conn, biz, "t", "sk_test_ABCdef12345678")
    CF.stamp(biz, "t", inv["id"], RECEIVER)
    assert CF.file(biz, inv["id"], "pdf") == (b"%PDF-fake", "application/pdf")
    assert CF.file(biz, inv["id"], "xml")[0] == b"<cfdi/>"
    CF.email(biz, "t", inv["id"], "otro@ruiz.mx")
    assert fake.calls[-1][1] == "/v2/invoices/inv_1/email" and fake.calls[-1][3] == {"email": "otro@ruiz.mx"}
    with pytest.raises(Invalid):
        CF.cancel(biz, "t", inv["id"], "01")                                            # 01 needs the replacing UUID
    got = CF.cancel(biz, "t", inv["id"], "02")
    assert fake.calls[-1][0] == "DELETE" and fake.calls[-1][2] == {"motive": "02"} and got["cancellation_status"] == "pending"
    got = CF.refresh(biz, inv["id"])
    assert got["status"] == "canceled" and got["cancellation_status"] == "accepted"
    again = CF.stamp(biz, "t", inv["id"], RECEIVER)                                     # canceled: a new CFDI may be stamped
    assert again["uuid"] == "UUID-2" and again["history"][0]["uuid"] == "UUID-1" and fake.calls[-1][3]["idempotency_key"].endswith("-1")


def test_the_screens(client, fake):
    biz, _, inv = shop()
    owner_session(client, biz)
    h = client.get(f"/app/invoices/{inv['id']}", headers={"Accept-Language": "es-MX"}).text
    assert "Factura electrónica (CFDI)" in h and "Conectar Facturapi" in h
    tok = csrf(client, "/app/cfdi")
    client.post("/app/cfdi", data={"csrf": tok, "act": "connect", "key": "sk_test_ABCdef12345678"})
    h = client.get(f"/app/invoices/{inv['id']}", headers={"Accept-Language": "es-MX"}).text
    assert "Timbrar CFDI" in h and 'name="publico"' in h
    r = client.post(f"/app/invoices/{inv['id']}/cfdi", data={"csrf": tok, "act": "stamp", **RECEIVER}, follow_redirects=False)
    assert r.status_code == 303
    h = client.get(f"/app/invoices/{inv['id']}", headers={"Accept-Language": "es-MX"}).text
    assert "UUID-1" in h and f"/app/invoices/{inv['id']}/cfdi.pdf" in h
    r = client.get(f"/app/invoices/{inv['id']}/cfdi.pdf")
    assert r.status_code == 200 and r.content == b"%PDF-fake"
    assert "/app/cfdi" in client.get("/app/invoices", headers={"Accept-Language": "es-MX"}).text
