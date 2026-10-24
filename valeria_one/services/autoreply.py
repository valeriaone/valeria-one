"""What happens right after a customer writes: an in-chat booking in progress (or a request to book) is
handled first; otherwise the business's AI answers if it is on 'auto'."""
from __future__ import annotations

from uuid import UUID

from . import ai, chatbook


def respond(business_id: UUID, conversation_id: UUID, http=None) -> str:
    r = chatbook.handle(business_id, conversation_id)
    if r in ("off", "not_booking"):
        return ai.auto_reply(business_id, conversation_id, http=http)
    return "booking:" + r
