"""Google Business Profile, with the business's OWN profile (OAuth, scope business.manage).

* Pick the profile: Account Management v1 (accounts) + Business Information v1 (locations, with a readMask).
* Reviews: read from the v4 API (accounts/*/locations/*/reviews) into Valeria One's reviews, at most every
  30 minutes by a job, the same review stored once. The owner answers from here. The AI may write a DRAFT
  answer; nothing goes to Google until the owner approves that exact text. Answers are at most 4096 bytes.
* Posts: a local post (v4 localPosts) with the text and, optionally, one public JPG/PNG picture (≤ 5 MB) —
  used by services/posts.py.
* Insights: the Performance API (fetchMultiDailyMetricsTimeSeries): calls, website clicks, direction requests
  and impressions, last 30 days.
* Info: the name, hours and phone Google shows, read-only.

Rules we keep: at most 10 changes a minute per profile (Google's limit for edits), and when Google answers 429
we wait (backoff_until) before asking anything again. Valeria One never writes reviews, never asks only the
happy customers, never offers anything for a review (services/reviews.py and growth.py keep those rules).

Settings (environment): VO_GOOGLE_BUSINESS=on plus the server's Google OAuth client (VO_GOOGLE_CLIENT_ID /
VO_GOOGLE_CLIENT_SECRET, the same as Calendar), with https://YOUR-SERVER/app/google/callback as an authorised
redirect URI. Without them it stays off ("not connected yet"). See docs/GOOGLE_SETUP.md.

While the Google Cloud app is in Testing mode (VO_GOOGLE_OAUTH_TESTING, on unless set to off) Google ends the
access after 7 days: we remind the owner on day 6, and when Google answers invalid_grant the profile shows
"reconnect Google" (and the owners get one email).
"""
from __future__ import annotations

import base64
import json
import time as _time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, NotFound, log

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPES = "openid email https://www.googleapis.com/auth/business.manage"
ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
V4 = "https://mybusiness.googleapis.com/v4"
PERF = "https://businessprofileperformance.googleapis.com/v1"
READ_MASK = "name,title,phoneNumbers,regularHours,websiteUri,storefrontAddress"
STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
METRICS = {"CALL_CLICKS": "calls", "WEBSITE_CLICKS": "website", "BUSINESS_DIRECTION_REQUESTS": "directions",
           "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "views", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "views",
           "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "views", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "views"}
POLL_MINUTES = 30                   # reviews are read at most this often per profile
EDITS_PER_MINUTE = 10               # Google: 10 edits a minute per profile
BACKOFF_SECONDS = 300               # after a 429 (or Google's Retry-After when it gives one)
REPLY_MAX_BYTES = 4096
POST_MAX_CHARS = 1500
TESTING_DAYS = 7                    # an app in Testing mode: refresh tokens die after 7 days
GL, R = db.google_links, db.reviews
_tokens: dict[str, tuple[str, float]] = {}


class GoogleError(Exception):
    def __init__(self, message: str, reconnect: bool = False, slow: bool = False):
        super().__init__(message)
        self.reconnect = reconnect          # Google ended the access: the owner connects again
        self.slow = slow                    # Google said 429: wait


def configured() -> bool:
    return (bool(config.get("VO_GOOGLE_CLIENT_ID") and config.get("VO_GOOGLE_CLIENT_SECRET"))
            and (config.get("VO_GOOGLE_BUSINESS") or "off").lower() == "on" and not config.SANDBOX())


def testing_mode() -> bool:
    return (config.get("VO_GOOGLE_OAUTH_TESTING") or "on").lower() != "off"


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL()}/app/google/callback"


def _http(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=20)


def auth_url(state: str) -> str:
    return AUTH + "?" + urlencode({"client_id": config.get("VO_GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri(),
                                   "response_type": "code", "scope": SCOPES, "access_type": "offline", "prompt": "consent",
                                   "include_granted_scopes": "true", "state": state})


def _email(id_token: str | None) -> str | None:
    """Only to show 'connected as …' — the token came straight from Google over TLS."""
    try:
        part = (id_token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email")
    except Exception:
        return None


# ---------------------------------------------------------------- connecting
def connect(conn: Connection, biz: dict, actor: str, code: str, http=None) -> dict:
    if not configured():
        raise Invalid("Google Business Profile is not set up on this server yet", "google")
    if biz["sandbox"]:
        raise Invalid("sandbox businesses can't connect a real Google profile", "google")
    r = _http(http).post(TOKEN, data={"code": code, "client_id": config.get("VO_GOOGLE_CLIENT_ID"),
                                      "client_secret": config.get("VO_GOOGLE_CLIENT_SECRET"),
                                      "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    j = _body(r)
    if r.status_code >= 300 or not j.get("refresh_token"):
        raise Invalid("Google did not give lasting access — try connecting again", "google")
    tokens = crypto.seal({"refresh_token": j["refresh_token"]})
    vals = {"tokens": tokens, "account_email": _email(j.get("id_token")), "status": "ok", "last_error": None,
            "reminded_at": None, "backoff_until": None, "connected_by": actor, "created_at": func.now(), "updated_at": func.now()}
    conn.execute(pg_insert(GL).values(business_id=biz["id"], **vals)
                 .on_conflict_do_update(index_elements=[GL.c.business_id], set_=vals))
    _tokens[str(biz["id"])] = (j.get("access_token", ""), _time.time() + int(j.get("expires_in", 0)) - 60)
    log(conn, biz["id"], "google.connected", actor, biz["id"], {"account": _email(j.get("id_token"))})
    return link(conn, biz["id"])


def link(conn: Connection, business_id: UUID) -> dict | None:
    r = conn.execute(select(GL).where(GL.c.business_id == business_id)).mappings().first()
    return dict(r) if r else None


def disconnect(conn: Connection, business_id: UUID, actor: str) -> None:
    """Forget the profile (its reviews stay, as written then)."""
    if conn.execute(delete(GL).where(GL.c.business_id == business_id)).rowcount:
        _tokens.pop(str(business_id), None)
        log(conn, business_id, "google.disconnected", actor, business_id)


def _body(r: httpx.Response) -> dict:
    try:
        j = r.json()
        return j if isinstance(j, dict) else {}
    except ValueError:
        return {}


def _access(business_id: UUID, http=None) -> str:
    """A working access token, from the sealed refresh token. invalid_grant = Google ended it: 'reconnect Google'."""
    tok, exp = _tokens.get(str(business_id), ("", 0))
    if tok and exp > _time.time():
        return tok
    with db.tx() as conn:
        lk = link(conn, business_id)
    if not lk or lk["status"] == "reconnect":
        raise GoogleError("reconnect Google", reconnect=True)
    refresh = crypto.open_(lk["tokens"]).get("refresh_token")
    r = _http(http).post(TOKEN, data={"client_id": config.get("VO_GOOGLE_CLIENT_ID"), "client_secret": config.get("VO_GOOGLE_CLIENT_SECRET"),
                                      "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code >= 300:
        if "invalid_grant" in r.text:
            needs_reconnect(business_id, "Google ended the access (it does that every 7 days while the app is in Testing)")
            raise GoogleError("reconnect Google", reconnect=True)
        raise GoogleError(f"Google answered {r.status_code}")
    j = _body(r)
    _tokens[str(business_id)] = (j.get("access_token", ""), _time.time() + int(j.get("expires_in", 3600)) - 60)
    return j.get("access_token", "")


def needs_reconnect(business_id: UUID, why: str) -> None:
    """Mark the profile 'reconnect Google' and tell the owners once (by email)."""
    from . import mailer
    _tokens.pop(str(business_id), None)
    with db.tx() as conn:
        row = conn.execute(update(GL).where(GL.c.business_id == business_id, GL.c.status == "ok")
                           .values(status="reconnect", last_error=why[:300], updated_at=func.now()).returning(GL.c.business_id)).first()
        if not row:
            return
        log(conn, business_id, "google.reconnect", "system", business_id)
        name = conn.execute(select(db.businesses.c.name).where(db.businesses.c.id == business_id)).scalar()
        emails = [r[0] for r in conn.execute(select(db.users.c.email).join(db.memberships, db.memberships.c.user_id == db.users.c.id)
                                             .where(db.memberships.c.business_id == business_id, db.memberships.c.role == "owner"))]
        conn.execute(update(GL).where(GL.c.business_id == business_id).values(reminded_at=func.now()))
    for e in emails:
        mailer.send(e, f"{name}: reconnect Google", f"Google stopped sharing your Business Profile with Valeria One.\n\n"
                    f"Open {config.PUBLIC_URL()}/app/google and tap \"Reconnect Google\" (it takes a minute). Your reviews "
                    f"and posts are kept.")


def reminder(lk: dict | None, now: datetime | None = None) -> str | None:
    """What the screens say: 'reconnect' (Google ended it) or 'soon' (Testing mode, day 6 of 7) or nothing."""
    if not lk:
        return None
    if lk["status"] == "reconnect":
        return "reconnect"
    now = now or datetime.now(timezone.utc)
    if testing_mode() and lk["created_at"] < now - timedelta(days=TESTING_DAYS - 1):
        return "soon"
    return None


# ---------------------------------------------------------------- talking to Google (rate limits, 429)
def _call(business_id: UUID, method: str, url: str, http=None, *, edit: bool = False, **kw) -> dict:
    """One request to Google for this profile. Edits count against 10 a minute; a 429 starts a pause."""
    now = datetime.now(timezone.utc)
    with db.tx() as conn:
        lk = link(conn, business_id)
        if lk and lk["backoff_until"] and lk["backoff_until"] > now:
            raise GoogleError("Google asked us to slow down; try again in a few minutes", slow=True)
        if edit:
            E = db.events
            n = conn.execute(select(func.count()).select_from(E).where(E.c.business_id == business_id, E.c.type == "google.edit",
                                                                       E.c.created_at > now - timedelta(minutes=1))).scalar()
            if n >= EDITS_PER_MINUTE:
                raise GoogleError(f"Google allows {EDITS_PER_MINUTE} changes a minute; wait a minute and try again", slow=True)
            log(conn, business_id, "google.edit", "system", business_id, {"method": method})
    h = _http(http)
    r = h.request(method, url, headers={"Authorization": f"Bearer {_access(business_id, h)}"}, **kw)
    if r.status_code == 429:
        try:
            wait = max(30, min(3600, int(r.headers.get("retry-after") or BACKOFF_SECONDS)))
        except ValueError:
            wait = BACKOFF_SECONDS
        with db.tx() as conn:
            conn.execute(update(GL).where(GL.c.business_id == business_id).values(backoff_until=now + timedelta(seconds=wait)))
        raise GoogleError("Google asked us to slow down; try again in a few minutes", slow=True)
    if r.status_code == 401:
        _tokens.pop(str(business_id), None)
    if r.status_code >= 300:
        e = _body(r).get("error")
        msg = (e.get("message") if isinstance(e, dict) else e) or f"answered {r.status_code}"
        raise GoogleError(f"Google: {str(msg)[:200]}")
    return _body(r)


# ---------------------------------------------------------------- (a) pick the profile, (e) its info
def locations(business_id: UUID, http=None) -> list[dict]:
    """Every profile this Google account manages: [{account, location, title, address}]."""
    out = []
    accs = _call(business_id, "GET", f"{ACCOUNTS}/accounts", http).get("accounts", [])
    for a in accs[:20]:
        token = None
        for _ in range(10):                                    # 100 a page; a business has a handful
            params = {"readMask": READ_MASK, "pageSize": 100, **({"pageToken": token} if token else {})}
            j = _call(business_id, "GET", f"{INFO}/{a['name']}/locations", http, params=params)
            for loc in j.get("locations", []):
                out.append({"account": a["name"], "location": loc["name"], "title": loc.get("title") or "",
                            "address": _address(loc)})
            token = j.get("nextPageToken")
            if not token:
                break
    return out


def _address(loc: dict) -> str:
    a = loc.get("storefrontAddress") or {}
    return ", ".join(x for x in [*(a.get("addressLines") or []), a.get("locality"), a.get("administrativeArea")] if x)


def choose(conn: Connection, biz: dict, actor: str, account: str, location: str, http=None) -> dict:
    """The owner picked one of its profiles (it must be one this Google account manages)."""
    mine = next((x for x in locations(biz["id"], http) if x["account"] == account and x["location"] == location), None)
    if not mine:
        raise Invalid("pick one of your profiles", "location")
    conn.execute(update(GL).where(GL.c.business_id == biz["id"]).values(account=account, location=location, polled_at=None,
                                                                         location_title=mine["title"][:300], updated_at=func.now()))
    log(conn, biz["id"], "google.location", actor, biz["id"], {"title": mine["title"][:100]})
    return link(conn, biz["id"])


def _hours(loc: dict) -> dict:
    """Google's regularHours.periods -> {"mon": [["09:00", "18:00"]], …} (as the rest of Valeria One keeps hours)."""
    days = {"MONDAY": "mon", "TUESDAY": "tue", "WEDNESDAY": "wed", "THURSDAY": "thu", "FRIDAY": "fri", "SATURDAY": "sat",
            "SUNDAY": "sun"}
    hm = lambda t: f"{int((t or {}).get('hours', 0)) % 24:02d}:{int((t or {}).get('minutes', 0)):02d}"   # noqa: E731
    out: dict[str, list] = {}
    for p in (loc.get("regularHours") or {}).get("periods", []):
        d = days.get(p.get("openDay"))
        if d:
            close = hm(p.get("closeTime"))
            out.setdefault(d, []).append([hm(p.get("openTime")), "23:59" if close == "00:00" else close])
    return out


def refresh_info(business_id: UUID, http=None) -> dict:
    """(e) The name, phone, hours and website Google shows — read-only here."""
    with db.tx() as conn:
        lk = link(conn, business_id)
    if not lk or not lk["location"]:
        return {}
    loc = _call(business_id, "GET", f"{INFO}/{lk['location']}", http, params={"readMask": READ_MASK})
    info = {"name": loc.get("title") or "", "phone": (loc.get("phoneNumbers") or {}).get("primaryPhone") or "",
            "website": loc.get("websiteUri") or "", "address": _address(loc), "hours": _hours(loc)}
    with db.tx() as conn:
        conn.execute(update(GL).where(GL.c.business_id == business_id).values(info=info, updated_at=func.now()))
    return info


# ---------------------------------------------------------------- (b) reviews
def _v4(lk: dict) -> str:
    """The v4 path of the chosen profile: accounts/1/locations/2."""
    return f"{lk['account']}/{lk['location']}"


def _when(v: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((v or "").replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def poll_reviews(business_id: UUID, http=None) -> int:
    """Read the profile's reviews (newest first, up to 200) into Valeria One. Same review = one row (updated if edited)."""
    with db.tx() as conn:
        lk = link(conn, business_id)
    if not lk or not lk["location"]:
        return 0
    seen, token = 0, None
    for _ in range(4):
        params = {"pageSize": 50, "orderBy": "updateTime desc", **({"pageToken": token} if token else {})}
        j = _call(business_id, "GET", f"{V4}/{_v4(lk)}/reviews", http, params=params)
        with db.tx() as conn:
            for rv in j.get("reviews", []):
                rid = rv.get("reviewId") or (rv.get("name") or "").split("/")[-1]
                if not rid:
                    continue
                who = rv.get("reviewer") or {}
                rep = rv.get("reviewReply") or {}
                vals = {"author": "" if who.get("isAnonymous") else (who.get("displayName") or "")[:200],
                        "rating": STARS.get(rv.get("starRating")), "comment": (rv.get("comment") or "")[:5000],
                        "reviewed_at": _when(rv.get("createTime")), "reply": rep.get("comment"),
                        "replied_at": _when(rep.get("updateTime"))}
                ins = pg_insert(R).values(business_id=business_id, source="google", external_id=rid[:200], **vals)
                new = conn.execute(ins.on_conflict_do_update(constraint="uq_review", set_={**vals, "updated_at": func.now()})
                                   .returning(R.c.id, (R.c.created_at == R.c.updated_at).label("new"))).first()
                if new and new.new:
                    log(conn, business_id, "review.received", "google", new.id, {"rating": vals["rating"]})
                seen += 1
        token = j.get("nextPageToken")
        if not token:
            break
    return seen


def list_reviews(conn: Connection, business_id: UUID, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(select(R).where(R.c.business_id == business_id)
                                          .order_by(R.c.reviewed_at.desc().nulls_last(), R.c.created_at.desc()).limit(limit)).mappings()]


def review(conn: Connection, business_id: UUID, review_id: UUID) -> dict:
    r = conn.execute(select(R).where(R.c.business_id == business_id, R.c.id == review_id)).mappings().first()
    if not r:
        raise NotFound("review")
    return dict(r)


def _clean_reply(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise Invalid("write the answer first", "reply")
    if len(text.encode("utf-8")) > REPLY_MAX_BYTES:
        raise Invalid(f"Google takes answers up to {REPLY_MAX_BYTES} bytes; make it shorter", "reply")
    return text


def save_draft(conn: Connection, biz: dict, actor: str, review_id: UUID, text: str, by_ai: bool = False) -> dict:
    review(conn, biz["id"], review_id)
    conn.execute(update(R).where(R.c.business_id == biz["id"], R.c.id == review_id)
                 .values(draft=_clean_reply(text), draft_by_ai=by_ai, reply_error=None, updated_at=func.now()))
    log(conn, biz["id"], "review.drafted", actor, review_id, {"by_ai": by_ai})
    return review(conn, biz["id"], review_id)


REPLY_PROMPT = ("Write a short public answer from the business to this Google review: thank the customer by first name if "
                "given, answer what they said (an apology and an offer to talk in private if it is a complaint), at most "
                "4 sentences, warm and professional, no emojis overload, no discount or reward offers, never ask them to "
                "change the review. Only the answer, nothing else.")


def draft_reply(biz: dict, actor: str, review_id: UUID, http=None) -> dict:
    """The AI writes a DRAFT answer, in the business's language and tone. It is saved, never sent: the owner reads,
    edits and approves it. -> the review with its draft."""
    from . import ai as AI
    from . import studio as ST
    with db.tx() as conn:
        rv = review(conn, biz["id"], review_id)
        tone = (AI.settings(conn, biz["id"]) or {}).get("instructions") or ""
    system = (f"You write for {biz['name']}, a small business. Write in {ST._lang_name(biz, None)}. "
              + (f"The owner's notes on tone and facts: {tone[:1500]}" if tone else ""))
    stars = f"{rv['rating']} of 5 stars" if rv["rating"] else "no star rating"
    prompt = f"{REPLY_PROMPT}\n\nReviewer: {rv['author'] or 'a customer'}\nRating: {stars}\nReview: {rv['comment'] or '(no text)'}"
    text = ST._write(biz, system, prompt, http=http, max_tokens=600).strip()
    while len(text.encode("utf-8")) > REPLY_MAX_BYTES:
        text = text[: len(text) - 50]
    with db.tx() as conn:
        return save_draft(conn, biz, actor, review_id, text, by_ai=True)


def send_reply(biz: dict, actor: str, review_id: UUID, text: str, *, approved: bool, http=None) -> dict:
    """Publish the owner's answer on Google. `approved` is the owner's own tap on "Approve and publish" for exactly
    this text; without it nothing is sent (an AI draft never goes out by itself)."""
    if not approved:
        raise Invalid("approve the answer before it goes to Google", "approve")
    text = _clean_reply(text)
    with db.tx() as conn:
        rv = review(conn, biz["id"], review_id)
        lk = link(conn, biz["id"])
    if not lk or not lk["location"] or rv["source"] != "google":
        raise Invalid("connect your Google profile first", "google")
    try:
        _call(biz["id"], "PUT", f"{V4}/{_v4(lk)}/reviews/{rv['external_id']}/reply", http, edit=True, json={"comment": text})
    except GoogleError as e:
        with db.tx() as conn:
            conn.execute(update(R).where(R.c.id == review_id).values(reply_error=str(e)[:300], draft=text))
        raise Invalid(str(e), "google") from None
    with db.tx() as conn:
        conn.execute(update(R).where(R.c.id == review_id).values(reply=text, replied_at=func.now(), draft=None, draft_by_ai=False,
                                                                 reply_error=None, updated_at=func.now()))
        log(conn, biz["id"], "review.replied", actor, review_id, {"source": "google", "by_ai_draft": rv["draft_by_ai"]})
        return review(conn, biz["id"], review_id)


def review_stats(conn: Connection, business_id: UUID) -> dict:
    row = conn.execute(select(func.count(), func.avg(R.c.rating), func.count().filter(R.c.reply.is_(None)))
                       .where(R.c.business_id == business_id)).one()
    return {"count": row[0], "avg": round(float(row[1]), 1) if row[1] is not None else None, "unanswered": row[2]}


# ---------------------------------------------------------------- (c) posts
def create_post(business_id: UUID, text: str, image_url: str | None = None, *, language: str = "en", http=None) -> dict:
    """A local post on the profile: the text and, if given, one public picture. -> {"id", "url"}."""
    with db.tx() as conn:
        lk = link(conn, business_id)
    if not lk or not lk["location"] or lk["status"] != "ok":
        raise GoogleError("reconnect Google" if lk and lk["status"] == "reconnect" else "connect your Google profile first",
                          reconnect=bool(lk))
    body = {"languageCode": language, "summary": text[:POST_MAX_CHARS], "topicType": "STANDARD"}
    if image_url:
        body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": image_url}]
    j = _call(business_id, "POST", f"{V4}/{_v4(lk)}/localPosts", http, edit=True, json=body)
    return {"id": j.get("name"), "url": j.get("searchUrl")}


# ---------------------------------------------------------------- (d) insights
def insights(business_id: UUID, http=None, today: date | None = None) -> dict:
    """The last 30 days: totals and one number a day for calls, website clicks, directions and views."""
    with db.tx() as conn:
        lk = link(conn, business_id)
    if not lk or not lk["location"]:
        return {}
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    start = end - timedelta(days=29)
    params = [("dailyMetrics", m) for m in METRICS] + [
        ("dailyRange.startDate.year", start.year), ("dailyRange.startDate.month", start.month), ("dailyRange.startDate.day", start.day),
        ("dailyRange.endDate.year", end.year), ("dailyRange.endDate.month", end.month), ("dailyRange.endDate.day", end.day)]
    loc = lk["location"] if lk["location"].startswith("locations/") else f"locations/{lk['location']}"
    j = _call(business_id, "GET", f"{PERF}/{loc}:fetchMultiDailyMetricsTimeSeries", http, params=params)
    days = [(start + timedelta(days=i)).isoformat() for i in range(30)]
    series = {k: {d: 0 for d in days} for k in ("calls", "website", "directions", "views")}
    for multi in j.get("multiDailyMetricTimeSeries", []):
        for ts in multi.get("dailyMetricTimeSeries", []):
            key = METRICS.get(ts.get("dailyMetric"))
            for dv in (ts.get("timeSeries") or {}).get("datedValues", []):
                d = dv.get("date") or {}
                try:
                    iso = date(int(d["year"]), int(d["month"]), int(d["day"])).isoformat()
                except (KeyError, ValueError, TypeError):
                    continue
                if key and iso in series[key]:
                    series[key][iso] += int(dv.get("value") or 0)
    out = {"from": start.isoformat(), "to": end.isoformat(), "totals": {k: sum(v.values()) for k, v in series.items()},
           "days": {k: [v[d] for d in days] for k, v in series.items()}}
    with db.tx() as conn:
        conn.execute(update(GL).where(GL.c.business_id == business_id).values(insights=out, insights_at=func.now()))
    return out


# ---------------------------------------------------------------- the job (every minute; each profile at most every 30 min)
def sync_all(http=None, now: datetime | None = None) -> dict:
    """Reviews (and once a day the info and insights) for every connected profile. A profile is claimed by setting
    polled_at first, so two workers never read the same one, and a slow or failing Google never stops the others."""
    if not configured():
        return {"profiles": 0}
    now = now or datetime.now(timezone.utc)
    stats = {"profiles": 0, "reviews": 0, "failed": 0}
    with db.tx() as conn:
        due = [r[0] for r in conn.execute(
            update(GL).where(GL.c.status == "ok", GL.c.location.isnot(None),
                             (GL.c.polled_at.is_(None)) | (GL.c.polled_at < now - timedelta(minutes=POLL_MINUTES)),
                             (GL.c.backoff_until.is_(None)) | (GL.c.backoff_until < now))
            .values(polled_at=now).returning(GL.c.business_id, GL.c.insights_at))]
        fresh_ins = {r[0] for r in conn.execute(select(GL.c.business_id).where(GL.c.insights_at > now - timedelta(hours=20)))}
    for bid in due:
        stats["profiles"] += 1
        try:
            stats["reviews"] += poll_reviews(bid, http)
            if bid not in fresh_ins:
                refresh_info(bid, http)
                insights(bid, http, now.date())
        except (GoogleError, httpx.HTTPError, KeyError, ValueError):
            stats["failed"] += 1                               # the next round (30 min) tries again
    return stats
