"""bk01 (v0.26): booking for a team, classes / tables with seats, deposits and no-show fees, the waitlist.

A business where nobody "takes bookings" works exactly as before (one lane, the same code path); with a team, each
person is a lane (their shifts / own hours / opening hours, minus time off), "anyone available" is fair; seats per time
with the party size; the deposit's pay link on the business's own account holds the time, a paid deposit confirms
(with a signed contract when that is asked too), the hold runs out and frees it; no-show fee; the waitlist is told."""
import json
import re
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, text, update

from _va import ALL_WEEK, LANGS, Net, gw_in, refuse_network, shop
from conftest import make_business
from test_payments import FakePSP, sig256
from valeria_one import db, kits, packs
from valeria_one.services import Invalid, NotFound
from valeria_one.services import booking as BK
from valeria_one.services import bookpay as BP
from valeria_one.services import chatcal as CC
from valeria_one.services import contacts as C
from valeria_one.services import deals as D
from valeria_one.services import export as EX
from valeria_one.services import followups as FU
from valeria_one.services import gcal
from valeria_one.services import payments as PM
from valeria_one.services import reminders as RM
from valeria_one.services import staff as SF

ROOT = Path(__file__).resolve().parent.parent
NOW = lambda: datetime.now(timezone.utc)                     # noqa: E731


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


@pytest.fixture
def psp(monkeypatch):
    f = FakePSP()
    c = httpx.Client(transport=httpx.MockTransport(f))
    monkeypatch.setattr(PM, "_http", lambda http=None: http or c)
    return f


def biz_with(industry="salon", country="MX", hours=None, name="Salón Prueba", slug=None):
    biz, key = make_business(country, industry, name=name)
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", enabled=True, slug=slug or f"s-{str(biz['id'])[:8]}", hours=hours or ALL_WEEK,
                               notice_minutes=0, days_ahead=30)
        svc = BK.add_service(conn, biz, "t", name="Corte", minutes=60, price_minor=20000)
    return biz, key, svc


def person(biz, name, bookable=True, **kw):
    with db.tx() as conn:
        m = SF.add_member(conn, biz, "t", {"name": name})
        return SF.set_booking(conn, biz, "t", m["id"], bookable=bookable, **kw)


def day_ahead(biz, n=1, weekday_max=5):
    """A local day n+ days ahead whose next day is in the same week (Mon–Sat)."""
    tz = ZoneInfo(biz["timezone"])
    d = NOW().astimezone(tz).date() + timedelta(days=n)
    while d.weekday() > weekday_max:
        d += timedelta(days=1)
    return d


def at(biz, d, h, m=0):
    return datetime.combine(d, time(h, m), ZoneInfo(biz["timezone"]))


def hhmm(ts):
    return [t.strftime("%H:%M") for t in ts]


def book(biz, svc, start, name="Ana", phone=None, **kw):
    phone = phone or ("+12025550100" if biz["country"] == "US" else "9991234567")
    with db.tx() as conn:
        return BK.book(conn, biz, service_id=svc["id"], starts_at=start, name=name, phone=phone, **kw)


def appt_row(aid):
    with db.tx() as conn:
        return dict(conn.execute(select(db.appointments).where(db.appointments.c.id == aid)).mappings().one())


def tok(client, path="/app"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def old_free(conn, biz, minutes, day, now):
    """The slot engine exactly as it was in v0.25.0 (one lane), to prove nothing changed for a business without a team."""
    tz = ZoneInfo(biz["timezone"])
    today = now.astimezone(tz).date()
    if day < today or day > today + timedelta(days=biz["booking_days_ahead"]):
        return []
    spans = (biz["hours"] or {}).get(BK.DAYS[day.weekday()]) or []
    if not spans:
        return []
    earliest = now + timedelta(minutes=biz["booking_notice_minutes"])
    dur = timedelta(minutes=minutes)
    day_start = datetime.combine(day, time(0), tz)
    AP = db.appointments
    busy = [(r.starts_at, r.ends_at) for r in conn.execute(select(AP.c.starts_at, AP.c.ends_at).where(
        AP.c.business_id == biz["id"], AP.c.status.in_(("booked", "confirmed")), AP.c.starts_at < day_start + timedelta(days=1, hours=2),
        AP.c.ends_at > day_start))]
    out = []
    for a, b in spans:
        t = datetime.combine(day, time.fromisoformat(a), tz)
        close = datetime.combine(day, time.fromisoformat(b), tz)
        while t + dur <= close:
            if t >= earliest and not any(s < t + dur and e > t for s, e in busy):
                out.append(t)
            t += timedelta(minutes=BK.STEP_MINUTES)
    return out


# ================================================================ a business without a team: exactly as before
def test_a_business_nobody_takes_bookings_at_works_exactly_as_before():
    hours = {d: [["09:00", "13:00"], ["15:00", "19:00"]] for d in BK.DAYS}
    biz, _, svc = biz_with(hours=hours)
    d = day_ahead(biz)
    person(biz, "Cajera", bookable=False)                                          # a team on the schedule, nobody bookable
    with db.tx() as conn:
        SF.add_shift(conn, biz, "t", SF.members(conn, biz["id"])[0]["id"], d, "09:00", "11:00")
    book(biz, svc, at(biz, d, 10))
    D_ = D.book
    with db.tx() as conn:
        D_(conn, biz, "t", {"title": "Bloqueo", "starts_at": at(biz, d, 16), "ends_at": at(biz, d, 17, 30)})
    now = NOW()
    with db.tx() as conn:
        for k in range(0, 5):
            day = d + timedelta(days=k)
            assert BK.slots(conn, biz, svc["id"], day, now) == old_free(conn, biz, 60, day, now), day
            assert BK.free(conn, biz, 30, day, now) == old_free(conn, biz, 30, day, now), day
        assert not BK.lanes(conn, biz["id"])
    with db.tx() as conn, pytest.raises(Invalid) as e:                             # the same time can't be taken twice
        BK.book(conn, biz, service_id=svc["id"], starts_at=at(biz, d, 10), name="Otra", phone="9992223333")
    assert e.value.field == "starts_at"
    a = book(biz, svc, at(biz, d, 11))["appointment"]
    assert a["staff_id"] is None and a["party"] == 1 and a["deposit_minor"] is None and a["status"] == "booked"
    with db.tx() as conn, pytest.raises(Invalid):                                  # the owner's own booking clashes as before
        D.book(conn, biz, "t", {"title": "x", "starts_at": at(biz, d, 11, 30), "ends_at": at(biz, d, 12)})


# ================================================================ a team: lanes, fairness, hours
def test_two_people_at_the_same_time_but_never_one_person_twice():
    biz, _, svc = biz_with()
    ana, luis = person(biz, "Ana López"), person(biz, "Luis Pérez")
    d = day_ahead(biz)
    a = book(biz, svc, at(biz, d, 10), staff_id=ana["id"])
    assert a["appointment"]["staff_id"] == ana["id"] and a["staff"] == "Ana López"
    b = book(biz, svc, at(biz, d, 10), name="Bea", phone="9992223333", staff_id=luis["id"])
    assert b["appointment"]["staff_id"] == luis["id"]
    with db.tx() as conn, pytest.raises(Invalid):                                  # Ana is taken at 10
        BK.book(conn, biz, service_id=svc["id"], starts_at=at(biz, d, 10, 30), name="C", phone="9993334444", staff_id=ana["id"])
    with db.tx() as conn, pytest.raises(Invalid):                                  # anyone: both are taken
        BK.book(conn, biz, service_id=svc["id"], starts_at=at(biz, d, 10), name="C", phone="9993334444")
    with db.tx() as conn:
        assert at(biz, d, 10) not in BK.slots(conn, biz, svc["id"], d, staff=ana["id"])
        assert at(biz, d, 11) in BK.slots(conn, biz, svc["id"], d, staff=ana["id"])
        assert at(biz, d, 10) not in BK.slots(conn, biz, svc["id"], d)
        # the owner's own calendar follows the same lanes
        D.book(conn, biz, "t", {"title": "x", "starts_at": at(biz, d, 12), "ends_at": at(biz, d, 13), "staff_id": ana["id"]})
        D.book(conn, biz, "t", {"title": "y", "starts_at": at(biz, d, 12), "ends_at": at(biz, d, 13), "staff_id": luis["id"]})
        with pytest.raises(Invalid):
            D.book(conn, biz, "t", {"title": "z", "starts_at": at(biz, d, 12), "ends_at": at(biz, d, 13), "staff_id": ana["id"]})


def test_an_appointment_with_nobody_named_is_the_business_lane_and_blocks_everyone():
    biz, _, svc = biz_with()
    d = day_ahead(biz)
    with db.tx() as conn:
        D.book(conn, biz, "t", {"title": "Old one", "starts_at": at(biz, d, 9), "ends_at": at(biz, d, 10)})   # before a team
    ana, luis = person(biz, "Ana"), person(biz, "Luis")
    with db.tx() as conn:
        assert at(biz, d, 9) not in BK.slots(conn, biz, svc["id"], d)
        assert at(biz, d, 9) not in BK.slots(conn, biz, svc["id"], d, staff=luis["id"])
        D.book(conn, biz, "t", {"title": "Ana's", "starts_at": at(biz, d, 11), "ends_at": at(biz, d, 12), "staff_id": ana["id"]})
        with pytest.raises(Invalid):                                               # nobody named = clashes with anything
            D.book(conn, biz, "t", {"title": "block", "starts_at": at(biz, d, 11), "ends_at": at(biz, d, 12)})


def test_anyone_available_is_fair():
    biz, _, svc = biz_with()
    people = [person(biz, n) for n in ("Ana", "Luis", "Marta")]
    d = day_ahead(biz)
    got = [book(biz, svc, at(biz, d, h), name=f"C{h}", phone=f"99912345{h:02d}")["appointment"]["staff_id"] for h in (9, 10, 11)]
    assert got == [p["id"] for p in people]                                        # nobody gets two while another has none
    fourth = book(biz, svc, at(biz, d, 12), name="C12", phone="9991234512")["appointment"]["staff_id"]
    assert fourth == people[0]["id"]                                               # all even: whoever was given one longest ago
    with db.tx() as conn:                                                          # 9:00 again: only Luis and Marta are free
        ok, lane = BK.place(conn, biz, 60, at(biz, d, 9), service=svc)
    assert ok and lane == people[1]["id"]


def test_each_person_works_their_shifts_or_own_hours_and_time_off_is_respected():
    biz, _, svc = biz_with()
    d = day_ahead(biz, 2)
    ana = person(biz, "Ana", hours={BK.DAYS[d.weekday()]: [["09:00", "11:00"]]})
    luis, marta = person(biz, "Luis"), person(biz, "Marta")
    with db.tx() as conn:
        SF.add_shift(conn, biz, "t", luis["id"], d, "12:00", "16:00")              # a shift this week: only shift days
        SF.add_time_off(conn, biz, "t", marta["id"], at(biz, d, 10), at(biz, d, 12), "médico")
        assert hhmm(BK.slots(conn, biz, svc["id"], d, staff=ana["id"])) == ["09:00", "09:15", "09:30", "09:45", "10:00"]
        assert hhmm(BK.slots(conn, biz, svc["id"], d, staff=luis["id"]))[0] == "12:00"
        assert hhmm(BK.slots(conn, biz, svc["id"], d, staff=luis["id"]))[-1] == "15:00"
        assert BK.slots(conn, biz, svc["id"], d + timedelta(days=1), staff=luis["id"]) == []   # same week, no shift: off
        m = hhmm(BK.slots(conn, biz, svc["id"], d, staff=marta["id"]))
        assert "09:00" in m and "09:15" not in m and "10:00" not in m and "12:00" in m      # 10–12 off (60-min service)
        assert hhmm(BK.slots(conn, biz, svc["id"], d, staff=marta["id"]))[-1] == "19:00"    # opening hours otherwise
        assert BK.slots(conn, biz, svc["id"], d + timedelta(days=1), staff=ana["id"]) == []  # her own hours: that day only
    with db.tx() as conn, pytest.raises(Invalid):
        BK.book(conn, biz, service_id=svc["id"], starts_at=at(biz, d, 10, 30), name="X", phone="9991112222", staff_id=marta["id"])


def test_a_service_only_some_people_do():
    biz, _, svc = biz_with()
    ana, luis = person(biz, "Ana"), person(biz, "Luis")
    with db.tx() as conn:
        color = BK.add_service(conn, biz, "t", name="Color", minutes=90, price_minor=80000)
        out = BK.save_options(conn, biz, "t", color["id"], {"staff_ids": [str(ana["id"])]})
        assert out["staff_ids"] == [ana["id"]]
    d = day_ahead(biz)
    with db.tx() as conn:
        assert BK.slots(conn, biz, color["id"], d, staff=luis["id"]) == []
        assert BK.slots(conn, biz, color["id"], d, staff=ana["id"])
    a = book(biz, color, at(biz, d, 9))
    assert a["appointment"]["staff_id"] == ana["id"]
    with db.tx() as conn, pytest.raises(Invalid):
        BK.book(conn, biz, service_id=color["id"], starts_at=at(biz, d, 12), name="X", phone="9991112222", staff_id=luis["id"])
    other, _, _ = biz_with(name="Otra")
    stranger = person(other, "Zoe")
    with db.tx() as conn, pytest.raises(NotFound):                                 # another business's person: not here
        BK.save_options(conn, biz, "t", color["id"], {"staff_ids": [str(stranger["id"])]})
    with db.tx() as conn, pytest.raises(NotFound):
        D.book(conn, biz, "t", {"title": "x", "starts_at": at(biz, d, 15), "ends_at": at(biz, d, 16), "staff_id": stranger["id"]})


# ================================================================ in the chat: with whom, how many
def test_the_chat_asks_with_whom_and_books_that_person():
    biz, tg, gw = shop(country="US", name="Sol")
    ana, luis = person(biz, "Ana Ruiz"), person(biz, "Luis Mora")
    net = Net()
    st, out = gw_in(gw, "+15550002222", "I want to book", name="Rosa", net=net)
    assert out == ["booking:asked"]
    asked = net.gw[-1]["body"]
    assert "With whom?" in asked and "Anyone available" in asked and "Ana Ruiz" in asked and "Luis Mora" in asked
    st, out = gw_in(gw, "+15550002222", "3", net=net)                               # Luis
    assert out == ["booking:asked"] and "Which day?" in net.gw[-1]["body"]
    gw_in(gw, "+15550002222", "1", net=net)                                         # the first day
    gw_in(gw, "+15550002222", "1", net=net)                                         # the first time
    st, out = gw_in(gw, "+15550002222", "+1 202 555 0143", net=net)
    assert out == ["booking:booked"] and "with Luis" in net.gw[-1]["body"]
    with db.tx() as conn:
        a = conn.execute(select(db.appointments)).mappings().one()
    assert a["staff_id"] == luis["id"] and a["conversation_id"]
    st, out = gw_in(gw, "+15550003333", "I want to book", name="Pia", net=net)     # anyone
    gw_in(gw, "+15550003333", "1", net=net)
    gw_in(gw, "+15550003333", "1", net=net)
    gw_in(gw, "+15550003333", "1", net=net)
    st, out = gw_in(gw, "+15550003333", "+1 202 555 0199", net=net)
    assert out == ["booking:booked"]
    with db.tx() as conn:
        rows = conn.execute(select(db.appointments.c.staff_id, db.appointments.c.starts_at).order_by(db.appointments.c.created_at)).all()
    assert rows[1].staff_id in (ana["id"], luis["id"])                              # anyone: someone free then, never doubled
    assert not (rows[1].starts_at == rows[0].starts_at and rows[1].staff_id == rows[0].staff_id)


def test_a_time_offered_in_the_chat_goes_to_whoever_is_free_never_the_whole_business():
    from valeria_one.services import chatbook as CB
    biz, tg, gw = shop(country="US", name="Sol")
    ana, luis = person(biz, "Ana"), person(biz, "Luis")
    net = Net()
    st, _ = gw_in(gw, "+15550008888", "hello", name="Bo", net=net)
    assert CB.start_offer(biz["id"], st["conversation_ids"][0], what="Pickup", minutes=30, http=net.http) == "asked"
    st, out = gw_in(gw, "+15550008888", "tomorrow at 10am", net=net)
    assert out == ["booking:booked"]
    with db.tx() as conn:
        a = conn.execute(select(db.appointments)).mappings().one()
    assert a["staff_id"] == ana["id"]                                              # fair: the first free person, not everyone
    tz = ZoneInfo(biz["timezone"])
    with db.tx() as conn:                                                          # Luis is still free at 10
        assert at(biz, a["starts_at"].astimezone(tz).date(), 10) in BK.free(conn, biz, 30, a["starts_at"].astimezone(tz).date())
    assert BP.round_() == {"released": 0, "told": 0, "waitlist": {"told": 0, "unreachable": 0}}   # the jobs step runs


@pytest.mark.parametrize("words,n", [("somos 4", 4), ("for 2 people", 2), ("٣", 3), ("四人", 4), ("nous sommes deux", 2),
                                     ("३ लोग", 3), ("tiga orang", 3)])
def test_the_party_size_is_read_in_many_languages(words, n):
    from valeria_one.services.chatbook import party_in
    assert party_in(words) == n


def test_a_table_asks_for_how_many_and_books_the_seats():
    biz, tg, gw = shop(country="US", name="Mesa")
    with db.tx() as conn:
        svc = BK.services(conn, biz["id"])[0]
        BK.save_options(conn, biz, "t", svc["id"], {"capacity": 6, "ask_party": True})
    net = Net()
    gw_in(gw, "+15550004444", "I want to book a table", name="Ken", net=net)
    assert "For how many people?" in net.gw[-1]["body"]
    gw_in(gw, "+15550004444", "nine", net=net)                                      # more than the seats: asked again
    assert "1 to 6" in net.gw[-1]["body"]
    gw_in(gw, "+15550004444", "we are 4", net=net)
    assert "Which day?" in net.gw[-1]["body"]
    gw_in(gw, "+15550004444", "1", net=net)
    assert " · 6 left" in net.gw[-1]["body"]                                        # seats left with each time
    gw_in(gw, "+15550004444", "1", net=net)
    st, out = gw_in(gw, "+15550004444", "+1 202 555 0177", net=net)
    assert out == ["booking:booked"] and "for 4" in net.gw[-1]["body"]
    with db.tx() as conn:
        a = conn.execute(select(db.appointments)).mappings().one()
        assert a["party"] == 4 and a["service_id"] == svc["id"]
        left, _ = BK.spots(conn, biz, svc["id"], a["starts_at"].astimezone(ZoneInfo(biz["timezone"])).date())
    assert left[a["starts_at"]] == 2                                              # 6 seats, 4 taken


# ================================================================ seats, spots left, full, the waitlist
def yoga(biz, d, capacity=3):
    with db.tx() as conn:
        y = BK.add_service(conn, biz, "t", name="Yoga", minutes=60, price_minor=15000)
        return BK.save_options(conn, biz, "t", y["id"], {"capacity": capacity, "ask_party": True, "waitlist": True,
                                                         "starts": {BK.DAYS[d.weekday()]: ["09:00", "18:00"]}})


def test_seats_count_spots_left_and_a_full_class_offers_the_waitlist():
    biz, _, _ = biz_with(industry="fitness")
    d = day_ahead(biz)
    y = yoga(biz, d)
    with db.tx() as conn:
        assert hhmm(BK.slots(conn, biz, y["id"], d)) == ["09:00", "18:00"]           # fixed start times only
    book(biz, y, at(biz, d, 9), name="A", phone="9990000001")
    book(biz, y, at(biz, d, 9), name="B", phone="9990000002")                      # the same class: no clash
    with db.tx() as conn:
        left, full = BK.spots(conn, biz, y["id"], d)
    assert left[at(biz, d, 9)] == 1 and full == []
    with db.tx() as conn, pytest.raises(Invalid):                                  # two people, one seat left
        BK.book(conn, biz, service_id=y["id"], starts_at=at(biz, d, 9), name="C", phone="9990000003", party=2)
    c = book(biz, y, at(biz, d, 9), name="C", phone="9990000003")
    with db.tx() as conn:
        left, full = BK.spots(conn, biz, y["id"], d)
        assert at(biz, d, 9) not in left and full == [at(biz, d, 9)]
        w1 = BP.join_waitlist(conn, biz, service_id=y["id"], starts_at=at(biz, d, 9), name="W1", phone="9990000011")
        w2 = BP.join_waitlist(conn, biz, service_id=y["id"], starts_at=at(biz, d, 9), name="W2", phone="9990000012")
        assert BP.join_waitlist(conn, biz, service_id=y["id"], starts_at=at(biz, d, 9), name="W1", phone="9990000011")["id"] == w1["id"]
        with pytest.raises(Invalid):                                               # 18:00 isn't full: book it instead
            BP.join_waitlist(conn, biz, service_id=y["id"], starts_at=at(biz, d, 18), name="W3", phone="9990000013")
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", c["appointment"]["id"], "cancelled")
        st = dict(conn.execute(select(db.booking_waitlist.c.id, db.booking_waitlist.c.status)).all())
    assert st[w1["id"]] == "notify" and st[w2["id"]] == "waiting"                   # the first in line is put to be told
    stats = BP.send_offers()
    assert stats == {"told": 0, "unreachable": 2}                                   # no channel for either: both tried, in order


def test_a_freed_seat_is_offered_to_the_first_in_line_on_their_channel():
    biz, tg, gw = shop(country="US", name="Studio")
    d = day_ahead(biz)
    y = yoga(biz, d, capacity=1)
    net = Net()
    st, _ = gw_in(gw, "+15550005555", "hi", name="Rosa", net=net)
    with db.tx() as conn:
        rosa = conn.execute(select(db.conversations.c.contact_id)).scalar()
        nobody = C.create(conn, biz, "t", {"name": "Sin canal"})
    a = book(biz, y, at(biz, d, 9), name="A", phone="+12025550101")
    with db.tx() as conn:
        for i, cid in enumerate((nobody["id"], rosa)):                             # first: someone we can't reach
            conn.execute(db.booking_waitlist.insert().values(business_id=biz["id"], service_id=y["id"], starts_at=at(biz, d, 9),
                                                             contact_id=cid, created_at=NOW() - timedelta(minutes=5 - i)))
        D.set_appointment_status(conn, biz["id"], "booking_chat", a["appointment"]["id"], "cancelled")
    net.gw.clear()
    assert BP.send_offers(http=net.http) == {"told": 1, "unreachable": 1}
    body = net.gw[-1]["body"]
    assert "A spot opened at Studio" in body and f"/b/{biz['slug']}/{y['id']}?d={d.isoformat()}" in body
    with db.tx() as conn:
        rows = {r.contact_id: r.status for r in conn.execute(select(db.booking_waitlist))}
    assert rows == {nobody["id"]: "unreachable", rosa: "notified"}


def test_two_customers_racing_for_the_last_seat_or_the_same_person_only_one_wins():
    biz, _, svc = biz_with(industry="fitness")
    d = day_ahead(biz)
    y = yoga(biz, d, capacity=2)
    book(biz, y, at(biz, d, 9), name="First", phone="9990000100")
    ana = person(biz, "Ana")

    def race(fn_list):
        gate, won, lost = threading.Barrier(len(fn_list)), [], []

        def run(fn):
            gate.wait()
            try:
                with db.tx() as conn:
                    fn(conn)
                won.append(1)
            except Invalid:
                lost.append(1)
        ts = [threading.Thread(target=run, args=(f,)) for f in fn_list]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return len(won), len(lost)

    seat = [lambda conn, i=i: BK.book(conn, biz, service_id=y["id"], starts_at=at(biz, d, 9), name=f"R{i}", phone=f"99900002{i:02d}")
            for i in range(4)]
    assert race(seat) == (1, 3)
    same = [lambda conn, i=i: BK.book(conn, biz, service_id=svc["id"], starts_at=at(biz, d, 15), name=f"S{i}",
                                      phone=f"99900003{i:02d}", staff_id=ana["id"]) for i in range(3)]
    assert race(same) == (1, 2)
    with db.tx() as conn:
        n = conn.execute(select(db.appointments.c.party).where(db.appointments.c.service_id == y["id"])).all()
    assert sum(r.party for r in n) == 2


# ================================================================ deposits, holds, fees
def money_biz(provider=True, country="MX"):
    biz, key, svc = biz_with(country=country)
    with db.tx() as conn:
        if provider:
            PM.connect(conn, biz, "t", "stripe", {"secret_key": "sk_test_X", "webhook_secret": "whsec_S"})
        svc = BK.save_options(conn, biz, "t", svc["id"], {"deposit_minor": 5000, "fee_minor": 15000, "cancel_hours": 24})
    return biz, key, svc


def test_the_deposit_link_holds_the_time_and_the_paid_webhook_confirms_it(client, psp):
    biz, key, svc = money_biz()
    d = day_ahead(biz, 3)
    res = book(biz, svc, at(biz, d, 10))
    a = res["appointment"]
    assert a["deposit_minor"] == 5000 and a["fee_minor"] == 15000 and a["cancel_hours"] == 24 and a["status"] == "booked"
    got = BP.ask_deposit(biz, a["id"])
    assert got["url"].startswith("https://checkout.stripe.com") and not got["manual"]
    a = appt_row(a["id"])
    assert a["deposit_payment_id"] and timedelta(minutes=29) < a["deposit_due_at"] - NOW() <= timedelta(minutes=30)
    assert BP.ask_deposit(biz, a["id"])["url"] == got["url"]                       # once per booking
    with db.tx() as conn:
        with pytest.raises(Invalid, match="dep"):                                  # it waits for its deposit (es: depósito)
            D.set_appointment_status(conn, biz["id"], "t", a["id"], "confirmed")
        hook = PM.accounts(conn, biz["id"])[0]["webhook_url"].replace("https://one.test", "")
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_1", "payment_status": "paid",
                                                                                 "metadata": {"vo_payment": str(a["deposit_payment_id"])}}}}).encode()
    t = str(int(_time.time()))
    assert client.post(hook, content=body, headers={"Stripe-Signature": f"t={t},v1={sig256('whsec_S', f'{t}.'.encode() + body)}",
                                                    "Content-Type": "application/json"}).status_code == 200
    a = appt_row(a["id"])
    assert a["status"] == "confirmed" and a["deposit_paid_at"] and a["deposit_due_at"] is None
    assert BP.expire_holds(NOW() + timedelta(hours=2)) == {"released": 0, "told": 0}   # paid: never released
    from valeria_one.services import books as BKS
    with db.tx() as conn:                                                          # in Books as money in
        led = BKS.ledger(conn, biz, NOW().date() - timedelta(days=1), NOW().date() + timedelta(days=1))
    assert any(r["amount_minor"] == 5000 for r in led)


def test_a_hold_that_runs_out_frees_the_time_and_tells_the_customer_in_the_chat():
    biz, tg, gw = shop(country="US", name="Sol")
    with db.tx() as conn:                                                          # a bank transfer: the link is our pay page
        PM.connect(conn, biz, "t", "bank_transfer", {"bank": "Chase", "holder": "Sol LLC", "account": "0123"})
        svc = BK.services(conn, biz["id"])[0]
        BK.save_options(conn, biz, "t", svc["id"], {"deposit_bp": 2500, "cancel_hours": 24})   # 25 % of 200.00 = 50.00
    net = Net()
    gw_in(gw, "+15550006666", "I want to book", name="Rosa", net=net)
    gw_in(gw, "+15550006666", "1", net=net)
    gw_in(gw, "+15550006666", "1", net=net)
    st, out = gw_in(gw, "+15550006666", "+1 202 555 0111", net=net)
    conf = net.gw[-1]["body"]
    assert out == ["booking:booked"] and "pay the deposit of $50.00 within 30 minutes here: https://one.test/p/" in conf
    assert "The deposit is kept if you cancel less than 24 h before." in conf
    with db.tx() as conn:
        a = dict(conn.execute(select(db.appointments)).mappings().one())
        assert BK.slots(conn, biz, svc["id"], a["starts_at"].astimezone(ZoneInfo(biz["timezone"])).date()).count(a["starts_at"]) == 0
    assert BP.expire_holds(NOW() + timedelta(minutes=10), http=net.http)["released"] == 0
    net.gw.clear()
    assert BP.expire_holds(NOW() + timedelta(minutes=31), http=net.http) == {"released": 1, "told": 1}
    assert "didn't arrive in time" in net.gw[-1]["body"]
    a = appt_row(a["id"])
    with db.tx() as conn:
        pay = conn.execute(select(db.payments.c.status).where(db.payments.c.id == a["deposit_payment_id"])).scalar()
        assert a["status"] == "cancelled" and pay == "pending"                    # left open: a late payment is never lost
        assert a["starts_at"] in BK.slots(conn, biz, svc["id"], a["starts_at"].astimezone(ZoneInfo(biz["timezone"])).date())
        assert PM.mark_paid(conn, a["deposit_payment_id"], "webhook:bank")          # they paid anyway, after the hold
    a = appt_row(a["id"])
    with db.tx() as conn:
        told = [r.summary for r in conn.execute(select(db.assistant_actions.c.summary).where(db.assistant_actions.c.kind == "notice"))]
    assert a["deposit_paid_at"] and a["status"] == "cancelled" and any("after the time was freed" in x for x in told)

    # in the chat: "cancel" says what becomes of a paid deposit
    gw_in(gw, "+15550006667", "I want to book", name="Lu", net=net)
    gw_in(gw, "+15550006667", "1", net=net)
    gw_in(gw, "+15550006667", "1", net=net)
    gw_in(gw, "+15550006667", "+1 202 555 0112", net=net)
    with db.tx() as conn:
        b = dict(conn.execute(select(db.appointments).where(db.appointments.c.status == "booked")).mappings().one())
        BP.mark_deposit(conn, biz, "t", b["id"])
    st, out = gw_in(gw, "+15550006667", "cancel", net=net)
    assert out == ["booking:cancelled"]
    late = b["starts_at"] - NOW() < timedelta(hours=24)
    assert ("is kept: the booking was cancelled less than 24 h before" if late else "will be returned to you") in net.gw[-1]["body"]


def test_without_a_payment_account_the_owner_marks_the_deposit_and_books_has_it():
    biz, key, svc = money_biz(provider=False)
    d = day_ahead(biz, 3)
    a = book(biz, svc, at(biz, d, 10))["appointment"]
    got = BP.ask_deposit(biz, a["id"])
    assert got == {"amount_minor": 5000, "url": None, "due_at": None, "manual": True}
    assert "will tell you how to pay the deposit" in BP.deposit_words(biz, got, "en")
    assert BP.expire_holds(NOW() + timedelta(days=1))["released"] == 0             # no link went out: nothing is released
    with db.tx() as conn:
        BP.mark_deposit(conn, biz, "user:x", a["id"])
        with pytest.raises(Invalid):
            BP.mark_deposit(conn, biz, "user:x", a["id"])                          # once
    a = appt_row(a["id"])
    assert a["status"] == "confirmed" and a["deposit_paid_at"]
    with db.tx() as conn:
        p = conn.execute(select(db.payments).where(db.payments.c.id == a["deposit_payment_id"])).mappings().one()
        assert p["provider"] == "cash" and p["status"] == "paid" and p["amount_minor"] == 5000
        visits = conn.execute(select(db.loyalty_visits)).all()
    assert visits == []                                                            # a deposit is not a visit (the booking is)


def test_a_no_show_sends_the_fee_less_the_kept_deposit(client, psp):
    biz, key, svc = money_biz()
    a = book(biz, svc, at(biz, day_ahead(biz, 2), 10))["appointment"]
    with db.tx() as conn:
        BP.mark_deposit(conn, biz, "t", a["id"])
        D.set_appointment_status(conn, biz["id"], "t", a["id"], "no_show")
    assert BP.after_status(biz, a["id"], "no_show", "t") == "bkg_fee_sent"
    a = appt_row(a["id"])
    with db.tx() as conn:
        fee = conn.execute(select(db.payments).where(db.payments.c.id == a["fee_payment_id"])).mappings().one()
    assert fee["amount_minor"] == 10000 and fee["status"] == "pending"              # 150.00 fee − 50.00 deposit kept
    assert BP.send_fee(biz, a["id"], "t") is None                                   # never twice


def test_cancelling_inside_the_window_keeps_the_deposit_outside_it_is_returned(psp):
    biz, key, svc = money_biz()
    soon = NOW() + timedelta(hours=3)
    pol = {"deposit_minor": 5000, "fee_minor": 15000, "cancel_hours": 24, "service_id": svc["id"]}

    def held(start, name):
        with db.tx() as conn:
            c = C.create(conn, biz, "t", {"name": name})
            return D.book(conn, biz, "t", {"title": "Corte", "contact_id": c["id"], "starts_at": start,
                                           "ends_at": start + timedelta(hours=1), **pol})
    late = held(soon, "Ana")
    early = held(NOW() + timedelta(days=4), "Bea")
    with db.tx() as conn:
        for a in (late, early):
            BP.mark_deposit(conn, biz, "t", a["id"])
        D.set_appointment_status(conn, biz["id"], "booking_chat", late["id"], "cancelled")
        D.set_appointment_status(conn, biz["id"], "booking_chat", early["id"], "cancelled")
        acts = [r.summary for r in conn.execute(select(db.assistant_actions.c.summary).where(db.assistant_actions.c.kind == "notice"))]
    assert appt_row(late["id"])["deposit_outcome"] == "kept" and appt_row(early["id"])["deposit_outcome"] == "refund"
    assert len(acts) == 1 and "Bea" in acts[0] and "$50.00" in acts[0]               # the owner is told to return one
    nodep = held(soon + timedelta(hours=1), "Cy")
    with db.tx() as conn:                                                          # late, deposit never paid: the fee may go
        D.set_appointment_status(conn, biz["id"], "booking_chat", nodep["id"], "cancelled")
    with db.tx() as conn:
        assert BP.fee_due(conn, appt_row(nodep["id"])) == 15000
        pend = conn.execute(select(db.payments.c.status).where(db.payments.c.id == appt_row(nodep["id"])["deposit_payment_id"])).scalar()
    assert appt_row(nodep["id"])["deposit_outcome"] == "late" and pend is None


def test_a_signed_contract_and_the_deposit_are_both_needed():
    from valeria_one.services import contracts as CT
    from test_ctr01 import draft, png, row
    biz, key, svc = money_biz(provider=False)
    a = book(biz, svc, at(biz, day_ahead(biz, 3), 10))["appointment"]
    with db.tx() as conn:
        CT.save_settings(conn, biz, "t", require_signed=True, require_deposit=False)
        assert CT.gate(conn, biz["id"], a["id"]) == "unsigned"
        ana = C.get(conn, biz["id"], a["contact_id"])
    c = draft(biz, ana, appointment_id=a["id"])
    res = CT.send(biz, "t", c["id"], message=False)
    CT.sign(res["id"], CT.sig(row(res["id"])), name="Ana López", privacy=True, agree=True, signature=png(), ip="1.2.3.4", ua="t")
    with db.tx() as conn:
        assert CT.gate(conn, biz["id"], a["id"]) == "booking_deposit"              # signed; its own deposit not in yet
        assert "deposit" in CT.gate_problem(conn, biz["id"], a["id"], "en")
    assert appt_row(a["id"])["status"] == "booked"
    with db.tx() as conn:
        BP.mark_deposit(conn, biz, "t", a["id"])
    assert appt_row(a["id"])["status"] == "confirmed"


# ================================================================ reminders, calendars, Google
def test_reminders_and_the_owners_note_name_the_person():
    biz, tg, gw = shop(country="US", name="Sol")
    ana = person(biz, "Ana Ruiz")
    net = Net()
    gw_in(gw, "+15550007777", "hi", name="Rosa", net=net)
    with db.tx() as conn:
        RM.set_on(conn, biz, "t", True, hours=2)
        rosa = conn.execute(select(db.conversations.c.contact_id)).scalar()
        st = at(biz, day_ahead(biz), 11)
        a = D.book(conn, biz, "t", {"title": "Corte", "contact_id": rosa, "starts_at": st, "ends_at": st + timedelta(hours=1),
                                    "staff_id": ana["id"]})
        due = conn.execute(select(db.scheduled_messages.c.send_at).where(db.scheduled_messages.c.subject_id == a["id"])).scalar()
    net.gw.clear()
    FU.send_due(now=due + timedelta(seconds=1), http=net.http)
    assert "with Ana" in net.gw[-1]["body"]
    with db.tx() as conn:
        assert CC.what_of(conn, biz, a, "es") == "Corte · con Ana"


def test_ics_feed_and_google_keep_working_and_name_the_person(client, monkeypatch):
    from test_gcal import FakeGoogle
    biz, _, svc = biz_with(country="US", name="Sol")
    ana = person(biz, "Ana Ruiz", color="#ff0000")
    a = book(biz, svc, at(biz, day_ahead(biz), 10), staff_id=ana["id"])["appointment"]
    r = client.get(f"/b/{biz['slug']}/ics/{a['id']}/{CC.sig(a['id'])}")
    assert r.status_code == 200 and f"UID:{a['id']}@valeria-one" in r.text
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "GS")
    gcal._tokens.clear()
    g = FakeGoogle()
    monkeypatch.setattr(gcal, "_http", lambda http=None: http or g.client())
    with db.tx() as conn:
        conn.execute(db.calendar_links.insert().values(business_id=biz["id"], provider="google", account_email="x@y",
                                                       refresh_token=__import__("valeria_one.crypto", fromlist=["seal"]).seal({"secret": "RT"})["secret"]))
    assert gcal.sync(biz["id"])["created"] == 1
    assert list(g.events.values())[0]["summary"].endswith("· Ana Ruiz")

    class Events(FakeGoogle):                                                       # the team's busy time: never our own events
        def __call__(self, req):
            if req.method == "GET" and req.url.path.endswith("/events"):
                return httpx.Response(200, json={"timeZone": "UTC", "items": [
                    {"start": {"dateTime": "2030-01-01T10:00:00Z"}, "end": {"dateTime": "2030-01-01T11:00:00Z"},
                     "extendedProperties": {"private": {"vo_id": "x"}}},
                    {"start": {"dateTime": "2030-01-01T12:00:00Z"}, "end": {"dateTime": "2030-01-01T13:00:00Z"}},
                    {"start": {"dateTime": "2030-01-01T14:00:00Z"}, "end": {"dateTime": "2030-01-01T15:00:00Z"}, "transparency": "transparent"},
                    {"start": {"date": "2030-01-02"}, "end": {"date": "2030-01-03"}}]})
            return super().__call__(req)
    e = Events()
    monkeypatch.setattr(gcal, "_http", lambda http=None: http or e.client())
    with db.tx() as conn:
        got = gcal.busy(conn, biz["id"], datetime(2030, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 3, tzinfo=timezone.utc), ours=False)
    assert got == [(datetime(2030, 1, 1, 12, tzinfo=timezone.utc), datetime(2030, 1, 1, 13, tzinfo=timezone.utc)),
                   (datetime(2030, 1, 2, tzinfo=timezone.utc), datetime(2030, 1, 3, tzinfo=timezone.utc))]


def test_a_team_member_sees_their_own_bookings(client):
    from test_web import signup
    signup(client, country="US", industry="salon", email="own@example.com")
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        user = dict(conn.execute(select(db.users)).mappings().one())
        conn.execute(update(db.memberships).values(role="staff"))                  # as if they were a team member
        biz = BK.save_settings(conn, biz, "t", enabled=True, hours=ALL_WEEK, notice_minutes=0, days_ahead=30)
        svc = BK.add_service(conn, biz, "t", name="Corte", minutes=60)
        m = SF.add_member(conn, biz, "t", {"name": "Ana", "bookable": True})
        conn.execute(update(db.staff_members).where(db.staff_members.c.id == m["id"]).values(user_id=user["id"]))
        luis = SF.add_member(conn, biz, "t", {"name": "Luis", "bookable": True})
    d = day_ahead(biz)
    book(biz, svc, at(biz, d, 10), staff_id=m["id"], name="Mía")
    book(biz, svc, at(biz, d, 10), staff_id=luis["id"], name="Suyo", phone="+12025550199")
    with db.tx() as conn:
        feed = CC.feed(conn, biz["id"], user["id"], CC.feed_sig(biz["id"], user["id"], 0))
    assert feed.count("BEGIN:VEVENT") == 1 and "Mía" in feed
    listed = lambda url: client.get(url).text.split('action="/app/appointments" class="card"')[0]   # noqa: E731 — the bookings list
    assert "Mía" in listed("/app/calendar") and "Suyo" not in listed("/app/calendar")
    assert "Suyo" in listed("/app/calendar?staff=all")


# ================================================================ the screens and the API
def test_owner_screens_and_the_public_booking_page(client, psp):
    from test_web import signup
    signup(client, country="MX", industry="salon", email="pantallas@example.com")
    client.post("/lang/en", data={"csrf": tok(client)})
    with db.tx() as conn:
        biz = dict(conn.execute(select(db.businesses)).mappings().one())
        biz = BK.save_settings(conn, biz, "t", enabled=True, slug="pantallas", hours=ALL_WEEK, notice_minutes=0, days_ahead=30)
        svc = BK.add_service(conn, biz, "t", name="Corte", minutes=60, price_minor=20000)
        PM.connect(conn, biz, "t", "stripe", {"secret_key": "sk_test_X", "webhook_secret": "whsec_S"})
    page = client.get("/app/staff?tab=team").text
    assert 'name="bookable" value="1" checked' in page                            # a salon's new team: takes bookings
    t = tok(client, "/app/staff?tab=team")
    for n in ("Ana López", "Luis Pérez"):
        client.post("/app/staff/members", data={"csrf": t, "name": n, "bookable_form": "1", "bookable": "1"})
    with db.tx() as conn:
        ana, luis = SF.members(conn, biz["id"])
    assert ana["bookable"] and luis["bookable"]
    assert client.get("/app/booking/team").status_code == 200
    r = client.post(f"/app/booking/team/{luis['id']}", data={"csrf": t, "bookable": "1", "color": "#22aa55", "mon_a1": "09:00",
                                                             "mon_b1": "13:00"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        assert SF.member(conn, biz["id"], luis["id"])["booking_hours"] == {"mon": [["09:00", "13:00"]]}
    r = client.post("/app/booking/off", data={"csrf": t, "staff_id": str(ana["id"]), "starts_at": "2030-01-01T09:00",
                                              "ends_at": "2030-01-02T09:00", "note": "vacaciones"}, follow_redirects=False)
    assert r.status_code == 303 and "vacaciones" in client.get("/app/booking/team").text
    assert client.get(f"/app/booking/services/{svc['id']}").status_code == 200
    r = client.post(f"/app/booking/services/{svc['id']}", data={"csrf": t, "capacity": "1", "deposit_kind": "fixed",
                                                                "deposit_fixed": "100", "fee": "300", "cancel_hours": "24",
                                                                "staff_form": "1", f"staff_{ana['id']}": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with db.tx() as conn:
        svc = BK._service(conn, biz, svc["id"])
        assert svc["deposit_minor"] == 10000 and svc["fee_minor"] == 30000 and BK.service_staff(conn, biz["id"], svc["id"]) == [ana["id"]]
    cal = client.get("/app/calendar").text
    assert "Team for bookings" in cal and "Seats, team, deposit" in cal
    # the public page: one person does this service, the policy is shown; the booking waits for the deposit
    pub = client.get(f"/b/pantallas/{svc['id']}").text
    assert "Deposit $100.00 to confirm the booking." in pub and "The deposit is kept if you cancel less than 24 h before." in pub
    start = re.search(r'name="starts_at" value="([^"]+)"', pub).group(1)
    ptok = re.search(r'name="csrf" value="([^"]+)"', pub).group(1)
    r = client.post(f"/b/pantallas/{svc['id']}", data={"csrf": ptok, "starts_at": start, "name": "Rosa", "phone": "9991234567",
                                                       "party": "1"})
    assert r.status_code == 200 and "Pay the deposit" in r.text and "checkout.stripe.com" in r.text and "with Ana" in r.text
    with db.tx() as conn:
        a = dict(conn.execute(select(db.appointments)).mappings().one())
    assert a["staff_id"] == ana["id"] and a["deposit_payment_id"]
    cal = client.get("/app/calendar").text
    assert "Deposit waiting" in cal and f"/app/appointments/{a['id']}/deposit" in cal
    r = client.post(f"/app/appointments/{a['id']}/deposit", data={"csrf": t}, follow_redirects=False)
    assert r.status_code == 303 and appt_row(a["id"])["status"] == "confirmed"
    # a class with seats: the roster and the waitlist form
    with db.tx() as conn:
        y = BK.add_service(conn, biz, "t", name="Yoga", minutes=60)
        BK.save_options(conn, biz, "t", y["id"], {"capacity": 1, "waitlist": True})
        BK.save_options(conn, biz, "t", y["id"], {"capacity": 2, "staff_ids": [str(ana["id"])]})   # Ana's class
    d = day_ahead(biz, 3)
    for i in range(2):
        book(biz, y, at(biz, d, 9), name=f"Y{i}", phone=f"99955500{i:02d}")
    pub = client.get(f"/b/pantallas/{y['id']}?d={d.isoformat()}").text
    assert "join the waitlist" in pub.lower() and f"/b/pantallas/{y['id']}/waitlist" in pub
    ptok = re.search(r'name="csrf" value="([^"]+)"', pub).group(1)
    r = client.post(f"/b/pantallas/{y['id']}/waitlist", data={"csrf": ptok, "starts_at": at(biz, d, 9).isoformat(), "name": "Wen",
                                                              "phone": "9997776666"})
    assert r.status_code == 200 and "on the waitlist" in r.text
    roster = client.get(f"/app/booking/roster?service={y['id']}&at={at(biz, d, 9).isoformat().replace('+', '%2B')}").text
    assert "2 of 2 seats taken" in roster and "Wen" in roster and "Y0" in roster


def test_signed_out_pages_and_bad_links(client):
    zero = f"{'0' * 8}-0000-0000-0000-{'0' * 12}"
    for p in ("/app/booking/team", f"/app/booking/services/{zero}", f"/app/booking/roster?service={zero}&at=2030-01-01T09:00"):
        assert client.get(p, follow_redirects=False).status_code == 303, p
    for p in (f"/app/booking/team/{zero}", "/app/booking/off", f"/app/booking/off/{zero}/remove", f"/app/booking/services/{zero}",
              f"/app/appointments/{zero}/deposit", f"/app/appointments/{zero}/deposit-link", f"/app/appointments/{zero}/fee",
              f"/app/booking/waitlist/{zero}/remove"):
        assert client.post(p, follow_redirects=False).status_code == 303, p
    assert client.post(f"/b/nobody-here/{zero}/waitlist", data={"csrf": "x"}).status_code in (403, 404)


def test_the_api_books_a_person_a_party_and_returns_the_deposit(client, psp):
    biz, key, svc = money_biz(country="US")
    h = {"Authorization": f"Bearer {key}"}
    ana = person(biz, "Ana")
    r = client.patch(f"/v1/booking/services/{svc['id']}", headers=h, json={"capacity": 4, "ask_party": True, "staff_ids": [str(ana["id"])]})
    assert r.status_code == 200 and r.json()["capacity"] == 4 and r.json()["staff_ids"] == [str(ana["id"])]
    info = client.get(f"/v1/public/{biz['slug']}").json()
    assert info["staff"] == [{"id": str(ana["id"]), "name": "Ana"}] and info["services"][0]["capacity"] == 4
    assert info["services"][0]["policy"][0].startswith("Deposit $50.00")
    d = day_ahead(biz)
    sl = client.get(f"/v1/public/{biz['slug']}/slots", params={"service_id": str(svc["id"]), "day": d.isoformat(), "party": 2}).json()
    assert sl["spots"][sl["slots"][0]] == 4
    r = client.post(f"/v1/public/{biz['slug']}/book", json={"service_id": str(svc["id"]), "starts_at": sl["slots"][0], "name": "Kim",
                                                          "phone": "+12025550123", "party": 3, "staff_id": str(ana["id"])})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["party"] == 3 and j["staff"] == "Ana" and j["deposit"]["url"].startswith("https://checkout.stripe.com")
    r = client.post(f"/v1/appointments/{j['appointment_id']}/deposit", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    r = client.patch(f"/v1/booking/staff/{ana['id']}", headers=h, json={"bookable": False})
    assert r.status_code == 200 and r.json()["bookable"] is False
    other, okey, _ = biz_with(name="Otra", country="US")
    oh = {"Authorization": f"Bearer {okey}"}
    assert client.patch(f"/v1/booking/services/{svc['id']}", headers=oh, json={"capacity": 2}).status_code == 404
    assert client.post(f"/v1/appointments/{j['appointment_id']}/deposit", headers=oh).status_code == 404
    r = client.post("/v1/appointments", headers=h, json={"title": "x", "starts_at": at(biz, d, 15).isoformat(),
                                                       "ends_at": at(biz, d, 16).isoformat(), "staff_id": str(ana["id"])})
    assert r.status_code == 201 and r.json()["staff_id"] == str(ana["id"])


# ================================================================ data, words, kits, the migration
def test_export_holds_the_new_tables_and_columns():
    assert {"booking_waitlist", "staff_time_off", "booking_service_staff"} <= set(EX.TABLES)
    biz, _, svc = biz_with()
    ana = person(biz, "Ana")
    book(biz, svc, at(biz, day_ahead(biz), 10), staff_id=ana["id"])
    with db.tx() as conn:
        out = EX.business_data(conn, biz["id"], "t")
    assert out["appointments"][0]["staff_id"] == ana["id"] and out["appointments"][0]["party"] == 1
    assert out["staff_members"][0]["bookable"] is True and "capacity" in out["booking_services"][0]


def test_kits_start_each_trade_right():
    assert packs.industry("salon").kit.booking.staff and packs.industry("clinic").kit.booking.staff
    assert packs.industry("fitness").kit.booking.capacity == 12
    r = packs.industry("restaurant").kit.booking
    assert r.party and r.capacity == 20 and not r.staff
    assert packs.industry("generic").kit.booking.capacity == 1 and not packs.industry("generic").kit.booking.staff
    assert kits.check_kits() == []


def test_every_new_word_is_in_all_11_languages():
    base = ROOT / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    keys = {k for k in files["en"] if k.startswith("bkg_")} | {"ct_gate_why_booking_deposit"}
    assert len(keys) >= 90
    used = set()
    for p in ("valeria_one/services/bookpay.py", "valeria_one/services/chatbook.py", "valeria_one/services/chatcal.py",
              "valeria_one/services/followups.py", "valeria_one/web/bookings.py", "valeria_one/web/__init__.py",
              "valeria_one/web/templates/calendar.html", "valeria_one/web/templates/book.html", "valeria_one/web/templates/staff.html",
              "valeria_one/web/templates/booking_team.html", "valeria_one/web/templates/booking_service.html",
              "valeria_one/web/templates/booking_roster.html"):
        used |= set(re.findall(r"\bbkg_[a-z0-9_]+[a-z0-9]", (ROOT / p).read_text(encoding="utf-8")))
    used = {k for k in used if k not in ("bkg_out", "bkg_cancel")} | {"bkg_out_kept", "bkg_out_refund", "bkg_out_late",
                                                                      "bkg_cancel_kept", "bkg_cancel_refund"}
    assert used <= keys, used - keys
    for lg in LANGS:
        for k in keys:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
    for k in keys:
        if len(files["en"][k]) > 14:
            assert files["es"][k] != files["en"][k] and files["ja"][k] != files["en"][k], k
    assert len(set(map(frozenset, files.values()))) == 1


def test_every_template_compiles():
    from valeria_one.web import templates
    names = [n for n in templates.env.list_templates() if n.endswith(".html")]
    assert {"booking_team.html", "booking_service.html", "booking_roster.html"} <= set(names)
    for n in names:
        templates.env.get_template(n)


def test_one_migration_on_top_of_the_agreement_that_changes_nothing_existing():
    import importlib.util
    path = next((ROOT / "alembic" / "versions").glob("*_bk01_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [mig.revision] and mig.down_revision == "b7e4d1f9a2c6"
    with db.tx() as conn:
        cols = {(r.table_name, r.column_name): r.column_default for r in conn.execute(text(
            "SELECT table_name, column_name, column_default FROM information_schema.columns WHERE table_name IN "
            "('appointments','booking_services','staff_members','businesses')"))}
        checks = {r[0] for r in conn.execute(text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))}
    assert cols[("appointments", "party")] == "1" and cols[("appointments", "staff_id")] is None
    assert cols[("booking_services", "capacity")] == "1" and cols[("staff_members", "bookable")] == "false"
    assert cols[("businesses", "deposit_hold_minutes")] == "30"
    assert {"ck_appointments_party", "ck_booking_services_capacity", "ck_appointments_deposit_outcome"} <= checks
