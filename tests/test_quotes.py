import re
from datetime import date, timedelta

import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import quotes as QS


def new_quote(client, country="MX", lines=None):
    biz, key = make_business(country)
    h = {"Authorization": f"Bearer {key}"}
    c = client.post("/v1/contacts", headers=h, json={"name": "Ana López"}).json()
    r = client.post("/v1/quotes", headers=h, json={"title": "Remodelación de baño", "contact_id": c["id"],
                                                   "lines": lines or [{"description": "Mano de obra", "qty": 3, "unit_minor": 150000},
                                                                      {"description": "Azulejo", "qty": 10, "unit_minor": 25000}],
                                                   "notes": "Incluye retiro de escombro.", "valid_days": 10})
    assert r.status_code == 201, r.text
    return biz, h, r.json()


def test_totals_use_the_country_tax(client):
    _, _, q = new_quote(client, "MX")
    assert (q["subtotal_minor"], q["tax_minor"], q["total_minor"], q["tax_name"]) == (700000, 112000, 812000, "IVA")
    assert q["number"] == 1 and len(q["doc_hash"]) == 64 and q["url"].startswith("https://one.test/q/")
    _, _, us = new_quote(client, "US")
    assert us["tax_minor"] is None and us["total_minor"] == us["subtotal_minor"]          # US sales tax: never invented


def test_customer_signs_and_evidence_is_kept(client):
    biz, h, q = new_quote(client)
    path = q["url"].replace("https://one.test", "")
    page = client.get(path).text
    assert "Remodelación de baño" in page and "$8,120.00" in page and "Código de Comercio" in page
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    bad = client.post(path, data={"csrf": tok, "name": "Ana López"}, follow_redirects=True)           # box not ticked
    assert "Aceptada" not in bad.text
    r = client.post(path, data={"csrf": tok, "name": "Ana López", "agree": "1"}, headers={"User-Agent": "Phone/1.0"},
                    follow_redirects=True)
    assert "Firmada por" in r.text or "Signed by" in r.text
    assert q["doc_hash"] in r.text
    stored = client.get("/v1/quotes", headers=h).json()[0]
    assert stored["status"] == "accepted" and stored["signed_name"] == "Ana López" and stored["signed_ua"] == "Phone/1.0"
    ev = [e for e in client.get("/v1/events", headers=h).json() if e["type"] == "quote.accepted"][0]
    assert ev["data"]["doc_hash"] == q["doc_hash"]
    again = client.post(path, data={"csrf": tok, "name": "Otra Persona", "agree": "1"}, follow_redirects=True)
    assert client.get("/v1/quotes", headers=h).json()[0]["signed_name"] == "Ana López"         # signs once
    assert again.status_code == 200


def test_tampering_expiry_decline_and_cancel(client):
    biz, h, q = new_quote(client)
    with db.tx() as conn:
        conn.execute(db.quotes.update().values(total_minor=1))                                   # changed after sending
        with pytest.raises(Invalid) as e:
            QS.accept(conn, q["token"], name="Ana López", agree=True, ip="1.2.3.4", ua="x")
        assert e.value.field == "doc_hash"
    biz, h, q2 = new_quote(client)
    with db.tx() as conn, pytest.raises(Invalid):
        QS.accept(conn, q2["token"], name="Ana López", agree=True, ip="", ua="", today=date.today() + timedelta(days=30))
    biz, h, q3 = new_quote(client)
    with db.tx() as conn:
        QS.decline(conn, q3["token"], "muy caro")
        with pytest.raises(Invalid):
            QS.accept(conn, q3["token"], name="Ana López", agree=True, ip="", ua="")
    biz, h, q4 = new_quote(client)
    assert client.post(f"/v1/quotes/{q4['id']}/cancel", headers=h).status_code == 200
    assert "Retirada" in client.get(q4["url"].replace("https://one.test", "")).text or \
           "Withdrawn" in client.get(q4["url"].replace("https://one.test", ""), headers={"Accept-Language": "en"}).text


def test_validation_numbering_and_isolation(client):
    biz, h, q = new_quote(client)
    assert client.post("/v1/quotes", headers=h, json={"title": "x", "lines": []}).status_code == 422
    assert client.post("/v1/quotes", headers=h, json={"title": "x", "lines": [{"description": "a", "qty": 0, "unit_minor": 1}]}).status_code == 422
    q2 = client.post("/v1/quotes", headers=h, json={"title": "Otra", "lines": [{"description": "a", "unit_minor": 100}]}).json()
    assert q2["number"] == 2
    other, okey = make_business("MX")
    assert client.post(f"/v1/quotes/{q['id']}/cancel", headers={"Authorization": f"Bearer {okey}"}).status_code == 404


def test_owner_creates_from_customer_screen(client):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Taller Q", "country": "CO", "industry": "contractor",
                                 "email": "q@example.com", "password": "una-clave-larga"})
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/contacts").text).group(1)
    r = client.post("/app/contacts", data={"csrf": tok, "name": "Juan"}, follow_redirects=False)
    cid = r.headers["location"].rsplit("/", 1)[1]
    client.post(f"/app/contacts/{cid}/quote", data={"csrf": tok, "title": "Pintura sala", "line_desc": ["Pintura", "", "", ""],
                                                    "line_qty": ["2", "1", "1", "1"], "line_unit": ["150000", "", "", ""],
                                                    "valid_days": "7", "account_id": ""})
    page = client.get(f"/app/contacts/{cid}").text
    assert "Pintura sala" in page and "#1" in page and "/q/" in page
