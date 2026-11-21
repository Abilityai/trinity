# mcp: none — the POST is the in-container forwarder's own-key ingest (not a tool); the per-agent GET and the fleet `/api/ops/guardrail-stats` are person UI/ops reads (an MCP tool is a follow-up)
"""Guardrail events (trinity-enterprise#165, GUARD-005).

`POST /api/agents/{agent_name}/guardrail-events` is called by the forwarder in
the agent server with the agent's OWN `TRINITY_MCP_API_KEY` — the heartbeat
predicate (`heartbeat_service.authorize_heartbeat`), outside
`get_current_user`, so the operator floor does not apply and no other principal
can write an agent's events. The statuses are the forwarder's contract:

* `403` — not the agent's own key;
* `413` — more than 500 events or a body over 256 KiB (the forwarder halves);
* `422` — the ENVELOPE is malformed (the forwarder drops that batch); a bad
  event never 422s, it is counted in `rejected`;
* `429` + `Retry-After` — past the per-agent request cap;
* `200 {accepted, duplicates, rejected}`.

`GET /api/agents/{agent_name}/guardrail-events` is owner tier (ruling D8: the
records name owner-only paths a shared person tried), with the uniform 404 of
`OwnedAgentByName` (#186).

`GET /api/ops/guardrail-stats?hours=` (slice S4) is the fleet read behind the
Operations › Health card: a person only (`require_person` — an agent key carries
its owner's role, so on an admin-owned install it would otherwise read the whole
fleet), every live agent for an admin, the agents they OWN for anyone else.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError

from database import db
from dependencies import OwnedAgentByName, is_fleet_admin, require_person
from models import (
    GuardrailEventBatch,
    GuardrailEventIngestResponse,
    GuardrailEventsPage,
    GuardrailStatsResponse,
    User,
)
from services import guardrail_event_service, heartbeat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["guardrail-events"])
# The fleet read lives beside the events it counts; registered in main.py.
ops_router = APIRouter(prefix="/api/ops", tags=["guardrail-events"])

_OWN_KEY_ONLY = "Guardrail events require the agent's own MCP key"


async def _read_capped_body(request: Request) -> bytes:
    """The body, refusing (413) past the byte cap without buffering more."""
    cap = guardrail_event_service.MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="batch_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > cap:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail="batch_too_large")
    return bytes(body)


def _require_own_key(request: Request, agent_name: str) -> None:
    """403 unless the bearer is the agent's own key (the heartbeat predicate)."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OWN_KEY_ONLY)
    res = db.validate_mcp_api_key(auth_header[7:], track_usage=False)
    if not heartbeat_service.authorize_heartbeat(res, agent_name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OWN_KEY_ONLY)


@router.post("/{agent_name}/guardrail-events", response_model=GuardrailEventIngestResponse)
async def post_guardrail_events(agent_name: str, request: Request):
    """Ingest one batch of the agent's own guardrail events."""
    _require_own_key(request, agent_name)

    limit = guardrail_event_service.check_rate(agent_name)
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="guardrail_event_rate_limited",
            headers={"Retry-After": str(limit.retry_after)},
        )

    raw = await _read_capped_body(request)
    # Again, now the body is in: a body streamed slowly can outlive an agent
    # delete or rename (keys deactivated or re-bound, rows cascaded, reporter
    # cleared). Writing then would leave rows under the retired name and mark a
    # later agent of that name as reporting.
    _require_own_key(request, agent_name)
    try:
        batch = GuardrailEventBatch.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        # json.JSONDecodeError is a ValueError. Envelope only: never a bad event.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="invalid_envelope") from exc
    if len(batch.events) > guardrail_event_service.MAX_EVENTS_PER_BATCH:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="batch_too_large")

    return guardrail_event_service.ingest(
        agent_name, batch.events, runtime=batch.runtime, dropped=batch.dropped)


@router.get("/{agent_name}/guardrail-events", response_model=GuardrailEventsPage)
async def get_guardrail_events(
    agent_name: OwnedAgentByName,
    since: Optional[str] = Query(None, description="ISO timestamp, inclusive"),
    until: Optional[str] = Query(None, description="ISO timestamp, exclusive"),
    kind: Optional[str] = Query(None),
    execution_id: Optional[str] = Query(None),
    limit: int = Query(guardrail_event_service.READ_LIMIT_DEFAULT),
    before: Optional[str] = Query(None, description="next_cursor of the previous page"),
):
    """The agent's guardrail events, newest first, with its reporting state."""
    try:
        return guardrail_event_service.list_events(
            agent_name, since=since, until=until, kind=kind,
            execution_id=execution_id, limit=limit, before=before)
    except guardrail_event_service.GuardrailEventQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"reason": str(exc)}) from exc


@ops_router.get("/guardrail-stats", response_model=GuardrailStatsResponse)
async def get_guardrail_stats(
    current_user: User = Depends(require_person),
    hours: int = Query(guardrail_event_service.STATS_HOURS_DEFAULT, ge=1,
                       le=guardrail_event_service.STATS_HOURS_MAX),
):
    """Guardrail event counts per kind and per agent over the last `hours`, with
    each agent's reporting state and coverage. An admin sees every live agent;
    anyone else, the agents they own."""
    owner = None if is_fleet_admin(current_user) else current_user.username
    try:
        return guardrail_event_service.fleet_stats(
            hours, guardrail_event_service.fleet_agent_names(owner))
    except guardrail_event_service.GuardrailEventQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"reason": str(exc)}) from exc
