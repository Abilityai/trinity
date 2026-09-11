# mcp: none — an agent-server hook about the agent itself (grant-vs-use: nothing an operator or an MCP client should call); the Work read is the consumer
"""Pipeline-state change notice (trinity-enterprise#533).

The #919 contract is a file the **agent** writes inside its own container, so
the only thing that knows when it changed is the agent. The agent server
watches its `~/.trinity/pipeline-state/` and posts here; Trinity publishes a
thin `/ws` trigger and the Workspace Work card refetches through the existing
access-controlled read. Trinity learns exactly one thing — *a file changed* —
and advances nothing, persists nothing (CLAUDE.md Rule #8).

**Auth is the heartbeat's auth (#307), for the same reason.** This is an agent
reporting about *itself*, so it carries the agent's OWN agent-scoped MCP key:
validated with ``track_usage=False`` (a notice must not amplify a key's usage
counter) and then ``authorize_heartbeat``, which is true only for an
agent-scoped key whose bound name equals the path. A user, system, connector or
*other agent's* key is the same 403 with the same detail — a differential here
would make the route an oracle for which agents exist (Invariant #8).

Deliberately **not** on `/api/internal/*`: that prefix's blanket router is gated
on `X-Internal-Secret`, which is never injected into an agent container, and its
one agent-key predicate (`_pull_authorized`) additionally requires a pull-pilot
agent — a route placed there would be silently dead for most of the fleet. The
two existing always-on agent→backend self-reports (`POST /{name}/heartbeat`,
`POST /{name}/executions/{id}/result`) both live here, which is also
Invariant #15's shape for a fact about one named agent.
"""
from fastapi import APIRouter, HTTPException, Request

from models import PipelineStateChangedPayload

router = APIRouter(prefix="/api/agents", tags=["agents"])

#: One sentence for every rejection — see the auth note above.
_DENIED = "Pipeline-state notices require the agent's own MCP key"


@router.post("/{agent_name}/pipeline-state/changed")
async def pipeline_state_changed(
    agent_name: str, payload: PipelineStateChangedPayload, request: Request
):
    """Accept one change notice; publish a thin trigger unless coalesced.

    Answers ``{"ok": true, "published": <bool>}``. A coalesced notice is a
    **200 with ``published: false``**, not a 429: the agent ignores the body
    either way, a burst is its normal, and the 12 s poll already covers the
    gap — a 429 would only invite a retry.
    """
    # Every import is function-local. `database`'s module import runs
    # `init_database()`, and `client_portal.work`'s service half imports it in
    # turn; all thirteen existing core→client_portal call sites are lazy for
    # exactly this reason, and a new router must not be the one that drags
    # either onto main.py's import graph.
    from database import db
    from services import heartbeat_service
    from client_portal.work import pipeline_state

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=403, detail=_DENIED)
    token = auth_header[7:]  # strip "Bearer "

    res = db.validate_mcp_api_key(token, track_usage=False)
    if not heartbeat_service.authorize_heartbeat(res, agent_name):
        raise HTTPException(status_code=403, detail=_DENIED)

    published = await pipeline_state.notify_changed(agent_name, payload)
    return {"ok": True, "published": published}
