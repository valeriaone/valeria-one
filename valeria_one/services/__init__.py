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
    conn.execute(insert(db.events).values(business_id=business_id, type=type_, actor=actor,
                                          subject_id=subject_id, data=data or {}))
