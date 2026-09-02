# mcp: none — the referee surface (ent#206): writes are human-admin-only by design, reads serve the UI
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
  is not.

`agent_reports` (#918) was rejected as the surface precisely because its create
is self-gated (an agent writes its own). This surface inverts that.
"""
from __future__ import annotations

import logging

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from database import db
from dependencies import (
    get_current_user,
    get_authorized_agent_by_name,
    reject_agent_principal,
    require_admin,
)
from models import EvaluationCreate, EvaluationResponse, User

# Mirrors `client_portal.service.WORKSPACE_EVALUATOR_PREFIX`. Imported would be
# a router→portal-module dependency for one constant; the two are pinned
# together by `test_ent366_workspace_ratings.py` instead.
WORKSPACE_EVALUATOR_PREFIX = "workspace:"
OPERATOR_EVALUATOR_PREFIX = "operator:"
from services.agent_service.helpers import accessible_agent_names, narrow_to_agent

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

    Only the comment is withheld. Quality, target and evaluator stay, so the
    agent can count its ratings and an operator surface loses nothing.
    """
    # A machine never reads the words. Neither does a human who is not an
    # OPERATOR of this agent — a shared non-owner is another client, and the
    # rating they would be reading is somebody else's (review finding).
    # `operator_agents` defaults empty, so a caller that forgets to pass it
    # redacts rather than discloses.
    if not _is_machine_principal(current_user) and row.get("agent_name") in operator_agents:
        return row
    redacted = dict(row)
    if row.get("comment"):
        redacted["comment"] = None
        redacted["comment_withheld"] = True
    # The rater's identity goes too, and this was missed on the first pass:
    # hiding the words while leaving `workspace:someone@example.com` tells the
    # rated agent exactly WHO was unhappy. That is arguably the more actionable
    # half — an agent that cannot read a complaint but can name the complainant
    # is in a better position to change its behaviour toward that person than
    # one that read the text. The KIND survives (`workspace` vs a Tier-0 pass
    # name), because "a person rated this" is the signal; "which person" is not.
    _evaluator = str(row.get("evaluator") or "")
    # ent#366 review: the operator form is anonymised on the same terms. An
    # agent must not learn WHO rated it either way — and leaving the new prefix
    # out would have made an operator's address the one identity that still
    # reached the graded agent.
    for _prefix in (WORKSPACE_EVALUATOR_PREFIX, OPERATOR_EVALUATOR_PREFIX):
        if _evaluator.startswith(_prefix):
            redacted["evaluator"] = _prefix.rstrip(":")
            break
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
            if db.can_user_share_agent(current_user.username, name):
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
    )


@router.get("/agents/{agent_name}/evaluations", response_model=List[EvaluationResponse])
async def list_agent_evaluations(
    agent_name: str = Depends(get_authorized_agent_by_name),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
):
    """An agent's own evaluations (newest first). `AuthorizedAgentByName` gates
    read to owner/admin/agent-self — reading feedback is allowed; writing is not."""
    # ent#366: an agent-scoped caller gets the tallies and not the words —
    # applied inside `_to_response`, so every read path below inherits it.
    rows = db.list_agent_evaluations(agent_name, limit)
    operator_agents = _operator_for(rows, current_user)
    return [_to_response(r, current_user, operator_agents) for r in rows]


@router.get("/evaluations", response_model=List[EvaluationResponse])
async def list_fleet_evaluations(
    limit: int = Query(100, ge=1, le=500),
    agent: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    """Fleet evaluations across accessible agents (admin = all)."""
    names = narrow_to_agent(accessible_agent_names(current_user), agent)
    rows = db.list_fleet_evaluations(names, limit)
    operator_agents = _operator_for(rows, current_user)
    return [_to_response(r, current_user, operator_agents) for r in rows]


@router.get("/evaluations/{eval_id}", response_model=EvaluationResponse)
async def get_evaluation(eval_id: str, current_user: User = Depends(get_current_user)):
    row = db.get_agent_evaluation(eval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    # tenant boundary: non-admin may only see an evaluation of an accessible agent
    names = accessible_agent_names(current_user)
    if names is not None and row["agent_name"] not in names:
        raise HTTPException(status_code=404, detail="Evaluation not found")
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
    # caller is an operator by construction — stated explicitly rather than
    # left to the fail-closed default, which would redact an admin's own write
    # back to them.
    return _to_response(row, current_user, _operator_for([row], current_user))
