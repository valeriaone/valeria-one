"""shop01 (v0.56.0): the online store — switched on in Website; its priced Inventory items at /shop/<slug>; checkout is a real
register sale (pay link from the business's own account, or «pay on pickup/delivery» without one), stock goes down, the
customer becomes a contact, the owner gets a push, the customer lands on the receipt. Shipping: flat, free above an amount."""
import pytest
from sqlalchemy import select

from _va import csrf, owner_session
from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import businesses as B
from valeria_one.services import inventory as INV
from valeria_one.services import shop as SH
from valeria_one.services import site as ST


def store(ship_cost="99", free_over="1000", on=True):
    biz, _ = make_business("MX", "shop", name="Tienda Luz")
    with db.tx() as conn:
        biz = ST.save(conn, B.get(conn, biz["id"]), "t", enabled=True, shop="1" if on else "", shop_pickup="1", shop_ship="1",
                      shop_ship_cost=ship_cost, shop_free_over=free_over, shop_note="Entregas en Mérida")
        lamp = INV.add(conn, biz, "t", {"name": "Lámpara", "price_minor": 45000, "stock": 3})
        mug = INV.add(conn, biz, "t", {"name": "Taza", "price_minor": 12000, "stock": 0})
        INV.add(conn, biz, "t", {"name": "Sin precio", "stock": 5})
        biz = B.get(conn, biz["id"])
    return biz, lamp, mug


def test_the_store_page_shows_priced_items_and_sold_out(client):
    biz, lamp, mug = store()
    h = client.get(f"/shop/{biz['slug']}").text
    assert "Lámpara" in h and f'name="qty_{lamp["id"]}"' in h and "Taza" in h and f'name="qty_{mug["id"]}"' not in h
    assert "Sin precio" not in h and "Entregas en Mérida" in h
    assert f'href="/shop/{biz["slug"]}"' in client.get(f"/s/{biz['slug']}").text
    off, _, _ = store(on=False)
    assert client.get(f"/shop/{off['slug']}").status_code == 404


def test_checkout_is_a_register_sale_stock_down_contact_push_receipt(client, monkeypatch):
    biz, lamp, _ = store()
    pushed = []
    from valeria_one.services import push
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushed.append(a) or 1)
    tok = csrf(client, f"/shop/{biz['slug']}")
    r = client.post(f"/shop/{biz['slug']}", data={"csrf": tok, f"qty_{lamp['id']}": "2", "name": "Ana Ruiz", "phone": "999 111 2233",
                                                   "how": "ship", "address": "Calle 60 #500, Centro, Mérida"}, follow_redirects=False)
    assert r.status_code == 303 and "/receipt/" in r.headers["location"]
    with db.tx() as conn:
        sale = conn.execute(select(db.pos_sales if hasattr(db, "pos_sales") else db.sales)).mappings().one()
        stock = INV.item(conn, biz["id"], lamp["id"])["stock"]
        c = conn.execute(select(db.contacts).where(db.contacts.c.business_id == biz["id"])).mappings().one()
    assert stock == 1 and c["phone"] == "+529991112233" and c["name"] == "Ana Ruiz"
    descs = [ln["description"] for ln in sale["lines"]]
    assert "Lámpara" in descs[0] and "Envío" in descs                                    # 900 < 1000: delivery charged
    assert sale["method"] == "other" and "Calle 60" in sale["note"]                       # no payment account: pay on delivery
    assert pushed and "Ana Ruiz" in pushed[-1][2]
    assert "Lámpara" in client.get(r.headers["location"]).text


def test_free_delivery_above_the_amount_and_checks():
    biz, lamp, mug = store(free_over="800")
    got = SH.checkout(biz, {f"qty_{lamp['id']}": "2", "name": "Beto", "email": "beto@x.mx", "how": "ship", "address": "Calle 1 #2, Itzimná"})
    assert [ln["description"] for ln in got["sale"]["lines"]] == ["Lámpara"]               # 900 ≥ 800: free delivery, no line
    for form, err in (({f"qty_{lamp['id']}": "5", "name": "X", "email": "x@x.mx", "how": "pickup"}, "shop_not_enough"),
                      ({f"qty_{mug['id']}": "1", "name": "X", "email": "x@x.mx", "how": "pickup"}, "shop_not_enough"),   # sold out
                      ({f"qty_{lamp['id']}": "0", "name": "X", "email": "x@x.mx", "how": "pickup"}, "shop_empty"),
                      ({f"qty_{lamp['id']}": "1", "name": "X", "how": "pickup"}, "shop_need_reach"),
                      ({f"qty_{lamp['id']}": "1", "name": "X", "email": "x@x.mx", "how": "ship"}, "shop_need_address"),
                      ({f"qty_{lamp['id']}": "1", "name": "X", "email": "x@x.mx", "how": "fly"}, "shop_pick_how")):
        with pytest.raises(Invalid) as e:
            SH.checkout(biz, form)
        assert str(e.value) == err, (form, str(e.value))


def test_with_a_payment_account_it_is_a_pay_link(monkeypatch):
    biz, lamp, _ = store()
    from valeria_one.services import payments as PM
    monkeypatch.setattr(PM, "can_link", lambda conn, bid: True)
    made = []

    def fake_create(biz_, actor, *, amount_minor, description, contact_id=None, http=None, **k):
        with db.tx() as conn:
            pid = conn.execute(db.payments.insert().values(business_id=biz_["id"], provider="stripe", amount_minor=amount_minor,
                                                           currency="MXN", description=description, status="pending", token="tokshop1",
                                                           url="https://pay.example/x").returning(db.payments.c.id)).scalar()
        made.append(amount_minor)
        return {"id": pid, "amount_minor": amount_minor}
    monkeypatch.setattr(PM, "create", fake_create)
    got = SH.checkout(biz, {f"qty_{lamp['id']}": "1", "name": "Ana", "phone": "9991112233", "how": "pickup"})
    assert got["sale"]["method"] == "link" and made == [45000] and got["pay_url"] == "https://pay.example/x"


def test_the_owner_switches_it_on_in_website(client):
    biz, _ = make_business("MX", "shop", name="Tienda Sol")
    owner_session(client, biz)
    tok = csrf(client, "/app/website")
    client.post("/app/website", data={"csrf": tok, "enabled": "1", "shop": "1", "shop_pickup": "1", "shop_ship_cost": "120",
                                      "shop_free_over": "1,500"})
    with db.tx() as conn:
        s = B.get(conn, biz["id"])["site"]
    assert s["shop"] == "1" and s["shop_ship_cost"] == "120" and s["shop_free_over"] == "1500"
    assert "Tienda en línea" in client.get("/app/website", headers={"Accept-Language": "es-MX"}).text
