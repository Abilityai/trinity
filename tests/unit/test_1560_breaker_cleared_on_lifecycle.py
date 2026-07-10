"""#1560 — a recycled agent name must not inherit its predecessor's breaker verdict.

The transport circuit breaker lives at ``agent:circuit:{agent_name}`` — keyed by
name, never expiring. Nothing in the agent lifecycle cleared it, so a container
recreated (or a name reused after the retention purge) started life carrying the
*previous* agent's ``dormant`` verdict. Every execution then fast-failed with
"Agent circuit breaker open — agent is unhealthy" **without the backend ever
contacting the agent**, for as long as the verdict stood.

Two layers here:

1. **Behaviour** of ``services/agent_runtime_state.py`` — the single enumeration
   point. Its two entry points have deliberately different blast radii, and each
   is fail-open per subsystem so one dead Redis call cannot stop the others.

2. **Wiring**, asserted against the source. Driving ``start_agent_internal``
   end-to-end needs the whole Docker surface mocked; what actually matters is
   structural and is checked directly:

   * the breaker clear runs on **start/recreate** — the only path that reproduces
     the bug at fleet scale (a subscription-key rotation or resource-default
     change mass-recreates every agent);
   * it is **guarded** so a no-op start of an already-running agent cannot be used
     to reset a breaker protecting a genuinely wedged agent;
   * ``lifecycle.py`` never calls the *full* sweep, because ``force_clear_slots``
     wholesale-``DEL``s ``agent:slots:{name}`` and the container is live there —
     that would drop capacity accounting for an in-flight fire-and-forget
     execution (#1083);
   * the teardown paths (delete / rename / purge) **replace** their old
     heartbeat-only clear rather than stacking a second one beside it.

``clear_agent_breakers`` resolves its dependencies through **call-time lazy
imports**, so patching an attribute on an already-imported module object would
never be seen (see ``docs/memory/learnings.md``, 2026-07-07). The stubs below own
the ``sys.modules`` keys instead.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load(mod_name: str, rel_path: str):
    """Load the module by path, bypassing ``services/__init__.py`` (Docker SDK).

    Deliberately NOT registered in ``sys.modules`` — the target is a stdlib-only
    leaf with no ``@dataclass`` annotation resolution to satisfy, so registering it
    would only risk cross-file pollution (#762). The per-test stubs below go
    through ``monkeypatch.setitem``, which restores itself.
    """
    spec = importlib.util.spec_from_file_location(mod_name, str(_BACKEND / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ars = _load("trinity_agent_runtime_state_behaviour", "services/agent_runtime_state.py")


def _await(coro):
    return asyncio.run(coro)


class _Recorder:
    """Captures which subsystems were cleared, in order, and for which agent."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.slots_cleared = 0


@pytest.fixture
def stubs(monkeypatch):
    """Install a fake `services` package whose submodules record their calls.

    `clear_agent_breakers` does `from services import heartbeat_service` and
    `from services.agent_client import reset_circuit` at call time, so owning the
    sys.modules keys is the only stub that production code actually resolves.
    """
    rec = _Recorder()
    raise_in: dict[str, Exception] = {}

    pkg = types.ModuleType("services")

    hb = types.ModuleType("services.heartbeat_service")

    def clear_heartbeat(name):
        rec.calls.append(("heartbeat", name))
        if "heartbeat" in raise_in:
            raise raise_in["heartbeat"]

    hb.clear_heartbeat = clear_heartbeat

    ac = types.ModuleType("services.agent_client")

    def reset_circuit(name):
        rec.calls.append(("transport_circuit", name))
        if "transport_circuit" in raise_in:
            raise raise_in["transport_circuit"]

    ac.reset_circuit = reset_circuit

    dbk = types.ModuleType("services.dispatch_breaker")

    def reset_dispatch(name):
        rec.calls.append(("dispatch_breaker", name))
        if "dispatch_breaker" in raise_in:
            raise raise_in["dispatch_breaker"]

    dbk.reset_dispatch = reset_dispatch

    ss = types.ModuleType("services.slot_service")

    class _SlotService:
        async def force_clear_slots(self, name):
            rec.calls.append(("slots", name))
            if "slots" in raise_in:
                raise raise_in["slots"]
            rec.slots_cleared += 1
            return rec.slots_cleared

    ss.get_slot_service = lambda: _SlotService()

    pkg.heartbeat_service = hb
    pkg.agent_client = ac
    pkg.dispatch_breaker = dbk
    pkg.slot_service = ss

    for name, mod in (
        ("services", pkg),
        ("services.heartbeat_service", hb),
        ("services.agent_client", ac),
        ("services.dispatch_breaker", dbk),
        ("services.slot_service", ss),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    rec.raise_in = raise_in
    return rec


# ---------------------------------------------------------------------------
# Behaviour: clear_agent_breakers (safe against a RUNNING container)
# ---------------------------------------------------------------------------


def test_clear_agent_breakers_clears_heartbeat_and_both_breakers(stubs):
    ars.clear_agent_breakers("recycled")

    assert stubs.calls == [
        ("heartbeat", "recycled"),
        ("transport_circuit", "recycled"),
        ("dispatch_breaker", "recycled"),
    ]


def test_clear_agent_breakers_never_touches_slots(stubs):
    """The whole point of the split: slots must survive a live container."""
    ars.clear_agent_breakers("running-agent")

    assert ("slots", "running-agent") not in stubs.calls
    assert stubs.slots_cleared == 0


@pytest.mark.parametrize(
    "failing", ["heartbeat", "transport_circuit", "dispatch_breaker"]
)
def test_one_failing_subsystem_does_not_block_the_others(stubs, failing):
    """Fail-open per subsystem — a Redis blip must not leave a stale verdict behind.

    In particular a heartbeat failure must not prevent the transport circuit
    (the #1560 key) from being cleared.
    """
    stubs.raise_in[failing] = RuntimeError(f"{failing} redis down")

    ars.clear_agent_breakers("agent-x")  # must not raise

    cleared = {subsystem for subsystem, _ in stubs.calls}
    assert cleared == {"heartbeat", "transport_circuit", "dispatch_breaker"}


def test_clear_agent_breakers_never_raises_into_the_lifecycle(stubs):
    for subsystem in ("heartbeat", "transport_circuit", "dispatch_breaker"):
        stubs.raise_in[subsystem] = RuntimeError("everything is down")

    ars.clear_agent_breakers("agent-x")  # must not raise


# ---------------------------------------------------------------------------
# Behaviour: clear_agent_runtime_state (teardown paths only)
# ---------------------------------------------------------------------------


def test_clear_agent_runtime_state_also_clears_slots(stubs):
    _await(ars.clear_agent_runtime_state("dead-agent"))

    assert stubs.calls == [
        ("heartbeat", "dead-agent"),
        ("transport_circuit", "dead-agent"),
        ("dispatch_breaker", "dead-agent"),
        ("slots", "dead-agent"),
    ]
    assert stubs.slots_cleared == 1


def test_clear_agent_runtime_state_survives_a_slot_failure(stubs):
    stubs.raise_in["slots"] = RuntimeError("redis down")

    _await(ars.clear_agent_runtime_state("dead-agent"))  # must not raise

    # The breakers — the actual #1560 defect — are still cleared.
    assert ("transport_circuit", "dead-agent") in stubs.calls


# ---------------------------------------------------------------------------
# Wiring: the five lifecycle call sites
# ---------------------------------------------------------------------------


def _src(rel_path: str) -> str:
    return (_BACKEND / rel_path).read_text(encoding="utf-8")


def test_start_path_clears_breakers_when_the_container_changed_or_came_up():
    """The load-bearing site: every recreate goes through `start_agent_internal`."""
    src = _src("services/agent_service/lifecycle.py")

    assert "from services.agent_runtime_state import clear_agent_breakers" in src
    assert re.search(
        r"if needs_recreation or not was_already_running:\s*\n\s*clear_agent_breakers\(agent_name\)",
        src,
    ), (
        "start_agent_internal must clear breakers when the container was recreated "
        "or newly started — and only then (a no-op start of a running agent must "
        "not reset a breaker protecting a wedged agent)"
    )


def test_start_path_clears_breakers_before_the_container_is_recreated():
    """Ordering matters: `recreate_container_with_updated_config` starts the
    replacement via `containers_run(detach=True)`. Clearing afterwards leaves a
    window where a concurrent dispatch reads the predecessor's verdict against an
    already-running container."""
    src = _src("services/agent_service/lifecycle.py")

    clear_at = src.index("clear_agent_breakers(agent_name)")
    recreate_at = src.index("await recreate_container_with_updated_config(")
    start_at = src.index("await container_start(container)")

    assert clear_at < recreate_at < start_at, (
        "clear_agent_breakers must run before the container is recreated/started"
    )


def test_start_path_never_clears_slots_on_a_live_container():
    """`force_clear_slots` would drop an in-flight async execution's slot (#1083)."""
    src = _src("services/agent_service/lifecycle.py")

    assert "clear_agent_runtime_state" not in src, (
        "lifecycle.py must use clear_agent_breakers, never the full sweep — the "
        "container is running there"
    )


def test_create_path_clears_breakers_before_the_container_exists():
    src = _src("services/agent_service/crud.py")

    assert "clear_agent_breakers(config.name)" in src
    assert "clear_agent_runtime_state" not in src
    assert src.index("clear_agent_breakers(config.name)") < src.index("await containers_run(")


def test_system_agent_create_clears_breakers_too():
    """`trinity-system` is a permanently-recycled fixed name, so its bootstrap is
    the create path in miniature. Fixing only `crud.py` would leave the one agent
    guaranteed to reuse its name unprotected."""
    src = _src("services/system_agent_service.py")

    assert "clear_agent_breakers(SYSTEM_AGENT_NAME)" in src
    assert src.index("clear_agent_breakers(SYSTEM_AGENT_NAME)") < src.index(
        "container = await containers_run("
    ), "the clear must precede container creation"


def test_delete_path_sweeps_everything_and_replaces_the_heartbeat_only_clear():
    src = _src("routers/agents.py")

    assert "await clear_agent_runtime_state(agent_name)" in src
    assert "heartbeat_service.clear_heartbeat" not in src, (
        "the full sweep supersedes the old heartbeat-only clear; stacking both "
        "leaves two places to forget a key"
    )


def test_rename_path_sweeps_both_the_old_and_the_new_name():
    src = _src("routers/agent_rename.py")

    assert "await clear_agent_runtime_state(agent_name)" in src
    assert "await clear_agent_runtime_state(sanitized_name)" in src
    assert "heartbeat_service.clear_heartbeat" not in src


def test_purge_sweeps_the_name_at_the_moment_it_becomes_reusable():
    """Until purge, `is_agent_name_reserved` blocks reuse; after it, the name is free."""
    src = _src("services/cleanup_service.py")

    assert "await clear_agent_runtime_state(name)" in src
    assert re.search(
        r"if db\.purge_agent_ownership\(name\):\s*\n\s*purged \+= 1\s*\n(\s*#.*\n)*\s*await clear_agent_runtime_state\(name\)",
        src,
    ), "the sweep must run for each successfully purged name"
