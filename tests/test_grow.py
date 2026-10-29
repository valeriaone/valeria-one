import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, update

from conftest import make_business
from valeria_one import blocks, db
from valeria_one.services import Invalid
from valeria_one.services import booking as BK
from valeria_one.services import businesses as B
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import followups as FU
from valeria_one.services import growth as GR
from valeria_one.services import inbox as X
from valeria_one.services import payments as PM

ALL_WEEK = {d: [["08:00", "20:00"]] for d in BK.DAYS}
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731


@pytest.fixture(autouse=True)
def any_hour(monkeypatch):
    monkeypatch.setattr(GR, "SEND_FROM", 0)                  # the daily round runs whatever hour the tests run at


class Gateway:
    """The business's own messaging gateway (webhook channel), answered in-process: nothing leaves the machine."""

    def __init__(self):
        self.sent = []

    def __call__(self, request):
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"gw-{len(self.sent)}"})

    @property
    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def shop(country="MX", booking=False, name="Estética Sol"):
    biz, key = make_business(country, "salon", name=name)
    with db.tx() as conn:
        acc = X.connect(conn, biz, "t", channel="webhook", name="Gateway", cfg={"url": "https://gw.example/send", "secret": "s3"})
        if booking:
            biz = BK.save_settings(conn, biz, "t", enabled=True, slug="estetica-sol", hours=ALL_WEEK, notice_minutes=0, days_ahead=30)
            BK.add_service(conn, biz, "t", name="Corte", minutes=60, price_minor=20000)
        biz = B.get(conn, biz["id"])
    return biz, {"Authorization": f"Bearer {key}"}, acc


def customer(biz, name, *, days=120, consent=True, reach="webhook", unsub=False):
    """A customer created `days` ago, reachable on the gateway (or not)."""
    with db.tx() as conn:
        data = {"name": name}
        if consent:
            data.update(marketing_consent=True, marketing_consent_source="signed card at the counter")
        c = C.create(conn, biz, "t", data)
        if reach:
            C.link(conn, biz["id"], c["id"], reach, f"id-{name}")
        conn.execute(update(db.contacts).where(db.contacts.c.id == c["id"]).values(
            created_at=NOW() - timedelta(days=days), unsubscribed_at=NOW() if unsub else None))
    return c


def visit(biz, c, days_ago):
    st = NOW() - timedelta(days=days_ago)
    with db.tx() as conn:
        return D.book(conn, biz, "t", {"title": "Corte", "contact_id": c["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})


def wrote(biz, c, acc, days_ago):
    with db.tx() as conn:
        conv = X._conversation(conn, biz["id"], acc["id"], c["id"])
        conn.execute(db.messages.insert().values(business_id=biz["id"], conversation_id=conv["id"], direction="in", body="hola",
                                                 status="received", created_at=NOW() - timedelta(days=days_ago)))
        conn.execute(update(db.conversations).where(db.conversations.c.id == conv["id"]).values(
            last_inbound_at=NOW() - timedelta(days=days_ago)))


def outbox():
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.scheduled_messages).order_by(db.scheduled_messages.c.created_at)).mappings()]


def winback_on(biz, **kw):
    with db.tx() as conn:
        return GR.set_winback(conn, biz, "t", True, **kw)


def new_day(biz):
    with db.tx() as conn:
        conn.execute(update(db.growth_settings).where(db.growth_settings.c.business_id == biz["id"])
                     .values(ran_on=datetime.now(ZoneInfo(biz["timezone"])).date() - timedelta(days=1)))


# ---------------------------------------------------------------- win-back
def test_only_quiet_reachable_consenting_customers_are_picked(client):
    biz, h, acc = shop()
    a = customer(biz, "Ana")
    visit(biz, a, 80)                                                         # last seen 80 days ago -> quiet
    b = customer(biz, "Beto")
    visit(biz, b, 10)                                                         # came 10 days ago
    customer(biz, "Carla", consent=False)                                     # quiet, never agreed to promotions
    customer(biz, "Dani", unsub=True)                                         # quiet, unsubscribed
    customer(biz, "Eva", days=5)                                              # new: not quiet
    customer(biz, "Fer", reach="email")                                       # quiet, but no email channel connected
    g = customer(biz, "Gabi")
    wrote(biz, g, acc, 20)                                                    # wrote 20 days ago
    hh = customer(biz, "Hugo")
    visit(biz, hh, -3)                                                        # booked for next week
    i = customer(biz, "Iris")
    PM.record_received(biz, "t", amount_minor=50000, description="Tinte", contact_id=i["id"])   # paid today
    cancelled = customer(biz, "Juan")
    ap = visit(biz, cancelled, 2)
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", ap["id"], "cancelled")  # a cancelled visit is not a visit
        q = GR.quiet(conn, biz)
    why = {r["name"]: r["why"] for r in q["rows"]}
    assert why == {"Ana": None, "Carla": "no_consent", "Dani": "unsubscribed", "Fer": "no_channel", "Juan": None}
    assert q["count"] == 5 and q["ready"] == 2 and q["rows"][0]["name"] == "Ana"       # newest-quiet first
    assert [r["days"] for r in q["rows"] if r["name"] == "Ana"] == [80]
    assert GR.daily() == {"businesses": 0, "queued": 0}                       # switched off: nothing happens
    winback_on(biz)
    assert GR.daily()["queued"] == 2
    assert {r["contact_id"] for r in outbox()} == {a["id"], cancelled["id"]}
    assert GR.daily()["queued"] == 0                                          # once per business per local day
    gw = Gateway()
    assert FU.send_due(http=gw.http)["sent"] == 2
    body = gw.sent[0]["body"]
    assert body.startswith("Hola ") and "te extrañamos en Estética Sol!" in body and "Responde a este mensaje" in body
    assert "{" not in body and "  " not in body
    new_day(biz)
    assert GR.daily()["queued"] == 0                                          # never again within 180 days
    with db.tx() as conn:
        q = GR.quiet(conn, biz)
    assert {r["name"]: r["why"] for r in q["rows"]}["Ana"] == "messaged" and q["ready"] == 0


def test_offer_booking_link_daily_cap_and_window(client):
    biz, h, acc = shop(booking=True)
    people = [customer(biz, f"P{n}", days=100 + n) for n in range(5)]
    with db.tx() as conn:
        GR.save_settings(conn, biz, "t", daily_cap=2, offer="Esta semana 10% de descuento.", gap_days=30)
    winback_on(biz)
    assert GR.daily()["queued"] == 2
    assert [r["contact_id"] for r in outbox()] == [people[0]["id"], people[1]["id"]]   # most recently quiet first
    gw = Gateway()
    FU.send_due(http=gw.http)
    assert gw.sent[0]["body"] == ("Hola P0, ¡te extrañamos en Estética Sol! Esta semana 10% de descuento. "
                                  "Agenda tu próxima visita aquí: https://one.test/b/estetica-sol")
    new_day(biz)
    assert GR.daily()["queued"] == 2 and len(outbox()) == 4                   # the next two, the next day
    with db.tx() as conn:                                                     # after the owner's window, once more
        conn.execute(update(db.scheduled_messages).values(created_at=NOW() - timedelta(days=31)))
    new_day(biz)
    assert GR.daily()["queued"] == 2
    with pytest.raises(Invalid):
        with db.tx() as conn:
            GR.save_settings(conn, biz, "t", quiet_days=3)
    with db.tx() as conn:
        GR.save_settings(conn, biz, "t", quiet_days=45)
        assert GR.ready_made(conn, biz["id"], "winback")["minutes"] == 45 * 1440


def test_rules_at_send_time_stop_came_back_and_quiet_hours(client):
    biz, h, acc = shop(country="US", name="Sunny Cuts")
    a, b, c = customer(biz, "Amy"), customer(biz, "Bob"), customer(biz, "Cal")
    winback_on(biz)
    ny = ZoneInfo("America/New_York")
    night = datetime.now(ny).replace(hour=22, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
    assert GR.daily(now=night)["queued"] == 3
    with db.tx() as conn:
        C.unsubscribe(conn, biz["id"], a["id"], "webhook:sms")               # texted STOP after the list was made
    visit(biz, b, 0)                                                          # came back before it went out
    gw = Gateway()
    res = FU.send_due(now=night, http=gw.http)
    assert res == {"sent": 0, "skipped": 2, "failed": 0, "later": 1} and gw.sent == []
    rows = {r["contact_id"]: r for r in outbox()}
    assert rows[a["id"]]["error"] == "unsubscribed" and rows[b["id"]]["error"] == "came back before the message went out"
    assert rows[c["id"]]["status"] == "pending" and rows[c["id"]]["send_at"].astimezone(ny).hour == 8   # US quiet hours
    with db.tx() as conn:
        assert GR.ready_made(conn, biz["id"], "winback")["kind"] == "marketing"


def test_whatsapp_like_channels_need_the_approved_template(client, monkeypatch):
    """The sandbox channel behaves like WhatsApp (24 h window); nothing real is ever sent from a sandbox business."""
    monkeypatch.setenv("VO_SANDBOX", "1")
    r = client.post("/v1/sandbox/signup", json={"name": "Barbería Demo", "country": "MX", "industry": "salon"}).json()
    h = {"Authorization": f"Bearer {r['api_key']}"}
    client.patch("/v1/ai", headers=h, json={"mode": "off"})
    for who in ("c1", "c2"):
        client.post("/v1/sandbox/simulate", headers=h, json={"from": who, "name": f"Cliente {who}", "body": "hola"})
    with db.tx() as conn:
        biz = B.get(conn, r["business"]["id"])
        conn.execute(update(db.contacts).values(created_at=NOW() - timedelta(days=90), marketing_consent=True,
                                                marketing_consent_source="WhatsApp: replied YES"))
        conn.execute(update(db.messages).values(created_at=NOW() - timedelta(days=90)))
        conn.execute(update(db.conversations).values(last_inbound_at=NOW() - timedelta(days=90)))
    assert client.patch("/v1/growth", headers=h, json={"winback": True}).status_code == 200
    q = client.get("/v1/growth/quiet", headers=h).json()                      # 24 h window long closed, no template:
    assert q["ready"] == 0 and {r["why"] for r in q["rows"]} == {"needs_template"}
    assert GR.daily()["queued"] == 0 and outbox() == []                       # not queued (so not used up) — and the owner sees why
    client.patch("/v1/growth", headers=h, json={"winback_template": "te_extranamos"})
    assert client.get("/v1/growth/quiet", headers=h).json()["ready"] == 2
    new_day(biz)
    assert GR.daily()["queued"] == 2 and FU.send_due()["sent"] == 2
    with db.tx() as conn:
        out = conn.execute(select(db.messages).where(db.messages.c.direction == "out")).mappings().all()
        chans = {a["channel"] for a in conn.execute(select(db.channel_accounts)).mappings()}
    assert all(m["template"]["name"] == "te_extranamos" and m["kind"] == "marketing" for m in out) and chans == {"sandbox"}


def test_channel_choice_passes_over_reply_window_channels_without_a_template(client):
    biz, h, gw_acc = shop()
    with db.tx() as conn:
        wa = X.connect(conn, biz, "t", channel="sandbox", name="WhatsApp-like")      # a 24 h reply window, like WhatsApp
    ana = customer(biz, "Ana")
    with db.tx() as conn:
        C.link(conn, biz["id"], ana["id"], "sandbox", "wa-ana")
    wrote(biz, ana, wa, 100)                                                  # her last chat was on the WhatsApp-like one
    winback_on(biz)
    assert GR.daily()["queued"] == 1
    gw = Gateway()
    assert FU.send_due(http=gw.http)["sent"] == 1 and gw.sent[0]["to"] == "id-Ana"   # so it went on the gateway
    with db.tx() as conn:
        GR.set_winback(conn, biz, "t", True, template="te_extranamos")
        assert GR.avoid_channels(GR.ready_made(conn, biz["id"], "winback")) == frozenset()          # with it: any channel
        assert "sandbox" in GR.avoid_channels({"preset": "winback"}) and not GR.avoid_channels({"preset": "referral"})


def test_results_count_replies_and_customers_who_came_back(client):
    biz, h, acc = shop()
    a, b, c = customer(biz, "Ana"), customer(biz, "Beto"), customer(biz, "Ceci")
    winback_on(biz)
    GR.daily()
    FU.send_due(http=Gateway().http)
    wrote(biz, a, acc, 0)                                                     # answered
    st = NOW() + timedelta(days=2)
    with db.tx() as conn:                                                     # and booked
        D.book(conn, biz, "t", {"title": "Corte", "contact_id": a["id"], "starts_at": st, "ends_at": st + timedelta(hours=1)})
    PM.record_received(biz, "t", amount_minor=30000, description="Uñas", contact_id=b["id"])   # paid, no reply
    with db.tx() as conn:                                                     # Ceci paid too, but 40 days after the message
        conn.execute(update(db.scheduled_messages).where(db.scheduled_messages.c.contact_id == c["id"])
                     .values(send_at=NOW() - timedelta(days=40)))
    PM.record_received(biz, "t", amount_minor=30000, description="Uñas", contact_id=c["id"])
    with db.tx() as conn:
        res = GR.results(conn, biz["id"])
    assert res == {"sent": 3, "replied": 1, "came_back": 2, "days": 90, "within_days": 30}
    assert client.get("/v1/growth", headers=h).json()["results"]["came_back"] == 2


def test_block_off_stops_and_hides_everything(client):
    biz, h, acc = shop()
    a = customer(biz, "Ana")
    winback_on(biz)
    GR.daily()
    assert outbox()[0]["status"] == "pending"
    with db.tx() as conn:                                                     # follow-ups off: Grow's message stays
        B.set_blocks(conn, biz["id"], "t", [b for b in blocks.BLOCKS if b != "followups"])
    assert outbox()[0]["status"] == "pending"
    with db.tx() as conn:
        B.set_blocks(conn, biz["id"], "t", [b for b in blocks.BLOCKS if b not in ("followups", "grow")])
        link = GR.link_for(conn, biz, a["id"])
    assert outbox()[0]["status"] == "skipped" and outbox()[0]["error"] == "grow switched off"
    new_day(biz)
    assert GR.daily()["queued"] == 0
    with db.tx() as conn:
        conn.execute(update(db.scheduled_messages).values(status="pending"))   # a row that slipped in is still not sent
    assert FU.send_due(http=Gateway().http)["skipped"] == 1
    assert client.get(f"/f/{link['code']}").status_code == 404
    assert client.get("/v1/growth", headers=h).status_code == 200            # the API is not limited by blocks


# ---------------------------------------------------------------- bring a friend
def owner_session(client, biz):
    """Sign in to the web app as the owner of `biz`."""
    from valeria_one.services import users as U
    email = f"owner-{str(biz['id'])[:6]}@example.com"
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email=email, name="Dueña", password_hash=U._ph.hash("una-clave-larga"))
                           .returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid)
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    r = client.post("/login", data={"csrf": tok, "email": email, "password": "una-clave-larga"}, follow_redirects=False)
    assert r.headers["location"] == "/app"
    return tok


def test_friend_link_then_booking_records_both_rewards_once(client):
    biz, h, acc = shop(booking=True)
    ana = customer(biz, "Ana López", days=30)
    with db.tx() as conn:
        GR.save_settings(conn, biz, "t", reward_friend="10% en tu primer corte", reward_referrer="un corte gratis")
        link = GR.link_for(conn, biz, ana["id"])
        assert GR.link_for(conn, biz, ana["id"])["code"] == link["code"]      # one link per customer
    page = client.get(f"/f/{link['code']}", headers={"Accept-Language": "es"})
    assert page.status_code == 200 and "Ana te invitó" in page.text and "10% en tu primer corte" in page.text
    assert 'href="/b/estetica-sol"' in page.text and page.headers["x-robots-tag"] == "noindex"
    with db.tx() as conn:
        svc = BK.services(conn, biz["id"])[0]
    form = client.get(f"/b/estetica-sol/{svc['id']}").text
    tok = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    t = re.search(r'name="starts_at" value="([^"]+)"', form).group(1)
    done = client.post(f"/b/estetica-sol/{svc['id']}", data={"csrf": tok, "starts_at": t, "name": "Rosa", "phone": "9991112233"})
    assert done.status_code == 200
    refs = client.get("/v1/referrals", headers=h).json()
    assert len(refs) == 1
    r = refs[0]
    assert (r["referrer_name"], r["friend_name"], r["how"]) == ("Ana López", "Rosa", "booking")
    assert (r["reward_referrer"], r["reward_friend"]) == ("un corte gratis", "10% en tu primer corte")
    assert r["referrer_given_at"] is None and r["friend_given_at"] is None
    # the friend comes again through the same link: still one referral
    client.get(f"/f/{link['code']}")
    form = client.get(f"/b/estetica-sol/{svc['id']}").text
    t2 = re.findall(r'name="starts_at" value="([^"]+)"', form)[1]
    client.post(f"/b/estetica-sol/{svc['id']}", data={"csrf": tok, "starts_at": t2, "name": "Rosa", "phone": "9991112233"})
    assert len(client.get("/v1/referrals", headers=h).json()) == 1
    given = client.post(f"/v1/referrals/{r['id']}/given", headers=h, json={"who": "friend"}).json()
    again = client.post(f"/v1/referrals/{r['id']}/given", headers=h, json={"who": "friend"}).json()
    assert given["friend_given_at"] and again["friend_given_at"] == given["friend_given_at"] and not given["referrer_given_at"]
    assert client.post(f"/v1/referrals/{r['id']}/given", headers=h, json={"who": "me"}).status_code == 422
    with db.tx() as conn:
        assert GR.referral_stats(conn, biz["id"]) == {"links": 1, "opens": 2, "friends": 1, "rewards_pending": 1}
        ev = [e["type"] for e in conn.execute(select(db.events).where(db.events.c.business_id == biz["id"])).mappings()]
    assert ev.count("referral.joined") == 1 and ev.count("referral.rewarded") == 1


def test_self_referral_existing_customers_and_other_businesses_get_nothing(client):
    biz, h, acc = shop()
    other, _, _ = shop(name="Otra Tienda")
    with db.tx() as conn:
        GR.save_settings(conn, biz, "t", reward_friend="un regalo")
        ana = C.create(conn, biz, "t", {"name": "Ana", "phone": "9991234567"})
        old = C.create(conn, biz, "t", {"name": "Viejo", "phone": "9997654321"})
        conn.execute(update(db.contacts).where(db.contacts.c.id == old["id"]).values(created_at=NOW() - timedelta(days=60)))
        code = GR.link_for(conn, biz, ana["id"])["code"]
    client.get(f"/f/{code}")
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get(f"/f/{code}").text).group(1)
    for who, phone in (("Ana", "9991234567"), ("Viejo", "9997654321")):     # herself; someone who was already a customer
        r = client.post(f"/f/{code}", data={"csrf": tok, "name": who, "reach": phone}, follow_redirects=False)
        assert r.status_code == 303
    with db.tx() as conn:
        assert GR.referrals(conn, biz["id"]) == []
        ref = GR.remember({"code": code, "business_id": biz["id"]})
        stranger = C.create(conn, other, "t", {"name": "Lejos"})
        assert GR.claim(conn, other, ref, stranger, "form") is None          # another business's link
        late = C.create(conn, biz, "t", {"name": "Tarde"})
        stale = {**ref, "at": (NOW() - timedelta(days=GR.REMEMBER_DAYS + 1)).isoformat()}
        assert GR.claim(conn, biz, stale, late, "form") is None              # remembered too long ago
        assert GR.claim(conn, biz, {**ref, "c": "x" * 12}, late, "form") is None
        assert GR.claim(conn, biz, ref, late, "form")["how"] == "form"
        assert GR.claim(conn, biz, ref, late, "booking") is None              # once per friend
    r = client.post(f"/f/{code}", data={"csrf": tok, "name": "Nueva", "reach": "nueva@example.com"}, follow_redirects=False)
    assert r.headers["location"] == f"/f/{code}?sent=1"
    with db.tx() as conn:
        refs = GR.referrals(conn, biz["id"])
        inbox = conn.execute(select(db.messages.c.body).where(db.messages.c.direction == "in")).scalars().all()
    assert sorted(x["friend_name"] for x in refs) == ["Nueva", "Tarde"]
    assert any("Ana" in b and "trae a un amigo" in b for b in inbox)


def test_codes_cannot_be_guessed_or_walked(client):
    biz, h, acc = shop()
    with db.tx() as conn:
        codes = [GR.link_for(conn, biz, C.create(conn, biz, "t", {"name": f"N{n}"})["id"])["code"] for n in range(20)]
    assert len(set(codes)) == 20 and all(re.fullmatch(r"[A-Za-z0-9_-]{12}", c) for c in codes)
    assert all(str(biz["id"])[:8] not in c for c in codes)
    for bad in (codes[0][:-1] + ("A" if codes[0][-1] != "A" else "B"), "1", "000000000001", codes[0] + "x" * 5, "../etc", "%00"):
        assert client.get(f"/f/{bad}").status_code == 404
    with db.tx() as conn:
        assert conn.execute(select(db.referral_links.c.opens).where(db.referral_links.c.code == codes[0])).scalar() == 0


def test_share_message_after_a_visit_or_payment(client):
    biz, h, acc = shop()
    ana = customer(biz, "Ana", days=30)
    with db.tx() as conn:
        with pytest.raises(Invalid):                                          # a reward first
            GR.set_share(conn, biz, "t", True)
        GR.save_settings(conn, biz, "t", reward_friend="un postre", reward_referrer="")
        rule = GR.set_share(conn, biz, "t", True)
        with pytest.raises(Invalid):                                          # rewards never ride on a review request
            FU.create_automation(conn, biz, "t", trigger="appointment.done", minutes=0,
                                 message="Reseña {review_link} y amigo {referral_link}")
    assert rule["trigger"] == "appointment.done" and rule["minutes"] == 120 and rule["kind"] == "marketing"
    for n in range(2):
        ap = visit(biz, ana, 1)
        with db.tx() as conn:
            D.set_appointment_status(conn, biz["id"], "t", ap["id"], "done")
    gw = Gateway()
    res = FU.send_due(now=NOW() + timedelta(hours=3), http=gw.http)
    assert res["sent"] == 1 and res["skipped"] == 1                           # the link at most once every 90 days
    body = gw.sent[0]["body"]
    with db.tx() as conn:
        code = GR.link_of(conn, biz["id"], ana["id"])["code"]
    assert f"https://one.test/f/{code}" in body and body.count("un postre") == 2 and "Ana" in body
    assert any(r["error"] == "sent the friend link in the last 90 days" for r in outbox())
    with db.tx() as conn:                                                     # moved to "after a payment": still one rule
        GR.set_share(conn, biz, "t", True, when="payment.paid")
        assert [r["trigger"] for r in FU.automations(conn, biz["id"]) if r["preset"] == "referral"] == ["payment.paid"]
    bea = customer(biz, "Bea", days=30)
    PM.record_received(biz, "t", amount_minor=10000, description="Corte", contact_id=bea["id"])
    assert outbox()[-1]["contact_id"] == bea["id"]


def test_payment_paid_is_a_follow_up_trigger(client):
    biz, h, acc = shop()
    r = client.post("/v1/automations", headers=h, json={"trigger": "payment.paid", "minutes": 5, "message": "¡Gracias {name}!"})
    assert r.status_code == 201
    assert client.post("/v1/automations", headers=h, json={"trigger": "contact.quiet", "minutes": 5, "message": "x"}).status_code == 422
    c = customer(biz, "Ana")
    PM.record_received(biz, "t", amount_minor=10000, description="Corte", contact_id=c["id"])
    PM.record_received(biz, "t", amount_minor=10000, description="Sin cliente")
    assert len(outbox()) == 1 and outbox()[0]["contact_id"] == c["id"]


# ---------------------------------------------------------------- screens + API
@pytest.mark.parametrize("lang,words", [("en", ("Bring customers back", "Bring a friend", "Who brought whom")),
                                        ("es", ("Clientes que vuelven", "Trae a un amigo", "Quién trajo a quién"))])
def test_pages_render_in_each_language(client, monkeypatch, lang, words):
    from valeria_one import channels
    gw, real = Gateway(), channels.adapter
    monkeypatch.setattr(channels, "adapter", lambda ch, http=None: real(ch, gw.http))     # the gateway, answered in-process
    biz, h, acc = shop(booking=True)
    owner_session(client, biz)
    client.post("/lang/" + lang, data={"csrf": re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)})
    a = customer(biz, "Ana")
    page = client.get("/app/grow")
    assert page.status_code == 200 and all(w in page.text for w in words), page.text[:400]
    assert 'href="/app/grow"' in client.get("/app").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    client.post("/app/grow/settings", data={"csrf": tok, "offer": "2x1 el martes", "quiet_days": "50", "daily_cap": "", "gap_days": ""})
    client.post("/app/grow/winback", data={"csrf": tok, "on": "1"})
    client.post("/app/grow/settings", data={"csrf": tok, "reward_friend": "un regalo", "reward_referrer": ""})
    client.post("/app/grow/share", data={"csrf": tok, "on": "1", "when": "appointment.done"})
    page = client.get("/app/grow").text
    assert "2x1 el martes" in page and "Ana" in page and 'value="50"' in page
    ov = client.get("/v1/growth", headers=h).json()
    assert ov["winback"]["on"] and ov["share"]["on"] and ov["settings"]["daily_cap"] == 30 and ov["settings"]["quiet_days"] == 50
    contact = client.get(f"/app/contacts/{a['id']}")
    assert contact.status_code == 200 and 'action="/app/contacts/' in contact.text and "friend-link" in contact.text
    client.post(f"/app/contacts/{a['id']}/friend-link", data={"csrf": tok, "act": "show"})
    with db.tx() as conn:
        code = GR.link_of(conn, biz["id"], a["id"])["code"]
    assert f"/f/{code}" in client.get(f"/app/contacts/{a['id']}").text
    r = client.post(f"/app/contacts/{a['id']}/friend-link", data={"csrf": tok, "act": "send"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        assert GR.link_of(conn, biz["id"], a["id"])["last_sent_at"] is not None
    assert gw.sent[-1]["to"] == "id-Ana" and f"https://one.test/f/{code}" in gw.sent[-1]["body"] and "un regalo" in gw.sent[-1]["body"]
    start = client.get("/app/start").text
    assert "/app/grow" in start
    friend = client.get(f"/f/{code}")
    assert friend.status_code == 200 and "un regalo" in friend.text
    client.post("/app/grow/winback", data={"csrf": tok, "on": "0"})
    assert not client.get("/v1/growth", headers=h).json()["winback"]["on"]


def test_every_language_renders_the_grow_and_friend_pages(client):
    from valeria_one.web.i18n import UI_LANGS
    biz, h, acc = shop(booking=True)
    owner_session(client, biz)
    a = customer(biz, "Ana")
    customer(biz, "Beto", consent=False)
    with db.tx() as conn:
        GR.save_settings(conn, biz, "t", reward_friend="un regalo", offer="2x1")
        GR.set_winback(conn, biz, "t", True)
        GR.set_share(conn, biz, "t", True)
        code = GR.link_for(conn, biz, a["id"])["code"]
    for lang in UI_LANGS:
        tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)
        client.post(f"/lang/{lang}", data={"csrf": tok})
        for path in ("/app/grow", f"/app/contacts/{a['id']}", f"/f/{code}", "/app/followups", "/app/start"):
            r = client.get(path)
            assert r.status_code == 200 and not re.search(r"\{(n|days|sent|opens|pending|within)\}", r.text), (lang, path)   # all filled in


def test_grow_page_hidden_when_switched_off_and_for_staff(client):
    biz, h, acc = shop()
    owner_session(client, biz)
    client.patch("/v1/business", headers=h, json={"blocks": [b for b in blocks.BLOCKS if b != "grow"]})
    assert 'href="/app/grow"' not in client.get("/app").text
    r = client.get("/app/grow", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/start"


def test_api_growth_endpoints(client):
    biz, h, acc = shop()
    a = customer(biz, "Ana")
    r = client.patch("/v1/growth", headers=h, json={"quiet_days": 30, "offer": "Café gratis", "winback": True,
                                                    "winback_message": "Hola {name}, vuelve a {business}. {offer} {book}"})
    assert r.status_code == 200 and r.json()["winback"]["message"].startswith("Hola {name}")
    assert r.json()["quiet"]["count"] == 1
    assert client.patch("/v1/growth", headers=h, json={"share": True}).status_code == 422        # no reward yet
    assert client.patch("/v1/growth", headers=h, json={"daily_cap": 0}).status_code == 422
    assert client.patch("/v1/growth", headers=h, json={"surprise": 1}).status_code == 422
    q = client.get("/v1/growth/quiet", headers=h).json()
    assert q["rows"][0]["name"] == "Ana" and q["rows"][0]["can"] is True
    l1 = client.post(f"/v1/contacts/{a['id']}/referral-link", headers=h).json()
    l2 = client.post(f"/v1/contacts/{a['id']}/referral-link", headers=h).json()
    assert l1["code"] == l2["code"] and l1["url"] == f"https://one.test/f/{l1['code']}"
    other, h2, _ = shop(name="Otra")
    assert client.post(f"/v1/contacts/{a['id']}/referral-link", headers=h2).status_code == 404   # another business's customer
    assert client.get("/v1/referrals", headers=h2).json() == []
    with db.tx() as conn:
        exported = __import__("valeria_one.services.export", fromlist=["x"]).business_data(conn, biz["id"], "t")
    assert len(exported["referral_links"]) == 1 and exported["growth_settings"][0]["offer"] == "Café gratis"


def test_sandbox_demo_renders_grow_page(client, monkeypatch):
    monkeypatch.setenv("VO_SANDBOX", "1")
    from valeria_one.sandbox import seed
    d = seed.seed()[0]
    client.get(f"/demo/{d['business_id']}")
    assert client.get("/app/grow").status_code == 200
    page = client.get("/app/grow").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/app/grow/winback", data={"csrf": tok, "on": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/app/start"                             # a shared demo stays as it is
    with db.tx() as conn:
        assert GR.ready_made(conn, d["business_id"], "winback") is None
