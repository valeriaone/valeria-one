"""Two-way Google Calendar for a business.

* Out: every appointment (booked here, on the public page or by API) appears in the business's Google
  Calendar, and changes / cancellations follow it. Done by `sync()`, run every minute by a job, so a slow or
  down Google never slows a booking.
* In: time the owner blocks in Google (a dentist appointment, a day off) is treated as busy, so the public
  booking page never offers it.

Settings: VO_GOOGLE_CLIENT_ID, VO_GOOGLE_CLIENT_SECRET (a Google Cloud OAuth client, type "Web application",
redirect URI https://YOUR-SERVER/app/calendar/google/callback). Without them the feature stays off.
"""
from __future__ import annotations

import base64
import json
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import config, crypto, db
from . import Invalid, log

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = "openid email https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy"
L, AP = db.calendar_links, db.appointments
_tokens: dict[str, tuple[str, float]] = {}


def configured() -> bool:
    return bool(config.get("VO_GOOGLE_CLIENT_ID") and config.get("VO_GOOGLE_CLIENT_SECRET"))


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL()}/app/calendar/google/callback"


def _http(http: httpx.Client | None = None) -> httpx.Client:
    return http or httpx.Client(timeout=8)


def auth_url(state: str) -> str:
    return AUTH + "?" + urlencode({"client_id": config.get("VO_GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri(),
                                   "response_type": "code", "scope": SCOPES, "access_type": "offline",
                                   "prompt": "consent", "include_granted_scopes": "true", "state": state})


def _email_from_id_token(tok: str | None) -> str | None:
    """Only to show 'connected as …' — the token came straight from Google over TLS."""
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email")
    except Exception:
        return None


def connect(conn: Connection, biz: dict, actor: str, code: str, http=None) -> dict:
    if not configured():
        raise Invalid("Google Calendar is not set up on this server yet", "google")
    if biz["sandbox"] or config.SANDBOX():
        raise Invalid("sandbox businesses can't connect a real calendar", "google")
    r = _http(http).post(TOKEN, data={"code": code, "client_id": config.get("VO_GOOGLE_CLIENT_ID"),
                                      "client_secret": config.get("VO_GOOGLE_CLIENT_SECRET"),
                                      "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 300 or not j.get("refresh_token"):
        raise Invalid("Google did not give lasting access — try connecting again", "google")
    row = conn.execute(pg_insert(L).values(business_id=biz["id"], provider="google", account_email=_email_from_id_token(j.get("id_token")),
                                           refresh_token=crypto.seal({"secret": j["refresh_token"]})["secret"])
                       .on_conflict_do_update(constraint="uq_calendar_link", set_={
                           "refresh_token": crypto.seal({"secret": j["refresh_token"]})["secret"],
                           "account_email": _email_from_id_token(j.get("id_token")), "last_error": None})
                       .returning(L)).mappings().one()
    _tokens[str(row["id"])] = (j.get("access_token", ""), _time.time() + int(j.get("expires_in", 0)) - 60)
    # everything still to come goes to Google on the next sync
    conn.execute(update(AP).where(AP.c.business_id == biz["id"], AP.c.ends_at > func.now()).values(synced_at=None))
    log(conn, biz["id"], "calendar.connected", actor, row["id"], {"provider": "google"})
    return dict(row)


def link(conn: Connection, business_id: UUID) -> dict | None:
    r = conn.execute(select(L).where(L.c.business_id == business_id, L.c.provider == "google")).mappings().first()
    return dict(r) if r else None


def disconnect(conn: Connection, business_id: UUID, actor: str) -> None:
    lk = link(conn, business_id)
    if lk:
        conn.execute(L.delete().where(L.c.id == lk["id"]))
        _tokens.pop(str(lk["id"]), None)
        log(conn, business_id, "calendar.disconnected", actor, lk["id"])


def _access(lk: dict, http) -> str:
    tok, exp = _tokens.get(str(lk["id"]), ("", 0))
    if tok and exp > _time.time():
        return tok
    refresh = crypto.open_({"secret": lk["refresh_token"]})["secret"]
    r = _http(http).post(TOKEN, data={"client_id": config.get("VO_GOOGLE_CLIENT_ID"), "client_secret": config.get("VO_GOOGLE_CLIENT_SECRET"),
                                      "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code >= 300:
        msg = "Google access was removed — connect the calendar again" if "invalid_grant" in r.text else f"Google answered {r.status_code}"
        with db.tx() as conn:
            conn.execute(update(L).where(L.c.id == lk["id"]).values(last_error=msg))
        raise GoogleError(msg)
    j = r.json()
    _tokens[str(lk["id"])] = (j["access_token"], _time.time() + int(j.get("expires_in", 3600)) - 60)
    return j["access_token"]


class GoogleError(Exception):
    pass


# ---------------------------------------------------------------- in: busy time
def busy(conn: Connection, business_id: UUID, start: datetime, end: datetime, http=None) -> list[tuple[datetime, datetime]]:
    """Busy intervals from the owner's Google Calendar. Any problem -> [] (bookings never break on Google)."""
    lk = link(conn, business_id)
    if not lk or not configured():
        return []
    try:
        h = _http(http)
        r = h.post(f"{API}/freeBusy", headers={"Authorization": f"Bearer {_access(lk, h)}"},
                   json={"timeMin": start.astimezone(timezone.utc).isoformat(), "timeMax": end.astimezone(timezone.utc).isoformat(),
                         "items": [{"id": lk["calendar_id"]}]})
        if r.status_code >= 300:
            return []
        cal = (r.json().get("calendars") or {}).get(lk["calendar_id"]) or {}
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in cal.get("busy", [])]
    except (GoogleError, httpx.HTTPError, ValueError, KeyError):
        return []


# ---------------------------------------------------------------- out: push appointments
def sync(business_id: UUID, http=None, now: datetime | None = None) -> dict:
    """Push every appointment that changed since its last sync. Safe to run any time, as often as wanted."""
    now = now or datetime.now(timezone.utc)
    stats = {"created": 0, "updated": 0, "deleted": 0, "failed": 0}
    with db.tx() as conn:
        lk = link(conn, business_id)
        if not lk or not configured():
            return stats
        biz = dict(conn.execute(select(db.businesses).where(db.businesses.c.id == business_id)).mappings().one())
        todo = conn.execute(select(AP, db.contacts.c.name.label("contact_name"))
                            .outerjoin(db.contacts, db.contacts.c.id == AP.c.contact_id)
                            .where(AP.c.business_id == business_id,
                                   (AP.c.synced_at.is_(None)) | (AP.c.updated_at > AP.c.synced_at),
                                   (AP.c.ends_at > now - timedelta(days=1)) | AP.c.external_id.isnot(None))
                            .order_by(AP.c.starts_at).limit(200)).mappings().all()
    h = _http(http)
    try:
        auth = {"Authorization": f"Bearer {_access(lk, h)}"}
    except GoogleError:
        stats["failed"] = len(todo)
        return stats
    base = f"{API}/calendars/{lk['calendar_id']}/events"
    for a in todo:
        try:
            ext = a["external_id"]
            if a["status"] == "cancelled":
                if ext:
                    r = h.delete(f"{base}/{ext}", headers=auth)
                    if r.status_code not in (200, 204, 404, 410):
                        raise GoogleError(str(r.status_code))
                    stats["deleted"] += 1
                _mark(a, None)
                continue
            body = {"summary": a["title"] + (f" — {a['contact_name']}" if a["contact_name"] else ""),
                    "description": ((a["notes"] or "") + "\n\nValeria One").strip(),
                    "location": a["location"] or None,
                    "start": {"dateTime": a["starts_at"].isoformat(), "timeZone": biz["timezone"]},
                    "end": {"dateTime": a["ends_at"].isoformat(), "timeZone": biz["timezone"]},
                    "extendedProperties": {"private": {"vo_id": str(a["id"])}}}
            if ext:
                r = h.put(f"{base}/{ext}", headers=auth, json=body)
                if r.status_code in (404, 410):
                    ext = None                                 # deleted in Google: put it back
                elif r.status_code >= 300:
                    raise GoogleError(str(r.status_code))
                else:
                    stats["updated"] += 1
            if not ext:
                r = h.post(base, headers=auth, json=body)
                if r.status_code >= 300:
                    raise GoogleError(str(r.status_code))
                ext = r.json()["id"]
                stats["created"] += 1
            _mark(a, ext)
        except (GoogleError, httpx.HTTPError, KeyError, ValueError):
            stats["failed"] += 1                               # stays unsynced; next run retries
    return stats


def _mark(a, external_id: str | None) -> None:
    """synced_at = the version we pushed; if it changed meanwhile, updated_at is newer and it goes again."""
    with db.tx() as conn:
        conn.execute(update(AP).where(AP.c.id == a["id"]).values(external_id=external_id, synced_at=a["updated_at"]))


def sync_all(http=None) -> dict:
    with db.tx() as conn:
        ids = [r[0] for r in conn.execute(select(L.c.business_id).where(L.c.provider == "google"))]
    total = {"businesses": len(ids), "created": 0, "updated": 0, "deleted": 0, "failed": 0}
    for bid in ids:
        for k, v in sync(bid, http).items():
            total[k] += v
    return total
