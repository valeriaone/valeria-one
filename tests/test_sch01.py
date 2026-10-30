"""sch01 (v0.23): a time picked in the chat lands in both calendars.

Valeria offers only times that are really free ("Today 5:00–7:00 PM or tomorrow …"), reads the answer by rules in the 11
languages (a number, a tap, "tomorrow at 10", "明日の18時"; morning-or-evening asked, never guessed), books it, and the
confirmation carries a signed .ics and a Google Calendar link (wrong signature or another business: 404). "Can we do 4pm
instead?" moves it — same UID, SEQUENCE +1, the old time free again, the reminder follows; "cancel" frees it. The owner
hears each on their chat, has a private calendar link for their phone, gets "in 60 min" before, and "did they come?" after
(yes on a Marketplace item -> "Mark it sold?"). A reminder a chat's 24 h window blocks is skipped and the owner is told.
Telegram, Messenger and the gateway are all mocked: nothing leaves the machine."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from _va import ALL_WEEK, Net, gw_in, link_owner, refuse_network, shop, tg_in
from conftest import make_business
from test_mkt01 import add, fb_in, setup
from test_web import signup
from valeria_one import db
from valeria_one.services import booking as BK
from valeria_one.services import chatbook as CB
from valeria_one.services import chatcal as CC
from valeria_one.services import deals as D
from valeria_one.services import followups as FU
from valeria_one.services import owner as OW
from valeria_one.services import reminders as RM

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def bookable(biz, hours=None, tz=None):
    with db.tx() as conn:
        if tz:
            conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(timezone=tz))
            biz = {**biz, "timezone": tz}
        return BK.save_settings(conn, biz, "t", enabled=True, hours=hours or ALL_WEEK, notice_minutes=0, days_ahead=30)


def tomorrow_at(biz, h, m=0):
    tz = ZoneInfo(biz["timezone"])
    return datetime.combine(NOW().astimezone(tz).date() + timedelta(days=1), datetime.min.time().replace(hour=h, minute=m), tz)


def appts(biz):
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(db.appointments).where(db.appointments.c.business_id == biz["id"])
                                              .order_by(db.appointments.c.created_at)).mappings()]


def n(text):
    """Babel puts a narrow no-break space before AM/PM; plain spaces to compare."""
    return (text or "").replace("\u202f", " ").replace("\xa0", " ")


def owner_said(net):
    return [n(t) for t in net.owner_texts()]


def links_in(text):
    return re.search(r"(https://one\.test/b/[^/\s]+/ics/[^\s]+)", text).group(1), re.search(r"(https://one\.test/b/[^/\s]+/gcal/[^\s]+)", text).group(1)


def path(url):
    return url.replace("https://one.test", "")


def ics_of(client, url):
    r = client.get(path(url))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar"), r.text[:200]
    return r.text


def field(ics, name):
    return re.search(rf"^{name}:(.*)$", ics.replace("\r\n ", ""), re.M).group(1).strip()


# ================================================================ reading a time (rules, 11 languages)
def test_times_in_plain_words_in_many_languages():
    fri = date(2026, 10, 30)                                     # a Friday
    sat, sun = fri + timedelta(days=1), fri + timedelta(days=2)
    cases = {
        "tomorrow at 10am": (sat, 10, 0, True), "today 6:30pm": (fri, 18, 30, True), "18h": (None, 18, 0, True),
        "mañana a las 10 de la mañana": (sat, 10, 0, True), "el viernes a las 6 de la tarde": (fri, 18, 0, True),
        "pasado mañana 11am": (sun, 11, 0, True), "hoje às 18": (fri, 18, 0, True), "demain à 10h": (sat, 10, 0, False),
        "morgen um 18 Uhr": (sat, 18, 0, True), "übermorgen 9:30": (sun, 9, 30, False), "bukas ng 3 ng hapon": (sat, 15, 0, True),
        "besok jam 10 pagi": (sat, 10, 0, True), "कल 6 बजे शाम": (sat, 18, 0, True), "غدا الساعة 6 مساء": (sat, 18, 0, True),
        "明日の18時": (sat, 18, 0, True), "明天下午6点": (sat, 18, 0, True), "明後日10時半": (sun, 10, 30, False),
        "noon tomorrow": (sat, 12, 0, True), "sábado 10": (sat, 10, 0, False),
    }
    for text, (d, h, m, sure) in cases.items():
        got = CC.parse(text, fri, loose=True)
        assert got == {"day": d, "h": h, "m": m, "sure": sure}, (text, got)
    assert CC.parse("can we do 7 instead?", fri, loose=True) == {"day": None, "h": 7, "m": 0, "sure": False}
    for text in ("Is the sofa still available?", "$150", "I am free", "would you take 150.00?"):
        assert CC.parse(text, fri) is None, text
    assert CC.parse("6", fri) is None                                              # a lone number is a choice, not a time
    for text in ("I'll be 10 minutes later", "in 2 hours", "2 jam lagi", "10分遅れます"):            # how long, not what time
        assert CC.parse(text, fri, loose=True) is None, text
    assert CC.parse("a las 18 hrs", fri) == {"day": None, "h": 18, "m": 0, "sure": True}
    assert CC.is_change("can we do 7 instead?") and CC.is_change("¿mejor a las 7?") and CC.is_change("7時に変更できますか")
    assert CC.is_cancel("sorry I need to cancel") and CC.is_cancel("cancelar por favor") and CC.is_cancel("キャンセルします")
    assert not CC.is_cancel("is it still available?") and not CC.is_change("thanks!")


def test_morning_or_evening_comes_from_the_opening_hours_and_both_open_means_ask():
    biz, _ = make_business("US", "shop", name="Muebles")
    biz = bookable(biz, tz="America/New_York")
    now = datetime(2026, 10, 30, 9, 0, tzinfo=NY)
    with db.tx() as conn:
        six = CC.parse("tomorrow at 6", now.date(), loose=True)
        assert CC.resolve(conn, biz, 30, six, now) == ("at", datetime(2026, 10, 31, 18, 0, tzinfo=NY))   # open 08–20: the evening
        early = {**biz, "hours": {d: [["05:00", "21:00"]] for d in BK.DAYS}}
        r = CC.resolve(conn, early, 30, six, now)                                   # open at both: ask, never guess
        assert r[0] == "ampm" and [x.hour for x in r[1]] == [6, 18]
        assert CC.resolve(conn, biz, 30, CC.parse("tomorrow at 9pm", now.date()), now)[0] == "busy"   # closed then
        assert CC.resolve(conn, biz, 30, CC.parse("tomorrow", now.date()), now) == ("day", date(2026, 10, 31))


# ================================================================ offer mode: only what is free
def test_offer_mode_proposes_only_free_times():
    biz, _ = make_business("US", "shop", name="Muebles")
    biz = bookable(biz, hours={d: [["09:00", "12:00"], ["17:00", "19:00"]] for d in BK.DAYS}, tz="America/New_York")
    now = datetime(2026, 10, 30, 17, 40, tzinfo=NY)                                  # Friday 5:40 PM
    with db.tx() as conn:
        D.book(conn, biz, "t", {"title": "Taken", "starts_at": datetime(2026, 10, 31, 10, 0, tzinfo=NY),
                                "ends_at": datetime(2026, 10, 31, 11, 0, tzinfo=NY)})
        text, opts = CC.offer(conn, biz, 30, "en", now)
        text = n(text)
        assert text == "today 5:45 PM–7:00 PM or tomorrow 9:00 AM–10:00 AM, 11:00 AM–12:00 PM"
        assert 2 <= len(opts) <= CC.OPTIONS
        for o in opts:
            st = datetime.fromisoformat(o["id"])
            assert BK.fits(conn, biz, 30, st, now), o
            assert not (datetime(2026, 10, 31, 9, 31, tzinfo=NY) <= st < datetime(2026, 10, 31, 11, 0, tzinfo=NY)), o
        assert n(opts[0]["title"]).startswith("today ") and any(n(o["title"]).startswith("tomorrow ") for o in opts)
        es, _ = CC.offer(conn, biz, 30, "es", now)
        assert es.startswith("hoy ") and " o mañana " in es


# ================================================================ a Marketplace buyer: offered times, booked, both calendars
def market(net_too=True):
    biz, tg, fb, owner, net = setup()
    biz = bookable(biz)
    sofa = add(biz)
    return biz, tg, fb, owner, net, sofa


def test_a_marketplace_buyer_picks_a_time_and_gets_ics_and_google_links_and_the_owner_hears_it(client):
    biz, tg, fb, owner, net, sofa = market()
    st, out = fb_in(fb, "PSID-JUAN", "Hi! Is the black sofa still available?", net)
    assert out == ["marketplace:answered"]
    said = n(net.to("PSID-JUAN")[-1])
    assert "still available" in said and "When works for you?" in said and "1) " in said and "When could you come?" not in said
    with db.tx() as conn:
        flow = conn.execute(select(db.conversations.c.flow)).scalar()
    assert flow["step"] == "offer" and flow["item"] == str(sofa["id"]) and flow["minutes"] == CC.PICKUP_MINUTES
    for o in flow["options"]:                                                        # only free times
        with db.tx() as conn:
            assert BK.fits(conn, biz, CC.PICKUP_MINUTES, datetime.fromisoformat(o["id"]))
    net.tg.clear()
    st, out = fb_in(fb, "PSID-JUAN", "tomorrow at 10am", net)
    assert out == ["booking:booked"]
    a = appts(biz)[0]
    assert a["starts_at"] == tomorrow_at(biz, 10) and a["item_id"] == sofa["id"] and a["sequence"] == 0
    assert a["title"] == "🛋 Black sofa · Pickup" and a["location"] == "Centro" and a["conversation_id"]
    conf = n(net.to("PSID-JUAN")[-1])
    assert conf.startswith("Booked ✓ 🛋 Black sofa · Pickup — tomorrow 10:00 AM") and "Add to my calendar" in conf and "cancel" in conf
    ics_url, g_url = links_in(conf)
    ics = ics_of(client, ics_url)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert field(ics, "UID") == f"{a['id']}@valeria-one" and field(ics, "SEQUENCE") == "0" and field(ics, "STATUS") == "CONFIRMED"
    assert field(ics, "DTSTART") == a["starts_at"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    assert "Black sofa" in field(ics, "SUMMARY") and "Juan" not in ics and "PSID" not in ics       # nothing they don't know
    r = client.get(path(g_url), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://calendar.google.com/calendar/render?")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["action"] == ["TEMPLATE"] and q["dates"][0].startswith(a["starts_at"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    assert client.get(path(ics_url)[:-4] + "zzzz").status_code == 404                                # wrong signature
    assert client.get(path(g_url)[:-4] + "zzzz", follow_redirects=False).status_code == 404
    ping = owner_said(net)[-1]                                                                      # the owner, naming the item
    assert ping.startswith("🛋 Black sofa · Pickup: Buyer #1 — tomorrow 10:00 AM") and "calendar" in ping
    with db.tx() as conn:
        b = conn.execute(select(db.marketplace_buyers)).mappings().one()
        assert b["status"] == "coming" and "10:00" in n(b["coming"])
        assert conn.execute(select(db.conversations.c.flow)).scalar() is None
        assert not BK.fits(conn, biz, 30, tomorrow_at(biz, 10))                                        # taken now


def test_another_business_never_opens_the_link():
    biz, tg, fb, owner, net, sofa = market()
    other, _ = make_business("US", "shop", name="Otra")
    other = bookable(other)
    fb_in(fb, "PSID-1", "Is the black sofa still available?", net)
    fb_in(fb, "PSID-1", "tomorrow at 11am", net)
    a = appts(biz)[0]
    s = CC.sig(a["id"])
    from fastapi.testclient import TestClient
    from valeria_one.api import app
    c = TestClient(app, raise_server_exceptions=False, base_url="https://testserver")
    assert c.get(f"/b/{biz['slug']}/ics/{a['id']}/{s}").status_code == 200
    assert c.get(f"/b/{other['slug']}/ics/{a['id']}/{s}").status_code == 404                        # other business: 404
    assert c.get(f"/b/{other['slug']}/gcal/{a['id']}/{s}", follow_redirects=False).status_code == 404
    with db.tx() as conn:
        assert CC.upcoming(conn, other["id"], a["conversation_id"], NOW()) is None


# ================================================================ move and cancel from the chat
def chat_booking(lang_text="tomorrow at 10am", **kw):
    biz, tg, gw = shop(country="US", name="Sol")
    net = Net()
    owner = link_owner(biz, tg, net=net)
    net.tg.clear()
    with db.tx() as conn:
        rule = RM.set_on(conn, biz, "t", True, hours=2)
    st, out = gw_in(gw, "+15550001111", "hello", name="Ana López", net=net)
    conv_id = st["conversation_ids"][0]
    assert CB.start_offer(biz["id"], conv_id, what="Estimate visit", minutes=60, http=net.http) == "asked"
    assert "When works for you?" in net.gw[-1]["body"]
    st, out = gw_in(gw, "+15550001111", lang_text, net=net)
    return biz, gw, net, owner, conv_id, out, rule


def test_reschedule_keeps_the_uid_bumps_sequence_frees_the_old_time_and_moves_the_reminder(client):
    biz, gw, net, owner, conv_id, out, rule = chat_booking()
    assert out == ["booking:booked"]
    a = appts(biz)[0]
    ics_url, _ = links_in(net.gw[-1]["body"])
    with db.tx() as conn:
        sm = conn.execute(select(db.scheduled_messages).where(db.scheduled_messages.c.subject_id == a["id"])).mappings().one()
    assert sm["send_at"] == a["starts_at"] - timedelta(hours=2)
    net.tg.clear()
    st, out = gw_in(gw, "+15550001111", "can we do 4pm instead?", net=net)
    assert out == ["booking:moved"]
    b = appts(biz)[0]
    assert b["id"] == a["id"] and b["starts_at"] == tomorrow_at(biz, 16) and b["sequence"] == 1 and b["synced_at"] is None
    assert n(net.gw[-1]["body"]).startswith("Done ✓ Estimate visit is now tomorrow 4:00 PM") and ics_url in net.gw[-1]["body"]
    ics = ics_of(client, ics_url)                                                                     # the same link, the new time
    assert field(ics, "UID") == f"{a['id']}@valeria-one" and field(ics, "SEQUENCE") == "1"
    assert field(ics, "DTSTART") == b["starts_at"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with db.tx() as conn:
        assert BK.fits(conn, biz, 60, tomorrow_at(biz, 10)) and not BK.fits(conn, biz, 60, tomorrow_at(biz, 16))
        sm = conn.execute(select(db.scheduled_messages).where(db.scheduled_messages.c.subject_id == a["id"])).mappings().one()
    assert sm["send_at"] == b["starts_at"] - timedelta(hours=2)                                         # the reminder followed
    assert "Ana moved it to tomorrow 4:00 PM (was tomorrow 10:00 AM)" in owner_said(net)[-1]


def test_a_time_that_is_not_free_gets_the_closest_free_ones_and_ambiguity_is_asked():
    biz, gw, net, owner, conv_id, out, rule = chat_booking("tomorrow at 9pm")                         # closed at 9 pm
    assert out == ["booking:asked"] and not appts(biz)
    assert n(net.gw[-1]["body"]).startswith("tomorrow 9:00 PM isn't free, sorry.") and "1) " in net.gw[-1]["body"]
    with db.tx() as conn:
        conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(hours={d: [["05:00", "21:00"]] for d in BK.DAYS}))
    st, out = gw_in(gw, "+15550001111", "tomorrow at 6", net=net)
    assert out == ["booking:asked"] and net.gw[-1]["body"].startswith("Morning or evening?")
    st, out = gw_in(gw, "+15550001111", "2", net=net)
    assert out == ["booking:booked"] and appts(biz)[0]["starts_at"] == tomorrow_at(biz, 18)


def test_a_day_alone_shows_that_days_times_and_change_words_without_a_time_are_left_alone():
    biz, gw, net, owner, conv_id, out, rule = chat_booking("tomorrow")
    assert out == ["booking:asked"] and not appts(biz)                                                # never "the first slot tomorrow"
    assert n(net.gw[-1]["body"]).startswith("What time tomorrow? These are free.")
    st, out = gw_in(gw, "+15550001111", "10am", net=net)
    assert out == ["booking:booked"] and appts(biz)[0]["starts_at"] == tomorrow_at(biz, 10)
    st, out = gw_in(gw, "+15550001111", "can I change the color of the sofa?", net=net)               # no time in it: not a move
    assert out != ["booking:moved"] and appts(biz)[0]["starts_at"] == tomorrow_at(biz, 10) and appts(biz)[0]["sequence"] == 0


def test_cancel_frees_the_time_and_both_sides_hear(client):
    biz, gw, net, owner, conv_id, out, rule = chat_booking()
    ics_url, g_url = links_in(net.gw[-1]["body"])
    net.tg.clear()
    st, out = gw_in(gw, "+15550001111", "Sorry, I have to cancel", net=net)
    assert out == ["booking:cancelled"]
    a = appts(biz)[0]
    assert a["status"] == "cancelled" and a["sequence"] == 1
    assert n(net.gw[-1]["body"]).startswith("Cancelled ✓ Estimate visit — tomorrow 10:00 AM")
    assert "Ana cancelled (tomorrow 10:00 AM). That time is free again." in owner_said(net)[-1]
    ics = ics_of(client, ics_url)
    assert field(ics, "STATUS") == "CANCELLED" and field(ics, "SEQUENCE") == "1"
    assert client.get(path(g_url), follow_redirects=False).status_code == 404
    with db.tx() as conn:
        assert BK.fits(conn, biz, 60, tomorrow_at(biz, 10))
    st, out = gw_in(gw, "+15550001111", "cancel", net=net)                                            # nothing left to cancel
    assert out != ["booking:cancelled"]


def test_the_service_flow_takes_a_written_time_and_its_confirmation_has_the_links():
    biz, tg, gw = shop(country="US", name="Sol")
    st, out = gw_in(gw, "+15550002222", "I want to book", name="Rosa")
    assert out == ["booking:asked"]

    def last_out():
        with db.tx() as conn:
            return n(conn.execute(select(db.messages.c.body).where(db.messages.c.direction == "out")
                                  .order_by(db.messages.c.created_at.desc())).scalars().first())
    assert last_out().startswith("Corte — Which day?\nFree: ") and "tomorrow 8:00 AM–8:00 PM" in last_out()   # the free stretches
    with db.tx() as conn:
        conv = conn.execute(select(db.conversations)).mappings().one()
    assert conv["flow"]["step"] == "day"
    st, out = gw_in(gw, "+15550002222", "tomorrow at 11am")                                          # written, not a number
    assert out == ["booking:asked"] and "phone" in last_out()                                         # no phone on this chat yet
    st, out = gw_in(gw, "+15550002222", "+1 202 555 0143")
    assert out == ["booking:booked"] and last_out().startswith("Booked! Corte — ") and "/ics/" in last_out() and "/gcal/" in last_out()
    a = appts(biz)[0]
    assert a["starts_at"] == tomorrow_at(biz, 11) and a["conversation_id"] == conv["id"]


# ================================================================ reminders both sides, inside the 24 h rules
def test_reminders_respect_the_24h_window_and_the_owner_is_told_when_one_cannot_go():
    biz, tg, fb, owner, net, sofa = market()
    with db.tx() as conn:
        RM.set_on(conn, biz, "t", True, hours=2)
    fb_in(fb, "PSID-ANA", "Is the black sofa still available?", net)
    fb_in(fb, "PSID-ANA", "tomorrow at 10am", net)
    a = appts(biz)[0]
    with db.tx() as conn:
        sm = conn.execute(select(db.scheduled_messages).where(db.scheduled_messages.c.subject_id == a["id"])).mappings().one()
        conn.execute(update(db.conversations).values(last_inbound_at=sm["send_at"] - timedelta(hours=30)))   # 30 h since they wrote
    net.tg.clear()
    sent_before = len(net.fb)
    FU.send_due(now=sm["send_at"] + timedelta(minutes=1), http=net.http)
    with db.tx() as conn:
        row = conn.execute(select(db.scheduled_messages).where(db.scheduled_messages.c.id == sm["id"])).mappings().one()
    assert row["status"] == "skipped" and row["error"] == "outside_reply_window" and len(net.fb) == sent_before
    assert any("couldn't send Buyer" in (t or "") and "24 hours" in (t or "") for t in net.owner_texts())
    FU.send_due(now=sm["send_at"] + timedelta(minutes=2), http=net.http)                             # told once
    assert sum("24 hours" in (t or "") for t in net.owner_texts()) == 1
    # inside the window it goes out, and "1" confirms it
    with db.tx() as conn:
        conn.execute(update(db.scheduled_messages).where(db.scheduled_messages.c.id == sm["id"]).values(status="pending", error=None))
        conn.execute(update(db.conversations).values(last_inbound_at=sm["send_at"] - timedelta(hours=1)))
    FU.send_due(now=sm["send_at"] + timedelta(minutes=3), http=net.http)
    assert len(net.fb) == sent_before + 1 and "Reply 1 to confirm" in net.fb[-1]["message"]["text"]
    # the owner's side: "in 60 min", once
    net.tg.clear()
    assert CC.round_(now=a["starts_at"] - timedelta(minutes=30), http=net.http)["soon"] == 1
    assert owner_said(net)[-1].startswith("⏰ In 60 min — 🛋 Black sofa · Pickup: Buyer #1")
    assert CC.round_(now=a["starts_at"] - timedelta(minutes=20), http=net.http)["soon"] == 0


# ================================================================ afterwards: did they come? -> Mark sold
def test_after_the_pickup_the_owner_is_asked_and_yes_offers_mark_sold():
    biz, tg, fb, owner, net, sofa = market()
    fb_in(fb, "PSID-JUAN", "Is the black sofa still available?", net)
    fb_in(fb, "PSID-JUAN", "tomorrow at 10am", net)
    fb_in(fb, "PSID-ROSA", "Is the black sofa still available?", net)                                 # another buyer
    a = [x for x in appts(biz)][0]
    with db.tx() as conn:                                                                             # the owner dealt with the pings
        conn.execute(update(db.assistant_actions).where(db.assistant_actions.c.status == "asked").values(status="done"))
    later = a["ends_at"] + timedelta(minutes=40)
    assert CC.round_(now=later, http=net.http)["asked"] == 1
    assert CC.round_(now=later, http=net.http)["asked"] == 0                                          # once
    net.tg.clear()
    assert OW.prompt_due(now=later + timedelta(minutes=1), http=net.http)["asked"] == 1
    q = owner_said(net)[-1]
    assert q.startswith("Did Buyer #1 come? 🛋 Black sofa · Pickup") and "1) Yes, came" in q and "2) No-show" in q
    st, ans = tg_in(tg, "900", "1", net=net)
    assert appts(biz)[0]["status"] == "done"
    said = owner_said(net)[-1]
    assert said.startswith("Marked as done ✓") and "Mark Black sofa sold to Buyer #1?" in said
    st, ans = tg_in(tg, "900", "1", net=net)
    OW.run_due(now=NOW() + timedelta(seconds=OW.UNDO_SECONDS + 1), http=net.http)
    with db.tx() as conn:
        assert conn.execute(select(db.marketplace_items.c.status)).scalar() == "sold"
        st = {r.ref: r.status for r in conn.execute(select(db.marketplace_buyers.c.ref, db.marketplace_buyers.c.status))}
    assert st == {1: "bought", 2: "told_sold"}
    assert "sold" in net.to("PSID-ROSA")[-1].lower()


def test_did_not_come_marks_a_no_show():
    biz, gw, net, owner, conv_id, out, rule = chat_booking()
    a = appts(biz)[0]
    later = a["ends_at"] + timedelta(minutes=40)
    CC.round_(now=later, http=net.http)
    OW.prompt_due(now=later + timedelta(minutes=1), http=net.http)
    with db.tx() as conn:
        tg = conn.execute(select(db.channel_accounts).where(db.channel_accounts.c.business_id == biz["id"],
                                                            db.channel_accounts.c.channel == "telegram")).mappings().one()
    tg_in(dict(tg), "900", "no", net=net)
    assert appts(biz)[0]["status"] == "no_show" and owner_said(net)[-1] == "Marked as no-show."


# ================================================================ the owner's own calendar: a private link per person
def test_the_owner_feed_shows_new_bookings_and_a_new_link_kills_the_old(client):
    signup(client, country="US", industry="shop", email="ana@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        user = dict(conn.execute(select(db.users)).mappings().one())
    page = client.get("/app/calendar").text
    feed = re.search(r'<code class="key"[^>]*>(https://one\.test/cal/[^<]+\.ics)</code>', page).group(1)
    assert 'href="webcal://one.test/cal/' in page
    st = tomorrow_at(biz, 15)
    with db.tx() as conn:
        a = D.book(conn, biz, "t", {"title": "Showing — 12 Palm St", "starts_at": st, "ends_at": st + timedelta(minutes=45)})
    ics = ics_of(client, feed)
    assert f"UID:{a['id']}@valeria-one" in ics and "Showing — 12 Palm St" in ics and "X-WR-CALNAME:" in ics
    assert client.get(path(feed)[:-8] + "0000.ics").status_code == 404
    other, _ = make_business("US", "shop", name="Otra")                                               # another business, same person
    assert client.get(f"/cal/{other['id']}/{user['id']}/{CC.feed_sig(other['id'], user['id'], 0)}.ics").status_code == 404
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert client.post("/app/calendar/feed/new", data={"csrf": tok}, follow_redirects=False).status_code == 303
    assert client.get(path(feed)).status_code == 404                                                  # the old link is dead
    new = re.search(r'<code class="key"[^>]*>(https://one\.test/cal/[^<]+\.ics)</code>', client.get("/app/calendar").text).group(1)
    assert new != feed and f"UID:{a['id']}" in ics_of(client, new)
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", a["id"], "cancelled")
    assert "STATUS:CANCELLED" in ics_of(client, new) and "SEQUENCE:1" in ics_of(client, new)         # phones take it off


def test_signed_out_pages_send_to_sign_in(client):
    for p in ("/app/calendar",):
        assert client.get(p, follow_redirects=False).status_code == 303
    assert client.post("/app/calendar/feed/new", follow_redirects=False).status_code == 303
    assert client.get(f"/cal/{NOW().timestamp():.0f}/x/y.ics").status_code in (404, 422)


def test_the_kits_own_word_names_it_when_nothing_else_does():
    for industry, lang, word in (("realtor", "en", "Showing"), ("shop", "es", "Entrega"), ("contractor", "en", "Visit")):
        biz, tg, gw = shop(country="US", name=f"K {industry}")
        with db.tx() as conn:
            conn.execute(update(db.businesses).where(db.businesses.c.id == biz["id"]).values(industry=industry, language=lang))
        st, _ = gw_in(gw, f"+1555000{len(industry):04d}", "hello", name="Kim")
        assert CB.start_offer(biz["id"], st["conversation_ids"][0], minutes=45, http=Net().http) == "asked"
        with db.tx() as conn:
            flow = conn.execute(select(db.conversations.c.flow).where(db.conversations.c.id == st["conversation_ids"][0])).scalar()
        assert flow["what"] == word and flow["minutes"] == 45


# ================================================================ time zones: the right moment, summer time or not
def test_ics_times_are_utc_and_right_across_a_summer_time_change():
    biz, _tg, gw_acc = shop(country="US", name="Sol")
    biz = bookable(biz, tz="America/New_York")
    st, out = gw_in(gw_acc, "+15550003333", "hi", name="Lee")
    with db.tx() as conn:
        conv = dict(conn.execute(select(db.conversations).where(db.conversations.c.id == st["conversation_ids"][0])).mappings().one())
        now = datetime(2026, 10, 31, 12, 0, tzinfo=NY)                                             # the day before clocks go back
        sat = CC.book(conn, biz, conv=conv, title="Visit", minutes=30, start=datetime(2026, 10, 31, 15, 0, tzinfo=NY), now=now)
        sun = CC.book(conn, biz, conv=conv, title="Visit", minutes=30, start=datetime(2026, 11, 1, 15, 0, tzinfo=NY), now=now)
    assert field(BK.ics(biz, sat), "DTSTART") == "20261031T190000Z"                                   # EDT, UTC−4
    assert field(BK.ics(biz, sun), "DTSTART") == "20261101T200000Z"                                   # EST, UTC−5
    assert "dates=20261101T200000Z%2F20261101T203000Z" in CC.google_url(biz, sun)
    tokyo, _ = make_business("JP", "salon", name="東京")
    tokyo = bookable(tokyo, tz="Asia/Tokyo")
    with db.tx() as conn:
        text, _ = CC.offer(conn, tokyo, 60, "ja", datetime(2026, 10, 30, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    assert text.startswith("今日 9:00–20:00")


# ================================================================ words: all 11 languages
def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith("sc_")} | {"va_c_came", "va_c_no_show"}
    assert len(keys) >= 35
    used = set()
    for p in ("valeria_one/services/chatcal.py", "valeria_one/services/chatbook.py", "valeria_one/services/actions.py",
              "valeria_one/services/followups.py", "valeria_one/services/marketplace.py", "valeria_one/web/__init__.py",
              "valeria_one/web/templates/calendar.html", "valeria_one/web/templates/book.html"):
        used |= set(re.findall(r"sc_[a-z0-9_]+[a-z0-9]", (ROOT / p).read_text(encoding="utf-8")))
    assert used <= keys, used - keys
    assert {"sc_ping_booked", "sc_ping_moved", "sc_ping_cancelled", "sc_ping_soon", "sc_q_came", "sc_rem_missed"} <= used
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)   # same order too
    for k in keys:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(frozenset, files.values()))) == 1
