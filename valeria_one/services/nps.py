"""nps01 (v0.53.0): «Del 0 al 10, ¿qué tan probable es que nos recomiende?» after a visit (Tad 2026-11-08 «close all gaps»).

A follow-up whose message has {survey} (the one-tap «Encuesta 0–10», 3 hours after each visit) marks the person as asked.
When they answer with a number within 3 days, it is their score. 0–6 (a detractor) reaches the owner at once (a push to the
installed app, and a «call them» card on Today) so they can make it right. The NPS on Today = % promoters (9–10) − % detractors
(0–6) over the last 90 days. The review request stays the same polite ask for everyone — never only to the happy ones
(Google / FTC rules against review gating): this score never decides who gets a review link.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from .. import db
from . import log

N = db.nps_responses
ANSWER_DAYS = 3
_SCORE = re.compile(r"^\s*(10|[0-9])\s*(?:/\s*10|de\s*10|of\s*10)?\s*[.!]?\s*$", re.I)


def asked(conn: Connection, biz: dict, contact_id: UUID, now: datetime | None = None) -> None:
    conn.execute(insert(N).values(business_id=biz["id"], contact_id=contact_id, asked_at=now or datetime.now(timezone.utc)))


def on_inbound(conn: Connection, biz: dict, contact: dict, body: str, now: datetime | None = None) -> int | None:
    """A message from someone asked in the last 3 days that is just a score -> kept. -> the score, or None."""
    m = _SCORE.match(body or "")
    if not m:
        return None
    now = now or datetime.now(timezone.utc)
    row = conn.execute(select(N.c.id).where(N.c.business_id == biz["id"], N.c.contact_id == contact["id"], N.c.score.is_(None),
                                            N.c.asked_at > now - timedelta(days=ANSWER_DAYS))
                       .order_by(N.c.asked_at.desc()).limit(1)).scalar()
    if not row:
        return None
    score = int(m.group(1))
    conn.execute(update(N).where(N.c.id == row).values(score=score, answered_at=now))
    log(conn, biz["id"], "nps.answered", "customer", contact["id"], {"score": score})
    if score <= 6:
        from . import push
        who = contact.get("name") or contact.get("phone") or contact.get("email") or "—"
        push.notify(biz["id"], biz["name"], f"{who}: {score}/10 — llámale hoy / call them today", f"/app/contacts/{contact['id']}")
    return score


def summary(conn: Connection, business_id: UUID, days: int = 90, now: datetime | None = None) -> dict:
    """{"n", "promoters", "passives", "detractors", "nps", "recent_low": [{contact…, score, at}]}"""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(select(N.c.score).where(N.c.business_id == business_id, N.c.score.isnot(None),
                                                N.c.answered_at > now - timedelta(days=days))).scalars().all()
    n = len(rows)
    pro, det = sum(1 for s in rows if s >= 9), sum(1 for s in rows if s <= 6)
    C = db.contacts
    low = [dict(r) for r in conn.execute(select(N.c.score, N.c.answered_at, C.c.id, C.c.name, C.c.phone).join(C, C.c.id == N.c.contact_id)
                                         .where(N.c.business_id == business_id, N.c.score <= 6, N.c.answered_at > now - timedelta(days=7))
                                         .order_by(N.c.answered_at.desc()).limit(5)).mappings()]
    return {"n": n, "promoters": pro, "passives": n - pro - det, "detractors": det,
            "nps": round(100 * (pro - det) / n) if n else None, "recent_low": low}
