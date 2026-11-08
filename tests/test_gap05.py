"""gap05 (v0.42.0): WhatsApp templates with header, footer and buttons (Meta), Twilio templates made and approved here through
the owner's own Twilio (Content API) and sent by their HX… SID, and the number's quality rating + daily tier."""
import json

import httpx
import pytest

from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import inbox as X
from valeria_one.services import wa_templates as WTS

from _va import owner_session
from conftest import make_business
from test_wati01 import WA_CFG

TW_CFG = {"account_sid": "AC1", "auth_token": "t", "from_number": "+529991234567"}


def mock(routes):
    seen = []
    def handler(req):
        seen.append(req)
        for key, resp in routes.items():
            if key in str(req.url):
                return httpx.Response(resp[0], json=resp[1])
        return httpx.Response(404, json={})
    return seen, httpx.Client(transport=httpx.MockTransport(handler))


def account(biz, channel, cfg):
    with db.tx() as conn:
        return X.connect(conn, biz, "t", channel=channel, name=channel, cfg=cfg)


def test_extras_are_checked():
    assert WTS.clean_extras("Hola", "Decora con Hugo", "Sí | No | Ver precios") == {
        "header": "Hola", "footer": "Decora con Hugo", "buttons": [{"type": "quick", "text": t} for t in ("Sí", "No", "Ver precios")]}
    assert WTS.clean_extras(buttons="Ver catálogo | https://x.mx/c")["buttons"] == [{"type": "url", "text": "Ver catálogo", "url": "https://x.mx/c"}]
    for bad in ({"buttons": "a | b | c | d"}, {"header": "x" * 61}, {"header": "Hola {{1}}"}):
        with pytest.raises(Invalid):
            WTS.clean_extras(**bad)


def test_meta_gets_header_body_footer_and_buttons():
    biz, _ = make_business("MX", "contractor", name="Taller")
    acc = account(biz, "whatsapp", WA_CFG)
    with db.tx() as conn:
        t = WTS.save(conn, biz, "t", account_id=acc["id"], name="aparta_cabecera", language="es_MX", category="MARKETING",
                     body="Hola {{1}}, ¿te apartamos tu cabecera?", example="Sofía", header="Decora con Hugo",
                     footer="Pagas al recibir", buttons="Sí, apártala | Ahora no")
    seen, http = mock({"message_templates": (200, {"id": "9", "status": "PENDING"})})
    row = WTS.submit(biz, "t", t["id"], http=http)
    comps = json.loads(seen[0].content)["components"]
    assert [c["type"] for c in comps] == ["HEADER", "BODY", "FOOTER", "BUTTONS"] and row["status"] == "PENDING"
    assert [b["text"] for b in comps[3]["buttons"]] == ["Sí, apártala", "Ahora no"]


def test_twilio_templates_are_made_approved_and_sent_by_sid():
    biz, _ = make_business("MX", "contractor", name="Taller T")
    acc = account(biz, "whatsapp_twilio", TW_CFG)
    with db.tx() as conn:
        assert any(a["id"] == acc["id"] and a["ready"] for a in WTS.accounts(conn, biz["id"]))
        t = WTS.save(conn, biz, "t", account_id=acc["id"], name="recordatorio", language="es_MX", category="UTILITY",
                     body="Hola {{1}}, mañana vamos a medir.", example="Sofía", buttons="Confirmo | Cambiar")
    seen, http = mock({"/ApprovalRequests/whatsapp": (201, {}), "content.twilio.com/v1/Content": (201, {"sid": "HX123"})})
    row = WTS.submit(biz, "t", t["id"], http=http)
    assert row["status"] == "PENDING" and row["meta_id"] == "HX123"
    made = json.loads(seen[0].content)
    assert "twilio/quick-reply" in made["types"] and made["variables"] == {"1": "Sofía"}
    _, http2 = mock({"/ApprovalRequests": (200, {"whatsapp": {"status": "approved"}})})
    WTS.sync(biz, acc["id"], http=http2)
    with db.tx() as conn:
        out = WTS.for_sending(conn, biz["id"], acc["id"], "recordatorio", "es_MX", "Sofía")
    assert out["name"] == "HX123" and out["params"] == ["Sofía"]


def test_quality_and_tier_from_meta():
    biz, _ = make_business("MX", "contractor", name="Taller Q")
    acc = account(biz, "whatsapp", WA_CFG)
    _, http = mock({"graph.facebook.com": (200, {"quality_rating": "GREEN", "messaging_limit_tier": "TIER_1K", "verified_name": "Decora"})})
    assert WTS.quality(biz, acc["id"], http=http) == {"quality": "GREEN", "tier": "TIER_1K", "number": "", "name": "Decora"}


def test_the_page_has_the_new_fields(client):
    biz, _ = make_business("MX", "contractor", name="Taller P")
    acc = account(biz, "whatsapp_twilio", TW_CFG)
    owner_session(client, biz)
    page = client.get(f"/app/channels/{acc['id']}/templates", headers={"Accept-Language": "es-MX"}).text
    assert 'name="header"' in page and 'name="buttons"' in page and "Revisar con WhatsApp (Twilio)" in page
