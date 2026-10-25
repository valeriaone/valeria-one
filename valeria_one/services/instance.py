"""This server: its first-time setup, who runs it (the admin), who may sign up, and what the admin sees on
the Server page (version, background jobs, backups, updates, the businesses it hosts).

Backups and updates run on the host (deploy/valeria-one), never inside the app. The app asks for one by
dropping a file in a folder the host watches (VO_OPS_DIR/requests) and reads back the results the host
writes (VO_OPS_DIR/status, VO_BACKUPS_DIR). The app never gets Docker, root or the host's shell.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from .. import __version__, config, crypto, db
from . import Invalid, NotFound, businesses, log, team, users

KV, U, MB, B = db.instance_kv, db.users, db.memberships, db.businesses
OPS = ("backup", "update")
BACKUP_NAME = re.compile(r"^vo-backup-\d{8}T\d{6}Z(-[a-z0-9.-]{1,40})?\.tar\.gz$")


# ---------------------------------------------------------------- who may come in
def signup_open() -> bool:
    """VO_SIGNUP=closed: only the first account (at /setup) and people invited. The sandbox is always open."""
    return config.SANDBOX() or (config.get("VO_SIGNUP") or "open").strip().lower() != "closed"


def setup_token() -> str:
    """Whoever can read the server's secret key can compute it (the host CLI prints it); nobody else can."""
    return crypto.hmac_sha256_hex(config.get("VO_SECRET_KEY", required=True), b"valeria-one:setup")[:32]


def setup_link() -> str:
    return f"{config.PUBLIC_URL()}/setup?k={setup_token()}"


def needs_setup(conn: Connection) -> bool:
    return conn.execute(select(U.c.id).limit(1)).first() is None


def setup(conn: Connection, *, token: str, email: str, name: str, password: str, business_name: str,
          country: str, industry: str) -> tuple[dict, dict]:
    """The first account: it runs this server and owns its first business. Works once."""
    if not secrets.compare_digest(str(token or ""), setup_token()):
        raise Invalid("this setup link is not valid — run `valeria-one setup-link` on the server", "k")
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('valeria-one:setup'))"))     # two tabs, one admin
    if not needs_setup(conn):
        raise Invalid("this server is already set up — sign in", "setup")
    user, biz = users.register(conn, email=email, name=name, password=password, business_name=business_name,
                               country=country, industry=industry)
    conn.execute(update(U).where(U.c.id == user["id"]).values(is_admin=True))
    conn.execute(update(B).where(B.c.id == biz["id"]).values(billing_exempt=True))   # the server's own business is never charged
    biz = {**biz, "billing_exempt": True}
    log(conn, biz["id"], "server.setup", f"user:{user['id']}", user["id"])
    return {**user, "is_admin": True}, biz


def make_admin(conn: Connection, email: str) -> dict:
    e = users._email(email)
    row = conn.execute(update(U).where(func.lower(U.c.email) == e).values(is_admin=True).returning(U.c.id, U.c.email)).first()
    if not row:
        raise NotFound("user")
    return {"id": row.id, "email": row.email}


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("is_admin")) and not config.SANDBOX()


# ---------------------------------------------------------------- small facts about the server
def kv_get(conn: Connection, key: str) -> dict | None:
    v = conn.execute(select(KV.c.value).where(KV.c.key == key)).scalar()
    return dict(v) if v is not None else None


def kv_set(conn: Connection, key: str, value: dict) -> None:
    conn.execute(pg_insert(KV).values(key=key, value=value)
                 .on_conflict_do_update(index_elements=[KV.c.key], set_={"value": value, "updated_at": func.now()}))


def heartbeat(results: dict, errors: dict) -> None:
    """Background jobs ran (called by valeria_one.jobs after each round)."""
    with db.tx() as conn:
        kv_set(conn, "jobs", {"at": datetime.now(timezone.utc).isoformat(), "errors": errors,
                              "ok": sorted(k for k in results if k not in errors)})


# ---------------------------------------------------------------- backups and updates (done by the host)
def _dir(var: str) -> Path | None:
    v = config.get(var)
    return Path(v) if v else None


def managed() -> bool:
    """Is a host watching? (the one-click and Docker installs; a hand-made install may not have it)"""
    d = _dir("VO_OPS_DIR")
    return bool(d and (d / "requests").is_dir() and os.access(d / "requests", os.W_OK))


def request(op: str, actor: str) -> None:
    if op not in OPS:
        raise Invalid(f"unknown request {op!r}", "op")
    if not managed():
        raise Invalid("backups and updates are not managed by the app on this server", "op")
    req = _dir("VO_OPS_DIR") / "requests"
    tmp = req / f".{op}.{secrets.token_hex(4)}"
    tmp.write_text(json.dumps({"op": op, "by": actor, "at": datetime.now(timezone.utc).isoformat()}))
    tmp.replace(req / op)                                    # the host only ever sees a whole file


def _read(path: Path | None) -> dict | None:
    try:
        return json.loads(path.read_text()) if path and path.is_file() else None
    except (OSError, ValueError):
        return None


def backups() -> list[dict]:
    d = _dir("VO_BACKUPS_DIR")
    if not d or not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        if BACKUP_NAME.match(f.name) and f.is_file():
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "at": datetime.fromtimestamp(st.st_mtime, timezone.utc)})
    return sorted(out, key=lambda x: x["at"], reverse=True)


def backup_path(name: str) -> Path:
    d = _dir("VO_BACKUPS_DIR")
    if not d or not BACKUP_NAME.match(name or ""):
        raise NotFound("backup")
    p = d / name
    if p.resolve().parent != d.resolve() or not p.is_file():
        raise NotFound("backup")
    return p


def version_tuple(v: str | None) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def status(conn: Connection) -> dict:
    ops = _dir("VO_OPS_DIR")
    st = (ops / "status") if ops else None
    host = _read(st / "host.json") if st else None
    latest = (host or {}).get("latest")
    newer = bool(version_tuple(latest) and version_tuple(__version__) and version_tuple(latest) > version_tuple(__version__))
    try:
        disk = shutil.disk_usage(_dir("VO_BACKUPS_DIR") or "/")
    except OSError:
        disk = None
    req = (ops / "requests") if ops else None
    return {
        "version": __version__, "latest": latest, "update_available": newer, "checked_at": (host or {}).get("checked_at"),
        "managed": managed(), "signups_open": signup_open(),
        "pending": [op for op in OPS if req and (req / op).exists()],
        "last_backup": _read(st / "backup.json") if st else None,
        "last_update": _read(st / "update.json") if st else None,
        "backups": backups(),
        "jobs": kv_get(conn, "jobs"),
        "db_bytes": conn.execute(text("SELECT pg_database_size(current_database())")).scalar(),
        "disk": {"total": disk.total, "free": disk.free} if disk else None,
        "counts": {"businesses": conn.execute(select(func.count()).select_from(B)).scalar(),
                   "users": conn.execute(select(func.count()).select_from(U)).scalar()},
    }


# ---------------------------------------------------------------- countries
def countries_summary(conn: Connection) -> list[dict]:
    """Every country with businesses here: how many, since when, and whether its rules were reviewed (a YAML pack)
    or are the safe defaults made from world data (to check: payments, channels, privacy and messaging rules)."""
    from .. import packs
    rows = conn.execute(select(B.c.country, func.count().label("n"), func.min(B.c.created_at).label("first"))
                        .where(B.c.sandbox.is_(False)).group_by(B.c.country).order_by(func.min(B.c.created_at).desc())).mappings()
    out = []
    for r in rows:
        try:
            p = packs.country(r["country"])
            out.append({**dict(r), "name": p.label("en"), "auto": p.auto, "reviewed": p.reviewed_by_counsel})
        except KeyError:
            out.append({**dict(r), "name": r["country"], "auto": True, "reviewed": False})
    return out


def country_alerts() -> dict:
    """Jobs: tell the server's admins, once, when the first business from a new country signs up."""
    from . import mailer
    from .. import packs
    E = db.instance_events
    sent = 0
    with db.tx() as conn:
        rows = conn.execute(select(E.c.id, E.c.data).where(E.c.type == "server.new_country")
                            .where(E.c.data["mailed"].as_boolean().is_(None))).mappings().all()
        if not rows:
            return {"sent": 0}
        admins = [r[0] for r in conn.execute(select(U.c.email).where(U.c.is_admin.is_(True)))]
        for r in rows:
            code = r["data"].get("country", "")
            try:
                p = packs.country(code)
                name, auto = p.label("en"), p.auto
            except KeyError:
                name, auto = code, True
            bname = r["data"].get("business") or "?"
            body = (f"The first business from {name} ({code}) just signed up: {bname}.\n\n"
                    + ("This country has no reviewed pack yet: it runs on safe defaults (bank transfer and cash to get "
                       "paid, marketing only with consent, no marketing 21:00-08:00, no tax added). Worth checking what "
                       "it needs: local payment providers, the channels people use, privacy and messaging rules, invoices."
                       if auto else "This country has its own pack.")
                    + f"\n\n{config.PUBLIC_URL()}/app/server")
            for a in admins:
                mailer.send(a, f"New country: {name} ({code})", body)
            conn.execute(update(E).where(E.c.id == r["id"]).values(data={**r["data"], "mailed": True}))
            sent += 1
    return {"sent": sent}


# ---------------------------------------------------------------- the businesses this server hosts
def hosted(conn: Connection, limit: int = 500) -> list[dict]:
    n = select(func.count()).select_from(MB).where(MB.c.business_id == B.c.id).scalar_subquery()
    return [dict(r) for r in conn.execute(select(B.c.id, B.c.name, B.c.country, B.c.industry, B.c.created_at, B.c.partner_ref,
                                                 B.c.suspended_at, B.c.suspended_reason, n.label("people"))
                                          .order_by(B.c.created_at.desc()).limit(limit)).mappings()]


def add_business(conn: Connection, admin: dict, *, name: str, country: str, industry: str, owner_email: str) -> tuple[dict, str]:
    """A business for someone else (an agency or host setting up a client): created here, owned by whoever
    accepts the invitation. The admin does not join it. -> (business, invitation token shown once)."""
    biz = businesses.create(conn, name=name, country=country, industry=industry)
    _, token = team.invite(conn, biz, f"user:{admin['id']}", owner_email, role="owner")
    log(conn, biz["id"], "server.business_added", f"user:{admin['id']}", biz["id"])
    return biz, token


def admin_of(conn: Connection, user_id: UUID | str | None) -> dict | None:
    if not user_id:
        return None
    u = users.get(conn, UUID(str(user_id)))
    return u if is_admin(u) else None
