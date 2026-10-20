"""Per-agent PULL_MODE opt-in (#946 / #1081 Phase 2).

Decides whether an agent runs the agent-side pull worker pool
(``docker/base-image/agent_server/services/pull_worker.py``) and builds the env
vars that turn it on. **Default OFF.** An agent opts in ONLY when its name is in
the fleet allowlist env ``PULL_MODE_PILOT_AGENTS`` (comma-separated). Not in the
allowlist ⇒ ``pull_mode_env_vars`` returns ``{}`` ⇒ no vars injected ⇒ the
agent-server gate stays OFF ⇒ the existing push path is byte-for-byte unchanged.

**Reconciliation with #1293** (``MCP_AGENT_CHAT_PULL_ENABLED``): that flag is the
global **producer**-routing switch (how agent→agent chat is *enqueued*, decided in
the MCP server). This is the orthogonal per-agent **consumer** switch (whether a
given agent *pulls* queued work). They live at different layers (global MCP env vs
one agent's container env) and gate different sides of the same durable queue, so
they are deliberately separate flags — not a duplicated one.

**Auth (least-privilege, #307/#1159).** The worker authenticates to the two pull
seams (``/api/internal/next-task`` + ``/api/internal/tasks/{id}/result``) with the
agent's OWN agent-scoped MCP key — already injected into every agent as
``TRINITY_MCP_API_KEY`` — mirroring ``heartbeat.py`` / ``result_callback.py``. The
seams accept that scoped key (validated via ``authorize_heartbeat``) as an
alternate to the internal secret, so the master ``INTERNAL_API_SECRET`` is NEVER
injected into an agent container: a compromised pilot can only claim/report for
ITSELF, never reach the other ``/api/internal/*`` endpoints. This module injects
ONLY the two non-secret pull knobs below.
"""
from __future__ import annotations

import logging
import os
from typing import Dict

from db.agents import SYSTEM_AGENT_NAME
# The predicates live in the LEAF module `services/pull_pilot.py` and are
# re-exported here so every existing importer keeps working. They CANNOT live in
# this file: `services/agent_service/__init__.py` eagerly imports the whole
# agent-lifecycle stack (helpers/lifecycle/crud/deploy/terminal → models), so a
# dispatch-path module doing `from services.agent_service.pull_mode import ...`
# drags all of it in. See the docstring in `services/pull_pilot.py`.
#
# Dispatch-path callers (`capacity_manager`, `backlog_service`, `routers/internal`)
# import from `services.pull_pilot` DIRECTLY — importing them from here would
# reintroduce exactly the dependency this split removes.
from services.pull_pilot import (  # noqa: F401 — re-exported for back-compat
    _pilot_allowlist,
    is_pull_pilot_agent,
    pull_owns_dispatch,
)

logger = logging.getLogger(__name__)


# The container env keys this module manages. ``apply_pull_mode_env`` pops these
# BEFORE re-applying ``pull_mode_env_vars`` so de-piloting an agent actually
# clears the baked pull flag: ``pull_mode_env_vars`` returns ``{}`` for
# a non-pilot, so a bare ``.update()`` would leave a stale ``TRINITY_PULL_MODE=
# true`` baked in and the worker would keep running after de-pilot (#1081 B1 —
# mirrors the guardrails / stall-limit set-or-clear idiom in ``lifecycle.py``).
PULL_MODE_ENV_KEYS = ("TRINITY_PULL_MODE", "TRINITY_MAX_PARALLEL_TASKS")


def pull_mode_env_vars(agent_name: str) -> Dict[str, str]:
    """Env vars to inject into an opted-in pilot agent's container. Empty dict for
    every non-pilot agent (the default) — the safety property the agent-server
    gate relies on. ``.update()`` this into the container ``env_vars`` at create
    (``crud.py``) and recreate (``lifecycle.py``).

    Deliberately NON-secret: the worker authenticates with the agent's own
    ``TRINITY_MCP_API_KEY`` (already injected by the platform), so the master
    internal secret is never placed in an agent container (#307/#1159)."""
    if not is_pull_pilot_agent(agent_name):
        return {}

    # Pool bound = the agent's effective max_parallel_tasks (ceiling-clamped).
    try:
        from services.settings_service import get_effective_max_parallel_tasks

        size = get_effective_max_parallel_tasks(agent_name)
    except Exception as e:  # noqa: BLE001 — never block agent creation on a settings read
        size = 3
        # #3590: the fallback used to be silent — a pilot sized 3 when its owner
        # set 8 was invisible until someone counted workers in the log.
        logger.warning(
            "[#3590] pull pilot %r: could not read max_parallel_tasks (%s); "
            "baking the default pool size %d",
            agent_name, e, size,
        )

    return {
        "TRINITY_PULL_MODE": "true",
        "TRINITY_MAX_PARALLEL_TASKS": str(size),
    }


def apply_pull_mode_env(agent_name: str, env_vars: Dict[str, str]) -> None:
    """Set-or-clear the pull knobs on a container env, in place.

    The ONE owner of the idiom for every site that assembles an agent container
    env — creation (``crud._build_env_vars``), the config-drift recreate and the
    #1559 rebuild-from-nothing (both in ``lifecycle.py``). #3590: the rebuild
    path re-derived its env independently and never applied the opt-in, so a
    pilot whose container was lost came back with no worker while the backend
    kept routing its work to the durable queue — every task sat ``queued`` with
    no warning. Two prior entries in the learnings ledger name this class
    ("the create path is never one call site"); a third inline copy would have
    re-armed it for the fourth site. ``tests/unit/test_3590_rebuild_pull_env.py``
    pins the writer set.

    Pop first, then update: ``pull_mode_env_vars`` returns ``{}`` for a
    non-pilot, so a bare ``.update()`` would leave a stale ``TRINITY_PULL_MODE=
    true`` baked in after de-pilot (#1081 B1). Resolves ``pull_mode_env_vars``
    through the module global at call time so the suites that stub it keep
    intercepting.
    """
    for key in PULL_MODE_ENV_KEYS:
        env_vars.pop(key, None)
    env_vars.update(pull_mode_env_vars(agent_name))

    # #3814: the system agent carries no TRINITY_BACKEND_URL (#1816), and the
    # pull worker cannot start without it. It gets the URL while piloted and
    # loses it otherwise — also on a pre-#1816 container that still had it.
    # De-pilot drops the heartbeat `seen` marker (no TTL) so the orchestrator
    # does not read as a lost heartbeat once it stops beating.
    if agent_name == SYSTEM_AGENT_NAME:
        if "TRINITY_PULL_MODE" in env_vars:
            env_vars["TRINITY_BACKEND_URL"] = os.getenv("TRINITY_BACKEND_URL", "http://backend:8000")
        elif env_vars.pop("TRINITY_BACKEND_URL", None) is not None:
            from services import heartbeat_service

            heartbeat_service.clear_heartbeat(agent_name)
