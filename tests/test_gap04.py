"""gap04 (v0.42.0): ready connectors — a Shopify / WooCommerce order becomes a customer + a deal with its total; a private live
CSV link for Google Sheets (=IMPORTDATA) that stops the moment it is switched off; the page lays out Zapier / Make."""
import pytest
from sqlalchemy import select

from valeria_one import db
from valeria_one.services import NotFound
from valeria_one.services import feeds as FD
from valeria_one.services import inbound as IN

from _va import owner_session
from conftest import make_business

SHOPIFY = {"name": "#1001", "order_number": 1001, "currency": "MXN", "total_price": "2800.00", "email": "sofia@example.com",
           "customer": {"first_name": "Sofía", "last_name": "Cauich", "email": "sofia@example.com", "phone": "+529991234567"},
           "line_items": [{"name": "Cabecera mandala queen", "quantity": 1}]}
WOO = {"id": 77, "number": "77", "currency": "MXN", "total": "1450.50",
       "billing": {"first_name": "Carlos", "last_name": "Chan", "email": "carlos@example.com", "phone": "+529992345678"},
       "line_items": [{"name": "Celosía", "quantity": 2}]}


def hook(biz):
    with db.tx() as conn:
        _, token = IN.create(conn, biz, "t", name="Shopify", makes="deal", stage="new")
    return token


@pytest.mark.parametrize("order, tag, title, amount", [(SHOPIFY, "shopify", "Shopify #1001", 280000), (WOO, "woocommerce", "WooCommerce 77", 145050)])
def test_a_store_order_becomes_a_customer_and_a_deal_with_its_total(order, tag, title, amount):
    biz, _ = make_business("MX", "shop", name="Tienda")
    out = IN.receive(hook(biz), order)
    with db.tx() as conn:
        c = conn.execute(select(db.contacts).where(db.contacts.c.id == out["contact_id"])).mappings().one()
        d = conn.execute(select(db.deals).where(db.deals.c.id == out["deal_id"])).mappings().one()
    assert c["email"] and c["phone"] and tag in c["tags"] and c["name"].split()[0] in ("Sofía", "Carlos")
    assert d["title"] == title and d["amount_minor"] == amount


def test_a_normal_hook_call_is_untouched():
    biz, _ = make_business("MX", "shop", name="Tienda 2")
    out = IN.receive(hook(biz), {"name": "Ana", "phone": "+529993456789", "amount_minor": 5000})
    with db.tx() as conn:
        assert conn.execute(select(db.deals.c.amount_minor).where(db.deals.c.id == out["deal_id"])).scalar() == 5000


def test_a_sheets_link_reads_live_and_stops_when_switched_off(client):
    biz, _ = make_business("MX", "shop", name="Tienda 3")
    IN.receive(hook(biz), SHOPIFY)
    with db.tx() as conn:
        feed, token = FD.create(conn, biz, "t", "contacts")
    r = client.get(f"/feeds/{token}.csv")
    assert r.status_code == 200 and "Sofía" in r.text and r.headers["content-type"].startswith("text/csv")
    with db.tx() as conn:
        FD.delete(conn, biz["id"], "t", feed["id"])
    assert client.get(f"/feeds/{token}.csv").status_code == 404
    assert client.get("/feeds/vfd_nope.csv").status_code == 404
    with pytest.raises(NotFound):
        FD.read(token)


def test_the_page_lays_out_store_sheets_and_zapier(client):
    biz, _ = make_business("MX", "shop", name="Tienda 4")
    owner_session(client, biz)
    page = client.get("/app/connect-apps", headers={"Accept-Language": "es-MX"}).text
    assert 'id="store"' in page and 'id="sheets"' in page and 'id="zap"' in page and "Order creation" in page
