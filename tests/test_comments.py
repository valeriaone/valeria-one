import json
import re
import time

import httpx
import pytest

from conftest import make_business
from test_meta import APP_SECRET, PAGES, FakeGraph, _signed, meta_on  # noqa: F401  (fixture re-used)
from valeria_one import db
from valeria_one.services import comments as CM
from valeria_one.services import meta as MT


def test_fold_and_match():
    assert CM.fold("¿CUÁNTO cuesta?") == "¿cuanto cuesta?"
    rules = [{"active": True, "channels": ["messenger"], "keywords": ["precio", "cuanto"], "id": 1},
             {"active": True, "channels": ["instagram"], "keywords": [], "id": 2}]
    assert CM.match(rules, "Cuánto?", "messenger")["id"] == 1
    assert CM.match(rules, "lindo!", "messenger") is None
    assert CM.match(rules, "lindo!", "instagram")["id"] == 2          # empty keywords = any comment
    assert CM.match([{**rules[0], "active": False}], "precio", "messenger") is None


def test_rule_validation(client):
    biz, key = make_business("MX")
    h = {"Authorization": f"Bearer {key}"}
    assert client.post("/v1/comment-rules", headers=h, json={"keywords": ["precio"], "reply": ""}).status_code == 422
    r = client.post("/v1/comment-rules", headers=h, json={"keywords": ["Precio", " CUÁNTO "], "reply": "Hola {name}"})
    assert r.status_code == 201 and r.json()["keywords"] == ["cuanto", "precio"]
    assert client.post("/v1/comment-rules", headers=h, json={"keywords": [], "reply": "x", "channels": ["tiktok"]}).status_code == 422
    rid = r.json()["id"]
    assert client.delete(f"/v1/comment-rules/{rid}", headers=h).status_code == 204
    assert client.get("/v1/comment-rules", headers=h).json() == []


@pytest.fixture
def connected(client, meta_on, monkeypatch):  # noqa: F811
    biz, key = make_business("MX", "salon")
    g = FakeGraph(pages=PAGES)
    sent = []

    def handler(req):
        if req.url.path.endswith("/me/messages"):
            sent.append({"token": req.url.params["access_token"], **json.loads(req.content)})
            return httpx.Response(200, json={"recipient_id": "x", "message_id": "m"})
        return g(req)
    client_http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(MT, "_http", lambda http=None: http or client_http)
    with db.tx() as conn:
        _, pages = MT.list_pages("CODE")
        MT.connect_pages(conn, biz, "t", pages, ["501"])
        CM.create_rule(conn, biz, "t", keywords=["precio", "cuanto"], reply="¡Hola {name}! Te mando precios por aquí.")
    return biz, {"Authorization": f"Bearer {key}"}, sent


def _fb_comment(cid, text, frm=("777", "Rosa María"), age_s=60, verb="add"):
    return {"object": "page", "entry": [{"id": "501", "time": 1, "changes": [{"field": "feed", "value": {
        "item": "comment", "verb": verb, "comment_id": cid, "post_id": "501_99", "message": text,
        "from": {"id": frm[0], "name": frm[1]}, "created_time": int(time.time()) - age_s}}]}]}


def test_facebook_comment_gets_one_private_reply(client, connected):
    biz, h, sent = connected
    assert _signed(client, _fb_comment("c1", "¿Cuánto cuesta el gelish?")).status_code == 200
    _signed(client, _fb_comment("c1", "¿Cuánto cuesta el gelish?"))                  # Meta retries: still one reply
    assert len(sent) == 1
    assert sent[0]["recipient"] == {"comment_id": "c1"} and sent[0]["token"] == "PAGE-TOKEN-501"
    assert sent[0]["message"]["text"] == "¡Hola Rosa! Te mando precios por aquí."
    rows = client.get("/v1/comments", headers=h).json()
    assert len(rows) == 1 and rows[0]["replied_at"] and rows[0]["author_name"] == "Rosa María"


def test_no_rule_own_comment_edits_and_old_comments(client, connected):
    biz, h, sent = connected
    _signed(client, _fb_comment("c2", "¡Qué bonito!"))                                 # stored, no rule
    _signed(client, _fb_comment("c3", "precio en DM", frm=("501", "Salón Lupita")))      # the page itself
    _signed(client, _fb_comment("c4", "precio?", verb="edited"))                        # not a new comment
    _signed(client, _fb_comment("c5", "precio?", age_s=8 * 86400))                       # Meta's 7-day limit
    assert sent == []
    rows = {r["comment_id"]: r for r in client.get("/v1/comments", headers=h).json()}
    assert set(rows) == {"c2", "c5"}
    assert rows["c2"]["replied_at"] is None and rows["c2"]["reply_error"] is None
    assert "7 days" in rows["c5"]["reply_error"]


def test_instagram_comment(client, connected):
    biz, h, sent = connected
    _signed(client, {"object": "instagram", "entry": [{"id": "17841400000000001", "time": 1, "changes": [{"field": "comments", "value": {
        "id": "ig_c1", "text": "PRECIO por favor", "from": {"id": "IGU9", "username": "ana.mx"}, "media": {"id": "M1"}}}]}]})
    assert len(sent) == 1 and sent[0]["recipient"] == {"comment_id": "ig_c1"} and "Hola ana.mx" in sent[0]["message"]["text"]


def test_meta_refusal_is_recorded(client, meta_on, monkeypatch):  # noqa: F811
    biz, key = make_business("MX")
    g = FakeGraph(pages=PAGES)

    def handler(req):
        if req.url.path.endswith("/me/messages"):
            return httpx.Response(400, json={"error": {"message": "(#10703) Can't reply privately"}})
        return g(req)
    hc = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(MT, "_http", lambda http=None: http or hc)
    with db.tx() as conn:
        _, pages = MT.list_pages("CODE")
        MT.connect_pages(conn, biz, "t", pages, ["501"])
        CM.create_rule(conn, biz, "t", keywords=[], reply="Hola")
    _signed(client, _fb_comment("c9", "hola"))
    row = client.get("/v1/comments", headers={"Authorization": f"Bearer {key}"}).json()[0]
    assert row["replied_at"] is None and "10703" in row["reply_error"]


def test_sandbox_comment_lands_in_inbox(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Tiendita", "country": "PH", "industry": "shop"})
    h = {"Authorization": f"Bearer {r.json()['api_key']}"}
    client.post("/v1/comment-rules", headers=h, json={"keywords": ["magkano", "price"], "reply": "Hi {name}! Sent you our prices."})
    res = client.post("/v1/sandbox/simulate-comment", headers=h, json={"name": "Maria Santos", "text": "Magkano po?"}).json()
    assert res["result"] == "replied"
    conv = client.get("/v1/conversations", headers=h).json()[0]
    msgs = client.get(f"/v1/conversations/{conv['id']}", headers=h).json()["messages"]
    assert [m["body"] for m in msgs] == ["Hi Maria! Sent you our prices."]


def test_comments_screen(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    demo = seed.seed()
    client.get(f"/demo/{demo[0]['business_id']}")                       # Salón Lupita (es)
    page = client.get("/app/comments").text
    assert "precio" in page and "Comentarios" in page
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/app/comments/simulate", data={"csrf": tok, "name": "Rosa", "text": "¿Cuánto la keratina?"}, follow_redirects=True)
    assert "Contestado en privado" in r.text
    client.post("/app/comments/rules", data={"csrf": tok, "keywords": "cita", "reply": "Agenda aquí", "channels": ["messenger"]})
    assert "Agenda aquí" in client.get("/app/comments").text
    inbox = client.get("/app/inbox").text
    assert "Rosa" in inbox
