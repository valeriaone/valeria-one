"""Running your own server: first-time setup, who may sign up, the Server page (backups, updates, the
businesses it hosts), background-job health, and the blocks and words each business chooses."""
import io
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_business
from valeria_one import blocks, db, jobs, ops
from valeria_one.services import Invalid, NotFound
from valeria_one.services import businesses as B
from valeria_one.services import deals as D
from valeria_one.services import followups as FU
from valeria_one.services import instance as I

PW = "una-clave-larga"


def csrf(client, path):
    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, (path, r.status_code, r.text[:300])
    return m.group(1)


def do_setup(client, email="admin@example.com"):
    k = I.setup_token()
    tok = csrf(client, f"/setup?k={k}")
    return client.post("/setup", data={"csrf": tok, "k": k, "business_name": "First Business", "country": "KE",
                                       "industry": "generic", "name": "Amina Otieno", "email": email, "password": PW},
                       follow_redirects=False)


def biz_row(bid):
    with db.tx() as conn:
        return B.get(conn, bid)


# ---------------------------------------------------------------- first-time setup
def test_new_server_goes_to_setup_and_needs_the_link(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    locked = client.get("/setup").text
    assert "valeria-one setup-link" in locked and 'name="password"' not in locked
    assert 'name="password"' not in client.get("/setup?k=wrong").text
    assert 'name="password"' in client.get(f"/setup?k={I.setup_token()}").text


def test_setup_makes_the_admin_once(client):
    r = do_setup(client)
    assert r.status_code == 303 and r.headers["location"] == "/app/start"
    with db.tx() as conn:
        u = conn.execute(db.users.select()).mappings().one()
        assert u["is_admin"] and u["email"] == "admin@example.com"
        assert not I.needs_setup(conn)
        assert conn.execute(db.events.select().where(db.events.c.type == "server.setup")).first()
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    with db.tx() as conn, pytest.raises(Invalid):
        I.setup(conn, token=I.setup_token(), email="second@example.com", name="", password=PW, business_name="X",
                country="US", industry="generic")
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"


def test_wrong_link_creates_nothing(client):
    tok = csrf(client, f"/setup?k={I.setup_token()}")
    client.post("/setup", data={"csrf": tok, "k": "x" * 32, "business_name": "Nope", "country": "US",
                                "email": "x@example.com", "password": PW})
    with db.tx() as conn:
        assert I.needs_setup(conn)


def test_setup_is_not_on_the_sandbox(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    assert client.get(f"/setup?k={I.setup_token()}").status_code == 404


def test_setup_link_command(capsys):
    assert ops.main(["ops", "setup-link"]) == 0
    assert capsys.readouterr().out.strip() == f"https://one.test/setup?k={I.setup_token()}"
    with db.tx() as conn:
        I.setup(conn, token=I.setup_token(), email="a@example.com", name="", password=PW, business_name="B",
                country="US", industry="generic")
    assert ops.main(["ops", "setup-link"]) == 1                                    # done: no link to hand out
    assert ops.main(["ops", "make-admin", "nobody@example.com"]) == 1
    assert ops.main(["ops", "status"]) == 0 and '"version"' in capsys.readouterr().out


def test_closed_signups(client, monkeypatch):
    do_setup(client)
    client.cookies.clear()
    monkeypatch.setenv("VO_SIGNUP", "closed")
    r = client.get("/signup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = client.get("/login").text
    assert 'href="/signup"' not in page and "closed" in page.lower()
    tok = csrf(client, "/login")
    client.post("/signup", data={"csrf": tok, "business_name": "Sneaky", "country": "US", "industry": "generic",
                                 "email": "sneaky@example.com", "password": PW})
    with db.tx() as conn:
        assert conn.execute(db.users.select().where(db.users.c.email == "sneaky@example.com")).first() is None
    monkeypatch.setenv("VO_SANDBOX", "1")
    assert I.signup_open()                                                         # the public sandbox stays open


# ---------------------------------------------------------------- the Server page
@pytest.fixture
def admin(client):
    do_setup(client)
    return client


@pytest.fixture
def opsdir(tmp_path, monkeypatch):
    (tmp_path / "ops" / "requests").mkdir(parents=True)
    (tmp_path / "ops" / "status").mkdir()
    (tmp_path / "backups").mkdir()
    monkeypatch.setenv("VO_OPS_DIR", str(tmp_path / "ops"))
    monkeypatch.setenv("VO_BACKUPS_DIR", str(tmp_path / "backups"))
    return tmp_path


def test_server_page_is_only_for_the_admin(admin, client):
    page = client.get("/app/server")
    assert page.status_code == 200 and "0." in page.text and 'href="/app/server"' in page.text
    assert "managed outside the app" in page.text                                  # no host watching in this test
    client.cookies.clear()
    tok = csrf(client, "/signup")
    client.post("/signup", data={"csrf": tok, "business_name": "Other", "country": "BR", "industry": "shop",
                                 "email": "other@example.com", "password": PW})
    assert client.get("/app/server").status_code == 404
    assert 'href="/app/server"' not in client.get("/app").text
    assert client.post("/app/server/backup", data={"csrf": csrf(client, "/app")}).status_code == 404


def test_backup_and_update_requests_reach_the_host(admin, client, opsdir):
    page = client.get("/app/server").text
    assert "Back up now" in page and "Up to date" in page
    tok = csrf(client, "/app/server")
    assert client.post("/app/server/backup", data={"csrf": tok}, follow_redirects=False).status_code == 303
    req = json.loads((opsdir / "ops" / "requests" / "backup").read_text())
    assert req["op"] == "backup" and req["by"].startswith("user:")
    assert "Requested" in client.get("/app/server").text
    assert client.post("/app/server/reboot", data={"csrf": tok}).status_code == 404
    assert not (opsdir / "ops" / "requests" / "reboot").exists()
    # the host says a newer version exists -> the button shows; the host's results show too
    (opsdir / "ops" / "status" / "host.json").write_text(json.dumps({"current": "0.0.1", "latest": "v99.0.0",
                                                                     "checked_at": "2026-10-24T10:00:00+00:00"}))
    (opsdir / "ops" / "status" / "update.json").write_text(json.dumps({"state": "rolled_back", "at": "2026-10-24T10:05:00+00:00",
                                                                       "message": "v98 did not start"}))
    page = client.get("/app/server").text
    assert "Update now · v99.0.0" in page and "rolled back" in page and "v98 did not start" in page
    client.post("/app/server/update", data={"csrf": tok})
    assert (opsdir / "ops" / "requests" / "update").exists()


def test_backups_list_and_download_safely(admin, client, opsdir):
    good = opsdir / "backups" / "vo-backup-20261024T030000Z-pre-update-0.1.0.tar.gz"
    good.write_bytes(b"\x1f\x8b fake")
    (opsdir / "backups" / "notes.txt").write_text("not a backup")
    (opsdir / "secret.tar.gz").write_bytes(b"outside")
    page = client.get("/app/server").text
    assert good.name in page and "notes.txt" not in page and "pre-update-0.1.0" in page
    r = client.get(f"/app/server/backups/{good.name}")
    assert r.status_code == 200 and r.content == b"\x1f\x8b fake" and r.headers["cache-control"] == "no-store"
    for bad in ("notes.txt", "..%2Fsecret.tar.gz", "vo-backup-20261024T030000Z-..%2F..%2Fsecret.tar.gz",
                "vo-backup-20261024T040000Z.tar.gz"):
        assert client.get(f"/app/server/backups/{bad}").status_code == 404, bad
    with pytest.raises(NotFound):
        I.backup_path("../secret.tar.gz")


def test_admin_adds_a_business_for_someone_else(admin, client):
    tok = csrf(client, "/app/server")
    r = client.post("/app/server-businesses", data={"csrf": tok, "name": "Kampala Repairs", "country": "UG",
                                                    "industry": "contractor", "email": "owner@example.com"})
    link = re.search(r"https://one\.test/join/[\w-]+", r.text).group(0)
    with db.tx() as conn:
        biz = conn.execute(db.businesses.select().where(db.businesses.c.name == "Kampala Repairs")).mappings().one()
        assert biz["currency"] == "UGX"
        assert conn.execute(db.memberships.select().where(db.memberships.c.business_id == biz["id"])).first() is None
    client.cookies.clear()
    path = link.replace("https://one.test", "")
    tok = csrf(client, path)
    r = client.post(path, data={"csrf": tok, "mode": "new", "name": "Okello", "password": PW}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        m = conn.execute(db.memberships.select().where(db.memberships.c.business_id == biz["id"])).mappings().one()
        assert m["role"] == "owner"


def test_status_counts_and_jobs_heartbeat(admin, monkeypatch):
    jobs._round()
    with db.tx() as conn:
        st = I.status(conn)
    assert st["jobs"]["errors"] == {} and "followups" in st["jobs"]["ok"]
    assert st["counts"]["businesses"] == 1 and st["db_bytes"] > 0 and st["signups_open"]

    from valeria_one.services import webhooks
    def broken():
        raise RuntimeError("webhook host down")
    monkeypatch.setattr(webhooks, "deliver_due", broken)
    assert jobs.main(["jobs", "all"]) == 1                                         # failing job -> non-zero exit
    with db.tx() as conn:
        hb = I.kv_get(conn, "jobs")
    assert "webhook host down" in hb["errors"]["webhooks"] and "payments" in hb["ok"]   # the others still ran


def test_version_compare():
    assert I.version_tuple("v0.10.0") > I.version_tuple("0.9.9")
    assert I.version_tuple("main") is None and I.version_tuple("") is None


# ---------------------------------------------------------------- blocks
def owner(client, industry="generic", email="o@example.com", country="NG"):
    tok = csrf(client, "/signup")
    client.post("/signup", data={"csrf": tok, "business_name": "Owner Biz", "country": country, "industry": industry,
                                 "email": email, "password": PW})
    with db.tx() as conn:
        bid = conn.execute(db.businesses.select()).mappings().all()[-1]["id"]
    return bid


def test_start_page_and_blocks_hide_what_is_off(client):
    bid = owner(client)
    page = client.get("/app/start")
    assert page.status_code == 200 and 'value="pipeline" checked' in page.text
    today = client.get("/app").text
    assert "Getting started" in today and 'href="/app/pipeline"' in today
    tok = csrf(client, "/app/start")
    r = client.post("/app/start/blocks", data={"csrf": tok, "block": ["calendar", "payments"]}, follow_redirects=False)
    assert r.headers["location"] == "/app/start#words"
    biz = biz_row(bid)
    assert blocks.on(biz, "calendar") and not blocks.on(biz, "pipeline") and blocks.chosen(biz)
    today = client.get("/app").text
    assert 'href="/app/pipeline"' not in today and 'href="/app/followups"' not in today and 'href="/app/calendar"' in today
    r = client.get("/app/pipeline", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/start"
    assert "switched off" in client.get("/app/start").text
    settings = client.get("/app/settings").text
    assert 'action="/app/ai"' not in settings and 'action="/app/payments/connect"' in settings
    with db.tx() as conn:
        ev = conn.execute(db.events.select().where(db.events.c.type == "business.blocks")).mappings().one()
    assert "pipeline" in ev["data"]["changed"]
    with db.tx() as conn, pytest.raises(Invalid):
        B.set_blocks(conn, bid, "t", ["calendar", "teleport"])


def test_staff_cannot_change_blocks(client):
    owner(client)
    from valeria_one.services import team as TM
    with db.tx() as conn:
        biz = conn.execute(db.businesses.select()).mappings().one()
        _, token = TM.invite(conn, dict(biz), "t", "staff@example.com")
    client.cookies.clear()
    tok = csrf(client, f"/join/{token}")
    client.post(f"/join/{token}", data={"csrf": tok, "mode": "new", "name": "Staff", "password": PW})
    assert client.get("/app/start", follow_redirects=False).headers["location"] == "/app"
    client.post("/app/start/blocks", data={"csrf": csrf(client, "/app"), "block": []})
    assert not blocks.chosen(biz_row(biz["id"]))


def test_followups_off_cancels_what_waits_and_schedules_nothing(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Off", "country": "KE", "industry": "salon"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    bid = r["business"]["id"]
    client.patch("/v1/ai", headers=h, json={"mode": "off"})
    client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Wanjiku", "body": "hi"})
    c = client.get("/v1/contacts", headers=h).json()[0]
    client.post("/v1/automations", headers=h, json={"trigger": "appointment.reminder", "minutes": 60, "message": "See you {when}"})
    client.post("/v1/campaigns", headers=h, json={"name": "Promo", "message": "Hi {name}",
                                                  "send_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
    st = datetime.now(timezone.utc) + timedelta(days=2)
    biz = biz_row(bid)
    with db.tx() as conn:
        D.book(conn, biz, "t", {"title": "Cut", "contact_id": c["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})
        assert conn.execute(db.scheduled_messages.select()).mappings().one()["status"] == "pending"
        B.set_blocks(conn, bid, "t", [b for b in blocks.BLOCKS if b != "followups"])
    with db.tx() as conn:
        sm = conn.execute(db.scheduled_messages.select()).mappings().one()
        cp = conn.execute(db.campaigns.select()).mappings().one()
    assert sm["status"] == "skipped" and sm["error"] == "follow-ups switched off" and cp["status"] == "cancelled"
    with db.tx() as conn:
        D.book(conn, biz_row(bid), "t", {"title": "Cut 2", "contact_id": c["id"], "starts_at": st + timedelta(hours=3),
                                         "ends_at": st + timedelta(hours=4)})
        assert conn.execute(db.scheduled_messages.select()).mappings().all().__len__() == 1    # nothing new
    # safety net: a row that slipped in is skipped, not sent
    with db.tx() as conn:
        conn.execute(db.scheduled_messages.update().values(status="pending", send_at=datetime.now(timezone.utc)))
    assert FU.send_due()["skipped"] == 1


def test_calendar_website_ai_and_comments_off_stop_their_automatic_work(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Quiet Shop", "country": "PH", "industry": "shop"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    bid = r["business"]["id"]
    week = {d: [["08:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    client.patch("/v1/booking", headers=h, json={"enabled": True, "hours": week, "notice_minutes": 0, "slug": "quiet-shop"})
    client.post("/v1/booking/services", headers=h, json={"name": "Fitting", "minutes": 30})
    from valeria_one.services import site as ST
    with db.tx() as conn:
        ST.save(conn, biz_row(bid), "t", enabled=True)
    client.post("/v1/comment-rules", headers=h, json={"keywords": ["price"], "reply": "Sent you our prices."})
    assert client.get("/b/quiet-shop").status_code == 200 and client.get("/s/quiet-shop").status_code == 200
    r = client.patch("/v1/business", headers=h, json={"blocks": ["pipeline"]})
    assert r.status_code == 200 and r.json()["features"]["calendar"] is False
    assert client.get("/b/quiet-shop").status_code == 404 and client.get("/s/quiet-shop").status_code == 404
    assert client.get("/v1/public/quiet-shop").status_code == 404
    ai = client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Ana", "body": "Can I book a fitting?"}).json()["ai"]
    assert ai == ["off"]                                                            # no in-chat booking, no AI
    res = client.post("/v1/sandbox/simulate-comment", headers=h, json={"name": "Maria", "text": "price?"}).json()
    assert res["result"] == "stored"                                                # kept, not answered
    from valeria_one.services import ai as AIS
    with pytest.raises(AIS.AIError):
        AIS.suggest(biz_row(bid), "t", client.get("/v1/conversations", headers=h).json()[0]["id"])
    assert client.get("/v1/contacts", headers=h).status_code == 200               # the API itself is never limited


# ---------------------------------------------------------------- the business's own words
def test_own_words_on_screens_and_api(client):
    bid = owner(client, industry="generic")
    tok = csrf(client, "/app/start")
    client.post("/app/start/words", data={"csrf": tok, "w__contact": "  Member ", "w__stage:new": "Inquiry",
                                          "w__deal": ""})
    biz = biz_row(bid)
    assert biz["words"] == {"contact": "Member", "stage:new": "Inquiry"}
    assert "Inquiry" in client.get("/app/pipeline").text
    with db.tx() as conn:
        assert D.pipeline(conn, biz, "ja")[0]["label"] == "Inquiry"                   # their word, in every language
        assert D.pipeline(conn, biz, "ja")[1]["label"] == "連絡済み"
        B.set_words(conn, bid, "t", {"stage:new": ""})                             # partial: contact stays
        assert B.get(conn, bid)["words"] == {"contact": "Member"}
        for bad in ({"teleport": "x"}, {"contact": "x" * 41}):
            with pytest.raises(Invalid):
                B.set_words(conn, bid, "t", bad)
    assert 'value="Member"' in client.get("/app/start").text


def test_start_steps_follow_the_business(client):
    bid = owner(client)
    page = client.get("/app/start").text
    assert page.count("To do") >= 3
    from valeria_one.services import inbox as X
    with db.tx() as conn:
        X.connect(conn, biz_row(bid), "t", channel="webhook", name="My app", cfg={"url": "https://example.com/in", "secret": "s"})
    page = client.get("/app/start").text
    assert re.search(r'href="/app/channels">[^<]+</a><span class="pill ok">', page)


@pytest.mark.parametrize("lang", ["ar", "ja", "hi", "es"])
def test_new_screens_in_other_languages(admin, client, lang):
    for path in ("/app/start", "/app/server", "/app"):
        r = client.get(path, headers={"Accept-Language": lang})
        assert r.status_code == 200 and f'lang="{lang}"' in r.text, (path, lang)
