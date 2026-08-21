"""Pydantic models for Workspace asks (ent#364). OSS core since ent#428."""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class WorkspaceAsk(BaseModel):
    """One ask, as the addressee sees it.

    Deliberately NOT the whole queue row. An operator-queue item carries fields a
    client has no business reading — `context` is agent-authored and may hold
    execution ids, internal paths, whatever the agent put there — so this is an
    explicit projection rather than a dump with a blocklist. A field reaches the
    client because it is named here.
    """
    id: str
    agent_name: str
    kind: str                       # question | approval | alert
    priority: str
    title: str
    question: str
    options: Optional[List[Any]] = None
    created_at: str
    expires_at: Optional[str] = None
    status: str                     # pending | expired  (terminal ones are not listed)
    chat_id: Optional[str] = None   # the thread it was attached to, when known


class WorkspaceAskAnswer(BaseModel):
    """An answer from the addressee.

    `response` is the chosen option (or approve/deny); `response_text` is free
    text. Both optional individually, at least one required — an empty answer is
    not an answer, and recording one would clear the ask while telling the agent
    nothing.
    """
    response: Optional[str] = Field(default=None, max_length=500)
    response_text: Optional[str] = Field(default=None, max_length=4000)
