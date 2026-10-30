"""ghl01 (v0.18): workflows — a trigger, steps with waits, if/else branches and a goal; run by the jobs with per-customer
state, once per thing that set them off, with loop protection and a history in words. Recipes keep working and can be
opened as a workflow. The builder is a vertical list (static/flow.js, no library)."""
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from _va import refuse_network
from conftest import make_business
from test_web import signup
from valeria_one import db
from valeria_one.channels import Inbound
from valeria_one.services import Invalid
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import fields as CF
from valeria_one.services import inbox as X
from valeria_one.services import recipes as RC
from valeria_one.services import workflows as WF

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
STATIC = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "static"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def biz_with_sandbox():
    biz, _ = make_business("MX", "salon", name="Salón Flor")
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/send", "secret": "s"})
    return biz, acc


def runs(wf_id=None):
    with db.tx() as conn:
        q = select(db.workflow_runs)
        if wf_id:
            q = q.where(db.workflow_runs.c.workflow_id == wf_id)
        return [dict(r) for r in conn.execute(q.order_by(db.workflow_runs.c.created_at)).mappings()]


def asks():
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.assistant_actions).order_by(db.assistant_actions.c.created_at)).mappings()]


def lead(conn, biz, name="Ana", **kw):
    return C.create(conn, biz, "webhook:whatsapp", {"name": name, "phone": kw.pop("phone", "+525511119999"), **kw})


# ---------------------------------------------------------------- the gallery, waits, branches, the goal
def test_new_lead_nurture_waits_branches_and_reaches_the_goal():
    biz, acc = biz_with_sandbox()
    with db.tx() as conn:
        wf = WF.from_template(conn, biz, "user:x", "lead_nurture")
        assert wf["active"] is False and wf["goal"] == "booked"
        WF.set_active(conn, biz, "user:x", wf["id"], True)
        ana = lead(conn, biz)
    r = runs(wf["id"])[0]
    assert r["status"] == "waiting" and r["pointer"] == [2] and r["contact_id"] == ana["id"]
    a = asks()
    assert len(a) == 1 and a[0]["kind"] == "reply" and a[0]["status"] == "asked" and "Ana" in a[0]["summary"]   # asks first
    wake = r["wake_at"]
    assert WF.round_(now=wake - timedelta(minutes=1))["woke"] == 0
    assert WF.round_(now=wake)["woke"] == 1
    r = runs(wf["id"])[0]
    assert r["status"] == "waiting" and r["pointer"] == [2, "no", 2]                         # no reply: the check-in, then 2 days
    assert len(asks()) == 2
    assert WF.round_(now=wake)["woke"] == 0                                                  # never twice
    with db.tx() as conn:
        st = datetime.now(timezone.utc) + timedelta(days=3)
        D.book(conn, biz, "t", {"title": "Corte", "contact_id": ana["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})
    r = runs(wf["id"])[0]
    assert r["status"] == "goal" and r["wake_at"] is None
    words = [WF.line_text(x, "en") for x in r["history"]]
    assert words[-1] == "reached the goal" and any(w.startswith("waiting 1 days") or w.startswith("waiting 1 ") for w in words)


def test_if_else_on_a_tag_and_a_reply():
    biz, acc = biz_with_sandbox()
    steps = [{"t": "if", "cond": {"kind": "tag", "value": "vip"}, "yes": [{"t": "tag", "tag": "gold"}],
              "no": [{"t": "tag", "tag": "silver"}, {"t": "notify", "text": "Nuevo cliente sin VIP"}]},
             {"t": "tag", "tag": "seen"}]
    with db.tx() as conn:
        wf = WF.save(conn, biz, "user:x", {"name": "VIP", "trigger": "lead.new", "steps": steps, "active": True})
        a = lead(conn, biz, "Ana", phone="+525511110001", tags=["vip"])
        b = lead(conn, biz, "Beto", phone="+525511110002")
        assert set(C.get(conn, biz["id"], a["id"])["tags"]) == {"vip", "gold", "seen"}
        assert set(C.get(conn, biz["id"], b["id"])["tags"]) == {"silver", "seen"}
    assert {r["status"] for r in runs(wf["id"])} == {"done"}
    assert [x["kind"] for x in asks()] == ["notice"]                                         # the owner hears about Beto only
    _ = acc


def test_reply_condition_reads_messages_since_the_run_started():
    biz, acc = biz_with_sandbox()
    steps = [{"t": "wait", "amount": 1, "unit": "hours"},
             {"t": "if", "cond": {"kind": "replied", "hours": 0}, "yes": [{"t": "tag", "tag": "talked"}], "no": [{"t": "tag", "tag": "silent"}]}]
    with db.tx() as conn:
        wf = WF.save(conn, biz, "user:x", {"name": "R", "trigger": "manual", "steps": steps, "active": True})
        ana = C.create(conn, biz, "user:x", {"name": "Ana", "phone": "+525511110001"})
        C.link(conn, biz["id"], ana["id"], "webhook", "+525511110001")
        assert WF.start_by_hand(conn, biz, "user:x", wf["id"], ana["id"]) == "waiting"
        assert WF.start_by_hand(conn, biz, "user:x", wf["id"], ana["id"]) is None              # never in it twice at once
    X.receive_items(acc | {"business_id": biz["id"]}, [Inbound(address="+525511110001", body="hola", provider_id="m1")])
    WF.round_(now=datetime.now(timezone.utc) + timedelta(hours=2))
    with db.tx() as conn:
        assert "talked" in C.get(conn, biz["id"], ana["id"])["tags"]


# ---------------------------------------------------------------- loops and the guard
def test_two_workflows_that_set_each_other_off_stop_as_a_loop():
    biz, _ = biz_with_sandbox()
    with db.tx() as conn:
        w1 = WF.save(conn, biz, "user:x", {"name": "A", "trigger": "tag.added", "trigger_arg": {"tag": "a"},
                                           "steps": [{"t": "untag", "tag": "a"}, {"t": "tag", "tag": "b"}], "active": True})
        w2 = WF.save(conn, biz, "user:x", {"name": "B", "trigger": "tag.added", "trigger_arg": {"tag": "b"},
                                           "steps": [{"t": "untag", "tag": "b"}, {"t": "tag", "tag": "a"}], "active": True})
        c = C.create(conn, biz, "user:x", {"name": "Ana"})
        C.patch(conn, biz, "user:x", c["id"], {"tags": ["a"]})
    st = [r["status"] for r in runs()]
    assert "loop" in st and len(st) <= 4                                                     # stopped, and said so
    assert any(WF.line_text(x, "en") for r in runs() if r["status"] == "loop" for x in r["history"])
    _ = (w1, w2)


def test_a_run_never_takes_more_than_the_guard(monkeypatch):
    biz, _ = biz_with_sandbox()
    monkeypatch.setattr(WF, "MAX_RUN_STEPS", 2)
    with db.tx() as conn:
        wf = WF.save(conn, biz, "user:x", {"name": "G", "trigger": "manual", "active": True,
                                           "steps": [{"t": "tag", "tag": "x1"}, {"t": "tag", "tag": "x2"}, {"t": "tag", "tag": "x3"}]})
        c = C.create(conn, biz, "user:x", {"name": "Ana"})
        assert WF.start_by_hand(conn, biz, "user:x", wf["id"], c["id"]) == "loop"
        assert set(C.get(conn, biz["id"], c["id"])["tags"]) == {"x1", "x2"}


def test_steps_are_checked_before_saving():
    biz, _ = biz_with_sandbox()
    with db.tx() as conn:
        for bad in ([], [{"t": "explode"}], [{"t": "wait", "amount": 0, "unit": "days"}], [{"t": "message", "text": ""}],
                    [{"t": "wait_until", "key": "nope"}], [{"t": "if", "cond": {"kind": "weather"}, "yes": [], "no": []}],
                    [{"t": "tag", "tag": "x"}] * (WF.MAX_STEPS + 1)):
            with pytest.raises(Invalid):
                WF.save(conn, biz, "user:x", {"trigger": "lead.new", "steps": bad})
        deep = [{"t": "tag", "tag": "x"}]
        for _ in range(WF.MAX_NEST + 1):
            deep = [{"t": "if", "cond": {"kind": "replied"}, "yes": deep, "no": []}]
        with pytest.raises(Invalid):
            WF.save(conn, biz, "user:x", {"trigger": "lead.new", "steps": deep})


# ---------------------------------------------------------------- time: birthdays (a date field, each year) and wait-until
def test_birthday_starts_once_a_year_in_the_morning_and_offers_need_consent():
    biz, _ = biz_with_sandbox()
    tz = ZoneInfo(biz["timezone"])
    with db.tx() as conn:
        f = CF.create(conn, biz, "user:x", obj="contact", label="Cumpleaños", kind="date")
        wf = WF.from_template(conn, biz, "user:x", "birthday")
        WF.set_active(conn, biz, "user:x", wf["id"], True)
        today = datetime.now(tz).date()
        yes = C.create(conn, biz, "user:x", {"name": "Ana", "phone": "+525511110001", "marketing_consent": True,
                                             "marketing_consent_source": "form", "custom": {f["key"]: f"1990-{today:%m-%d}"}})
        no = C.create(conn, biz, "user:x", {"name": "Beto", "phone": "+525511110002", "custom": {f["key"]: f"1985-{today:%m-%d}"}})
    morning = datetime.combine(today, time(10), tz).astimezone(timezone.utc)
    early = datetime.combine(today, time(7), tz).astimezone(timezone.utc)
    assert WF.round_(now=early)["started"] == 0
    assert WF.round_(now=morning)["started"] == 2 and WF.round_(now=morning + timedelta(hours=1))["started"] == 0
    lines = {r["contact_id"]: [x["k"] for x in r["history"]] for r in runs(wf["id"])}
    assert "rc_l_asked" in lines[yes["id"]] and "rc_l_no_consent" in lines[no["id"]]       # an offer only to who said yes
    nxt = WF._until(biz, {"key": f["key"], "offset": 0, "yearly": True, "at": "09:00"}, yes, morning)
    assert nxt.astimezone(tz).date() == date(today.year + 1, today.month, today.day) or (today.month, today.day) == (2, 29)


# ---------------------------------------------------------------- recipes stay; one becomes a workflow
def test_recipe_opens_as_a_workflow_and_is_switched_off():
    biz, _ = biz_with_sandbox()
    with db.tx() as conn:
        r = RC.save(conn, biz, "user:x", {"trigger": "lead.new", "condition": {"kind": "source", "value": "web"},
                                          "actions": [{"do": "tag", "tag": "web"}, {"do": "message", "text": "Hola {name}"}],
                                          "ask_first": False, "name": "Web leads"})
        wf = WF.from_recipe(conn, biz, "user:x", r["id"])
        assert RC.get(conn, biz["id"], r["id"])["active"] is False and wf["active"] is True and wf["recipe_id"] == r["id"]
        assert wf["trigger"] == "lead.new" and wf["steps"][0]["t"] == "if"
        assert [s["t"] for s in wf["steps"][0]["yes"]] == ["tag", "message"] and wf["steps"][0]["yes"][1]["ask"] is False
        C.create(conn, biz, "webhook:whatsapp", {"name": "Eva", "phone": "+525511110007", "source": "web"})
    assert [x["status"] for x in asks()] == ["queued"]                                       # "ask me first" was off: queued with undo
    with db.tx() as conn:
        assert conn.execute(select(db.recipe_runs)).first() is None                         # only the workflow ran


# ---------------------------------------------------------------- the screens
def test_builder_screens_and_saving_json(client):
    signup(client, email="wf@example.com")
    gallery = client.get("/app/workflows").text
    assert 'action="/app/workflows/template/lead_nurture"' in gallery
    t = re.search(r'name="csrf" value="([^"]+)"', gallery).group(1)
    r = client.post("/app/workflows/template/no_show", data={"csrf": t}, follow_redirects=False)
    url = r.headers["location"]
    page = client.get(url).text
    assert 'id="wf-root"' in page and '/static/flow.js' in page and 'id="wf-data"' in page and "</script>" in page
    data = json.loads(re.search(r'<script type="application/json" id="wf-data">(.*?)</script>', page, re.S).group(1))
    assert {"stages", "fields", "team", "sequences", "words"} <= set(data) and data["words"]["wf_e_t_wait"]
    steps = [{"t": "wait", "amount": 2, "unit": "hours"}, {"t": "task", "title": "Llamar", "hours": 1}]
    client.post(url, data={"csrf": t, "act": "save", "name": "Mi flujo", "trigger": "booking.no_show", "steps": json.dumps(steps),
                           "goal": "booked", "active": "1"})
    with db.tx() as conn:
        wf = dict(conn.execute(select(db.workflows)).mappings().one())
    assert wf["name"] == "Mi flujo" and wf["steps"] == steps and wf["active"] is True
    bad = client.post(url, data={"csrf": t, "act": "save", "trigger": "booking.no_show", "steps": "not json"}, follow_redirects=True)
    assert bad.status_code == 200
    new = client.get("/app/workflows/new")
    assert new.status_code == 200 and 'name="trigger"' in new.text


def test_flow_js_is_small_and_plain():
    js = (STATIC / "flow.js").read_text()
    assert len(js.encode()) < 25 * 1024 and "import " not in js and "require(" not in js


def test_new_words_are_in_all_11_languages():
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.startswith(("wf_", "rc_l_seq", "rc_a_sequence", "rc_do_sequence"))] + ["workflows"]
    assert len(new) > 150
    for lg in LANGS:
        for k in new:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
