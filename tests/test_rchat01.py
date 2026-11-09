"""rchat01 (v0.58.0): a partner's sites carry one chat line per customer (by partner_ref); it is an empty script until that
business's office exists and its assistant is on (the partner switches it on), then it is the business's live chat."""
import pytest

from valeria_one import db
from valeria_one.services import partner as PT


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "valeria.valai.mx", "test")
    return {"Authorization": f"Bearer {key}"}


def test_the_chat_by_reference_appears_once_the_assistant_is_on(client, pk):
    r = client.get("/w/ref/valai-agency-1/assistant.js")
    assert r.status_code == 200 and "not on yet" in r.text and r.headers["access-control-allow-origin"] == "*"   # no office yet
    biz = client.post("/v1/partner/businesses", headers=pk, json={"name": "Luis Cámara Bienes Raíces", "country": "MX",
                                                                    "owner_email": "luis@example.com", "industry": "realtor",
                                                                    "partner_ref": "valai-agency-1"}).json()["business"]
    assert "not on yet" in client.get("/w/ref/valai-agency-1/assistant.js").text           # office, but chat not on
    r = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk, json={"whatsapp": "999 442 7745", "headline": "Casas en Mérida"})
    assert r.status_code == 200 and r.json()["slug"]
    js = client.get("/w/ref/valai-agency-1/assistant.js").text
    assert "not on yet" not in js and "Luis Cámara Bienes Raíces" in js and "wa.me/529994427745" in js
    assert client.get(f"/w/{r.json()['slug']}/assistant.js").status_code == 200
    r2 = client.post(f"/v1/partner/businesses/{biz['id']}/assistant", headers=pk, json={"whatsapp": "123"})
    assert r2.status_code == 200                                                            # an odd number never blocks the chat
    assert client.post(f"/v1/partner/businesses/{biz['id']}/assistant", json={}).status_code == 401
