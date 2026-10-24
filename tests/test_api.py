from datetime import datetime, timedelta, timezone




def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_packs_are_public(client):
    r = client.get("/v1/packs/countries")
    assert r.status_code == 200 and {"MX", "US", "PH", "BR", "JP"} <= {p["code"] for p in r.json()}
    assert client.get("/v1/packs/industries/salon").json()["code"] == "salon"
    assert client.get("/v1/packs/countries/XX").status_code == 404


def test_api_key_required(client):
    assert client.get("/v1/business").status_code == 401
    assert client.get("/v1/business", headers={"Authorization": "Bearer vo_live_nope"}).status_code == 401


def test_business_shows_its_packs(client, mx):
    biz, h = mx
    b = client.get("/v1/business", headers=h).json()
    assert b["country"] == "MX" and b["currency"] == "MXN" and b["language"] == "es"
    assert b["country_pack"]["tax"]["rate_bp"] == 1600 and b["industry_pack"]["code"] == "salon"


def test_contact_phone_uses_business_country(client, mx, us):
    _, hmx = mx
    _, hus = us
    a = client.post("/v1/contacts", headers=hmx, json={"name": "Ana", "phone": "999 123 4567"}).json()
    b = client.post("/v1/contacts", headers=hus, json={"name": "Bob", "phone": "(951) 434-3352"}).json()
    assert a["phone"] == "+529991234567" and b["phone"] == "+19514343352"
    full = client.get(f"/v1/contacts/{a['id']}", headers=hmx).json()
    assert {"channel": "whatsapp", "address": "+529991234567"} in full["identities"]


def test_contact_validation(client, mx):
    _, h = mx
    assert client.post("/v1/contacts", headers=h, json={}).status_code == 422
    r = client.post("/v1/contacts", headers=h, json={"name": "X", "phone": "123"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "phone"
    r = client.post("/v1/contacts", headers=h, json={"name": "X", "marketing_consent": True})
    assert r.status_code == 422 and r.json()["error"]["field"] == "marketing_consent_source"


def test_one_business_never_sees_another(client, mx, us):
    _, hmx = mx
    _, hus = us
    c = client.post("/v1/contacts", headers=hmx, json={"name": "Secret"}).json()
    assert client.get(f"/v1/contacts/{c['id']}", headers=hus).status_code == 404
    assert client.patch(f"/v1/contacts/{c['id']}", headers=hus, json={"name": "x"}).status_code == 404
    assert client.delete(f"/v1/contacts/{c['id']}", headers=hus).status_code == 404
    assert all(x["name"] != "Secret" for x in client.get("/v1/contacts", headers=hus).json())
    d = client.post("/v1/deals", headers=hus, json={"title": "steal", "contact_id": c["id"]})
    assert d.status_code == 404


def test_deals_follow_the_industry(client, mx):
    _, h = mx                                  # salon: new, booked, visited, won, lost
    c = client.post("/v1/contacts", headers=h, json={"name": "Ana"}).json()
    d = client.post("/v1/deals", headers=h, json={"title": "Bridal package", "contact_id": c["id"], "amount_minor": 350000}).json()
    assert d["stage"] == "new" and d["currency"] == "MXN" and d["closed_at"] is None
    r = client.patch(f"/v1/deals/{d['id']}", headers=h, json={"stage": "offer"})       # realtor stage, not salon
    assert r.status_code == 422 and r.json()["error"]["field"] == "stage"
    won = client.patch(f"/v1/deals/{d['id']}", headers=h, json={"stage": "won"}).json()
    assert won["closed_at"] is not None
    pipe = client.get("/v1/pipeline", headers=h).json()
    assert [s["key"] for s in pipe] == ["new", "booked", "visited", "won", "lost"]
    assert next(s for s in pipe if s["key"] == "won") == {"key": "won", "label": "Clienta frecuente", "outcome": "won",
                                                          "count": 1, "amount_minor": 350000}


def test_amount_must_be_whole_minor_units(client, mx):
    _, h = mx
    assert client.post("/v1/deals", headers=h, json={"title": "x", "amount_minor": 12.5}).status_code == 422
    assert client.post("/v1/deals", headers=h, json={"title": "x", "amount_minor": -1}).status_code == 422


def test_appointments_no_double_booking(client, mx):
    _, h = mx
    t = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)
    a = {"title": "Corte", "starts_at": t.isoformat(), "ends_at": (t + timedelta(hours=1)).isoformat()}
    assert client.post("/v1/appointments", headers=h, json=a).status_code == 201
    b = {**a, "starts_at": (t + timedelta(minutes=30)).isoformat(), "ends_at": (t + timedelta(minutes=90)).isoformat()}
    assert client.post("/v1/appointments", headers=h, json=b).status_code == 422
    assert client.post("/v1/appointments", headers=h, json={**b, "allow_overlap": True}).status_code == 201
    bad = {**a, "ends_at": a["starts_at"]}
    assert client.post("/v1/appointments", headers=h, json=bad).status_code == 422


def test_channel_connect_needs_config_and_hides_secrets(client, mx):
    _, h = mx
    r = client.post("/v1/channels", headers=h, json={"channel": "whatsapp", "name": "WA"})
    assert r.status_code == 422 and "phone_number_id" in r.json()["error"]["message"]
    r = client.post("/v1/channels", headers=h, json={"channel": "whatsapp", "name": "WA", "config": {
        "phone_number_id": "123", "token": "SECRET-TOKEN", "app_secret": "APPSECRET", "verify_token": "vt"}})
    assert r.status_code == 201
    j = r.json()
    assert j["config"]["token"] == "••••" and "SECRET" not in r.text
    assert j["webhook_url"].startswith("https://one.test/hooks/")
    assert client.post("/v1/channels", headers=h, json={"channel": "pigeon"}).status_code == 422


def test_erase_contact(client, mx):
    _, h = mx
    c = client.post("/v1/contacts", headers=h, json={"name": "Ana", "phone": "9991234567", "email": "a@b.co"}).json()
    assert client.delete(f"/v1/contacts/{c['id']}", headers=h).status_code == 204
    assert client.get(f"/v1/contacts/{c['id']}", headers=h).status_code == 404


def test_events_trail(client, mx):
    _, h = mx
    client.post("/v1/contacts", headers=h, json={"name": "Ana"})
    ev = client.get("/v1/events", headers=h).json()
    assert [e["type"] for e in ev][:2] == ["business.created", "api_key.created"]
    assert ev[-1]["type"] == "contact.created" and ev[-1]["actor"].startswith("api_key:vo_live_")


def test_sandbox_is_off_on_a_real_server(client):
    assert client.post("/v1/sandbox/signup", json={"name": "x"}).status_code == 404


def test_sandbox_flow(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Salón Lupita", "country": "MX", "industry": "salon"})
    assert r.status_code == 201
    key = r.json()["api_key"]
    assert key.startswith("vo_test_")
    h = {"Authorization": f"Bearer {key}"}
    s = client.post("/v1/sandbox/simulate", headers=h, json={"from": "cust-1", "name": "Lupe", "body": "¿Tienen cita mañana?"})
    assert s.json()["messages"] == 1 and s.json()["new_contacts"] == 1
    conv = client.get("/v1/conversations", headers=h).json()[0]
    sent = client.post("/v1/messages", headers=h, json={"contact_id": str(conv["contact_id"]), "channel": "sandbox",
                                                        "body": "¡Sí! ¿A las 10?"})
    assert sent.status_code == 201 and sent.json()["status"] == "delivered"
    full = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()
    assert [m["direction"] for m in full["messages"]] == ["in", "out"]
    # a sandbox business can never connect a real channel
    r = client.post("/v1/channels", headers=h, json={"channel": "webhook", "config": {"url": "https://x", "secret": "s"}})
    assert r.status_code == 422


def test_sandbox_simulate_needs_sandbox_business(client, mx):
    _, h = mx
    assert client.post("/v1/sandbox/simulate", headers=h, json={"from": "x", "body": "hi"}).status_code == 404


def test_sandbox_key_cannot_touch_live_business(client, mx, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    biz, h = mx
    c = client.post("/v1/contacts", headers=h, json={"name": "Real"}).json()
    sk = client.post("/v1/sandbox/signup", json={"name": "Play"}).json()["api_key"]
    assert client.get(f"/v1/contacts/{c['id']}", headers={"Authorization": f"Bearer {sk}"}).status_code == 404


def test_sandbox_seed_builds_the_world(monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    demo = seed.seed()
    assert {d["country"] for d in demo} >= {"MX", "US", "PH", "BR", "JP", "GB", "NG", "CO", "DE"}
    assert all(d["api_key"].startswith("vo_test_") for d in demo)
    assert seed.wipe() == len(demo)


def test_seed_refuses_on_a_real_server(monkeypatch):
    import pytest
    monkeypatch.setenv("VO_SANDBOX", "0")
    from valeria_one.sandbox import seed
    with pytest.raises(SystemExit):
        seed.seed()
