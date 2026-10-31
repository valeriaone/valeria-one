"""Business logic. Every function takes the business it acts for and only ever touches that business's rows."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.engine import Connection

from .. import db


class NotFound(Exception):
    pass


class Invalid(Exception):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class Blocked(Exception):
    """A send the rules don't allow (no consent, quiet hours, outside the reply window, unsubscribed)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def log(conn: Connection, business_id: UUID, type_: str, actor: str, subject_id: UUID | None = None,
        data: dict[str, Any] | None = None) -> None:
    """Record what happened (audit trail) and, in the same transaction, queue it for the business's webhooks."""
    row = conn.execute(insert(db.events).values(business_id=business_id, type=type_, actor=actor,
                                                subject_id=subject_id, data=data or {})
                       .returning(db.events.c.id, db.events.c.created_at)).one()
    from . import followups, invoices, loyalty, recipes, sequences, webhooks, workflows
    followups.on_event(conn, business_id, type_, subject_id, data or {})
    loyalty.on_event(conn, business_id, type_, subject_id, data or {})      # a visit done / paid counts toward the reward
    recipes.on_event(conn, business_id, type_, subject_id, data or {}, actor, row.id)   # "when this, do that" (once per event)
    sequences.on_event(conn, business_id, type_, subject_id, data or {}, actor)         # v0.18: drips in and out
    workflows.on_event(conn, business_id, type_, subject_id, data or {}, actor, row.id)  # v0.18: workflows start, goals end them
    invoices.on_event(conn, business_id, type_, subject_id, data or {})                 # v0.18: a pay link paid = its invoice paid
    from . import pos
    pos.on_event(conn, business_id, type_, subject_id, data or {})                      # v0.20: a pay link paid = its sale paid
    from . import affiliates
    affiliates.on_event(conn, business_id, type_, subject_id, data or {})               # v0.21: a paid sale earns its affiliate
    from . import contracts
    contracts.on_event(conn, business_id, type_, subject_id, data or {})                # v0.24: a paid deposit, its booking confirmed
    webhooks.enqueue(conn, business_id, {"id": row.id, "type": type_, "business_id": str(business_id),
                                         "subject_id": str(subject_id) if subject_id else None, "data": data or {},
                                         "created_at": row.created_at.isoformat()})
