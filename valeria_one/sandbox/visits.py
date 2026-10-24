"""Sandbox only: who is looking at the demo — counts, where they came from, and whether the visit came from
Meta's own network (AS32934). No IP address is stored: a visitor is a hash of the IP with a secret that
changes every day, so people can be counted but never identified or followed."""
from __future__ import annotations

import hashlib
import ipaddress
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, insert, literal_column, select

from .. import config, db

PV = db.page_views
SKIP = ("/static/", "/v1/", "/hooks/", "/health", "/favicon", "/stats/", "/openapi.json")


@lru_cache(maxsize=1)
def _meta_nets():
    f = Path(__file__).parent / "meta_prefixes.txt"
    nets = []
    for line in f.read_text().split() if f.exists() else []:
        try:
            nets.append(ipaddress.ip_network(line.strip(), strict=False))
        except ValueError:
            pass
    return nets


def is_meta(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a.version == n.version and a in n for n in _meta_nets())


def _ua_kind(ua: str) -> str:
    u = (ua or "").lower()
    if any(b in u for b in ("bot", "crawler", "spider", "preview", "facebookexternalhit", "linkedinbot", "slackbot", "curl", "python")):
        return "bot"
    return "mobile" if "mobile" in u else "desktop"


def record(path: str, ip: str, referer: str | None, ua: str | None) -> None:
    if not config.SANDBOX() or path.startswith(SKIP):
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    visitor = hashlib.sha256(f"{day}:{config.get('VO_SECRET_KEY', '')}:{ip}".encode()).hexdigest()[:16]
    host = urlparse(referer or "").hostname
    if host and host.endswith("sandbox.valai.mx"):
        host = None
    try:
        with db.tx() as conn:
            conn.execute(insert(PV).values(path=path[:300], ref_host=(host or None), visitor=visitor,
                                           meta_network=is_meta(ip), ua_kind=_ua_kind(ua or "")))
    except Exception:
        pass                                                  # analytics never break a page


def summary(days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with db.tx() as conn:
        base = select(PV).where(PV.c.created_at >= since, PV.c.ua_kind != "bot").subquery()
        day = func.date_trunc(literal_column("'day'"), base.c.created_at)
        per_day = conn.execute(select(day.label("d"), func.count(), func.count(func.distinct(base.c.visitor)))
                               .group_by(day).order_by(day.desc())).all()
        refs = conn.execute(select(base.c.ref_host, func.count()).where(base.c.ref_host.isnot(None))
                            .group_by(base.c.ref_host).order_by(func.count().desc()).limit(20)).all()
        pages = conn.execute(select(base.c.path, func.count()).group_by(base.c.path)
                             .order_by(func.count().desc()).limit(20)).all()
        meta = conn.execute(select(base.c.created_at, base.c.path, base.c.ref_host).where(base.c.meta_network)
                            .order_by(base.c.created_at.desc()).limit(50)).all()
        bots = conn.execute(select(PV.c.created_at, PV.c.path).where(PV.c.created_at >= since, PV.c.ua_kind == "bot",
                                                                      PV.c.meta_network)
                            .order_by(PV.c.created_at.desc()).limit(20)).all()
    return {"per_day": per_day, "refs": refs, "pages": pages, "meta": meta, "meta_bots": bots}


_gh_cache: dict = {}


def github_numbers(repo: str | None = None) -> dict | None:
    """Stars, forks and watchers of the public repo (cached 5 minutes; nothing if GitHub can't be reached)."""
    import time
    import httpx
    repo = repo or config.get("VO_GITHUB_REPO", "valeriaone/valeria-one")
    hit = _gh_cache.get(repo)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        j = httpx.get(f"https://api.github.com/repos/{repo}", timeout=5).json()
        out = {k: j.get(k) for k in ("stargazers_count", "forks_count", "subscribers_count", "open_issues_count")}
    except Exception:
        out = None
    _gh_cache[repo] = (time.time(), out)
    return out
