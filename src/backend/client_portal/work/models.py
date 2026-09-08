"""Pydantic models for Workspace work (trinity-enterprise#525)."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

#: What kind of work a row is, in the client's vocabulary. Derived from the
#: ledger's `triggered_by` + the ent#117 channel stamp — see `service.work_kind`.
WorkKind = Literal["turn", "delegated", "loop", "schedule", "room", "other"]

#: The honest outcome word the card keys on. `timeout` is a FAILED row whose
#: error names a timeout; `lost` is a RUNNING row past the staleness bound
#: (the 120-minute sweep has not reached it yet, but nothing is watching it).
WorkOutcome = Literal[
    "queued", "running", "success", "failed", "timeout", "cancelled", "skipped", "lost",
]

#: Three states, not two (ent#457 ruling 2, as reviewed): `reported` — the
#: agent publishes a pipeline and this is it; `none` — the agent reachable and
#: publishing nothing, so the card says "this agent doesn't report steps";
#: `unknown` — stopped, unreachable, unreadable, or more than one execution
#: running on the agent so no instance can be attributed. Telling a user a
#: stopped agent "doesn't report steps" is the misrender the ruling forbids.
StepsState = Literal["reported", "none", "unknown"]


class WorkStage(BaseModel):
    """One stage of a published pipeline, as the card draws it."""
    id: str
    name: str
    state: Literal["done", "current", "pending"]
    #: The agent holding this stage, when the definition names one and it is
    #: on the caller's roster; otherwise None (rendered as the executing
    #: agent, or "another agent" — never an off-roster name, ent#467).
    holder: Optional[str] = None


class WorkSteps(BaseModel):
    """The #919 read surface, folded onto one running execution."""
    state: StepsState
    pipeline: Optional[str] = None      # human name or id
    current: Optional[str] = None       # current stage id
    holder: Optional[str] = None        # who holds the current stage (roster-masked)
    health: Optional[str] = None
    updated_at: Optional[str] = None
    stages: List[WorkStage] = Field(default_factory=list)


class WorkItem(BaseModel):
    """One execution, projected for the person who asked for it.

    An explicit projection, never a dump: `response`, `execution_log` and
    `tool_calls` are not here, `error` is the sanitized 200-char summary and
    only on a failed row, and every agent name that is not on the caller's
    roster has already been masked to None (`agent_name` for a delegated
    child on an agent the caller cannot see; `delegated_by`; stage holders).
    """
    id: str
    agent_name: Optional[str]
    status: str
    outcome: WorkOutcome
    kind: WorkKind
    title: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    #: Seconds since `started_at` at read time, for a row still in flight; the
    #: client advances it. None once terminal.
    elapsed_seconds: Optional[int] = None
    #: A RUNNING row older than the agent's own turn bound (×1.5, floor 30
    #: min): nothing is watching it, so it is not "live" — no clock, no
    #: signal, no poll — and the card says so instead of counting up forever.
    stale: bool = False
    chat_id: Optional[str] = None
    #: Started by this caller (the same predicate the terminate route enforces).
    mine: bool = False
    #: `mine` ∧ in flight ∧ a turn or a delegated child on a rostered agent —
    #: exactly what `POST .../executions/{id}/terminate` will accept.
    can_stop: bool = False
    delegated_by: Optional[str] = None
    loop_id: Optional[str] = None
    error: Optional[str] = None
    steps: Optional[WorkSteps] = None


class PortalWork(BaseModel):
    """`GET /api/enterprise/client-portal/work` — the chat's work, three ways."""
    agents: List[str]
    now: List[WorkItem]
    earlier: List[WorkItem]
    #: Terminal rows on these agents inside the window — the "N in the last
    #: 30 days" of the summary line, counted server-side so the bounded page
    #: never has to pretend to be the total.
    earlier_total: int
    window_days: int
    #: `earlier` is bounded; this is the bound, so the client can say "30+"
    #: honestly when the page is full.
    earlier_limit: int
