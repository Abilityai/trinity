"""Pydantic models for Workspace suggestions (trinity-enterprise#465)."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

#: `invoke` acts inside the Workspace (prefill, open a section, open the chat);
#: `configure` is read-only awareness whose Accept deep-links to the operator
#: agent page (decision 15, option 1 — the engine-bay ruling).
SuggestionKind = Literal["invoke", "configure"]

#: Which signal produced the item. Stored with the feedback row so a source
#: whose suggestions are always dismissed can be found — and dropped — by data.
SuggestionSource = Literal["asks", "decisions", "schedules", "usage", "capability"]

#: The door each class is declared behind (ent#78: no surface ships
#: undeclared). `platform` = any platform-authenticated viewer of the agent;
#: `owner_or_admin` = additionally the agent's owner or an instance admin.
SuggestionDoor = Literal["platform", "owner_or_admin"]

#: What Accept does on the client. `prefill` puts `value` in the composer and
#: never sends (ent#138); `open_section` opens the Info-tab section `value`
#: names; `open_chat` focuses the composer; `link` navigates to the operator
#: path in `value`.
ActionType = Literal["prefill", "open_section", "open_chat", "link"]


class SuggestionAction(BaseModel):
    type: ActionType
    value: Optional[str] = None


class Suggestion(BaseModel):
    """One actionable suggestion. The shape is the contract later sources
    (role, project, objective gap, cross-agent) attach to without changing it."""
    key: str
    kind: SuggestionKind
    source: SuggestionSource
    door: SuggestionDoor
    title: str
    #: The evidence, in words the viewer can check ("Failed 4 runs in a row").
    signal: str
    description: Optional[str] = None
    action: SuggestionAction


class PortalSuggestions(BaseModel):
    agent_name: str
    suggestions: List[Suggestion] = Field(default_factory=list)
    #: How many were eligible before the display cap — a truncated list says so.
    total: int = 0
    #: `available` — the agent answered and exposes playbooks; `none` — it
    #: answered and exposes none; `unavailable` — it did not answer (stopped,
    #: unreachable, over budget), so "nothing unused" would be a guess.
    capabilities: Literal["available", "none", "unavailable"] = "unavailable"
    #: `capabilities_only` — the viewer has never talked to this agent, so the
    #: list can only say what the agent can do (said, not implied).
    basis: Literal["history", "capabilities_only"] = "history"


class SuggestionFeedback(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    action: Literal["accept", "dismiss"]


class SuggestionFeedbackResult(BaseModel):
    ok: bool = True
