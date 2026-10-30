"""mon01 (v0.16): "when this, do that" — ready-made with one tap or built from dropdowns; once per event, loop-safe,
written down in words, and customers only ever written to through the usual doors (ask first, consent, STOP, 24 h)."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from conftest import make_business
from test_web import signup
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import fields as CF
from valeria_one.services import owner as OW
from valeria_one.services import projects as PR
from valeria_one.services import recipes as RC


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def runs():
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.recipe_runs).order_by(db.recipe_runs.c.created_at)).mappings()]


def actions(kind=None):
    with db.tx() as conn:
        q = select(db.assistant_actions)
        if kind:
            q = q.where(db.assistant_actions.c.kind == kind)
        return [dict(r) for r in conn.execute(q).mappings()]


@pytest.fixture
def biz():
    b, _ = make_business("MX", "salon")
    return b


def lead(biz, phone="+529991112233", name="Rosa María"):
    """Someone writes on WhatsApp for the first time (the way every channel's inbound creates a customer)."""
    with db.tx() as conn:
        c, new = C.find_or_create_by_identity(conn, biz, "whatsapp", phone, name, "whatsapp")
    return c


def test_gallery_one_tap_new_lead_gets_a_reply_task_once(biz):
    with db.tx() as conn:
        g = {p["key"]: p for p in RC.presets(conn, biz, "es")}
        assert {"lead_task", "lead_tag", "won_thanks", "booking_tag", "quiet_notify", "form_deal", "field_notify"} <= set(g)
        assert g["field_notify"]["needs"] == "fields" and g["form_deal"]["needs"] == "forms"
        assert g["lead_task"]["sentence"].startswith("Cuando llega un nuevo prospecto")
        assert not any("{" in p["sentence"] for p in g.values())                    # no {name} in front of the owner
        assert biz["name"] in g["won_thanks"]["sentence"] and "«Responder a …»" in g["lead_task"]["sentence"]
        RC.switch_on(conn, biz, "user:x", "lead_task")
        RC.switch_on(conn, biz, "user:x", "lead_task")                               # a second tap: still one recipe
        assert len(RC.list_(conn, biz["id"])) == 1
        with pytest.raises(Invalid):
            RC.switch_on(conn, biz, "user:x", "field_notify")                         # needs a field first
    c = lead(biz)
    lead(biz)                                                                      # writes again: not a new lead
    with db.tx() as conn:
        tasks = PR.tasks(conn, biz["id"])
    assert [t["title"] for t in tasks] == ["Responder a Rosa"] and tasks[0]["contact_id"] == c["id"]
    assert tasks[0]["due_at"] is not None and tasks[0]["created_by"].startswith("recipe:")
    r = runs()
    assert len(r) == 1 and r[0]["status"] == "done" and r[0]["lines"][0]["k"] == "rc_l_task"


def test_runs_once_per_event_even_if_replayed(biz):
    with db.tx() as conn:
        r = RC.switch_on(conn, biz, "user:x", "lead_tag")
    c = lead(biz)
    with db.tx() as conn:
        ev = conn.execute(select(db.events.c.id).where(db.events.c.type == "contact.created")).scalar()
        RC.on_event(conn, biz["id"], "contact.created", c["id"], {}, "webhook:whatsapp", ev)       # the same event again
        assert RC.fire(conn, r, subject_id=c["id"], contact_id=c["id"], deal_id=None, dedupe=f"ev:{ev}", actor="system") is None
        assert C.get(conn, biz["id"], c["id"])["tags"] == ["nuevo-prospecto"]
    assert len(runs()) == 1


def test_the_team_adding_someone_is_not_a_lead(biz):
    with db.tx() as conn:
        RC.switch_on(conn, biz, "user:x", "lead_task")
        C.create(conn, biz, "user:abc", {"name": "Cliente de siempre"})
        C.create(conn, biz, "import:user:abc", {"name": "De la hoja"})
        C.create(conn, biz, "website", {"name": "De la web", "phone": "9990001111"})
        assert [t["title"] for t in PR.tasks(conn, biz["id"])] == ["Responder a De"]


def test_condition_only_if(biz):
    with db.tx() as conn:
        RC.save(conn, biz, "user:x", {"trigger": "lead.new", "condition": {"kind": "source", "value": "WhatsApp"},
                                      "actions": [{"do": "tag", "tag": "wa"}]})
        a = C.create(conn, biz, "website", {"name": "Web", "phone": "9990001111", "source": "website"})
    b = lead(biz)
    with db.tx() as conn:
        assert C.get(conn, biz["id"], a["id"])["tags"] == [] and C.get(conn, biz["id"], b["id"])["tags"] == ["wa"]
    assert len(runs()) == 1                                                        # "only if" said no: nothing written down


def test_builder_validates_everything(biz):
    with db.tx() as conn:
        for bad in [{"trigger": "moon.full", "actions": [{"do": "tag", "tag": "x"}]},
                    {"trigger": "lead.new", "actions": []},
                    {"trigger": "lead.new", "actions": [{"do": "tag", "tag": "a"}] * 4},
                    {"trigger": "lead.new", "actions": [{"do": "fly"}]},
                    {"trigger": "lead.new", "actions": [{"do": "message", "text": ""}]},
                    {"trigger": "lead.new", "actions": [{"do": "assign", "user_id": "not-a-person"}]},
                    {"trigger": "deal.stage", "trigger_arg": {"stage": "nope"}, "actions": [{"do": "tag", "tag": "x"}]},
                    {"trigger": "contact.quiet", "trigger_arg": {"hours": 500}, "actions": [{"do": "notify"}]},
                    {"trigger": "field.changed", "trigger_arg": {"key": "f_none"}, "actions": [{"do": "notify"}]},
                    {"trigger": "lead.new", "condition": {"kind": "tag", "value": ""}, "actions": [{"do": "notify"}]}]:
            with pytest.raises(Invalid):
                RC.save(conn, biz, "user:x", bad)
        r = RC.save(conn, biz, "user:x", {"trigger": "contact.quiet", "trigger_arg": {"hours": "3"}, "actions": [{"do": "notify", "text": ""}]})
        assert r["trigger_arg"] == {"hours": 3} and r["actions"][0]["text"]               # the usual words when left empty
        assert RC.describe(conn, biz, r, "en") == "When a customer has had no answer for 3 h → tell you on Valeria"


def _stages(biz):
    from valeria_one import packs
    return [s.key for s in packs.industry(biz["industry"]).stages if s.outcome == "open"]


def test_loop_protection_stops_ping_pong(biz):
    s1, s2, s3 = _stages(biz)[:3]
    with db.tx() as conn:
        a = RC.save(conn, biz, "user:x", {"trigger": "deal.stage", "trigger_arg": {"stage": s2}, "actions": [{"do": "move", "stage": s3}]})
        b = RC.save(conn, biz, "user:x", {"trigger": "deal.stage", "trigger_arg": {"stage": s3}, "actions": [{"do": "move", "stage": s2}]})
        c = C.create(conn, biz, "user:x", {"name": "Ana"})
        d = D.create(conn, biz, "user:x", {"title": "Paquete", "contact_id": c["id"], "stage": s1})
        D.patch(conn, biz, "user:x", d["id"], {"stage": s2})                        # the owner moves it: A → s3, B → s2, A stops
        assert D.get(conn, biz["id"], d["id"])["stage"] == s2
    by = {(r["recipe_id"], r["status"]) for r in runs()}
    assert (a["id"], "done") in by and (b["id"], "done") in by and (a["id"], "loop") in by
    assert next(r for r in runs() if r["status"] == "loop")["lines"] == [{"k": "rc_l_loop", "p": {}}]
    with db.tx() as conn:                                                          # an hour later the owner may move it again
        conn.execute(update(db.recipe_runs).values(created_at=datetime.now(timezone.utc) - timedelta(hours=2)))
        D.patch(conn, biz, "user:x", d["id"], {"stage": s1})
        D.patch(conn, biz, "user:x", d["id"], {"stage": s2})
    assert len([r for r in runs() if r["status"] == "done"]) == 4


def test_a_failing_recipe_never_undoes_what_set_it_off(biz):
    with db.tx() as conn:
        RC.save(conn, biz, "user:x", {"trigger": "lead.new", "actions": [{"do": "tag", "tag": "x"}]})
        conn.execute(update(db.recipes).values(actions=[{"do": "task", "title": "", "hours": 1}]))   # broken by hand
    c = lead(biz)
    with db.tx() as conn:
        assert C.get(conn, biz["id"], c["id"])["name"] == "Rosa María"            # the customer is there
    r = runs()
    assert r[0]["status"] == "failed" and r[0]["lines"][0]["k"] == "rc_l_failed"


def test_message_asks_first_and_never_to_someone_who_said_stop(biz):
    won = next(s.key for s in __import__("valeria_one.packs", fromlist=["x"]).industry(biz["industry"]).stages if s.outcome == "won")
    with db.tx() as conn:
        RC.switch_on(conn, biz, "user:x", "won_thanks")
        a = C.create(conn, biz, "user:x", {"name": "Ana López", "phone": "9991234567"})
        b = C.create(conn, biz, "user:x", {"name": "Bea", "phone": "9997654321"})
        C.unsubscribe(conn, biz["id"], b["id"], "test")
        for c in (a, b):
            d = D.create(conn, biz, "user:x", {"title": "Paquete", "contact_id": c["id"]})
            D.patch(conn, biz, "user:x", d["id"], {"stage": won})
    asked = actions("reply")
    assert len(asked) == 1 and asked[0]["status"] == "asked" and asked[0]["contact_id"] == a["id"]
    assert asked[0]["payload"]["text"] == f"¡Gracias, Ana! Fue un gusto. — {biz['name']}" and asked[0]["by"].startswith("recipe:")
    assert "Ana" in asked[0]["summary"]
    lines = {r["contact_id"]: r["lines"][0]["k"] for r in runs()}
    assert lines == {a["id"]: "rc_l_asked", b["id"]: "rc_l_optout"}
    with db.tx() as conn:
        assert conn.execute(select(db.messages)).first() is None                    # nothing went out by itself
        assert [q["id"] for q in OW.waiting(conn, biz["id"])] == [asked[0]["id"]]  # it waits on the Valeria page


def test_message_without_asking_still_follows_the_24h_window(client, monkeypatch):
    """Allowed to send by itself: queued with Valeria's minute to undo, then out through inbox.send — inside the reply
    window it goes; outside it (no approved template) it is blocked like any other message."""
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Recetas", "country": "MX", "industry": "salon"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    client.patch("/v1/ai", headers=h, json={"mode": "off"})
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == r["business"]["id"])).mappings().one())
        RC.save(conn, biz, "user:x", {"trigger": "lead.new", "ask_first": False, "actions": [{"do": "message", "text": "Hola {name}, ya te leímos"}]})
    client.post("/v1/sandbox/simulate", headers=h, json={"from": "c1", "name": "Rosa María", "body": "hola"})
    q = actions("reply")
    assert len(q) == 1 and q[0]["status"] == "queued"
    OW.run_due(now=datetime.now(timezone.utc) + timedelta(minutes=2))
    with db.tx() as conn:
        out = conn.execute(select(db.messages.c.body, db.messages.c.status).where(db.messages.c.direction == "out")).all()
    assert len(out) == 1 and out[0][0] == "Hola Rosa, ya te leímos" and out[0][1] in ("sent", "delivered")
    with db.tx() as conn:                                                          # a day later someone new: outside the window
        conn.execute(update(db.conversations).values(last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=30)))
    client.post("/v1/sandbox/simulate", headers=h, json={"from": "c2", "name": "Bob", "body": "hi"})
    with db.tx() as conn:
        conn.execute(update(db.conversations).values(last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=30)))
    OW.run_due(now=datetime.now(timezone.utc) + timedelta(minutes=2))
    last = [a for a in actions("reply") if a["status"] != "done"]
    assert len(last) == 1 and last[0]["status"] == "failed"
    with db.tx() as conn:
        assert len(conn.execute(select(db.messages.c.id).where(db.messages.c.direction == "out",
                                                               db.messages.c.status.in_(("sent", "delivered")))).all()) == 1


def test_paused_valeria_means_asking_again(biz):
    with db.tx() as conn:
        RC.save(conn, biz, "user:x", {"trigger": "lead.new", "ask_first": False, "actions": [{"do": "message", "text": "Hola"}]})
        OW.pause(conn, biz, "test")
    lead(biz)
    assert [a["status"] for a in actions("reply")] == ["asked"]


def test_notify_and_quiet_customers(biz):
    with db.tx() as conn:
        RC.switch_on(conn, biz, "user:x", "quiet_notify")
    c = lead(biz)
    with db.tx() as conn:
        acc = conn.execute(db.channel_accounts.insert().values(business_id=biz["id"], channel="sandbox", name="t", config={},
                                                                hook_id="hk-quiet").returning(db.channel_accounts.c.id)).scalar()
        cv = conn.execute(db.conversations.insert().values(business_id=biz["id"], contact_id=c["id"], channel_account_id=acc,
                                                           last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=3))
                          .returning(db.conversations.c.id)).scalar()
        conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=cv, direction="in", body="¿precio?", status="received"))
    assert RC.round_()["ran"] == 1
    assert RC.round_()["ran"] == 0                                                 # the same wait is told once
    n = actions("notice")
    assert len(n) == 1 and n[0]["status"] == "queued" and "Rosa" in n[0]["summary"]
    OW.run_due()
    assert actions("notice")[0]["status"] == "done"                                # on the Valeria log (no phone linked: nothing to send)
    with db.tx() as conn:                                                          # answered: no more "waiting"
        conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=cv, direction="out", body="¡Hola!", status="sent"))
        conn.execute(update(db.conversations).values(last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=4)))
    assert RC.round_()["ran"] == 0


def test_switched_off_and_followups_off_do_nothing(biz):
    from valeria_one.services import businesses as B
    with db.tx() as conn:
        r = RC.switch_on(conn, biz, "user:x", "lead_tag")
        RC.set_active(conn, biz, "user:x", r["id"], False)
    lead(biz, "+529990000001")
    with db.tx() as conn:
        RC.set_active(conn, biz, "user:x", r["id"], True)
        B.set_blocks(conn, biz["id"], "test", [b for b in __import__("valeria_one.blocks", fromlist=["x"]).BLOCKS if b != "followups"])
    lead(biz, "+529990000002")
    assert runs() == []


def test_booking_form_and_field_triggers(biz):
    from valeria_one.services import forms as FM
    s1, s2 = _stages(biz)[:2]
    with db.tx() as conn:
        RC.switch_on(conn, biz, "user:x", "booking_tag")
        f = CF.create(conn, biz, "user:x", obj="contact", label="Listo", kind="yesno")
        RC.save(conn, biz, "user:x", {"trigger": "field.changed", "trigger_arg": {"key": f["key"], "value": "sí"},
                                      "actions": [{"do": "move", "stage": s2}]})
        fm = FM.save(conn, biz, "user:x", {"name": "Cotización", "fields": [{"key": "phone", "required": True}]})
        RC.save(conn, biz, "user:x", {"trigger": "form.submitted", "trigger_arg": {"form_id": str(fm["id"])},
                                      "actions": [{"do": "deal", "stage": s1}]})
        c = C.create(conn, biz, "user:x", {"name": "Ana", "phone": "9991234567"})
        D.book(conn, biz, "booking_page", {"title": "Corte", "contact_id": c["id"], "starts_at": datetime.now(timezone.utc) + timedelta(days=1),
                                           "ends_at": datetime.now(timezone.utc) + timedelta(days=1, hours=1)})
        assert "reservó" in C.get(conn, biz["id"], c["id"])["tags"]
        FM.submit(conn, biz, FM.get(conn, biz["id"], fm["id"]), {"name": "Ana", "phone": "9991234567"}, request_consent=True,
                  marketing_consent=False)
        deals = D.search(conn, biz["id"], contact_id=c["id"])
        assert len(deals) == 1 and deals[0]["stage"] == s1 and deals[0]["title"].startswith("Cotización")
        C.patch(conn, biz, "user:x", c["id"], {"custom": {"Listo": "yes"}})
        assert D.get(conn, biz["id"], deals[0]["id"])["stage"] == s2


def test_screens_gallery_builder_log(client):
    signup(client)
    t = tok(client)
    page = client.get("/app/recipes")
    assert page.status_code == 200 and 'action="/app/recipes/on/lead_task"' in page.text and "/app/recipes/new?preset=lead_task" in page.text
    assert '<a href="/app/recipes" class="' in client.get("/app").text                 # in the menu
    r = client.post("/app/recipes/on/lead_task", data={"csrf": t}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        rid = conn.execute(select(db.recipes.c.id)).scalar()
    b = client.get(f"/app/recipes/{rid}").text
    assert 'name="trigger"' in b and 'data-when="trigger=deal.stage"' in b and 'name="a2_do"' in b and 'name="ask_first"' in b
    assert client.get("/app/recipes/new?preset=won_thanks").status_code == 200
    r = client.post("/app/recipes", data={"csrf": t, "trigger": "lead.new", "c_kind": "", "a0_do": "tag", "a0_tag": "Nuevo",
                                          "a1_do": "task", "a1_title": "Llamar a {name}", "a1_hours": "2", "ask_first": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        mine = RC.list_(conn, conn.execute(select(db.businesses.c.id)).scalar())
    assert [x["actions"] for x in mine][1] == [{"do": "tag", "tag": "nuevo"}, {"do": "task", "title": "Llamar a {name}", "hours": 2}]
    client.post(f"/app/recipes/{mine[1]['id']}", data={"csrf": t, "act": "off"})
    with db.tx() as conn:
        assert not RC.get(conn, mine[1]["business_id"], mine[1]["id"])["active"]
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
    lead(biz)
    log = client.get("/app/recipes").text
    assert "Tarea agregada: Responder a Rosa" in log or "Task added: Reply to Rosa" in log
    client.post(f"/app/recipes/{mine[1]['id']}", data={"csrf": t, "act": "delete"})
    with db.tx() as conn:
        assert len(RC.list_(conn, biz["id"])) == 1
    assert "/app/recipes" in client.get("/app/start").text
