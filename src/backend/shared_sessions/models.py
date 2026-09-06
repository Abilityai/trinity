"""Pydantic models for the enterprise shared-sessions module (ent#169)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agents: list[str] = Field(min_length=1, max_length=12)
    topic: Optional[str] = Field(default=None, max_length=2000)
    max_messages: Optional[int] = Field(default=None, ge=1, le=500)
    max_cost_usd: Optional[float] = Field(default=None, gt=0)
    ttl_hours: Optional[int] = Field(default=None, ge=0, le=168)
    scribe: Optional[str] = None


class RoomRename(BaseModel):
    """ent#473 — a person's title for the room. Bounded here only against
    abuse; the one-line / non-empty / 100-char rules are `services/chat_title`'s,
    applied in the service so the refusal is a NAMED 400 the person can act on
    rather than a 422 about a schema."""
    name: str = Field(max_length=4000)


class RoomMessageCreate(BaseModel):
    """``sender`` is deliberately absent — the acting principal comes from the
    auth context, never the body, so nobody can post as someone else."""
    content: str = Field(min_length=1, max_length=8000)


class RoomParticipantAdd(BaseModel):
    agent_name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="member", pattern="^(member|scribe|moderator)$")


class RoomCloseRequest(BaseModel):
    reason: Optional[str] = Field(default="user_closed", max_length=50)


# --- Operator room-budget defaults (ent#387) -------------------------------

class RoomBudgetDefaults(BaseModel):
    """The operator's defaults for a room started without an explicit budget.

    `sources` reports `db-row` vs `code-default` per key, so the panel can show
    what is configured versus what is merely inherited — the ent#375 shape.
    """
    max_messages: int
    max_cost_usd: Optional[float] = None      # None = uncapped
    ttl_hours: int                            # 0 = no expiry
    sources: Dict[str, str] = {}
    max_messages_ceiling: int
    max_ttl_hours: int


class RoomBudgetDefaultsUpdate(BaseModel):
    """Partial update. `clear` reverts named keys to the code default.

    Ranges mirror `RoomCreate` exactly: an operator default must never be a value
    a caller would have been refused, or the default becomes a way to exceed the
    API's own bounds.
    """
    max_messages: Optional[int] = Field(default=None, ge=1, le=500)
    max_cost_usd: Optional[float] = Field(default=None, gt=0)
    ttl_hours: Optional[int] = Field(default=None, ge=0, le=168)
    clear: List[str] = []
