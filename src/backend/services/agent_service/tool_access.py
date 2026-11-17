"""Tool-access profiles — reads and the three writers (trinity-enterprise#341, GUARD-008).

The policy (bundles, derivation, what each writer changes) is the pure
`services/tool_access_profiles.py`. This module reads the agent's row, applies
that policy, and writes the result in ONE compare-and-set
(`db.write_tool_access_row`), so the profile and the enforced layers can never
disagree — a write that lost a race to another writer is a 409.

Three writers, all through `_commit`:
* `set_tool_access_logic` — `PUT /tool-access`, the one writer of the profile;
* `set_read_only_shortcut_logic` — `PUT /read-only`, the header and list toggle
  (ON = Strict read-only, OFF = back to the profile it came from);
* `apply_guardrails_put` — `PUT /guardrails`, which keeps the stored profile.

The read-only layer is applied live to a running agent (the existing root
`docker exec`); the tool list is baked into `AGENT_GUARDRAILS` and applies on
the next restart, which `restart_required` reports honestly.
"""
import json
import logging

from fastapi import HTTPException

from database import db
from services import tool_access_profiles as tap
from services.docker_service import get_agent_container
from services.platform_audit_service import platform_audit_service, AuditEventType

from .read_only import get_default_config, inject_read_only_hooks, remove_read_only_hooks

logger = logging.getLogger(__name__)

_KEEP = object()


def _http(err: tap.ToolAccessError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.detail)


def _container_or_404(agent_name: str):
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    return container


def _row_or_404(agent_name: str) -> dict:
    row = db.get_tool_access_row(agent_name)
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row


def _runtime_of(container) -> str:
    labels = (getattr(container, "attrs", None) or {}).get("Config", {}).get("Labels") or {}
    return labels.get("trinity.agent-runtime") or "claude-code"


def _restart_required(container, cfg: dict) -> bool:
    """True when the container runs a different TOOL LIST than is stored.

    The tool list only: a pending turn-cap change is the rules editor's own
    banner entry, and naming it "Tool access" too would list one change twice.
    An unreadable `AGENT_GUARDRAILS` reads as pending, as the drift check
    (`check_guardrails_env_matches`) treats it as drift and recreates.
    """
    env = (getattr(container, "attrs", None) or {}).get("Config", {}).get("Env") or []
    raw = next((e.split("=", 1)[1] for e in env if isinstance(e, str) and e.startswith("AGENT_GUARDRAILS=")), "")
    try:
        running = json.loads(raw) if raw else {}
    except ValueError:
        return True
    return set(tap.enforced_tools(running)) != set(tap.enforced_tools(cfg))


def _view(agent_name: str, container, cfg: dict, read_only: bool) -> dict:
    resolved = tap.resolve(cfg, read_only)
    runtime = _runtime_of(container)
    return {
        "agent_name": agent_name,
        **resolved,
        "profile_label": tap.label(resolved["profile"]),
        "runtime": runtime,
        "profiles": tap.catalog(runtime, resolved),
        "restart_required": _restart_required(container, cfg),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def get_tool_access_logic(agent_name: str) -> dict:
    """`GET /tool-access` (the route gates owner tier)."""
    container = _container_or_404(agent_name)
    row = _row_or_404(agent_name)
    return _view(agent_name, container, row["guardrails"], row["read_only"])


def tool_access_profile_for(agent_name: str) -> str:
    """The profile id for the agent detail payload (the header toggle)."""
    row = db.get_tool_access_row(agent_name) or {}
    return tap.resolve(row.get("guardrails"), row.get("read_only", False))["profile"]


# ---------------------------------------------------------------------------
# The one commit path
# ---------------------------------------------------------------------------

async def _commit(agent_name, container, row, new_cfg, new_ro, *, current_user, request,
                  via: str, read_only_config=_KEEP) -> dict:
    """Write the row in one compare-and-set, apply the read-only layer live,
    audit the change, and answer with the resolved state."""
    before = tap.resolve(row["guardrails"], row["read_only"])
    if new_ro and read_only_config is _KEEP and not row["read_only_config"]:
        read_only_config = get_default_config()

    kwargs = {}
    if read_only_config is not _KEEP:
        kwargs["read_only_config"] = read_only_config
    ok = db.write_tool_access_row(
        agent_name,
        expected_guardrails_raw=row["guardrails_raw"],
        expected_read_only=row["read_only"],
        guardrails=new_cfg or None,
        read_only=new_ro,
        **kwargs,
    )
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Another change to this agent's tool access was saved at the same moment. "
                   "Reload and try again.",
        )

    # The read-only layer applies now; a failure leaves it to the next start,
    # which re-syncs it from the row (lifecycle start), and says so.
    read_only_applied = None
    layer_moved = new_ro != row["read_only"] or read_only_config is not _KEEP
    if layer_moved and getattr(container, "status", None) == "running":
        read_only_applied = await _apply_read_only_live(agent_name, new_ro)
        # Converge: two writers each win their compare-and-set in turn, but
        # their docker execs run concurrently and can finish in either order.
        # Whoever finishes re-reads the row once and, if the layer moved
        # since its own write, applies what is stored now — so the last exec
        # to finish always leaves the container matching the row.
        latest = bool(db.get_read_only_mode(agent_name).get("enabled"))
        if latest != new_ro:
            read_only_applied = await _apply_read_only_live(agent_name, latest)

    view = _view(agent_name, container, new_cfg, new_ro)
    if read_only_applied is False:
        view["restart_required"] = True
    view["read_only_applied"] = read_only_applied

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="tool_access_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request is not None and request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=request.scope.get("path") if request is not None else None,
        request_id=getattr(request.state, "request_id", None) if request is not None else None,
        details={
            "via": via,
            "from": before["profile"],
            "to": view["profile"],
            "from_read_only": before["read_only"],
            "read_only": new_ro,
            "from_disallowed_tools": before["disallowed_tools"],
            "disallowed_tools": view["disallowed_tools"],
        },
    )
    logger.info("Tool access for %s: %s -> %s (via %s, by %s)", agent_name, before["profile"],
                view["profile"], via, getattr(current_user, "username", None))
    return view


async def _apply_read_only_live(agent_name: str, read_only: bool) -> bool:
    """Write (or remove) the root read-only config in the running container."""
    try:
        if read_only:
            stored = db.get_read_only_mode(agent_name).get("config") or get_default_config()
            result = await inject_read_only_hooks(agent_name, stored)
        else:
            result = await remove_read_only_hooks(agent_name)
        return bool(result.get("success"))
    except Exception as e:  # noqa: BLE001 — reported as read_only_applied=False + restart_required
        logger.warning("Tool access: live read-only apply failed for %s: %s", agent_name, e)
        return False


def _refuse_system_agent(agent_name: str) -> None:
    if db.is_system_agent(agent_name):
        raise HTTPException(status_code=403, detail="The system agent's tool access is managed by the platform")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

async def set_tool_access_logic(agent_name: str, body, current_user, request=None) -> dict:
    """`PUT /tool-access` — select a profile (the route has gated the caller)."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    container = _container_or_404(agent_name)
    _refuse_system_agent(agent_name)
    row = _row_or_404(agent_name)
    try:
        new_cfg, new_ro = tap.plan_selection(
            row["guardrails"], row["read_only"], body.get("profile"), body.get("custom"))
    except tap.ToolAccessError as e:
        raise _http(e)
    return await _commit(agent_name, container, row, new_cfg, new_ro,
                         current_user=current_user, request=request, via="tool_access")


async def set_read_only_shortcut_logic(agent_name: str, enabled: bool, config, current_user,
                                       request=None, *, container) -> dict:
    """`PUT /read-only` — ON selects Strict read-only, OFF returns to the
    profile it came from. `config` (patterns) is stored when given. The
    caller (`read_only.set_read_only_status_logic`) has looked the container up."""
    _refuse_system_agent(agent_name)
    row = _row_or_404(agent_name)
    try:
        plan = tap.plan_shortcut(row["guardrails"], row["read_only"], enabled)
    except tap.ToolAccessError as e:
        raise _http(e)

    ro_config = config if config is not None else _KEEP
    if plan is None:
        if ro_config is _KEEP or not row["read_only"]:
            view = _view(agent_name, container, row["guardrails"], row["read_only"])
            view["read_only_applied"] = None
            return view
        # Already Strict and new patterns were sent: store and apply them.
        plan = (row["guardrails"], row["read_only"])
    new_cfg, new_ro = plan
    return await _commit(agent_name, container, row, new_cfg, new_ro, current_user=current_user,
                         request=request, via="read_only_toggle", read_only_config=ro_config)


def apply_guardrails_put(agent_name: str, cleaned: dict) -> dict:
    """`PUT /guardrails` — write the validated overrides, keeping the stored
    profile (and its tool list). Returns what was stored."""
    row = _row_or_404(agent_name)
    try:
        merged = tap.merge_guardrails_put(row["guardrails"], cleaned)
    except tap.ToolAccessError as e:
        raise _http(e)
    ok = db.write_tool_access_row(
        agent_name,
        expected_guardrails_raw=row["guardrails_raw"],
        expected_read_only=row["read_only"],
        guardrails=merged or None,
        read_only=row["read_only"],
    )
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Another change to this agent's guardrails was saved at the same moment. "
                   "Reload and try again.",
        )
    return merged
