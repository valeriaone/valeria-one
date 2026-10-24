import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import make_business
from valeria_one import db
from valeria_one.services import Invalid
from valeria_one.services import booking as BK
from valeria_one.services import deals as D
from valeria_one.services import gcal

TZ = ZoneInfo("America/Mexico_City")


def _idtok(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


class FakeGoogle:
    def __init__(self):
        self.events, self.calls, self.busy, self.n = {}, [], [], 0
        self.revoked = False

    def __call__(self, req: httpx.Request):
        p = req.url.path
        self.calls.append((req.method, p))
        if req.url.host == "oauth2.googleapis.com":
            form = parse_qs(req.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600, "refresh_token": "RT-SECRET",
                                                 "id_token": _idtok("lupita@gmail.com")})
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})
        assert req.headers["authorization"].startswith("Bearer ")
        if p.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": self.busy}}})
        if req.method == "POST" and p.endswith("/events"):
            self.n += 1
            eid = f"g{self.n}"
            self.events[eid] = json.loads(req.content)
            return httpx.Response(200, json={"id": eid})
        eid = p.rsplit("/", 1)[1]
        if req.method == "PUT":
            if eid not in self.events:
                return httpx.Response(404, json={})
            self.events[eid] = json.loads(req.content)
            return httpx.Response(200, json={"id": eid})
        if req.method == "DELETE":
            self.events.pop(eid, None)
            return httpx.Response(204)
        return httpx.Response(404, json={})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "GSECRET")
    gcal._tokens.clear()
    g = FakeGoogle()
    monkeypatch.setattr(gcal, "_http", lambda http=None: http or g.client())
    return g


def test_off_without_settings(client):
    biz, _ = make_business("MX")
    with db.tx() as conn, pytest.raises(Invalid):
        gcal.connect(conn, biz, "t", "code")
    with db.tx() as conn:
        assert gcal.busy(conn, biz["id"], datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=1)) == []


def test_connect_push_update_cancel(google):
    biz, _ = make_business("MX", "salon")
    st = datetime.now(TZ).replace(microsecond=0) + timedelta(days=2)
    with db.tx() as conn:
        before = D.book(conn, biz, "t", {"title": "Corte", "starts_at": st, "ends_at": st + timedelta(hours=1)})
        link = gcal.connect(conn, biz, "t", "CODE")
    assert link["account_email"] == "lupita@gmail.com" and "RT-SECRET" not in link["refresh_token"]
    assert gcal.sync(biz["id"]) == {"created": 1, "updated": 0, "deleted": 0, "failed": 0}
    ev = google.events["g1"]
    assert ev["summary"] == "Corte" and ev["extendedProperties"]["private"]["vo_id"] == str(before["id"])
    assert gcal.sync(biz["id"])["created"] == 0                                 # nothing changed: nothing sent
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", before["id"], "confirmed")
    assert gcal.sync(biz["id"])["updated"] == 1
    del google.events["g1"]                                                     # owner deleted it in Google
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", before["id"], "booked")
    assert gcal.sync(biz["id"])["created"] == 1                                 # it comes back
    with db.tx() as conn:
        D.set_appointment_status(conn, biz["id"], "t", before["id"], "cancelled")
    assert gcal.sync(biz["id"])["deleted"] == 1 and google.events == {}


def test_google_busy_hides_slots(google):
    biz, _ = make_business("MX", "salon")
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", enabled=True, slug="s1", hours={"mon": [["09:00", "11:00"]]}, notice_minutes=0)
        svc = BK.add_service(conn, biz, "t", name="Corte", minutes=30)
        gcal.connect(conn, biz, "t", "CODE")
    monday = date(2026, 11, 2)
    now = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)
    google.busy = [{"start": "2026-11-02T15:00:00Z", "end": "2026-11-02T16:30:00Z"}]   # 9:00–10:30 Mexico City
    with db.tx() as conn:
        got = [t.strftime("%H:%M") for t in BK.slots(conn, biz, svc["id"], monday, now)]
    assert got == ["10:30"]
    freebusy_calls = [c for c in google.calls if c[1].endswith("/freeBusy")]
    with db.tx() as conn:
        BK.open_days(conn, biz, svc["id"], now)
    assert len([c for c in google.calls if c[1].endswith("/freeBusy")]) == len(freebusy_calls) + 1   # one call for all days


def test_revoked_access_is_shown_and_booking_still_works(google):
    biz, _ = make_business("MX", "salon")
    with db.tx() as conn:
        biz = BK.save_settings(conn, biz, "t", enabled=True, slug="s2", hours={"mon": [["09:00", "10:00"]]}, notice_minutes=0)
        svc = BK.add_service(conn, biz, "t", name="Corte", minutes=30)
        gcal.connect(conn, biz, "t", "CODE")
    gcal._tokens.clear()
    google.revoked = True
    with db.tx() as conn:
        assert len(BK.slots(conn, biz, svc["id"], date(2026, 11, 2), datetime(2026, 11, 1, tzinfo=TZ))) == 3
        assert "connect the calendar again" in gcal.link(conn, biz["id"])["last_error"]


def test_screen_connect_flow(client, google):
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/signup").text).group(1)
    client.post("/signup", data={"csrf": tok, "business_name": "Clínica Sol", "country": "MX", "industry": "clinic",
                                 "email": "sol@example.com", "password": "una-clave-larga"})
    assert "Conectar Google Calendar" in client.get("/app/calendar").text
    r = client.get("/app/calendar/google", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["access_type"] == ["offline"] and "calendar.freebusy" in q["scope"][0]
    bad = client.get("/app/calendar/google/callback", params={"code": "X", "state": "forged"}, follow_redirects=True)
    assert "no terminó" in bad.text
    r = client.get("/app/calendar/google", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    ok = client.get("/app/calendar/google/callback", params={"code": "CODE", "state": state}, follow_redirects=True)
    assert "lupita@gmail.com" in ok.text and "Google Calendar conectado" in ok.text
