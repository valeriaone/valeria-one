import json

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import ai as AIS
from valeria_one.services import booking as BK


class FakeAI:
    """Answers like Gemini / Claude / OpenAI, and records the prompts."""

    def __init__(self, answer="¡Hola! El corte cuesta $350. Agenda aquí."):
        self.answer, self.calls = answer, []

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content)
        self.calls.append((req.url.host, dict(req.headers), body))
        if req.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": self.answer}]}}]})
        if req.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"content": [{"type": "text", "text": self.answer}]})
        if req.url.host == "api.openai.com":
            return httpx.Response(200, json={"choices": [{"message": {"content": self.answer}}]})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = FakeAI()
    c = httpx.Client(transport=httpx.MockTransport(f))
    monkeypatch.setattr(AIS, "_http", lambda http=None: http or c)
    return f


def sandbox_biz(client, monkeypatch, industry="salon"):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Salón Prueba", "country": "MX", "industry": industry})
    biz_id = r.json()["business"]["id"]
    return biz_id, {"Authorization": f"Bearer {r.json()['api_key']}"}


def test_settings_rules(client):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    r = client.patch("/v1/ai", headers=h, json={"provider": "gemini", "mode": "auto"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "api_key"
    r = client.patch("/v1/ai", headers=h, json={"provider": "gemini", "api_key": "AIza-SECRET-KEY", "mode": "suggest"})
    assert r.status_code == 200 and r.json()["has_key"] and "AIza" not in r.text
    with db.tx() as conn:
        assert "AIza" not in AIS.settings(conn, biz["id"])["api_key"]               # encrypted at rest
    r = client.patch("/v1/ai", headers=h, json={"provider": "openai", "mode": "suggest"})
    assert r.status_code == 422                                                       # the Gemini key isn't an OpenAI key
    with db.tx() as conn, pytest.raises(Invalid):
        AIS.save(conn, biz, "t", provider="demo", mode="auto")                       # demo AI: sandbox only


@pytest.mark.parametrize("provider,host", [("gemini", "generativelanguage.googleapis.com"),
                                           ("anthropic", "api.anthropic.com"), ("openai", "api.openai.com")])
def test_suggest_each_provider_knows_the_business(client, fake, provider, host):
    biz, key = make_business("MX", "salon")
    h = {"Authorization": f"Bearer {key}"}
    with db.tx() as conn:
        BK.save_settings(conn, biz, "t", enabled=True, slug="lupita", hours={"mon": [["09:00", "18:00"]]})
        BK.add_service(conn, biz, "t", name="Corte", minutes=60, price_minor=35000)
    client.patch("/v1/ai", headers=h, json={"provider": provider, "api_key": "KEY-123", "mode": "suggest",
                                            "instructions": "Aceptamos tarjeta."})
    client.post("/v1/channels", headers=h, json={"channel": "webhook", "name": "w", "config": {"url": "https://x.test", "secret": "s"}})
    c = client.post("/v1/contacts", headers=h, json={"name": "Ana"}).json()
    with db.tx() as conn:
        from valeria_one.services import inbox as X
        acc = X._account(conn, biz["id"], None, "webhook")
        conv = X._conversation(conn, biz["id"], acc["id"], c["id"])
        conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=conv["id"], direction="in",
                                                 body="¿Cuánto cuesta el corte?", status="received"))
    r = client.post(f"/v1/conversations/{conv['id']}/suggest", headers=h)
    assert r.status_code == 200 and r.json() == {"text": "¡Hola! El corte cuesta $350. Agenda aquí.", "needs_human": False}
    sent_host, headers, body = fake.calls[-1]
    assert sent_host == host
    prompt = json.dumps(body, ensure_ascii=False)
    assert "Corte, 60 min, $350.00" in prompt and "/b/lupita" in prompt and "Aceptamos tarjeta." in prompt
    assert "¿Cuánto cuesta el corte?" in prompt and "[[HUMAN]]" in prompt
    if provider == "gemini":
        assert headers["x-goog-api-key"] == "KEY-123"
    elif provider == "anthropic":
        assert headers["x-api-key"] == "KEY-123" and body["model"] == "claude-haiku-5-5"
    else:
        assert headers["authorization"] == "Bearer KEY-123"


def test_auto_reply_and_hand_off(client, fake, monkeypatch):
    monkeypatch.setenv("VO_DEMO_AI_PROVIDER", "gemini")
    monkeypatch.setenv("VO_DEMO_AI_KEY", "DEMO-KEY")
    bid, h = sandbox_biz(client, monkeypatch)
    client.patch("/v1/ai", headers=h, json={"mode": "auto"})                         # sandbox defaults to the demo AI
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == bid)).mappings().one())
        AIS.save(conn, biz, "t", provider="demo", mode="auto")
    r = client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Ana", "body": "¿Precio del corte?"})
    assert r.json()["ai"] == ["replied"]
    conv = client.get("/v1/conversations", headers=h).json()[0]
    msgs = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"]
    assert [(m["direction"], m["by_ai"]) for m in msgs] == [("in", False), ("out", True)]
    assert fake.calls[-1][1]["x-goog-api-key"] == "DEMO-KEY"
    # the AI isn't sure -> it says so, and the conversation goes to a person
    fake.answer = "Déjame pasarte con la dueña, te escribe en un momento. [[HUMAN]]"
    r = client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Ana", "body": "Quiero un descuento"})
    assert r.json()["ai"] == ["handed_off"]
    full = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()
    assert full["needs_human"] is True and full["messages"][-1]["body"].endswith("en un momento.")
    n = len(fake.calls)
    r = client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Ana", "body": "¿Hola?"})
    assert r.json()["ai"] == ["human"] and len(fake.calls) == n                      # quiet until the owner says so
    client.post(f"/v1/conversations/{conv['id']}/resume-ai", headers=h)
    fake.answer = "¡Aquí estoy!"
    assert client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "body": "¿Siguen?"}).json()["ai"] == ["replied"]
    assert client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "body": "STOP"}).json()["ai"] == ["stop"]


def test_rate_limit_hands_off(client, fake, monkeypatch):
    monkeypatch.setenv("VO_DEMO_AI_PROVIDER", "gemini")
    monkeypatch.setenv("VO_DEMO_AI_KEY", "DEMO-KEY")
    bid, h = sandbox_biz(client, monkeypatch)
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == bid)).mappings().one())
        AIS.save(conn, biz, "t", provider="demo", mode="auto")
    results = [client.post("/v1/sandbox/simulate", headers=h, json={"from": "loop", "body": f"hola {i}"}).json()["ai"][0]
               for i in range(AIS.MAX_AUTO_PER_HOUR + 1)]
    assert results[:-1] == ["replied"] * AIS.MAX_AUTO_PER_HOUR and results[-1] == "limit"


def test_provider_error_never_breaks_the_inbox(client, monkeypatch):
    monkeypatch.setenv("VO_DEMO_AI_PROVIDER", "gemini")
    monkeypatch.setenv("VO_DEMO_AI_KEY", "DEMO-KEY")
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"message": "quota"}})))
    monkeypatch.setattr(AIS, "_http", lambda http=None: http or c)
    bid, h = sandbox_biz(client, monkeypatch)
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == bid)).mappings().one())
        AIS.save(conn, biz, "t", provider="demo", mode="auto")
    r = client.post("/v1/sandbox/simulate", headers=h, json={"from": "x", "body": "hola"})
    assert r.status_code == 200 and r.json()["ai"] == ["failed"] and r.json()["messages"] == 1
    ev = [e for e in client.get("/v1/events", headers=h).json() if e["type"] == "ai.failed"]
    assert "quota" in ev[0]["data"]["error"]


def test_demo_daily_cap(client, fake, monkeypatch):
    monkeypatch.setenv("VO_DEMO_AI_PROVIDER", "gemini")
    monkeypatch.setenv("VO_DEMO_AI_KEY", "DEMO-KEY")
    monkeypatch.setenv("VO_DEMO_AI_DAILY", "1")
    bid, h = sandbox_biz(client, monkeypatch)
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == bid)).mappings().one())
        AIS.save(conn, biz, "t", provider="demo", mode="auto")
    assert client.post("/v1/sandbox/simulate", headers=h, json={"from": "a", "body": "hola"}).json()["ai"] == ["replied"]
    assert client.post("/v1/sandbox/simulate", headers=h, json={"from": "b", "body": "hola"}).json()["ai"] == ["failed"]


def test_inbox_screen_suggest_and_resume(client, fake, monkeypatch):
    import re
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Clínica Sol", "country": "MX", "industry": "clinic",
                                 "email": "sol@example.com", "password": "una-clave-larga"})
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app/settings").text).group(1)
    client.post("/app/ai", data={"csrf": tok, "provider": "gemini", "api_key": "AIza-K", "mode": "suggest", "instructions": "x"})
    assert "Llave guardada" in client.get("/app/settings").text
    client.post("/app/channels", data={"csrf": tok, "channel": "webhook", "name": "w", "cfg_url": "https://x.test", "cfg_secret": "s"})
    from valeria_one.services import inbox as X
    with db.tx() as conn:
        bid = conn.execute(db.businesses.select()).mappings().one()["id"]
        c = conn.execute(db.contacts.insert().values(business_id=bid, name="Pepe").returning(db.contacts.c.id)).scalar()
        acc = X._account(conn, bid, None, "webhook")
        conv = X._conversation(conn, bid, acc["id"], c)["id"]
        conn.execute(db.messages.insert().values(business_id=bid, conversation_id=conv, direction="in", body="¿Atienden sábado?",
                                                 status="received"))
    thread = client.post(f"/app/inbox/{conv}/suggest", data={"csrf": tok}, follow_redirects=True).text
    assert "Sugerir respuesta" in thread and "El corte cuesta $350" in thread


def test_busy_model_falls_back_to_the_next(client, monkeypatch):
    monkeypatch.setenv("VO_DEMO_AI_PROVIDER", "gemini")
    monkeypatch.setenv("VO_DEMO_AI_KEY", "DEMO-KEY")
    monkeypatch.setenv("VO_DEMO_AI_MODEL", "model-busy, model-ok")
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if "model-busy" in req.url.path:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "¡Hola!"}]}}]})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(AIS, "_http", lambda http=None: http or c)
    bid, h = sandbox_biz(client, monkeypatch)
    assert client.post("/v1/sandbox/simulate", headers=h, json={"from": "a", "body": "hola"}).json()["ai"] == ["replied"]
    assert [p.split("/")[-1] for p in seen] == ["model-busy:generateContent", "model-ok:generateContent"]


@pytest.mark.parametrize("text,lang", [("Hi! How much is a haircut?", "en"), ("Hola, ¿cuánto cuesta el corte?", "es"),
                                       ("Olá, quanto custa?", "pt"), ("Magkano po ang gupit?", "fil"),
                                       ("土曜日の夜、予約できますか？", "ja"), ("ok", None)])
def test_detect_language(text, lang):
    assert AIS.detect_language(text) == lang
