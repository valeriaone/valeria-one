"""cfg01 (v0.47.0): «Valeria lo configura» — the owner tells their prices (typed or a photo of the list) and hours; Valeria
loads them. The step goes green only when real prices are live; the page promises «you plug in, Valeria finishes»."""
import pytest

from _va import csrf, owner_session, refuse_network
from conftest import make_business
from valeria_one import db
from valeria_one.services import booking as BK
from valeria_one.services import onboard as OB

def _png() -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 30, 30)).save(b, "PNG")
    return b.getvalue()


PNG = _png()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def st(biz):
    with db.tx() as conn:
        return OB.state(conn, biz, links=False)["qs"]["prices"]


def test_prices_typed_then_green_once_loaded(client):
    biz, _ = make_business("MX", "fitness", name="Armor Gym")
    owner_session(client, biz)
    page = client.get("/app/onboard?q=prices", headers={"Accept-Language": "es-MX"}).text
    assert "¿Qué vendes y a qué precio?" in page and "Valeria termina lo demás" in page and 'enctype="multipart/form-data"' in page
    tok = csrf(client, "/app/onboard?q=prices")
    r = client.post("/app/onboard", data={"csrf": tok, "q": "prices", "act": "answer", "prices": ""}, follow_redirects=False)
    assert r.headers["location"] == "/app/onboard?q=prices" and st(biz)["status"] == "new"            # nothing given: asked again
    r = client.post("/app/onboard", data={"csrf": tok, "q": "prices", "act": "answer", "prices": "Mensualidad $650\nVisita $80",
                                          "hours": "Lun–Vie 6–22"}, follow_redirects=False)
    assert r.status_code == 303
    it = st(biz)
    assert it["status"] == "todo" and it["answer"]["value"] == "Mensualidad $650\nVisita $80" and it["answer"]["hours"] == "Lun–Vie 6–22"
    assert any(l["k"] == "ob_l_prices_valeria" for l in it["lines"])
    with db.tx() as conn:                                                      # Valeria loads them
        BK.add_service(conn, biz, "valeria", name="Mensualidad", minutes=60, price_minor=65000)
    assert st(biz)["status"] == "ready"


def test_a_photo_of_the_price_list_is_enough(client):
    biz, _ = make_business("MX", "salon", name="Estética Sol")
    owner_session(client, biz)
    tok = csrf(client, "/app/onboard?q=prices")
    r = client.post("/app/onboard", data={"csrf": tok, "q": "prices", "act": "answer"},
                    files={"photo": ("lista.png", PNG, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    it = st(biz)
    assert it["status"] == "todo" and it["answer"]["photo"]
    with db.tx() as conn:
        f = conn.execute(db.files.select().where(db.files.c.id == it["answer"]["photo"])).mappings().one()
    assert str(f["business_id"]) == str(biz["id"])
    r = client.post("/app/onboard", data={"csrf": tok, "q": "prices", "act": "answer", "prices": "Corte $200"}, follow_redirects=False)
    assert st(biz)["answer"]["photo"] == it["answer"]["photo"]                # adding words keeps the photo
    r = client.post("/app/onboard", data={"csrf": tok, "q": "prices", "act": "answer"},
                    files={"photo": ("rota.png", b"\x89PNG\r\n\x1a\nbroken", "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/onboard?q=prices"   # a broken picture: a calm word


def test_later_and_no_are_fine(client):
    biz, _ = make_business("MX", "salon", name="Estética Luna")
    owner_session(client, biz)
    tok = csrf(client, "/app/onboard?q=prices")
    client.post("/app/onboard", data={"csrf": tok, "q": "prices", "act": "later"}, follow_redirects=False)
    assert st(biz)["has"] == "later"
