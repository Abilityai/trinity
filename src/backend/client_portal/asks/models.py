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
    # pending | expired on a LISTING (`list_asks` queries `status="pending"`, so
    # terminal rows never appear there); `answered` is reachable only from the
    # ANSWER response, where the row this call just recorded is projected back.
    status: str
    chat_id: Optional[str] = None   # the thread it was attached to, when known
    # ent#430 AC #5: whether answering this ask sets work in motion, so a
    # surface can say "answered" without implying the agent started working.
    # Populated only on the ANSWER response — a pending ask has not been
    # answered, so the question does not arise, and defaulting it to False on a
    # listing would read as "answering this does nothing".
    resume_requested: Optional[bool] = None


class WorkspaceAskAnswer(BaseModel):
    """An answer from the addressee.

    `response` is the DECISION — the chosen option or the typed answer — and it
    is what the agent reads: the sync write-back copies it to the queue file
    verbatim and the ent#329 resume framing presents it as "the answer".
    `response_text` is an optional free-text NOTE riding alongside a decision;
    it cannot stand alone (#2375: the Workspace panel used to post a typed
    answer as `response_text`, the service coerced the missing `response` to
    "", and the agent read an empty answer). Both stay Optional at the model so
    the service can refuse with its own named 422 (`empty_answer`) instead of a
    bare validation shape; the service is the gate.
    """
    response: Optional[str] = Field(default=None, max_length=500)
    response_text: Optional[str] = Field(default=None, max_length=4000)
