import json
import re

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.crypto import hmac_sha256_hex
from valeria_one.services import Invalid
from valeria_one.services import meta as MT

APP_SECRET = "PLATFORM-APP-SECRET"


@pytest.fixture
def meta_on(monkeypatch):
    monkeypatch.setenv("VO_META_APP_ID", "1234567890")
    monkeypatch.setenv("VO_META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("VO_META_VERIFY_TOKEN", "PLATFORM-VT")
    monkeypatch.setenv("VO_META_WA_CONFIG_ID", "WA-CONFIG")
    monkeypatch.setenv("VO_META_LOGIN_CONFIG_ID", "LOGIN-CONFIG")


class FakeGraph:
    """Answers the Graph API calls the connection makes, and records them."""

    def __init__(self, pages=None, lead=None):
        self.calls = []
        self.pages = pages or []
        self.lead = lead

    def __call__(self, req: httpx.Request):
        p, q = req.url.path, dict(req.url.params)
        self.calls.append((req.method, p, q))
        if p.endswith("/oauth/access_token"):
            if q.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "LONG-USER-TOKEN"})
            assert q["client_secret"] == APP_SECRET and q["client_id"] == "1234567890"
            return httpx.Response(200, json={"access_token": f"TOKEN-FOR-{q['code']}"})
        if p.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        if p.endswith("/register"):
            return httpx.Response(200, json={"success": True})
        if p.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": self.pages})
        if re.search(r"/\d+$", p) and q.get("fields") == "display_phone_number,verified_name":
            return httpx.Response(200, json={"display_phone_number": "+52 999 111 2222", "verified_name": "Salón Lupita"})
        if self.lead and p.endswith("/" + self.lead["id"]):
            return httpx.Response(200, json=self.lead)
        return httpx.Response(404, json={"error": {"message": f"unexpected {p}"}})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def _signed(client, payload, secret=APP_SECRET):
    raw = json.dumps(payload).encode()
    return client.post("/hooks/meta", content=raw, headers={"Content-Type": "application/json",
                                                            "X-Hub-Signature-256": "sha256=" + hmac_sha256_hex(secret, raw)})


def _wa_msg(pnid, frm, mid, body):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": pnid}, "contacts": [{"wa_id": frm, "profile": {"name": "Cliente"}}],
        "messages": [{"from": frm, "id": mid, "timestamp": "1761300000", "type": "text", "text": {"body": body}}]}}]}]}


def test_off_until_configured(client):
    biz, key = make_business("MX")
    assert MT.configured() is False
    assert _signed(client, _wa_msg("1", "5219991234567", "w1", "hola")).status_code == 403
    with db.tx() as conn, pytest.raises(Invalid):
        MT.connect_whatsapp(conn, biz, "t", code="c", waba_id="1", phone_number_id="2")


def test_handshake(client, meta_on):
    ok = client.get("/hooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "PLATFORM-VT", "hub.challenge": "77"})
    assert ok.status_code == 200 and ok.text == "77"
    assert client.get("/hooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "77"}).status_code == 403


def test_whatsapp_embedded_signup_and_routing(client, meta_on):
    a, ka = make_business("MX", "salon", name="Salón A")
    b, kb = make_business("CO", "shop", name="Tienda B")
    g = FakeGraph()
    with db.tx() as conn:
        acc = MT.connect_whatsapp(conn, a, "t", code="CODE-A", waba_id="111", phone_number_id="2001", http=g.client())
        MT.connect_whatsapp(conn, b, "t", code="CODE-B", waba_id="222", phone_number_id="2002", http=g.client())
    assert acc["channel"] == "whatsapp" and "Salón Lupita" in acc["name"] and acc["config"]["token"] == "••••"
    paths = [c[1] for c in g.calls]
    assert any(p.endswith("/111/subscribed_apps") for p in paths) and any(p.endswith("/2001/register") for p in paths)
    with db.tx() as conn:
        raw = conn.execute(db.channel_accounts.select()).mappings().all()
    assert all("TOKEN-FOR" not in json.dumps(r["config"]) for r in raw)          # tokens encrypted at rest
    # each business gets only its own messages
    assert _signed(client, _wa_msg("2001", "5219991234567", "wA", "para A")).status_code == 200
    assert _signed(client, _wa_msg("2002", "573001234567", "wB", "para B")).status_code == 200
    assert _signed(client, _wa_msg("9999", "573001234567", "wX", "nadie")).status_code == 200   # unknown number: ignored
    ha, hb = {"Authorization": f"Bearer {ka}"}, {"Authorization": f"Bearer {kb}"}
    ca = client.get("/v1/contacts", headers=ha).json()
    cb = client.get("/v1/contacts", headers=hb).json()
    assert [c["phone"] for c in ca] == ["+529991234567"] and [c["phone"] for c in cb] == ["+573001234567"]
    assert _signed(client, _wa_msg("2001", "5219991234567", "wZ", "x"), secret="WRONG").status_code == 403


def test_number_can_belong_to_one_business_only(meta_on):
    a, _ = make_business("MX")
    b, _ = make_business("MX")
    g = FakeGraph()
    with db.tx() as conn:
        MT.connect_whatsapp(conn, a, "t", code="C1", waba_id="1", phone_number_id="3001", http=g.client())
    with db.tx() as conn, pytest.raises(Invalid):
        MT.connect_whatsapp(conn, b, "t", code="C2", waba_id="1", phone_number_id="3001", http=g.client())


def test_sandbox_business_cannot_connect_meta(meta_on):
    biz, _ = make_business("MX", sandbox=True)
    with db.tx() as conn, pytest.raises(Invalid):
        MT.connect_whatsapp(conn, biz, "t", code="c", waba_id="1", phone_number_id="2", http=FakeGraph().client())


PAGES = [{"id": "501", "name": "Salón Lupita", "access_token": "PAGE-TOKEN-501",
          "instagram_business_account": {"id": "17841400000000001", "username": "salonlupita"}},
         {"id": "502", "name": "Otra página", "access_token": "PAGE-TOKEN-502"}]


def _web_signup(client):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Salón Lupita", "country": "MX", "industry": "salon",
                                 "email": "l@example.com", "password": "una-clave-larga"})
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/app/channels").text).group(1)


def test_pages_flow_from_the_screen(client, meta_on, monkeypatch):
    g = FakeGraph(pages=PAGES)
    monkeypatch.setattr(MT, "_http", lambda http=None: http or g.client())
    tok = _web_signup(client)
    page = client.get("/app/channels").text
    assert 'data-meta="wa"' in page and 'data-meta="pages"' in page and "PLATFORM-APP-SECRET" not in page
    pick = client.post("/app/channels/meta/pages", data={"csrf": tok, "code": "LOGIN-CODE"})
    assert "Salón Lupita" in pick.text and "@salonlupita" in pick.text and "PAGE-TOKEN" not in pick.text
    assert "PAGE-TOKEN" not in str(client.cookies.get("vo_session"))                        # encrypted, not readable
    client.post("/app/channels/meta/pages/connect", data={"csrf": tok, "page": "501"})
    chans = client.get("/app/channels").text
    assert "Salón Lupita" in chans and "@salonlupita" in chans and "Otra página" not in chans
    assert any(c[1].endswith("/501/subscribed_apps") and "leadgen" in c[2]["subscribed_fields"] for c in g.calls)
    # the encrypted page list is single-use
    again = client.post("/app/channels/meta/pages/connect", data={"csrf": tok, "page": "501"}, follow_redirects=True)
    assert "expired" in again.text or "caducó" in again.text


def test_messenger_instagram_and_lead_ads_via_platform_hook(client, meta_on, monkeypatch):
    biz, key = make_business("MX", "salon")
    lead = {"id": "900100", "created_time": "2026-10-24T15:00:00+0000", "form_id": "F1", "ad_id": "AD1",
            "field_data": [{"name": "full_name", "values": ["Daniela Ruiz"]}, {"name": "email", "values": ["dani@example.com"]},
                           {"name": "phone_number", "values": ["999 765 4321"]}, {"name": "servicio", "values": ["uñas"]}]}
    g = FakeGraph(pages=PAGES, lead=lead)
    monkeypatch.setattr(MT, "_http", lambda http=None: http or g.client())
    with db.tx() as conn:
        _, pages = MT.list_pages("CODE")
        MT.connect_pages(conn, biz, "t", pages, ["501"])
    h = {"Authorization": f"Bearer {key}"}
    _signed(client, {"object": "page", "entry": [{"id": "501", "time": 1, "messaging": [
        {"sender": {"id": "PSID1"}, "recipient": {"id": "501"}, "timestamp": 1761300000000, "message": {"mid": "m1", "text": "precio?"}}]}]})
    _signed(client, {"object": "instagram", "entry": [{"id": "17841400000000001", "time": 1, "messaging": [
        {"sender": {"id": "IGS1"}, "recipient": {"id": "17841400000000001"}, "timestamp": 1761300000000,
         "message": {"mid": "i1", "text": "info"}}]}]})
    leadgen = {"object": "page", "entry": [{"id": "501", "time": 1, "changes": [{"field": "leadgen", "value": {
        "leadgen_id": "900100", "page_id": "501", "form_id": "F1", "ad_id": "AD1", "created_time": 1761300000}}]}]}
    _signed(client, leadgen)
    _signed(client, leadgen)                                             # Meta retries
    chans = sorted(c["channel"] for c in client.get("/v1/conversations", headers=h).json())
    assert chans == ["instagram", "messenger"]
    leads = client.get("/v1/leads", headers=h).json()
    assert len(leads) == 1 and leads[0]["contact_name"] == "Daniela Ruiz"
    c = client.get(f"/v1/contacts/{leads[0]['contact_id']}", headers=h).json()
    assert c["phone"] == "+529997654321" and c["email"] == "dani@example.com" and "lead-ad" in c["tags"]
    assert "servicio: uñas" in c["notes"] and c["source"] == "lead_ad"


def test_lead_matches_existing_customer(client, meta_on, monkeypatch):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    existing = client.post("/v1/contacts", headers=h, json={"name": "Dani", "phone": "9997654321"}).json()
    lead = {"id": "900200", "field_data": [{"name": "full_name", "values": ["Daniela"]}, {"name": "phone_number", "values": ["+52 999 765 4321"]}]}
    g = FakeGraph(pages=PAGES, lead=lead)
    monkeypatch.setattr(MT, "_http", lambda http=None: http or g.client())
    with db.tx() as conn:
        _, pages = MT.list_pages("CODE")
        MT.connect_pages(conn, biz, "t", pages, ["502"])
    PAGES_502 = {"object": "page", "entry": [{"id": "502", "changes": [{"field": "leadgen", "value": {"leadgen_id": "900200", "page_id": "502"}}]}]}
    _signed(client, PAGES_502)
    all_c = client.get("/v1/contacts", headers=h).json()
    assert len(all_c) == 1 and all_c[0]["id"] == existing["id"] and "lead-ad" in all_c[0]["tags"]


def test_channels_screen_without_meta_explains(client):
    tok = _web_signup(client)
    assert tok
    page = client.get("/app/channels").text
    assert 'data-meta="wa"' not in page and ("not set up" in page or "aún no está configurada" in page)
