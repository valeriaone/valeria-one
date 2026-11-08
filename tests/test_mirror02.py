"""mirror02 (v0.50.0): the setup talk asks their OK to rebuild their site from their own website / Facebook page, and tells them
how to send their Facebook photos (the page's «Download your information»)."""
import pytest

from _va import csrf, owner_session, refuse_network
from conftest import make_business
from valeria_one import db
from valeria_one.services import onboard as OB


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def ans(biz, key):
    with db.tx() as conn:
        return OB.answers(conn, biz["id"]).get(key) or {}


def test_website_and_facebook_steps_ask_the_authorization(client):
    biz, _ = make_business("MX", "design", name="Decora con Hugo")
    owner_session(client, biz)
    h = client.get("/app/onboard?q=website", headers={"Accept-Language": "es-MX"}).text
    assert 'name="mirror_ok"' in h and "Autorizo a Valeria a usar el contenido de mi página" in h
    tok = csrf(client, "/app/onboard?q=website")
    client.post("/app/onboard", data={"csrf": tok, "q": "website", "act": "answer", "sites": "www.decoraconhugo.com", "mirror_ok": "1"},
                follow_redirects=False)
    assert ans(biz, "website")["mirror_ok"] is True and ans(biz, "website")["value"] == ["https://www.decoraconhugo.com"]
    h = client.get("/app/onboard?q=facebook", headers={"Accept-Language": "es-MX"}).text
    assert "Autorizo a Valeria a usar las fotos y la información de mi página de Facebook" in h
    client.post("/app/onboard", data={"csrf": tok, "q": "facebook", "act": "answer", "value": "facebook.com/decoraconhugo"},
                follow_redirects=False)
    assert ans(biz, "facebook").get("mirror_ok") is False                              # not ticked: never assumed


def test_the_photos_step_explains_the_facebook_export(client):
    biz, _ = make_business("MX", "fitness", name="Up Fitness")
    owner_session(client, biz)
    h = client.get("/app/onboard?q=look", headers={"Accept-Language": "es-MX"}).text
    assert "Descargar tu información" in h
