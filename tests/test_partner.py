"""Partner API: a host or agency runs Valeria One from its own systems — create businesses for its customers,
pause and resume them, re-invite owners, read usage for billing, delete for good, back up and update."""
import json
import re
from datetime import datetime, timezone

import pytest

from conftest import make_business
from valeria_one import blocks, crypto, db
from valeria_one.services import Blocked, Invalid
from valeria_one.services import inbox as X
from valeria_one.services import instance as I
from valeria_one.services import partner as PT

PW = "una-clave-larga"


@pytest.fixture
def pk(client):
    with db.tx() as conn:
        key, _ = PT.new_key(conn, "Acme Hosting", "test")
    return {"Authorization": f"Bearer {key}"}


def make(client, pk, **kw):
    body = {"name": "Mama Njeri Salon", "country": "KE", "owner_email": "njeri@example.com", **kw}
    r = client.post("/v1/partner/businesses", headers=pk, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def join(client, invite_url, password=PW, name="Owner"):
    path = invite_url.replace("https://one.test", "")
    page = client.get(path)
    tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post(path, data={"csrf": tok, "mode": "new", "name": name, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]


def login(client, email, password=PW):
    client.cookies.clear()
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    return client.post("/login", data={"csrf": tok, "email": email, "password": password}, follow_redirects=False)


def test_keys_guard_everything(client, pk, monkeypatch):
    assert client.get("/v1/partner/server").status_code == 401
    assert client.get("/v1/partner/server", headers={"Authorization": "Bearer vo_partner_nope"}).status_code == 401
    biz, key = make_business("MX")
    assert client.get("/v1/partner/businesses", headers={"Authorization": f"Bearer {key}"}).status_code == 401   # a business key is not a partner key
    assert client.get("/v1/partner/server", headers=pk).status_code == 200
    with db.tx() as conn:
        k = PT.keys(conn)[0]
        assert k["last_used_at"] and k["prefix"].startswith("vo_partner_")
        assert conn.execute(db.partner_keys.select()).mappings().one()["key_hash"] == crypto.sha256(pk["Authorization"][7:])
        PT.revoke_key(conn, k["id"], "test")
    assert client.get("/v1/partner/server", headers=pk).status_code == 401
    monkeypatch.setenv("VO_SANDBOX", "1")
    assert client.get("/v1/partner/server", headers=pk).status_code == 404       # never on the public sandbox


def test_create_invite_and_own(client, pk):
    out = make(client, pk, partner_ref="cust-1042", industry="salon", blocks=["calendar", "payments"])
    b = out["business"]
    assert b["currency"] == "KES" and b["partner_ref"] == "cust-1042" and b["people"] == 0
    assert b["blocks"]["calendar"] and not b["blocks"]["pipeline"]
    join(client, out["invite_url"])
    assert client.get("/v1/partner/businesses/" + b["id"], headers=pk).json()["people"] == 1
    with db.tx() as conn:
        m = conn.execute(db.memberships.select()).mappings().one()
    assert m["role"] == "owner"
    again = client.post("/v1/partner/businesses", headers=pk, json={"name": "Dup", "country": "KE", "owner_email": "x@example.com",
                                                                   "partner_ref": "cust-1042"})
    assert again.status_code == 422 and again.json()["error"]["field"] == "partner_ref"
    bad = client.post("/v1/partner/businesses", headers=pk, json={"name": "X", "country": "ZZ", "owner_email": "x@example.com"})
    assert bad.status_code == 422
    lst = client.get("/v1/partner/businesses?partner_ref=cust-1042", headers=pk).json()
    assert [x["id"] for x in lst] == [b["id"]]
    re_inv = client.post(f"/v1/partner/businesses/{b['id']}/invite", headers=pk, json={"email": "second@example.com"})
    assert re_inv.status_code == 201 and "/join/" in re_inv.json()["invite_url"]
    with db.tx() as conn:
        assert {e["type"] for e in PT.audit_log(conn)} >= {"partner_key.created", "business.created"}


def test_sandbox_businesses_are_not_partner_businesses(client, pk):
    sb, _ = make_business("PH", sandbox=True)
    assert client.get(f"/v1/partner/businesses/{sb['id']}", headers=pk).status_code == 404
    assert all(x["id"] != str(sb["id"]) for x in client.get("/v1/partner/businesses", headers=pk).json())


def test_pause_stops_everything_but_keeps_incoming_messages(client, pk):
    out = make(client, pk)
    bid = out["business"]["id"]
    join(client, out["invite_url"])
    assert client.get("/app").status_code == 200
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == bid)).mappings().one())
        key, _ = __import__("valeria_one.services.businesses", fromlist=["x"]).new_key(conn, biz["id"])
        acc = X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/out", "secret": "s3cret"})
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/contacts", headers=h).status_code == 200
    r = client.post(f"/v1/partner/businesses/{bid}/suspend", headers=pk, json={"reason": "invoice 1042 unpaid"})
    assert r.status_code == 200 and r.json()["suspended"] and r.json()["suspended_reason"] == "invoice 1042 unpaid"
    assert all(v is False for v in {b: blocks.on(dict(biz, suspended_at=datetime.now(timezone.utc)), b) for b in blocks.BLOCKS}.values())
    # the people who were signed in are out; signing in again says why
    assert client.get("/app", follow_redirects=False).headers["location"] == "/login"
    assert login(client, "njeri@example.com").headers["location"] == "/login"
    assert "paused by its provider" in client.get("/login").text
    assert client.get("/v1/contacts", headers=h).status_code == 403
    # a customer still writes: kept, nothing goes out
    body = json.dumps({"from": "c-77", "name": "Wanjiru", "body": "Are you open?"}).encode()
    hook = acc["webhook_url"].replace("https://one.test", "")
    r = client.post(hook, content=body, headers={"X-VO-Signature": crypto.hmac_sha256_hex("s3cret", body),
                                                 "Content-Type": "application/json"})
    assert r.status_code == 200
    with db.tx() as conn:
        assert conn.execute(db.messages.select().where(db.messages.c.direction == "in")).mappings().one()["body"] == "Are you open?"
        cid = conn.execute(db.contacts.select()).mappings().one()["id"]
    with pytest.raises(Blocked):
        X.send(biz, "t", contact_id=cid, body="hello")
    # resume: back to normal
    assert not client.post(f"/v1/partner/businesses/{bid}/resume", headers=pk).json()["suspended"]
    assert login(client, "njeri@example.com").headers["location"] == "/app"
    assert client.get("/v1/contacts", headers=h).status_code == 200
    with db.tx() as conn:
        types = [e["type"] for e in PT.audit_log(conn)]
        ev = [r["type"] for r in conn.execute(db.events.select().where(db.events.c.business_id == bid)).mappings()]
    assert "business.suspended" in types and "business.resumed" in types
    assert "business.suspended" in ev and "business.resumed" in ev


def test_someone_in_two_businesses_lands_on_the_open_one(client, pk):
    a = make(client, pk, name="Shop A", owner_email="both@example.com")
    b = make(client, pk, name="Shop B", owner_email="both@example.com")
    join(client, a["invite_url"])
    path = b["invite_url"].replace("https://one.test", "")
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)
    client.post(path, data={"csrf": tok, "mode": "accept"})
    client.post(f"/v1/partner/businesses/{a['business']['id']}/suspend", headers=pk, json={})
    assert login(client, "both@example.com").headers["location"] == "/app"
    assert "Shop B" in client.get("/app").text


def test_delete_for_good(client, pk):
    out = make(client, pk, name="Closing Down Ltd")
    bid = out["business"]["id"]
    join(client, out["invite_url"])
    r = client.post(f"/v1/partner/businesses/{bid}/delete", headers=pk, json={"confirm": "closing down ltd"})
    assert r.status_code == 422                                                    # must be the exact name
    r = client.post(f"/v1/partner/businesses/{bid}/delete", headers=pk, json={"confirm": "Closing Down Ltd"})
    assert r.status_code == 204
    assert client.get(f"/v1/partner/businesses/{bid}", headers=pk).status_code == 404
    with db.tx() as conn:
        assert conn.execute(db.users.select().where(db.users.c.email == "njeri@example.com")).first() is None
        e = [x for x in PT.audit_log(conn) if x["type"] == "business.deleted"][0]
    assert e["data"]["name"] == "Closing Down Ltd" and e["data"]["accounts_removed"] == 1


def test_usage_for_billing(client, pk):
    out = make(client, pk, partner_ref="cust-7")
    bid = out["business"]["id"]
    with db.tx() as conn:
        biz = dict(conn.execute(db.businesses.select().where(db.businesses.c.id == bid)).mappings().one())
        acc = X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/out", "secret": "s"})
        c = conn.execute(db.contacts.insert().values(business_id=bid, name="A").returning(db.contacts.c.id)).scalar()
        cv = conn.execute(db.conversations.insert().values(business_id=bid, contact_id=c, channel_account_id=acc["id"])
                          .returning(db.conversations.c.id)).scalar()
        for d, st, at in [("in", "received", "2026-09-30T23:59:00+00:00"), ("in", "received", "2026-10-02T10:00:00+00:00"),
                          ("out", "sent", "2026-10-02T10:01:00+00:00"), ("out", "sent", "2026-10-31T23:59:59+00:00"),
                          ("out", "sent", "2026-11-01T00:00:00+00:00")]:
            conn.execute(db.messages.insert().values(business_id=bid, conversation_id=cv, direction=d, status=st,
                                                     created_at=datetime.fromisoformat(at)))
    u = client.get("/v1/partner/usage?month=2026-10", headers=pk).json()
    row = u["businesses"][0]
    assert u["month"] == "2026-10" and row["partner_ref"] == "cust-7"
    assert (row["messages_in"], row["messages_out"], row["customers"]) == (1, 2, 1)
    assert "body" not in json.dumps(u)                                             # counts, never content
    assert client.get("/v1/partner/usage?month=2026-13", headers=pk).status_code == 422
    assert client.get(f"/v1/partner/usage?month=2026-09&business_id={bid}", headers=pk).json()["businesses"][0]["messages_in"] == 1


def test_server_status_and_requests(client, pk, tmp_path, monkeypatch):
    st = client.get("/v1/partner/server", headers=pk).json()
    assert st["version"] and "backups" in st and "jobs" in st
    assert client.post("/v1/partner/server/backup", headers=pk).status_code == 409   # nobody watching here
    (tmp_path / "ops" / "requests").mkdir(parents=True)
    (tmp_path / "ops" / "status").mkdir()
    monkeypatch.setenv("VO_OPS_DIR", str(tmp_path / "ops"))
    assert client.post("/v1/partner/server/update", headers=pk).status_code == 202
    assert json.loads((tmp_path / "ops" / "requests" / "update").read_text())["by"].startswith("partner:vo_partner_")
    assert client.post("/v1/partner/server/reboot", headers=pk).status_code == 404
    with db.tx() as conn:
        assert PT.audit_log(conn)[0]["type"] == "server.update_requested"


def test_server_page_keys_and_pause(client):
    from test_selfhost import do_setup
    do_setup(client)
    page = client.get("/app/server")
    tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/app/server-keys", data={"csrf": tok, "name": "Acme Hosting"})
    key = re.search(r"vo_partner_[\w-]+", r.text).group(0)
    assert client.get("/v1/partner/server", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert key not in client.get("/app/server").text                               # shown once
    with db.tx() as conn:
        other, _ = PT.create(conn, "test", name="Client Co", country="BR", owner_email="c@example.com")
        kid = PT.keys(conn)[0]["id"]
    client.post(f"/app/server-businesses/{other['id']}/suspend", data={"csrf": tok})
    assert "paused" in client.get("/app/server").text
    with db.tx() as conn:
        assert PT.get(conn, other["id"])["suspended"]
    client.post(f"/app/server-businesses/{other['id']}/resume", data={"csrf": tok})
    with db.tx() as conn:
        assert not PT.get(conn, other["id"])["suspended"]
    client.post(f"/app/server-keys/{kid}/revoke", data={"csrf": tok})
    assert client.get("/v1/partner/server", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert "Partner key revoked" in client.get("/app/server").text


def test_partner_api_is_documented(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/v1/partner/businesses", "/v1/partner/usage", "/v1/partner/server"} <= set(paths)


def test_key_needs_a_name():
    with db.tx() as conn, pytest.raises(Invalid):
        PT.new_key(conn, "  ", "t")


def test_auth_errors_speak_the_same_json(client, pk):
    r = client.get("/v1/partner/server", headers={"Authorization": "Bearer vo_partner_x"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    out = make(client, pk)
    with db.tx() as conn:
        from valeria_one.services import businesses as B
        key, _ = B.new_key(conn, out["business"]["id"])
    client.post(f"/v1/partner/businesses/{out['business']['id']}/suspend", headers=pk, json={})
    r = client.get("/v1/contacts", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "suspended"
