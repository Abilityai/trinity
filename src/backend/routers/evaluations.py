# mcp: src/mcp-server/src/tools/evaluations.ts — get_evaluation_summary (an agent's own summary, ent#937); writes are human-admin-only and never exposed
"""Behavioral evaluations — the referee surface (ent#206).

Three-layer (Invariant #1). The load-bearing rule of the eval epic: **the graded
agent must never write its own grade.** Enforced structurally here —

* **Write** (`POST /api/agents/{name}/evaluations`) is **human-admin-only**:
  `require_admin` (role) AND `reject_agent_principal` (so an agent-scoped key —
  which resolves to its owner and inherits the owner's role — can never reach it,
  the trinity-ops-agent#232 trap). In child 3 the Tier-0 evaluator writes
  server-side via `db.create_agent_evaluation`, and a dedicated evaluator agent
  gets a narrow write grant; the *graded* agent has no write path either way.
* **Read** is access-controlled: an agent (or its owner) reads its own
  evaluations; an admin reads all. Read ≠ write — feedback is fine, self-grading
  is not. An agent-scoped key reads its OWN rows on every fleet read (and, on
  the pair views, the pairs it is a side of) — never its owner's other agents
  (ent#937 T1, `evaluation_read_service.scope_for`).
* **Read API** (ent#937): aggregate (`by=agent|schedule|pair`), failed runs,
  the delegation chain, the stored `eval.*` trend and an agent's summary. All
  GET; the static `/evaluations/<name>` routes are declared before
  `/evaluations/{eval_id}` (Invariant #4).

`agent_reports` (#918) was rejected as the surface precisely because its create
is self-gated (an agent writes its own). This surface inverts that.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from database import db
from dependencies import (
    agent_may_reach,
    get_current_user,
    get_authorized_agent_by_name,
    reject_agent_principal,
    require_admin,
)
from models import (
    EvaluationAgentSummaryResponse,
    EvaluationAggregateResponse,
    EvaluationChainResponse,
    EvaluationCreate,
    EvaluationFailedResponse,
    EvaluationResponse,
    EvaluationTrendResponse,
    User,
)

# Mirrors `client_portal.service.WORKSPACE_EVALUATOR_PREFIX`. Imported would be
# a router→portal-module dependency for one constant; the two are pinned
# together by `test_ent366_workspace_ratings.py` instead.
WORKSPACE_EVALUATOR_PREFIX = "workspace:"
OPERATOR_EVALUATOR_PREFIX = "operator:"
from services import evaluation_read_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["evaluations"])


# The ALLOWLIST of scopes that read as a PERSON. `mcp_scope` is a free-text
# column with no CHECK constraint, so the live values are a snapshot and never a
# closed set — a denylist of machine scopes (`("agent", "system")`) hands the
# words and the rater's email to everything it has not heard of: `connector`,
# `portal_delegate`, any sixth scope added later, and a NULL column, which
# `dependencies` coerces to `"user"` only on some paths. That those scopes are
# route-fenced away from `/api/evaluations` elsewhere is exactly the
# safety-property-held-somewhere-else this function's own docstring objects to
# one paragraph down, and it is the #848 `!== "connector"` class verbatim.
#
# `None` is the JWT branch (a person in a browser); `"user"` is a person's own
# MCP credential, which reads like a JWT. Everything else is a machine until
# someone decides otherwise HERE.
_HUMAN_SCOPES = (None, "user")


def _is_machine_principal(current_user: User) -> bool:
    """Whether this caller is software rather than a person.

    Gating on `agent_name` alone (set only for `scope="agent"`) left
    `trinity-system` reading both the words and the rater's email. The first
    version of this argued that was safe because the portal roster excludes
    system agents, so nothing can rate one — true, and exactly the reasoning
    this repo's learnings ledger warns about: a safety property that holds only
    by a fact elsewhere in the call graph, which this function neither states
    nor enforces. It is also the wrong frame — the risk is not "the rated agent
    reads its own grade", it is "a person's words and identity enter a machine
    context", and the orchestrator is a machine context that can read every
    agent's rows.

    A `user`-scoped MCP key is a person's own credential and reads like a JWT.

    Written as a fail-CLOSED allowlist (review of this PR): the predicate names
    the two principals that are people and treats everything else as software,
    so a scope introduced tomorrow is redacted on the day it appears rather than
    on the day someone remembers this function.
    """
    if current_user.agent_name:
        # Set only for `scope="agent"`, but checked first and independently: a
        # principal carrying an agent identity is a machine whatever its scope
        # column says.
        return True
    return current_user.mcp_scope not in _HUMAN_SCOPES


def _redact_for_agent_principal(row: dict, current_user: User,
                                operator_agents: set = frozenset()) -> dict:
    """Strip a Workspace rating's free text when the RATED agent is reading.

    The ent#366 grooming decision, made explicitly rather than inherited: an
    agent may read its own tallies — three people finding an answer unhelpful is
    a signal worth having — but never the verbatim words. Two reasons, and the
    second is the harder one:

      * a rating the agent can read is a feedback loop it may start optimizing
        for, which is what the issue flags;
      * the comment is untrusted free text written by an annoyed stranger, so
        handing it to the rated agent verbatim is a prompt-injection path INTO
        the thing being criticised.

    The comment is withheld and the grader reduced to its kind. A rating's
    quality and target stay, so the agent can count its ratings and an operator
    surface loses nothing; a Tier-1 verdict's quality does not (TD-8).
    """
    # A machine never reads the words. Neither does a human who is not an
    # OPERATOR of this agent — a shared non-owner is another client, and the
    # rating they would be reading is somebody else's (review finding).
    # `operator_agents` defaults empty, so a caller that forgets to pass it
    # redacts rather than discloses.
    machine = _is_machine_principal(current_user)
    operator = row.get("agent_name") in operator_agents
    if not machine and operator:
        return row
    # The grader goes too, and this was missed on the first pass: hiding the
    # words while leaving `workspace:someone@example.com` tells the rated agent
    # exactly WHO was unhappy. The KIND survives (`workspace` vs a Tier-0 pass
    # name), because "a person rated this" is the signal; "which person" is
    # not. ent#781: the rule is an ALLOWLIST (`redact_evaluator`) — the old
    # two-prefix denylist let `admin:<email>` through verbatim. The same call
    # is the Tier-1 read guard: a machine never reads a judge's quality, model
    # or prose (ent#936 TD-8).
    redacted = evaluation_read_service.project_row(row, machine=machine, operator=operator)
    if row.get("comment"):
        redacted["comment"] = None
        redacted["comment_withheld"] = True
    return redacted


def _operator_for(rows, current_user: User) -> set:
    """Which of these rows' agents this caller is an OPERATOR of.

    Review finding: `_redact_for_agent_principal` split principals into machine
    and human — the right axis, and not the only one. `list_agent_evaluations`
    is gated by `get_authorized_agent_by_name` → `can_user_access_agent`, which
    is True for **any user the agent is SHARED with**; Workspace clients are
    exactly that set (`agent_on_roster` is built from `agent_sharing`), and
    `routers/sharing.py` auto-adds a shared email to the login whitelist when
    email auth is on. So one client's verbatim complaint — and the
    `workspace:bob@y.com` that names them — was readable by every OTHER client
    of the same agent through `GET /api/agents/{name}/evaluations`.

    "Operator surfaces see everything" is the right model; a shared non-owner is
    not an operator. Owner ∪ admin is, and `can_user_share_agent` is already
    that exact predicate (it backs `assert_agent_owner`).

    Resolved once per agent per request rather than per row, and fail-CLOSED:
    a lookup that raises leaves the agent out of the set, so the words are
    withheld rather than disclosed on an error.
    """
    names = {r.get("agent_name") for r in rows if r.get("agent_name")}
    operator = set()
    for name in names:
        try:
            if (db.can_user_share_agent(current_user.username, name) and agent_may_reach(current_user, name, manage=True)):
                operator.add(name)
        except Exception:  # noqa: BLE001
            logger.warning("evaluations: operator check failed for %s", name)
    return operator


def _to_response(row: dict, current_user: User, operator_agents: set = frozenset()) -> EvaluationResponse:
    """Project a row for THIS caller.

    `current_user` is required rather than optional on purpose. The first
    version of ent#366 redacted inside `list_agent_evaluations` only, which left
    the fleet list and the by-id read handing the rated agent its own comments —
    an agent-scoped key resolves to its OWNER, so `accessible_agent_names`
    includes the agent itself. That is the "defense added to one of N call
    sites" class this repo keeps re-learning (#686, #1264, #1153). Threading the
    caller through the single projection makes the redaction structural: a new
    read path cannot compile without deciding.
    """
    row = _redact_for_agent_principal(row, current_user, operator_agents)
    return EvaluationResponse(
        id=row["id"],
        agent_name=row["agent_name"],
        execution_id=row.get("execution_id"),
        archetype=row.get("archetype"),
        completion=(None if row.get("completion") is None else bool(row["completion"])),
        quality=row.get("quality"),
        checks=row.get("checks"),
        judge=row.get("judge"),
        evaluator=row["evaluator"],
        created_at=row["created_at"],
        target_kind=row.get("target_kind"),
        target_id=row.get("target_id"),
        comment=row.get("comment"),
        comment_withheld=bool(row.get("comment_withheld")),
        updated_at=row.get("updated_at"),
        caller_agent=row.get("caller_agent"),
        parent_execution_id=row.get("parent_execution_id"),
        delegation_mode=row.get("delegation_mode"),
        judge_findings=row.get("judge_findings"),
    )


@router.get("/agents/{agent_name}/evaluations", response_model=List[EvaluationResponse])
async def list_agent_evaluations(
    agent_name: str = Depends(get_authorized_agent_by_name),
    limit: int = Query(50, ge=1, le=200),
    caller: Optional[str] = Query(None, max_length=64),
    current_user: User = Depends(get_current_user),
):
    """An agent's own evaluations (newest first). `AuthorizedAgentByName` gates
    read to owner/admin/agent-self — reading feedback is allowed; writing is not.
    `caller=` keeps the runs that agent delegated to this one (ent#937)."""
    # ent#366: an agent-scoped caller gets the tallies and not the words —
    # applied inside `_to_response`, so every read path below inherits it.
    # ent#937: an agent key reads a sibling's rows only for the pairs it is a
    # side of, even where a permission edge lets it past the path gate.
    empty, caller = _scope(current_user).caller_for(agent_name, caller)
    if empty:
        return []
    rows = db.list_agent_evaluations(agent_name, limit, caller=caller)
    operator_agents = _operator_for(rows, current_user)
    return [_to_response(r, current_user, operator_agents) for r in rows]


def _scope(current_user: User) -> evaluation_read_service.ReadScope:
    return evaluation_read_service.scope_for(
        current_user, machine=_is_machine_principal(current_user))


def _window(period: str, since: Optional[str], until: Optional[str]) -> tuple[str, str]:
    try:
        return evaluation_read_service.parse_period(period, since=since, until=until)
    except evaluation_read_service.PeriodError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})


@router.get("/evaluations", response_model=List[EvaluationResponse])
async def list_fleet_evaluations(
    limit: int = Query(100, ge=1, le=500),
    agent: Optional[str] = Query(None, max_length=64),
    caller: Optional[str] = Query(None, max_length=64),
    callee: Optional[str] = Query(None, max_length=64),
    current_user: User = Depends(get_current_user),
):
    """Fleet evaluations across accessible agents (admin = all). An agent key
    reads its own rows only (ent#937 T1). `callee=` is the graded agent (same
    as `agent=`; both given and different → empty); `caller=` the delegator."""
    scope = _scope(current_user)
    names = evaluation_read_service.narrow(scope, agent)
    if callee is not None:
        names = (evaluation_read_service.narrow(scope, callee) if names is None
                 else names & {callee})
    rows = db.list_fleet_evaluations(None if names is None else sorted(names), limit,
                                     caller=caller)
    operator_agents = _operator_for(rows, current_user)
    return [_to_response(r, current_user, operator_agents) for r in rows]


_BY = "^(agent|schedule|pair)$"
_PERIOD = "^(day|week|custom)$"


@router.get("/evaluations/aggregate", response_model=EvaluationAggregateResponse,
            response_model_exclude_none=False)
async def aggregate_evaluations(
    by: str = Query("agent", pattern=_BY),
    period: str = Query("week", pattern=_PERIOD),
    since: Optional[str] = Query(None, max_length=40),
    until: Optional[str] = Query(None, max_length=40),
    agent: Optional[str] = Query(None, max_length=64),
    caller: Optional[str] = Query(None, max_length=64),
    callee: Optional[str] = Query(None, max_length=64),
    schedule_id: Optional[str] = Query(None, max_length=64),
    current_user: User = Depends(get_current_user),
):
    """Tier-0-graded runs that completed in the window, folded `by` agent,
    schedule or caller→callee pair (ent#937). An agent outside the caller's
    scope reads as zeros — the same body as "no runs", so no oracle. A
    machine reader never gets a judge's score."""
    lo, hi = _window(period, since, until)
    scope = _scope(current_user)
    rows, truncated = db.evaluation_window_rows(
        since=lo, until=hi, **evaluation_read_service.window_filters(
            scope, by=by, agent=agent, caller=caller, callee=callee, schedule_id=schedule_id))
    labels = db.evaluation_human_labels([r["execution_id"] for r in rows]) if rows else {}
    out = evaluation_read_service.aggregate(by, rows, labels, machine=scope.machine)
    return {"by": by, "since": lo, "until": hi, "truncated": truncated, **out}


@router.get("/evaluations/failed", response_model=EvaluationFailedResponse)
async def failed_evaluations(
    by: str = Query("agent", pattern=_BY),
    period: str = Query("week", pattern=_PERIOD),
    since: Optional[str] = Query(None, max_length=40),
    until: Optional[str] = Query(None, max_length=40),
    agent: Optional[str] = Query(None, max_length=64),
    caller: Optional[str] = Query(None, max_length=64),
    callee: Optional[str] = Query(None, max_length=64),
    schedule_id: Optional[str] = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=10_000),
    current_user: User = Depends(get_current_user),
):
    """Runs in the window that did not complete or failed a gate check,
    newest first, paged. Check ids and reason codes only — never evidence."""
    lo, hi = _window(period, since, until)
    scope = _scope(current_user)
    rows, truncated = db.evaluation_window_rows(
        since=lo, until=hi, **evaluation_read_service.window_filters(
            scope, by=by, agent=agent, caller=caller, callee=callee, schedule_id=schedule_id))
    folded = evaluation_read_service.fold(rows, {}, failed_limit=len(rows))
    failed = folded["failed"]
    return {"since": lo, "until": hi, "total": len(failed), "truncated": truncated,
            "items": failed[offset:offset + limit]}


@router.get("/evaluations/trend", response_model=EvaluationTrendResponse)
async def evaluation_trend(
    metric: str = Query(..., max_length=64),
    agent: Optional[str] = Query(None, max_length=64),
    caller: Optional[str] = Query(None, max_length=64),
    callee: Optional[str] = Query(None, max_length=64),
    days: int = Query(30, ge=1, le=evaluation_read_service.MAX_TREND_DAYS),
    current_user: User = Depends(get_current_user),
):
    """The stored daily `eval.*` series for one agent (`agent=`) or one pair
    (`caller=` + `callee=`) — read from the points the platform writes, never
    recomputed. Out of scope reads as an empty series."""
    scope = _scope(current_user)
    try:
        name = evaluation_read_service.trend_metric(metric, machine=scope.machine)
    except evaluation_read_service.MetricError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)})
    if caller is not None or callee is not None:
        if not (caller and callee) or agent is not None:
            raise HTTPException(status_code=422, detail={
                "code": "invalid_series", "message": "name agent=, or caller= and callee="})
        dims, absent = {"caller": caller, "callee": callee}, ["agent"]
        visible = ((scope.self_agent in (caller, callee)) if scope.self_agent
                   else scope.sees(caller) and scope.sees(callee))
    elif agent:
        dims, absent = {"agent": agent}, ["caller", "callee"]
        visible = scope.sees(agent)
    else:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_series", "message": "name agent=, or caller= and callee="})
    points = []
    if visible:
        now = datetime.now(timezone.utc)
        rows = db.metric_series_points_for_dims(
            evaluation_read_service.PLATFORM_EVAL_OWNER, name, dims, absent,
            (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), days + 1)
        points = [{"ts": p["ts"], "value": p["value_numeric"]} for p in reversed(rows)
                  if p.get("value_numeric") is not None]
    return {"metric": name, "dims": dims, "points": points}


@router.get("/evaluations/chain/{execution_id}", response_model=EvaluationChainResponse)
async def evaluation_chain(execution_id: str, current_user: User = Depends(get_current_user)):
    """A run's delegation chain: its ancestors (nearest first) and the runs it
    delegated (breadth first), bounded by the platform's chain depth and 200
    nodes. A run outside the caller's scope is the uniform 404; a walk that
    reaches one stops there and says `access`, without naming it."""
    scope = _scope(current_user)
    root = db.execution_chain_node(execution_id)
    if not root or not scope.sees(root.get("agent_name")):
        raise HTTPException(status_code=404, detail="not_found")
    depth = evaluation_read_service.chain_max_depth()
    up, up_stop = db.execution_chain_ancestors(execution_id, max_depth=depth, visible=scope.names)
    down, down_stop = db.execution_chain_descendants(execution_id, max_depth=depth,
                                                     visible=scope.names)
    served = {n["id"] for n in [root, *up, *down]}

    def _view(node):
        v = evaluation_read_service.chain_node_view(node)
        # Never echo an id or an agent the walk did not serve.
        if v["parent_execution_id"] not in served:
            v["parent_execution_id"] = None
        if v["caller_agent"] is not None and not scope.sees(v["caller_agent"]):
            v["caller_agent"] = None
        return v

    return {"execution": _view(root), "ancestors": [_view(n) for n in up],
            "descendants": [_view(n) for n in down],
            "ancestors_truncated": up_stop, "descendants_truncated": down_stop}


@router.get("/agents/{agent_name}/evaluations/summary",
            response_model=EvaluationAgentSummaryResponse)
async def agent_evaluation_summary(
    agent_name: str = Depends(get_authorized_agent_by_name),
    period: str = Query("week", pattern=_PERIOD),
    since: Optional[str] = Query(None, max_length=40),
    until: Optional[str] = Query(None, max_length=40),
    caller: Optional[str] = Query(None, max_length=64),
    current_user: User = Depends(get_current_user),
):
    """One agent's summary and failed runs — what the MCP self-read serves.
    An agent key reads its own only (another agent it may reach reads as
    zeros); a machine never gets a judge's score."""
    lo, hi = _window(period, since, until)
    scope = _scope(current_user)
    names = evaluation_read_service.narrow(scope, agent_name)
    rows, truncated = db.evaluation_window_rows(since=lo, until=hi, agents=names, caller=caller)
    labels = db.evaluation_human_labels([r["execution_id"] for r in rows]) if rows else {}
    folded = evaluation_read_service.fold(rows, labels)
    failed = folded.pop("failed")
    failed_truncated = folded.pop("failed_truncated")
    return {"agent_name": agent_name, "since": lo, "until": hi, "truncated": truncated,
            "summary": evaluation_read_service.project_summary(folded, machine=scope.machine),
            "failed": failed, "failed_truncated": failed_truncated}


@router.get("/evaluations/{eval_id}", response_model=EvaluationResponse)
async def get_evaluation(eval_id: str, current_user: User = Depends(get_current_user)):
    row = db.get_agent_evaluation(eval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    # tenant boundary: non-admin may only see an evaluation of an accessible
    # agent; an agent key only its own (ent#937 T1)
    if not _scope(current_user).sees(row["agent_name"]):
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return _to_response(row, current_user, _operator_for([row], current_user))


@router.get("/agents/{agent_name}/executions/{execution_id}/evaluation",
            response_model=EvaluationResponse)
async def get_execution_evaluation(
    execution_id: str,
    agent_name: str = Depends(get_authorized_agent_by_name),
    current_user: User = Depends(get_current_user),
):
    """How one run graded — the Tier-0 row (trinity-enterprise#935).

    Same gate as the agent's other evaluation reads (`get_authorized_agent_by_name`,
    uniform 404), same projection (`_to_response`). Not evaluated yet, or an
    execution that is not this agent's, is one 404 `not_evaluated`: the path
    must not become an oracle over other agents' execution ids. Read only —
    the Tier-0 writer is the sweep, reachable by no route.
    """
    row = db.latest_tier0_for_execution(execution_id)
    if (not row or row.get("agent_name") != agent_name
            or not _scope(current_user).sees_row(row)):
        raise HTTPException(status_code=404, detail="not_evaluated")
    return _to_response(row, current_user, _operator_for([row], current_user))


@router.post("/agents/{agent_name}/evaluations", response_model=EvaluationResponse)
async def create_evaluation(
    agent_name: str,
    body: EvaluationCreate,
    current_user: User = Depends(require_admin),
):
    """Write an evaluation for `agent_name` — human-admin-only (the write-fence).

    `require_admin` already rejects connector keys and checks the role, but an
    agent-scoped key inherits its owner's role; `reject_agent_principal` closes
    that so no agent — graded or otherwise — can write a grade via this route.
    """
    reject_agent_principal(current_user)
    row = db.create_agent_evaluation(
        agent_name,
        evaluator=f"admin:{current_user.username}",
        execution_id=body.execution_id,
        archetype=body.archetype,
        completion=body.completion,
        quality=body.quality,
        checks=body.checks,
        judge=body.judge,
    )
    # The write fence is `require_admin` + `reject_agent_principal`, so this
    # caller should be an operator; the status is still resolved through
    # `_operator_for` (`can_user_share_agent` + `agent_may_reach`), not
    # assumed, so the echo is redacted only if that check says otherwise.
    return _to_response(row, current_user, _operator_for([row], current_user))
