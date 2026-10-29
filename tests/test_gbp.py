import base64
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from conftest import make_business
from valeria_one import crypto, db
from valeria_one.services import Invalid
from valeria_one.services import ai as AI
from valeria_one.services import gbp as G
from valeria_one.services import mailer

LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")
ACC, LOC = "accounts/111", "locations/222"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, request):
        raise RuntimeError(f"real network call in a test: {request.url}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    G._tokens.clear()


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("VO_GOOGLE_BUSINESS", "on")


def _id_token(email):
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class Google:
    """Google's APIs, answered in-process."""

    def __init__(self):
        self.calls, self.reviews, self.refresh_error, self.slow = [], [], None, False

    def __call__(self, req: httpx.Request):
        self.calls.append((req.method, req.url.host, req.url.path, req.content, str(req.url)))
        path, host = req.url.path, req.url.host
        if host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            if form.get("grant_type") == "refresh_token":
                if self.refresh_error:
                    return httpx.Response(400, json={"error": self.refresh_error})
                return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-secret",
                                             "id_token": _id_token("owner@sol.mx")})
        if self.slow:
            return httpx.Response(429, json={"error": {"message": "quota"}}, headers={"retry-after": "120"})
        if host == "mybusinessaccountmanagement.googleapis.com":
            return httpx.Response(200, json={"accounts": [{"name": ACC, "accountName": "Sol"}]})
        if host == "mybusinessbusinessinformation.googleapis.com":
            loc = {"name": LOC, "title": "Estética Sol", "phoneNumbers": {"primaryPhone": "+52 999 123 4567"},
                   "websiteUri": "https://sol.mx", "storefrontAddress": {"addressLines": ["Calle 60 #1"], "locality": "Mérida"},
                   "regularHours": {"periods": [{"openDay": "MONDAY", "openTime": {"hours": 9}, "closeDay": "MONDAY",
                                                 "closeTime": {"hours": 18, "minutes": 30}}]}}
            return httpx.Response(200, json={"locations": [loc]} if path.endswith("/locations") else loc)
        if host == "mybusiness.googleapis.com" and path.endswith("/reviews"):
            return httpx.Response(200, json={"reviews": self.reviews, "totalReviewCount": len(self.reviews)})
        if host == "mybusiness.googleapis.com" and path.endswith("/reply") and req.method == "PUT":
            return httpx.Response(200, json={"comment": json.loads(req.content)["comment"]})
        if host == "mybusiness.googleapis.com" and path.endswith("/localPosts"):
            return httpx.Response(200, json={"name": f"{ACC}/{LOC}/localPosts/9", "searchUrl": "https://g.co/post/9"})
        if host == "businessprofileperformance.googleapis.com":
            return httpx.Response(200, json={"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [
                {"dailyMetric": "CALL_CLICKS", "timeSeries": {"datedValues": [{"date": dict(zip(("year", "month", "day"), self.day(-3))), "value": "4"},
                                                                              {"date": dict(zip(("year", "month", "day"), self.day(-2))), "value": "2"}]}},
                {"dailyMetric": "BUSINESS_IMPRESSIONS_MOBILE_MAPS", "timeSeries": {"datedValues": [
                    {"date": dict(zip(("year", "month", "day"), self.day(-2))), "value": "30"}]}},
                {"dailyMetric": "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", "timeSeries": {"datedValues": [
                    {"date": dict(zip(("year", "month", "day"), self.day(-2))), "value": "10"}]}}]}]})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    @staticmethod
    def day(n):
        d = datetime.now(timezone.utc).date() + timedelta(days=n)
        return d.year, d.month, d.day

    @property
    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def count(self, method, needle):
        return sum(1 for m, _, p, *_ in self.calls if m == method and needle in p)


def review(rid, stars="FIVE", text="Excelente servicio", name="Ana López", reply=None):
    r = {"reviewId": rid, "name": f"{ACC}/{LOC}/reviews/{rid}", "reviewer": {"displayName": name}, "starRating": stars,
         "comment": text, "createTime": "2026-10-20T15:00:00Z", "updateTime": "2026-10-20T15:00:00Z"}
    if reply:
        r["reviewReply"] = {"comment": reply, "updateTime": "2026-10-21T10:00:00Z"}
    return r


def connected(google, sandbox=False):
    biz, _ = make_business("MX", "salon", name="Estética Sol", sandbox=sandbox)
    with db.tx() as conn:
        G.connect(conn, biz, "t", "code-1", http=google.http)
        G.choose(conn, biz, "t", ACC, LOC, http=google.http)
    return biz


def owner_session(client, biz):
    from valeria_one.services import users as U
    email = f"owner-{str(biz['id'])[:6]}@example.com"
    with db.tx() as conn:
        uid = conn.execute(db.users.insert().values(email=email, name="Dueña", password_hash=U._ph.hash("una-clave-larga"))
                           .returning(db.users.c.id)).scalar()
        U.add_member(conn, biz["id"], uid)
    tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"csrf": tok, "email": email, "password": "una-clave-larga"})
    return email


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


# ---------------------------------------------------------------- dormant until the server sets it up
def test_dormant_without_operator_keys(client, monkeypatch):
    monkeypatch.setenv("VO_GOOGLE_CLIENT_ID", "cid")                           # Calendar's keys alone don't switch it on
    monkeypatch.setenv("VO_GOOGLE_CLIENT_SECRET", "cs")
    assert not G.configured() and G.sync_all() == {"profiles": 0}
    biz, _ = make_business("MX", "salon")
    owner_session(client, biz)
    page = client.get("/app/google")
    assert page.status_code == 200 and ("Not connected yet" in page.text or "Aún no conectado" in page.text)
    assert 'href="/app/google/connect"' not in page.text
    assert client.get("/app/google/connect", follow_redirects=False).status_code == 404
    with db.tx() as conn:
        with pytest.raises(Invalid):
            G.connect(conn, biz, "t", "code")


def test_sandbox_businesses_cant_connect(google_on):
    biz, _ = make_business("MX", "salon", sandbox=True)
    with db.tx() as conn, pytest.raises(Invalid):
        G.connect(conn, biz, "t", "code", http=Google().http)


# ---------------------------------------------------------------- connect, pick, info, the sealed refresh token
def test_connect_pick_profile_and_info(google_on):
    g = Google()
    biz, _ = make_business("MX", "salon", name="Estética Sol")
    url = G.auth_url("st4te")
    assert "scope=openid+email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fbusiness.manage" in url and "access_type=offline" in url
    with db.tx() as conn:
        lk = G.connect(conn, biz, "user:x", "code-1", http=g.http)
        raw = conn.execute(select(db.google_links.c.tokens)).scalar()
    assert lk["account_email"] == "owner@sol.mx" and raw["refresh_token"].startswith("enc:")           # sealed
    assert crypto.open_(raw)["refresh_token"] == "rt-secret" and "refresh_token" in crypto._SECRET_FIELDS
    assert crypto.redact(raw)["refresh_token"] == "••••"
    places = G.locations(biz["id"], g.http)
    assert places == [{"account": ACC, "location": LOC, "title": "Estética Sol", "address": "Calle 60 #1, Mérida"}]
    read = [c for c in g.calls if c[1] == "mybusinessbusinessinformation.googleapis.com"][0]
    assert read[2] == f"/v1/{ACC}/locations" and "readMask=name%2Ctitle%2CphoneNumbers" in read[4]
    with db.tx() as conn:
        with pytest.raises(Invalid):
            G.choose(conn, biz, "t", ACC, "locations/999", http=g.http)        # not one of theirs
        G.choose(conn, biz, "t", ACC, LOC, http=g.http)
    info = G.refresh_info(biz["id"], g.http)
    assert info["name"] == "Estética Sol" and info["phone"] == "+52 999 123 4567" and info["hours"] == {"mon": [["09:00", "18:30"]]}


# ---------------------------------------------------------------- reviews: polled into Valeria One, idempotent, every 30 min
def test_reviews_are_polled_once_each_and_at_most_every_30_minutes(google_on):
    g = Google()
    biz = connected(g)
    g.reviews = [review("r1"), review("r2", "TWO", "Tardaron mucho", "Beto", reply="Lo sentimos")]
    now = datetime.now(timezone.utc)
    st = G.sync_all(g.http, now)
    assert st["profiles"] == 1 and st["reviews"] == 2
    assert G.sync_all(g.http, now + timedelta(minutes=5))["profiles"] == 0             # claimed: not again so soon
    g.reviews[0]["comment"] = "Excelente servicio, volveré"
    assert G.sync_all(g.http, now + timedelta(minutes=31))["profiles"] == 1
    with db.tx() as conn:
        rows = G.list_reviews(conn, biz["id"])
        stats = G.review_stats(conn, biz["id"])
        received = conn.execute(select(db.func.count()).select_from(db.events).where(db.events.c.type == "review.received")).scalar()
    assert len(rows) == 2 and received == 2                                    # stored once, edits update the same row
    r1 = next(r for r in rows if r["external_id"] == "r1")
    assert r1["comment"].endswith("volveré") and r1["rating"] == 5 and r1["author"] == "Ana López"
    assert next(r for r in rows if r["external_id"] == "r2")["reply"] == "Lo sentimos"
    assert stats == {"count": 2, "avg": 3.5, "unanswered": 1}
    assert g.count("GET", "fetchMultiDailyMetricsTimeSeries") == 1               # insights once a day, not every 30 min


def test_insights_last_30_days(google_on):
    g = Google()
    biz = connected(g)
    ins = G.insights(biz["id"], g.http)
    assert ins["totals"] == {"calls": 6, "website": 0, "directions": 0, "views": 40} and len(ins["days"]["calls"]) == 30
    q = [c for c in g.calls if c[1] == "businessprofileperformance.googleapis.com"]
    assert q and f"/v1/{LOC}:fetchMultiDailyMetricsTimeSeries" == q[0][2]


# ---------------------------------------------------------------- answers: the owner's OK, always
def test_ai_drafts_but_only_the_owners_approval_publishes(google_on):
    g = Google()
    biz = connected(g)
    g.reviews = [review("r1", "FOUR", "Muy bien, pero tardaron")]
    G.poll_reviews(biz["id"], g.http)
    with db.tx() as conn:
        rid = G.list_reviews(conn, biz["id"])[0]["id"]
        AI.save(conn, biz, "t", provider="gemini", api_key="k", mode="suggest")
    seen = {}

    def gemini(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "¡Gracias, Ana! Trabajamos en ser más rápidos."}]}}]})
    r = G.draft_reply(biz, "t", rid, http=httpx.Client(transport=httpx.MockTransport(gemini)))
    assert r["draft"].startswith("¡Gracias") and r["draft_by_ai"] and r["reply"] is None
    assert "never ask them to change the review" in seen["body"]["contents"][0]["parts"][0]["text"]
    assert g.count("PUT", "/reply") == 0                                       # a draft never goes to Google by itself
    with pytest.raises(Invalid):
        G.send_reply(biz, "t", rid, r["draft"], approved=False, http=g.http)
    assert g.count("PUT", "/reply") == 0
    with pytest.raises(Invalid):
        G.send_reply(biz, "t", rid, "á" * 2049, approved=True, http=g.http)  # 4098 bytes: over Google's 4096
    out = G.send_reply(biz, "t", rid, "¡Gracias, Ana!", approved=True, http=g.http)
    assert out["reply"] == "¡Gracias, Ana!" and out["draft"] is None and g.count("PUT", f"{ACC}/{LOC}/reviews/r1/reply") == 1
    put = [c for c in g.calls if c[0] == "PUT"][0]
    assert json.loads(put[3]) == {"comment": "¡Gracias, Ana!"}


def test_review_screen_needs_the_approve_tick(client, google_on, monkeypatch):
    g = Google()
    biz = connected(g)
    g.reviews = [review("r1")]
    G.poll_reviews(biz["id"], g.http)
    monkeypatch.setattr(G, "_http", lambda http=None: http or g.http)
    owner_session(client, biz)
    page = client.get("/app/google")
    assert page.status_code == 200 and "Excelente servicio" in page.text and 'name="approve"' in page.text
    with db.tx() as conn:
        rid = G.list_reviews(conn, biz["id"])[0]["id"]
    tok = csrf(client, "/app/google")
    client.post(f"/app/google/reviews/{rid}/reply", data={"csrf": tok, "reply": "Gracias", "act": "publish"})   # no tick
    assert g.count("PUT", "/reply") == 0
    client.post(f"/app/google/reviews/{rid}/reply", data={"csrf": tok, "reply": "Gracias", "act": "save"})
    with db.tx() as conn:
        assert G.review(conn, biz["id"], rid)["draft"] == "Gracias"
    r = client.post(f"/app/google/reviews/{rid}/draft", data={"csrf": tok}, follow_redirects=False)      # no AI key: a calm note
    assert r.status_code == 303 and g.count("PUT", "/reply") == 0
    client.post(f"/app/google/reviews/{rid}/reply", data={"csrf": tok, "reply": "Gracias, Ana", "act": "publish", "approve": "1"})
    assert g.count("PUT", "/reply") == 1
    with db.tx() as conn:
        assert G.review(conn, biz["id"], rid)["reply"] == "Gracias, Ana"


# ---------------------------------------------------------------- token expiry (Testing mode: 7 days), limits, 429
def test_invalid_grant_means_reconnect_once_with_a_reminder(client, google_on):
    g = Google()
    biz = connected(g)
    owner = owner_session(client, biz)
    G._tokens.clear()                                                          # the hour-long access token is gone
    g.refresh_error = "invalid_grant"
    mailer.OUTBOX.clear()
    assert G.sync_all(g.http)["failed"] == 1
    with db.tx() as conn:
        lk = G.link(conn, biz["id"])
    assert lk["status"] == "reconnect" and G.reminder(lk) == "reconnect"
    assert [m["to"] for m in mailer.OUTBOX] == [owner] and "/app/google" in mailer.OUTBOX[0]["body"]
    with db.tx() as conn:
        conn.execute(update(db.google_links).values(polled_at=None))
    assert G.sync_all(g.http)["profiles"] == 0 and len(mailer.OUTBOX) == 1    # skipped until reconnected; told once
    today = client.get("/app").text
    assert 'href="/app/google"' in today and ("reconnect Google" in today or "vuelve a conectar Google" in today)
    assert "/app/google/connect" in client.get("/app/google").text
    with db.tx() as conn:
        G.connect(conn, biz, "t", "code-2", http=g.http)                      # reconnecting clears it
        assert G.link(conn, biz["id"])["status"] == "ok"


def test_testing_mode_reminds_on_day_six(google_on, monkeypatch):
    g = Google()
    biz = connected(g)
    with db.tx() as conn:
        lk = G.link(conn, biz["id"])
    assert G.reminder(lk) is None
    assert G.reminder(lk, datetime.now(timezone.utc) + timedelta(days=6, minutes=1)) == "soon"
    monkeypatch.setenv("VO_GOOGLE_OAUTH_TESTING", "off")                       # a published (verified) app: no weekly reminder
    assert G.reminder(lk, datetime.now(timezone.utc) + timedelta(days=30)) is None


def test_429_pauses_every_call_to_google(google_on):
    g = Google()
    biz = connected(g)
    g.slow = True
    with pytest.raises(G.GoogleError) as e:
        G.poll_reviews(biz["id"], g.http)
    assert e.value.slow
    with db.tx() as conn:
        until = G.link(conn, biz["id"])["backoff_until"]
    assert until > datetime.now(timezone.utc) + timedelta(seconds=100)       # Google's Retry-After: 120 s
    g.slow, before = False, len(g.calls)
    with pytest.raises(G.GoogleError):
        G.poll_reviews(biz["id"], g.http)                                      # still paused: Google isn't even asked
    assert len(g.calls) == before
    assert G.sync_all(g.http)["profiles"] == 0


def test_ten_edits_a_minute_per_profile(google_on):
    g = Google()
    biz = connected(g)
    for i in range(10):
        G.create_post(biz["id"], f"Post {i}", http=g.http)
    with pytest.raises(G.GoogleError) as e:
        G.create_post(biz["id"], "one too many", http=g.http)
    assert e.value.slow and g.count("POST", "/localPosts") == 10


def test_local_post_with_a_picture(google_on):
    g = Google()
    biz = connected(g)
    out = G.create_post(biz["id"], "2x1 los martes", "https://one.test/pf/x/1/s", language="es", http=g.http)
    assert out == {"id": f"{ACC}/{LOC}/localPosts/9", "url": "https://g.co/post/9"}
    body = json.loads([c for c in g.calls if c[2].endswith("/localPosts")][0][3])
    assert body == {"languageCode": "es", "summary": "2x1 los martes", "topicType": "STANDARD",
                    "media": [{"mediaFormat": "PHOTO", "sourceUrl": "https://one.test/pf/x/1/s"}]}


# ---------------------------------------------------------------- screens, every language
def test_google_page_renders_in_every_language(client, google_on, monkeypatch):
    g = Google()
    biz = connected(g)
    g.reviews = [review("r1"), review("r2", "THREE", "", "", reply="Gracias")]
    G.poll_reviews(biz["id"], g.http)
    G.insights(biz["id"], g.http)
    G.refresh_info(biz["id"], g.http)
    owner_session(client, biz)
    for lang in LANGS:
        tok = re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)
        client.post(f"/lang/{lang}", data={"csrf": tok})
        r = client.get("/app/google")
        assert r.status_code == 200 and not re.search(r"\{(email|when|start|end)\}", r.text), lang
        assert "Calle 60 #1" in r.text and 'href="/app/google" class="on"' in r.text
    assert 'href="/app/google"' in client.get("/app/connect").text


def test_compliance_rules_stay():
    """No review gating, no rewards for reviews: the friend link never rides with a review request (lead01)."""
    src = open("valeria_one/services/growth.py").read()
    assert 'raise Skip("a review request never carries a reward")' in src
    assert "no discount or reward offers" in G.REPLY_PROMPT and "never ask them to change the review" in G.REPLY_PROMPT
