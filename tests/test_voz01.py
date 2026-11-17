"""voz01 (v0.66.3): a realtor's Valeria One page carries Valeria LIVE with her voice (answers + voice + mic from their
valeria.valai.mx site) when the partner sends the voice address; only valeria.valai.mx/voz-vo/<slug> is accepted; one bubble."""
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


def test_voice_widget_on_the_realtor_page(client, pk):
    _, slug = _office(client, pk, "valai-agency-v1", agent="Elizabeth Castillo", voice="https://valeria.valai.mx/voz-vo/kw-regio")
    h = client.get(f"/s/{slug}", headers={"Accept-Language": "en-US"}).text
    assert 'data-url="https://valeria.valai.mx/voz-vo/kw-regio/pregunta"' in h and 'data-voz="https://valeria.valai.mx/voz-vo/kw-regio/valeria"' in h
    assert "Escúcheme" in h and "asistente de Elizabeth Castillo" in h and 'id="svMic"' in h and "/assistant-face.jpg" in h
    assert "assistant.js" not in h                                                      # never two bubbles
    assert "Pregúntele a Valeria" in h and "Pregúntale" not in h                       # usted01: «usted» like the rest of the page
    assert "' sv-wa'" in h and ".sv .sv-panel{padding:0" in h                         # the page's .wa / section rules never reach her panel


def test_no_voice_keeps_the_office_chat(client, pk):
    _, slug = _office(client, pk, "valai-agency-v2", agent="Mauricio Zárate")
    h = client.get(f"/s/{slug}").text
    assert f"/w/{slug}/assistant.js" in h and 'id="sv"' not in h


def test_only_our_voice_address(client, pk):
    _, slug = _office(client, pk, "valai-agency-v3", agent="X", voice="https://evil.example/voz-vo/x")
    h = client.get(f"/s/{slug}").text
    assert "evil.example" not in h and 'id="sv"' not in h                               # a bad address never reaches the page


def test_voice_follows_the_portal(client, pk):
    biz_id, slug = _office(client, pk, "valai-agency-v4", agent="Y", voice="https://valeria.valai.mx/voz-vo/old-slug")
    client.post(f"/v1/partner/businesses/{biz_id}/assistant", headers=pk, json={"voice": "https://valeria.valai.mx/voz-vo/new-slug"})
    assert "voz-vo/new-slug/pregunta" in client.get(f"/s/{slug}").text


def test_one_bad_field_drops_only_itself(client, pk):
    _, slug = _office(client, pk, "valai-agency-v5", agent="Luis Cámara", whatsapp="999 442 7745 999 486 9634",
                      phone="9994427745", email="luis@example.com", voice="https://valeria.valai.mx/voz-vo/camara")
    h = client.get(f"/s/{slug}").text
    assert "voz-vo/camara/pregunta" in h and "luis@example.com" in h and "+529994427745" in h   # only the glued WhatsApp is left out
