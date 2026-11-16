"""realty02 (v0.66.1): a realtor's Valeria One site has their own design (their name, portrait, properties, Spanish), never the
generic page; the partner profile sets their language and their name; the chat wears Valeria's face in their colour."""
import pytest

from valeria_one import db
from valeria_one.services import partner as PT


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "valeria.valai.mx", "test")
    return {"Authorization": f"Bearer {key}"}


def _office(client, pk, ref, **prof):
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": "KW Regio", "country": "MX", "owner_email": f"{ref}@example.com",
                      "industry": "realtor", "language": "es", "partner_ref": ref}).json()["business"]
    r = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk, json=prof)
    assert r.status_code == 200
    return biz["id"], r.json()["slug"]


def test_realtor_site_is_theirs_in_their_language(client, pk):
    biz_id, slug = _office(client, pk, "valai-agency-r1", agent="Elizabeth Castillo", headline="Alta gama en Monterrey",
                           about="Asesora en San Pedro.", photo_url="https://valeria.valai.mx/media/a.jpg")
    h = client.get(f"/s/{slug}", headers={"Accept-Language": "en-US"}).text
    assert "<h1>Elizabeth Castillo</h1>" in h and "KW Regio" in h and "Sobre mí" in h and "Hablemos" in h and "Su mensaje" in h
    assert "Write to us" not in h and "Muy pronto verá aquí mis propiedades" in h
    client.put(f"/v1/partner/businesses/{biz_id}/listings", headers=pk, json={"listings": [
        {"ref": "a", "title": "Casa Del Valle", "operation": "sale", "price_minor": 850000000, "photos": ["https://x.mx/1.jpg"], "beds": 3},
        {"ref": "b", "title": "Depa Valle Oriente", "operation": "rent", "price_minor": 3500000}]})
    h = client.get(f"/s/{slug}").text
    assert h.count('class="card home"') == 2 and 'id="q"' in h and "https://x.mx/1.jpg" in h and "3 rec." in h
    js = client.get(f"/w/{slug}/assistant.js").text
    assert "/assistant-face.jpg" in js and "#a8823e" in js


def test_without_bio_or_photo_no_empty_about(client, pk):
    _, slug = _office(client, pk, "valai-agency-r2", agent="Mauricio Zárate")
    h = client.get(f"/s/{slug}").text
    assert 'id="nosotros"' not in h and "<h1>Mauricio Zárate</h1>" in h


def test_other_trades_keep_their_page(client, pk):
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": "Salón Lupita", "country": "MX", "owner_email": "l@example.com",
                      "industry": "salon", "partner_ref": "x-salon"}).json()["business"]
    slug = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk, json={}).json()["slug"]
    h = client.get(f"/s/{slug}").text
    assert 'class="site-hero"' in h and "Hablemos" not in h
