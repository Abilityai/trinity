"""Brain Orb backend proxy (#58, trinity-enterprise).

Read-only broker between the first-party orb page (frontend origin) and the
agent container. Replaces the old localhost-CORS + per-start `X-Orb-Token` model
with the platform trust boundary:

  * read authz via ``AuthorizedAgentByName`` (owner / shared / admin);
  * the mutating scope route is ``OwnedAgentByName`` (owner / admin only);
  * transport via ``agent_httpx_client`` (per-agent agent-auth token, #1159);
  * the agent owns generation + scope state (Invariant #8) — Trinity only brokers.

Phase 1 shipped the read-only data proxy. Phase 2 (#58) adds the live scope
control loop: list scopes (read) and mutate the active set → agent re-export
(owner-gated). Isolated in its own router (no edits to agent_files.py). The 5-
segment paths never collide with the ``/api/agents/{name}`` catch-all (Inv #4).
"""
import json
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response

from config import (
    BRAIN_ORB_ENABLED,
    BRAIN_ORB_VOICE_ENABLED,
    BRAIN_ORB_WRITE_ENABLED,
    GEMINI_API_KEY,
)
from database import db
from dependencies import AuthorizedAgentByName, CurrentUser, OwnedAgentByName
from services import brain_orb_voice_service, idempotency_service, rate_limiter
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.docker_utils import container_reload
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["brain-orb"])

_AGENT_PORT = 8000
_MAX_SCOPE_BODY = 64 * 1024
_MAX_TOOL_BODY = 16 * 1024        # read-tool requests are tiny (a query + scope)
# 1 MiB: capture/link bodies are tiny, but capture_transcript (#66) carries a whole
# voice conversation's turn events. Owner-only + rate-limited, so a generous cap is safe.
_MAX_ACTION_BODY = 1024 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
# KB-write actions the backend accepts. capture/link (Phase 4a) + capture_transcript
# and process_transcript (Phase 4b, trinity-enterprise#66 — save a voice transcript,
# and run the agent's configured post-session prompt over it). run_skill remains out.
# Enum-validated at the boundary so an unknown verb never reaches the agent hook.
_ALLOWED_ACTIONS = frozenset({"capture", "link", "capture_transcript", "process_transcript"})
# Owner-initiated capture/link burst budget per (user, agent, action). Generous — an
# owner may jot several quick notes; distinct key per action so link doesn't starve capture.
_ACTION_RATE_LIMIT = 30
_ACTION_RATE_WINDOW = 60
# Per-(user, agent) voice-token mint budget — a leaked JWT can't spin up unbounded
# Gemini Live sessions on the platform key. Mirrors the VoIP spend-control precedent.
_VOICE_TOKEN_RATE_LIMIT = 10
_VOICE_TOKEN_RATE_WINDOW = 60


async def _agent_request(agent_name: str, method: str, path: str, *, content: bytes | None = None, timeout: float) -> httpx.Response:
    """Shared gate + proxy: flag-gate → agent running → agent-auth'd request.

    Returns the upstream ``httpx.Response`` for the caller to map. Raises the
    mapped ``HTTPException`` on flag-off (404 — flag is the single source of
    truth), missing/stopped agent (404 / 503), or transport failure (503 / 504).
    """
    if not BRAIN_ORB_ENABLED:
        raise HTTPException(status_code=404, detail="Brain Orb is not enabled")
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")
    url = f"http://agent-{agent_name}:{_AGENT_PORT}{path}"
    try:
        async with agent_httpx_client(agent_name, timeout=timeout) as client:
            return await client.request(method, url, content=content)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Agent is not reachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent timed out")


def _passthrough(response: httpx.Response, *, not_found: str) -> Response:
    """Map a brain-orb agent response to a byte pass-through, or a mapped error:
    404 (flag off / no export / unsupported), 413 (too large), 502 (other)."""
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=not_found)
    if response.status_code == 413:
        raise HTTPException(status_code=413, detail="Request too large")
    if response.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Agent returned an error")
    # Byte pass-through — never re-serialize (the data payload is multi-MB).
    return Response(content=response.content, media_type="application/json", headers=_NO_STORE)


@router.get("/{agent_name}/brain-orb/data")
async def get_brain_orb_data(agent_name: AuthorizedAgentByName):
    """Proxy the agent's visualization data.json to the orb page (read access)."""
    response = await _agent_request(agent_name, "GET", "/api/brain-orb/data", timeout=30.0)
    return _passthrough(response, not_found="Brain Orb data not found")


@router.get("/{agent_name}/brain-orb/scopes")
async def get_brain_orb_scopes(agent_name: AuthorizedAgentByName):
    """List the agent's selectable + active vault scopes for the scope panel
    (read access). 404 when the agent ships no scope hook."""
    response = await _agent_request(agent_name, "GET", "/api/brain-orb/scopes", timeout=30.0)
    return _passthrough(response, not_found="Scope control not supported")


@router.post("/{agent_name}/brain-orb/scope")
async def post_brain_orb_scope(agent_name: OwnedAgentByName, request: Request):
    """Mutate the agent's active scope set (mount/unmount) → agent re-export.

    **Owner/admin only** — this is the one mutating brain-orb route. Forwards the
    raw JSON body to the agent hook; the agent owns the scope state + the re-export
    (Invariant #8). 200s timeout sits just above the agent-server's 180s hook
    timeout so a slow re-export surfaces as the agent's 504, not a premature one.
    """
    body = await request.body()
    if len(body) > _MAX_SCOPE_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    response = await _agent_request(agent_name, "POST", "/api/brain-orb/scope", content=body, timeout=200.0)
    return _passthrough(response, not_found="Scope control not supported")


@router.post("/{agent_name}/brain-orb/voice-token")
async def post_brain_orb_voice_token(agent_name: AuthorizedAgentByName, current_user: CurrentUser):
    """Mint a short-lived, config-locked Gemini Live **ephemeral token** for the
    orb's client-held voice tile (#58 Phase 3).

    The browser connects DIRECTLY to Gemini Live with this token — Trinity never
    proxies the audio. The token's own constraints (model lock, locked tool
    surface, single new-session use, short expiry) are the security envelope; this
    route's job is the JWT gate + a per-user mint budget.

    Gated on the **voice** flag (distinct from ``BRAIN_ORB_ENABLED``): 404 when
    ``BRAIN_ORB_VOICE_ENABLED`` is off, 503 when no Gemini key, 502 on mint error.

    The response field is deliberately named ``ephemeral_token``, never a bare
    ``token`` — defensive/explicit (the voice iframe only ever sees the single-use
    Google token). Whether the owner's session may WRITE is decided server-side by
    ``can_write`` folding the capture/link tools into the locked manifest (#58 Phase
    4a); the backend ``/action`` route is the hard gate regardless. The agent is not
    contacted (no container check) — the mint is a Google call, not an agent call.
    """
    if not BRAIN_ORB_VOICE_ENABLED:
        raise HTTPException(status_code=404, detail="Brain Orb voice is not enabled")
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Voice is not configured")
    rate_limiter.enforce(
        f"brain_orb_voice_token:{current_user.id}:{agent_name}",
        _VOICE_TOKEN_RATE_LIMIT,
        _VOICE_TOKEN_RATE_WINDOW,
        detail="Too many voice sessions.",
    )
    # Write tools are minted into the locked manifest ONLY for owners/admins (#58
    # Phase 4a). The mint route is AuthorizedAgentByName (shared users may voice-chat),
    # so compute owner access here — same predicate OwnedAgentByName enforces. Shared
    # users get the read-only manifest; the backend /action route is the hard gate.
    can_write = BRAIN_ORB_WRITE_ENABLED and db.can_user_share_agent(current_user.username, agent_name)
    try:
        result = await brain_orb_voice_service.mint_voice_token(
            agent_name,
            voice_name=db.get_voice_name(agent_name),
            agent_prompt=db.get_voice_system_prompt(agent_name),
            can_write=can_write,
        )
    except ValueError:
        # No Gemini key surfaced from the service layer — treat as unconfigured.
        raise HTTPException(status_code=503, detail="Voice is not configured")
    except Exception as exc:  # SDK / network / quota — never leak internals.
        logger.warning("brain-orb voice-token mint failed for %s: %s", agent_name, exc)
        raise HTTPException(status_code=502, detail="Could not mint a voice session")
    return Response(
        content=json.dumps(result),
        media_type="application/json",
        headers=_NO_STORE,
    )


@router.post("/{agent_name}/brain-orb/tool")
async def post_brain_orb_tool(agent_name: AuthorizedAgentByName, request: Request):
    """Read-only KB-search broker (#58 Phase 3). Proxies to the agent-server, which
    runs the agent's ``~/.trinity/brain-orb/search`` convention hook (scope-aware,
    read-only). Read access (``AuthorizedAgentByName``) — search never writes. 404
    when the agent ships no ``search`` hook."""
    body = await request.body()
    if len(body) > _MAX_TOOL_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    response = await _agent_request(agent_name, "POST", "/api/brain-orb/tool", content=body, timeout=30.0)
    return _passthrough(response, not_found="KB search not supported")


def _require_write_enabled() -> None:
    """404 unless the KB-write surface flag is on. Combines with ``_agent_request``'s
    ``BRAIN_ORB_ENABLED`` gate so both flags are required. Separate kill-switch so
    writes can be disabled without downing the read path / voice tile (#58 Phase 4a)."""
    if not BRAIN_ORB_WRITE_ENABLED:
        raise HTTPException(status_code=404, detail="Brain Orb writes are not enabled")


async def _safe_audit(**kwargs) -> None:
    """Best-effort audit: a logging failure must never mask a completed write or 500 the
    caller — for a fresh-idempotency-key client that would drive a duplicate write (review F5)."""
    try:
        await platform_audit_service.log(**kwargs)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("brain-orb audit log failed: %s", e)


@router.get("/{agent_name}/brain-orb/actions")
async def get_brain_orb_actions(agent_name: OwnedAgentByName):
    """Report the agent's write-surface capability + skill allow-list for the orb's
    action panel (#58 Phase 4a). **Owner/admin only** — a non-owner gets 403 and the
    orb hides the panel. 404 when the write flag is off or the agent ships no `action`
    hook. Proxies the agent-server `GET /api/brain-orb/actions`."""
    _require_write_enabled()
    response = await _agent_request(agent_name, "GET", "/api/brain-orb/actions", timeout=30.0)
    return _passthrough(response, not_found="KB writes not supported")


@router.post("/{agent_name}/brain-orb/action")
async def post_brain_orb_action(
    agent_name: OwnedAgentByName,
    request: Request,
    current_user: CurrentUser,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Run an owner-gated KB-write action — **capture** (write a note), **link**
    (connect two notes), **capture_transcript** (save a voice-session transcript),
    or **process_transcript** (run the agent's configured post-session prompt over a
    transcript). **Owner/admin only.**

    The `action` verb is enum-validated at this boundary (`run_skill` remains out of
    scope → 400 here, never reaching the agent hook).
    Body-capped (413), rate-limited per (user, agent, action), audit-logged, and
    deduped via `Idempotency-Key` (Invariant #18): a re-POST with the same key replays
    the stored result (`X-Idempotent-Replay: true`) without a second write; an in-flight
    duplicate → 409. The key folds the action verb so a reused client key can't replay a
    different verb's snapshot. The agent owns the write semantics (Invariant #8)."""
    _require_write_enabled()
    body = await request.body()
    if len(body) > _MAX_ACTION_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in _ALLOWED_ACTIONS:
        # run_skill (arbitrary exec) remains out of scope; anything else invalid.
        raise HTTPException(status_code=400, detail="Unsupported action")

    # Idempotency FIRST (before rate limit) so a legit retry replays instead of 429ing.
    scope = idempotency_service.make_agent_scope(agent_name)
    key = f"brain_orb_action:{action}:{idempotency_key}" if idempotency_key else None
    decision = idempotency_service.begin(scope, key)
    if decision.replay:
        if decision.in_flight:
            raise HTTPException(status_code=409, detail="Action already in progress")
        snapshot = decision.snapshot or {}
        return Response(
            content=json.dumps(snapshot),
            media_type="application/json",
            headers={**_NO_STORE, "X-Idempotent-Replay": "true"},
        )

    try:
        rate_limiter.enforce(
            f"brain_orb_action:{current_user.id}:{agent_name}:{action}",
            _ACTION_RATE_LIMIT,
            _ACTION_RATE_WINDOW,
            detail="Too many Brain Orb actions.",
        )
        # Backend timeout sits ABOVE the agent-server hook's 60s (review F3) so a slow
        # hook surfaces as the agent's own 504, not a premature backend timeout that
        # would release the idempotency claim on an ambiguous outcome → double-write.
        response = await _agent_request(
            agent_name, "POST", "/api/brain-orb/action", content=body, timeout=90.0
        )
        result = _passthrough(response, not_found="KB writes not supported")
    except HTTPException:
        # Release the in-flight claim so the client can retry (429 / agent error / 504).
        idempotency_service.fail(decision)
        raise
    except Exception:
        idempotency_service.fail(decision)
        raise

    try:
        snapshot = json.loads(response.content)
    except ValueError:
        snapshot = None
    idempotency_service.complete(decision, None, snapshot)

    await _safe_audit(
        event_type=AuditEventType.CONFIGURATION,
        event_action=f"brain_orb_{action}",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"action": action},
    )
    return result


@router.post("/{agent_name}/brain-orb/refresh")
async def post_brain_orb_refresh(agent_name: OwnedAgentByName, request: Request, current_user: CurrentUser):
    """Reindex + re-export the agent's graph so newly captured notes / links appear on
    the orb (#66/#67). **Owner/admin only.** Closes the write → re-export → refetch loop:
    the orb calls this after a capture/link (and via a manual refresh), then re-fetches
    `/brain-orb/data` and rebuilds in place — mirroring the Phase-2 scope flow. The agent
    owns the reindex + generation (Invariant #8); Trinity brokers the trigger. 200s timeout
    sits above the agent hook's 180s (a large-vault reindex can be slow). 404 when the agent
    ships no `action` hook. Audited (`brain_orb_refresh`).

    Write-flag-gated (review F1): refresh drives the agent's `action` hook (an exec/
    reindex), so the `BRAIN_ORB_WRITE_ENABLED` kill-switch must gate it too — like
    every other Phase-4 write route — not just `BRAIN_ORB_ENABLED`."""
    _require_write_enabled()
    response = await _agent_request(agent_name, "POST", "/api/brain-orb/refresh", timeout=200.0)
    result = _passthrough(response, not_found="Refresh not supported")
    await _safe_audit(
        event_type=AuditEventType.CONFIGURATION,
        event_action="brain_orb_refresh",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
    )
    return result


@router.get("/{agent_name}/brain-orb/postprocess")
async def get_brain_orb_postprocess(agent_name: OwnedAgentByName):
    """Read the agent's post-voice-processing config `{enabled, prompt}` for the Brain
    tab (#73). **Owner/admin only**, write-flag-gated (it configures the exec surface).
    Proxies the agent-server `GET /api/brain-orb/postprocess`."""
    _require_write_enabled()
    response = await _agent_request(agent_name, "GET", "/api/brain-orb/postprocess", timeout=30.0)
    return _passthrough(response, not_found="Brain Orb not available")


@router.put("/{agent_name}/brain-orb/postprocess")
async def put_brain_orb_postprocess(agent_name: OwnedAgentByName, request: Request, current_user: CurrentUser):
    """Write the agent's post-voice-processing config `{enabled, prompt}` from the Brain
    tab (#73). **Owner/admin only**, write-flag-gated, body-capped, audited. The agent's
    `action` hook reads this to gate the post-session `claude -p` (enabled:false keeps
    the saved prompt but skips execution)."""
    _require_write_enabled()
    body = await request.body()
    # A prompt config is small — cap at 64 KiB to match the agent-server (review F4),
    # not the 1 MiB transcript cap, so an over-large body is rejected at the boundary.
    if len(body) > _MAX_SCOPE_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    response = await _agent_request(agent_name, "PUT", "/api/brain-orb/postprocess", content=body, timeout=30.0)
    result = _passthrough(response, not_found="Brain Orb not available")
    await _safe_audit(
        event_type=AuditEventType.CONFIGURATION,
        event_action="brain_orb_postprocess_config",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
    )
    return result
