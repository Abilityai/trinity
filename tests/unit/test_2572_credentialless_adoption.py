"""#2572 — a credential-less fleet adopts an available subscription.

The bug: on an instance with no ``ANTHROPIC_API_KEY`` every agent sits in
``api_key`` auth mode with nothing behind it, and registering a subscription —
the one action the product tells the operator to take — assigns nobody. SUB-003
cannot help: it only moves agents that ALREADY have a subscription (its
precondition 2), which is exactly where the gap is written down.

Three triggers, no periodic sweep:
  A1  ``POST /api/subscriptions``              (routers/subscriptions.py)
  A2  the two Anthropic-key clear paths        (routers/settings.py)
  B   agent creation — already shipped by #74, pinned here, not re-implemented

Every test EXECUTES the service or the router boundary against a real schema
(``db_harness``) — a source-text assertion is not behaviour coverage (#2659).
The only things stubbed are the two edges a unit test cannot have: the Docker
runtime read and the container restart.

Modules under test:
    src/backend/services/subscription_service.py
    src/backend/routers/subscriptions.py::register_subscription
    src/backend/routers/settings.py::delete_anthropic_key / delete_setting
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_harness import db_backend, seed_user  # noqa: E402,F401
from db_harness import _engine as _harness_engine  # noqa: E402

_BACKEND_STR = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.asyncio

_ENCRYPTION_KEY = "a" * 64
SYSTEM_AGENT = "trinity-system"


@dataclass
class _Admin:
    """The principal shape the admin gate reads (#2323 allowlist)."""

    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    mcp_scope: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None


def _http_request(path: str = "/api/subscriptions"):
    """The injected ``Request``, read only for audit context."""
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host="10.0.0.9"),
        url=types.SimpleNamespace(path=path),
        state=types.SimpleNamespace(request_id="req-2572"),
    )


# ---------------------------------------------------------------------------
# Seeds (raw SQL through the harness engine — `seed_agent` cannot express the
# three columns this predicate is made of)
# ---------------------------------------------------------------------------

def _seed_agent(
    name: str,
    *,
    use_platform_api_key: int = 1,
    subscription_id: Optional[str] = None,
    is_ephemeral: int = 0,
    owner_id: int = 1,
) -> None:
    from sqlalchemy import text

    with _harness_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_ownership "
                "(agent_name, owner_id, created_at, use_platform_api_key, "
                " subscription_id, is_ephemeral) "
                "VALUES (:a, :o, :n, :k, :s, :e)"
            ),
            {"a": name, "o": owner_id, "n": "2026-01-01T00:00:00Z",
             "k": use_platform_api_key, "s": subscription_id, "e": is_ephemeral},
        )


def _write_raw_setting(key: str, value: str) -> None:
    """Write a `system_settings` row WITHOUT ent#435's sink guard — the only way
    to reproduce a legacy cleartext credential row (a pre-fix backup restore)."""
    from sqlalchemy import text

    with _harness_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO system_settings (key, value, updated_at) "
                 "VALUES (:k, :v, :n)"),
            {"k": key, "v": value, "n": "2026-01-01T00:00:00Z"},
        )


def _audit_rows(event_action: str) -> list[dict]:
    from sqlalchemy import text

    with _harness_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT target_id, details, actor_ip, endpoint FROM audit_log "
                "WHERE event_action = :a ORDER BY target_id"
            ),
            {"a": event_action},
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture
def env(db_backend, monkeypatch):
    """A real schema, a seeded owner, no instance API key, and stubs for the
    only two edges a unit test cannot have: the Docker runtime read and the
    container restart."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _ENCRYPTION_KEY)
    # The predicate's condition 1 resolves through the env fallback, so the
    # "keyless instance" this issue is about means BOTH the settings row and
    # the environment variable are absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    seed_user(1, "owner", "admin")

    from database import db
    import services.subscription_service as svc
    import services.subscription_auto_switch as auto_switch
    import services.docker_utils as docker_utils

    # Locks are loop-bound; every test gets a fresh loop.
    svc._reset_sweep_lock_for_test()
    auto_switch._reset_locks_for_test()

    labels: dict[str, Optional[str]] = {}
    docker_calls: list[int] = []
    docker_answer: dict = {"value": labels}

    async def _labels():
        docker_calls.append(1)
        return docker_answer["value"]

    monkeypatch.setattr(
        docker_utils, "agent_container_runtime_labels_async", _labels
    )

    restarts: list[str] = []

    async def _restart(agent_name: str) -> str:
        restarts.append(agent_name)
        return "success"

    monkeypatch.setattr(auto_switch, "_restart_agent", _restart)

    yield types.SimpleNamespace(
        db=db, svc=svc, auto_switch=auto_switch, docker_utils=docker_utils,
        labels=labels, docker_calls=docker_calls, docker_answer=docker_answer,
        restarts=restarts, admin=_Admin(),
    )

    svc._reset_sweep_lock_for_test()
    auto_switch._reset_locks_for_test()


def _make_subscription(env, name: str, **kwargs):
    return env.db.create_subscription(
        name=name, token=f"sk-ant-oat01-{name}", owner_id=1, **kwargs
    )


def _claude(env, *names: str) -> None:
    """Give each agent a container carrying a Claude runtime label."""
    for name in names:
        env.labels[name] = "claude-code"


async def _drain_background() -> None:
    """Let the backgrounded apply phase run to completion."""
    import services.subscription_service as svc

    for _ in range(100):
        pending = [t for t in svc._adoption_tasks if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


# ===========================================================================
# The predicate — who is adopted, and (far more importantly) who is not
# ===========================================================================

class TestThePredicate:

    async def test_a_credentialless_agent_adopts_the_registered_subscription(self, env):
        """T1 / AC1 — the reported scenario: no instance key, an agent that
        predates the subscription, no per-agent click."""
        _seed_agent("scout")
        _claude(env, "scout")
        sub = _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {"scout": "eugene-max"}
        assert env.db.get_agent_subscription_id("scout") == sub.id
        # AC4 — both visibility surfaces are DB-derived, so they are correct the
        # instant the row is written. Asserted through the real resolver.
        status = await env.svc.get_agent_auth_mode("scout")
        assert status.auth_mode == "subscription"
        assert status.subscription_name == "eugene-max"
        assert env.db.get_agents_by_subscription(sub.id) == ["scout"]

    async def test_an_agent_with_a_working_platform_api_key_is_not_moved(self, env, monkeypatch):
        """T2 / AC2 — the issue's cardinal sin. An instance key means real spend
        on a metered key; moving it onto someone's personal plan is a billing
        decision the operator has not made."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {}
        assert env.db.get_agent_subscription_id("scout") is None
        # Short-circuited before Docker was even asked.
        assert env.docker_calls == []

    async def test_an_agent_already_on_a_subscription_is_not_moved(self, env):
        """T3 / AC3."""
        first = _make_subscription(env, "aaa-first")
        _make_subscription(env, "zzz-second")
        _seed_agent("scout", subscription_id=first.id)
        _claude(env, "scout")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {}
        assert env.db.get_agent_subscription_id("scout") == first.id

    async def test_an_agent_opted_out_of_the_platform_key_is_not_moved(self, env):
        """T4 — `use_platform_api_key=False` is the operator asserting the agent
        brings its own `.env` credential, which the backend cannot see. Adopting
        would override that choice AND make the agent-side #2114 guard
        force-unset the key that was working."""
        _seed_agent("byo-key", use_platform_api_key=0)
        _claude(env, "byo-key")
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {}
        assert env.db.get_agent_subscription_id("byo-key") is None


# ===========================================================================
# The runtime gate — three branches, three outcomes
# ===========================================================================

class TestTheRuntimeGate:

    async def test_a_codex_agent_is_never_given_a_claude_subscription(self, env):
        """T5 / #1187 decision 7 — a subscription IS a Claude OAuth token."""
        _seed_agent("scout")
        _seed_agent("coder")
        _claude(env, "scout")
        env.labels["coder"] = "codex"
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {"scout": "eugene-max"}
        assert env.db.get_agent_subscription_id("coder") is None

    async def test_an_agent_with_no_container_is_skipped(self, env):
        """T6 — Docker ANSWERED and this agent simply has no container. Skipped
        rather than adopted: the runtime is unverifiable without one."""
        _seed_agent("scout")
        _seed_agent("pruned")
        _claude(env, "scout")  # `pruned` is absent from a VALID map
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {"scout": "eugene-max"}
        assert env.db.get_agent_subscription_id("pruned") is None

    async def test_an_unreadable_docker_adopts_nobody(self, env):
        """T6b — the tri-state's third arm: Docker could not be ASKED. Fail
        CLOSED. Adopting across an unverifiable fleet would hand every
        Gemini/Codex agent a subscription in one shot."""
        _seed_agent("scout")
        _seed_agent("coder")
        env.docker_answer["value"] = None
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents(actor_user=env.admin)

        assert adopted == {}
        assert env.db.get_agent_subscription_id("scout") is None
        assert env.db.get_agent_subscription_id("coder") is None

        # ...and the ABORT is on the record. This is the case the summary row
        # exists for: without it, "adopted 0 of 40 because Docker was
        # unreadable" is indistinguishable in `audit_log` from "nothing to do",
        # and the operator's only other signal is `agent_count` staying 0.
        rows = _audit_rows("subscription_auto_adopt_sweep")
        assert len(rows) == 1
        details = json.loads(rows[0]["details"])
        assert details["adopted"] == 0
        assert details["candidates"] == 2
        assert details["skipped"]["docker_unreadable"] == 2
        assert _audit_rows("subscription_auto_adopt") == []

    async def test_a_container_with_no_runtime_label_is_skipped(self, env):
        """T6c — the one that catches the `trinity-system` adoption. The batch
        runtime map defaults a MISSING label to "claude-code" and
        `is_claude_runtime(None)` is True by design, so a default-trusting gate
        adopts on no evidence. `trinity-system` carries no runtime label at all.
        """
        _seed_agent("scout")
        _seed_agent("unlabelled")
        _claude(env, "scout")
        env.labels["unlabelled"] = None  # container present, label absent
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {"scout": "eugene-max"}
        assert env.db.get_agent_subscription_id("unlabelled") is None

    async def test_at_most_one_docker_call_for_n_agents(self, env):
        """T6d — the cost bound. Pins the batch read so nobody "simplifies" it
        back to an inspect per agent (which is also how the fail-open
        "claude-code" default would sneak back in)."""
        for i in range(6):
            _seed_agent(f"agent-{i}")
            _claude(env, f"agent-{i}")
        _make_subscription(env, "eugene-max")

        await env.svc.adopt_for_credentialless_agents()

        assert len(env.docker_calls) == 1


# ===========================================================================
# Idempotency, concurrency and fail-open
# ===========================================================================

class TestSweepMechanics:

    async def test_the_sweep_is_idempotent(self, env):
        """T7 — `register_subscription` is an upsert, so a re-registration
        re-runs this. The second pass adopts nothing, through the REAL in-lock
        `subscription_id IS NULL` re-read."""
        _seed_agent("scout")
        _claude(env, "scout")
        sub = _make_subscription(env, "eugene-max")

        first = await env.svc.adopt_for_credentialless_agents()
        second = await env.svc.adopt_for_credentialless_agents()

        assert first == {"scout": "eugene-max"}
        assert second == {}
        assert env.db.get_agent_subscription_id("scout") == sub.id

    async def test_a_second_concurrent_sweep_is_skipped_not_queued(self, env):
        """T7b — the module `_SWEEP_LOCK`. Pre-HOLD it (a free `asyncio.Lock`
        does not yield on acquire, so a bare gather is not a race)."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")

        lock = env.svc._sweep_lock()
        await lock.acquire()
        try:
            adopted = await env.svc.adopt_for_credentialless_agents()
        finally:
            lock.release()

        assert adopted == {}
        assert env.db.get_agent_subscription_id("scout") is None
        assert env.docker_calls == []  # skipped, not queued behind the holder

    async def test_one_agents_failure_does_not_abort_the_sweep(self, env, monkeypatch):
        """T8 — fail-open per agent."""
        _seed_agent("aaa-bad")
        _seed_agent("bbb-good")
        _claude(env, "aaa-bad", "bbb-good")
        _make_subscription(env, "eugene-max")

        real_assign = env.db.assign_subscription_to_agent

        def _assign(agent_name, subscription_id):
            if agent_name == "aaa-bad":
                raise RuntimeError("db went away for this one row")
            return real_assign(agent_name, subscription_id)

        monkeypatch.setattr(env.db, "assign_subscription_to_agent", _assign)

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {"bbb-good": "eugene-max"}
        assert env.db.get_agent_subscription_id("aaa-bad") is None

    async def test_the_assign_happens_under_the_per_agent_switch_lock(self, env):
        """T14 / #799 — the same mutex SUB-003 and the manual assign path take.
        PRE-HOLD it and prove the sweep parks rather than assigning."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")

        lock = await env.auto_switch.agent_switch_lock("scout")
        await lock.acquire()

        task = asyncio.create_task(env.svc.adopt_for_credentialless_agents())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Parked at the per-agent lock — nothing written yet.
        assert env.db.get_agent_subscription_id("scout") is None

        lock.release()
        adopted = await task

        assert adopted == {"scout": "eugene-max"}

    async def test_a_single_candidate_is_resolved_once_for_the_whole_sweep(self, env, monkeypatch):
        """T15 — the dominant case (a first registration) pays ONE rank+decrypt,
        not one per agent."""
        for i in range(4):
            _seed_agent(f"agent-{i}")
            _claude(env, f"agent-{i}")
        _make_subscription(env, "eugene-max")

        calls: list[int] = []
        real_select = env.svc.select_subscription_for_new_agent

        def _counting_select():
            calls.append(1)
            return real_select()

        monkeypatch.setattr(env.svc, "select_subscription_for_new_agent", _counting_select)

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert len(adopted) == 4
        assert len(calls) == 1

    async def test_the_selection_rule_is_the_new_agent_ranker(self, env, monkeypatch):
        """T12 — the REAL `select_subscription_for_new_agent` decides, ranked by
        cached headroom (#2409), not patched and asserted against its own return.

        `busy` carries three agents and would win a naive "first row" pick only
        if load order were reversed; `roomy` wins because the ranker puts the
        subscription furthest from its nearest wall first.
        """
        import services.subscription_headroom_service as headroom

        busy = _make_subscription(env, "aaa-busy")     # sorts FIRST by name
        roomy = _make_subscription(env, "zzz-roomy")   # sorts last by name
        _seed_agent("holder-1", subscription_id=busy.id)
        _seed_agent("scout")
        _claude(env, "scout")

        def _readings(ids, **kwargs):
            return {
                busy.id: headroom.HeadroomReading(
                    tier="measured", five_hour_pct=95.0, seven_day_pct=95.0,
                    primary_pct=95.0, blocked=False, refused=False,
                ),
                roomy.id: headroom.HeadroomReading(
                    tier="measured", five_hour_pct=5.0, seven_day_pct=5.0,
                    primary_pct=5.0, blocked=False, refused=False,
                ),
            }

        monkeypatch.setattr(headroom, "cached_headroom_readings", _readings)

        adopted = await env.svc.adopt_for_credentialless_agents()

        assert adopted == {"scout": "zzz-roomy"}
        assert env.db.get_agent_subscription_id("scout") == roomy.id


# ===========================================================================
# Phase B — the container apply
# ===========================================================================

class TestTheApplyPhase:

    async def test_the_apply_phase_restarts_each_adopted_agent(self, env):
        """T11 — driven through `_spawn_apply_adoptions`, so the strong-ref /
        GC path (#526) is exercised rather than bypassed."""
        _seed_agent("scout")
        _seed_agent("scribe")
        _claude(env, "scout", "scribe")
        _make_subscription(env, "eugene-max")

        await env.svc.adopt_for_credentialless_agents()
        await _drain_background()

        assert sorted(env.restarts) == ["scout", "scribe"]

    async def test_the_system_agent_is_adopted_but_never_restarted(self, env):
        """T11b — `_restart_agent` STOPS the container first, so
        `was_already_running` is False by the time #1816's "never recreate a
        running trinity-system" guard is evaluated: the guard is bypassed by
        construction. An unattended fan-out triggered by a request on another
        resource must not be the caller that stops the orchestrator."""
        _seed_agent(SYSTEM_AGENT)
        _seed_agent("scout")
        _claude(env, SYSTEM_AGENT, "scout")
        sub = _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()
        await _drain_background()

        # Adopted in the DB — the badge and GET /api/subscriptions are correct,
        # and its next DELIBERATE restart applies the token.
        assert adopted[SYSTEM_AGENT] == "eugene-max"
        assert env.db.get_agent_subscription_id(SYSTEM_AGENT) == sub.id
        # ...but the sweep never touched its container.
        assert env.restarts == ["scout"]

    async def test_an_ephemeral_ghost_is_never_swept(self, env):
        """T11c — ghosts are volume-less by invariant and the AUTH recreate
        predicate carries no ghost exemption (unlike image-drift), so an adopted
        ghost would be recreated and its workspace destroyed mid-budget.
        Excluded from the sweep ENTIRELY, so it can never reach Phase B."""
        _seed_agent("ghost-1", is_ephemeral=1)
        _seed_agent("scout")
        _claude(env, "ghost-1", "scout")
        _make_subscription(env, "eugene-max")

        adopted = await env.svc.adopt_for_credentialless_agents()
        await _drain_background()

        assert adopted == {"scout": "eugene-max"}
        assert env.db.get_agent_subscription_id("ghost-1") is None
        assert env.restarts == ["scout"]

    async def test_phase_b_skips_an_agent_whose_subscription_moved(self, env):
        """T11d — Phase A CREATES SUB-003 eligibility, so between the phases a
        failing turn can legitimately auto-switch the agent onto a working
        credential and start a real turn. Restarting blind would kill it."""
        _seed_agent("scout")
        _claude(env, "scout")
        sub = _make_subscription(env, "eugene-max")
        other = _make_subscription(env, "other-max")

        adopted = await env.svc.adopt_for_credentialless_agents()
        assert adopted == {"scout": "eugene-max"}

        # SUB-003 moves it while Phase B is still queued.
        env.db.assign_subscription_to_agent("scout", other.id)
        await _drain_background()

        assert env.restarts == []
        assert env.db.get_agent_subscription_id("scout") == other.id
        assert sub.id != other.id


# ===========================================================================
# The audit trail (AC5)
# ===========================================================================

class TestTheAuditTrail:

    async def test_each_adoption_writes_one_audit_row_and_no_token_appears_in_it(self, env):
        """T10 — AC5 plus Invariant #12: `details` carries the subscription id
        and name, never the token."""
        _seed_agent("scout")
        _claude(env, "scout")
        sub = _make_subscription(env, "eugene-max")

        await env.svc.adopt_for_credentialless_agents(
            actor_user=env.admin, actor_ip="10.0.0.9",
            endpoint="/api/subscriptions", request_id="req-2572",
        )

        rows = _audit_rows("subscription_auto_adopt")
        assert len(rows) == 1
        assert rows[0]["target_id"] == "scout"
        assert rows[0]["actor_ip"] == "10.0.0.9"
        details = json.loads(rows[0]["details"])
        assert details["subscription_id"] == sub.id
        assert details["subscription_name"] == "eugene-max"
        assert details["reason"] == "no_usable_credential"
        # No token anywhere in the row.
        assert "sk-ant-oat01-" not in json.dumps(rows[0])

    async def test_the_sweep_writes_one_summary_row_with_counts(self, env):
        """T10b — without it, "adopted 0 of 40 because Docker was unreadable" is
        indistinguishable in the record from "nothing to do"."""
        _seed_agent("scout")
        _seed_agent("coder")
        _claude(env, "scout")
        env.labels["coder"] = "codex"
        _make_subscription(env, "eugene-max")

        await env.svc.adopt_for_credentialless_agents(actor_user=env.admin)

        rows = _audit_rows("subscription_auto_adopt_sweep")
        assert len(rows) == 1
        details = json.loads(rows[0]["details"])
        assert details["adopted"] == 1
        assert details["candidates"] == 2
        assert details["skipped"]["non_claude"] == 1
        assert details["agents"] == ["scout"]

    async def test_the_audit_row_records_which_trigger_fired(self, env):
        """T21 — `details.trigger` distinguishes the two hooks."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")

        await env.svc.adopt_for_credentialless_agents(
            trigger=env.svc.TRIGGER_INSTANCE_KEY_DELETED
        )

        rows = _audit_rows("subscription_auto_adopt")
        assert json.loads(rows[0]["details"])["trigger"] == "instance_key_deleted"
        summary = _audit_rows("subscription_auto_adopt_sweep")
        assert json.loads(summary[0]["details"])["trigger"] == "instance_key_deleted"

    async def test_a_manual_assign_and_clear_are_audited_too(self, env, monkeypatch):
        """AC5 says adoption is recorded "alongside manual assignment" — only
        literally true if the manual move is recorded as well (#2421 found this
        router carried no audit call at all). Takes two action names only."""
        import routers.subscriptions as rs

        _seed_agent("scout")
        _make_subscription(env, "eugene-max")
        monkeypatch.setattr(rs, "db", env.db)
        monkeypatch.setitem(rs.assert_agent_owner.__globals__, "db", env.db)
        monkeypatch.setattr(env.db, "can_user_share_agent", lambda *a, **k: True)

        _stub_container_edges(monkeypatch)

        await rs.assign_subscription_to_agent(
            agent_name="scout", subscription_name="eugene-max",
            request=_http_request("/api/subscriptions/agents/scout"),
            current_user=env.admin,
        )
        await rs.clear_agent_subscription(
            agent_name="scout",
            request=_http_request("/api/subscriptions/agents/scout"),
            current_user=env.admin,
        )

        assigned = _audit_rows("subscription_assign")
        cleared = _audit_rows("subscription_clear")
        assert [r["target_id"] for r in assigned] == ["scout"]
        assert [r["target_id"] for r in cleared] == ["scout"]
        assert json.loads(assigned[0]["details"])["subscription_name"] == "eugene-max"
        assert "sk-ant-oat01-" not in json.dumps(assigned + cleared)


def _stub_container_edges(monkeypatch) -> None:
    """Neutralize the container stop/start the manual assign/clear routes run."""
    docker_service = types.ModuleType("services.docker_service")
    docker_service.get_agent_container = lambda name: None
    docker_service.get_agent_status_from_container = (
        lambda c: types.SimpleNamespace(status="exited")
    )
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_service)


# ===========================================================================
# Trigger A1 — the registration hook, at the router boundary
# ===========================================================================

@pytest.fixture
def register_router(env, monkeypatch):
    """`routers.subscriptions` wired to the harness DB."""
    import routers.subscriptions as rs

    monkeypatch.setattr(rs, "db", env.db)
    monkeypatch.setattr(
        env.db, "get_user_by_username", lambda username: {"id": 1}
    )

    async def _no_fanout(sub_id):
        return {}

    monkeypatch.setattr(
        env.auto_switch, "reload_subscription_for_all_agents", _no_fanout
    )
    return rs


class TestTriggerA1Registration:

    async def test_registering_a_subscription_adopts_the_credentialless_fleet(
        self, env, register_router
    ):
        """AC1 end to end at the router boundary — and Phase A is AWAITED, so
        `GET /api/subscriptions` is already correct when the POST returns (the
        Settings panel refetches it on the very next line)."""
        from db_models import SubscriptionCredentialCreate

        _seed_agent("scout")
        _claude(env, "scout")

        result = await register_router.register_subscription(
            SubscriptionCredentialCreate(name="eugene-max", token="sk-ant-oat01-x"),
            http_request=_http_request(),
            current_user=env.admin,
        )

        listed = env.db.list_subscriptions_with_agents()
        by_name = {s.name: s for s in listed}
        assert by_name["eugene-max"].agents == ["scout"]
        assert by_name["eugene-max"].agent_count == 1
        assert result.name == "eugene-max"

    async def test_registration_still_succeeds_when_the_sweep_raises(
        self, env, register_router, monkeypatch, caplog
    ):
        """T9 — the failure this whole design exists to prevent: a slip in the
        sweep returning 500 with the credential already stored. The `except`
        body must not raise either (it logs `payload.name`, not `request.name`).
        """
        from db_models import SubscriptionCredentialCreate

        _seed_agent("scout")
        _claude(env, "scout")

        async def _boom(**kwargs):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(env.svc, "adopt_for_credentialless_agents", _boom)

        with caplog.at_level("ERROR"):
            result = await register_router.register_subscription(
                SubscriptionCredentialCreate(name="eugene-max", token="sk-ant-oat01-x"),
                http_request=_http_request(),
                current_user=env.admin,
            )

        assert result.name == "eugene-max"  # the credential IS stored
        messages = [r.getMessage() for r in caplog.records]
        assert any("[#2572]" in m and "eugene-max" in m for m in messages)
        assert not any("AttributeError" in m for m in messages)

    async def test_the_router_still_accepts_a_real_request_after_the_rename(
        self, env, register_router, monkeypatch
    ):
        """T9b — a real HTTP request through FastAPI, which is the only thing
        that catches a `payload` / `http_request` signature break (the body
        silently becoming a query parameter, or the `Request` becoming a body
        field)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        _seed_agent("scout")
        _claude(env, "scout")

        app = FastAPI()
        app.include_router(register_router.router)
        # Override the object the ROUTER holds — `dependencies` can be
        # re-imported between tests, and two module objects mean the override
        # silently misses (the #1089 module-identity gotcha).
        app.dependency_overrides[register_router.get_current_user] = lambda: env.admin

        with TestClient(app) as client:
            resp = client.post(
                "/api/subscriptions",
                json={"name": "eugene-max", "token": "sk-ant-oat01-x"},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "eugene-max"
        assert env.db.get_agent_subscription_id("scout") is not None


# ===========================================================================
# Trigger A2 — the instance API key is deleted
# ===========================================================================

@pytest.fixture
def settings_router(env, monkeypatch):
    import routers.settings as st

    monkeypatch.setattr(st, "db", env.db)
    return st


def _set_instance_key(value: str = "sk-ant-instance-key") -> None:
    from services.settings_service import set_secret_setting

    set_secret_setting("anthropic_api_key", value)


class TestTriggerA2KeyDeletion:

    async def test_deleting_the_instance_key_adopts_the_credentialless_fleet(
        self, env, register_router, settings_router
    ):
        """T16 — the ordering the human's answer exists for: the canonical
        migration is *register the subscription, THEN delete the key*. In that
        order Trigger A1 correctly adopts NOBODY (the key still resolves), and
        without this hook the deletion strands the fleet with no trigger left.
        """
        from db_models import SubscriptionCredentialCreate

        _seed_agent("scout")
        _claude(env, "scout")
        _set_instance_key()

        await register_router.register_subscription(
            SubscriptionCredentialCreate(name="eugene-max", token="sk-ant-oat01-x"),
            http_request=_http_request(),
            current_user=env.admin,
        )
        # Step 1: registration adopts nobody — the instance key still works.
        assert env.db.get_agent_subscription_id("scout") is None

        resp = await settings_router.delete_anthropic_key(
            request=_http_request("/api/settings/api-keys/anthropic"),
            current_user=env.admin,
        )

        assert resp["deleted"] is True
        assert resp["fallback_configured"] is False
        assert env.db.get_agent_subscription_id("scout") is not None
        status = await env.svc.get_agent_auth_mode("scout")
        assert status.auth_mode == "subscription"

    async def test_an_env_var_fallback_key_blocks_adoption_after_the_row_is_deleted(
        self, env, settings_router, monkeypatch
    ):
        """T17 — the single most likely silent regression on this change. The
        predicate resolves encrypted row → legacy row → `os.getenv`, so deleting
        the settings row does NOT make the instance keyless while the backend
        environment still carries one. A "cleanup" swapping condition 1 to
        `has_secret_setting()` (DB-only, presence-only) would adopt the whole
        fleet off a still-working key — the issue's cardinal sin."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-dotenv")
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        _set_instance_key()

        resp = await settings_router.delete_anthropic_key(
            request=_http_request("/api/settings/api-keys/anthropic"),
            current_user=env.admin,
        )

        assert resp["deleted"] is True
        assert resp["fallback_configured"] is True
        assert env.db.get_agent_subscription_id("scout") is None

    async def test_the_generic_delete_route_also_triggers_the_sweep(
        self, env, settings_router
    ):
        """T18 — the SECOND clear path. `db.delete_setting` carries no
        delete-side twin of ent#435's sink guard, so `DELETE /api/settings/
        anthropic_api_key_encrypted` clears the instance key without ever
        touching `clear_secret_setting`."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        _set_instance_key()

        resp = await settings_router.delete_setting(
            key="anthropic_api_key_encrypted",
            request=_http_request("/api/settings/anthropic_api_key_encrypted"),
            current_user=env.admin,
        )

        assert resp["deleted"] is True
        assert env.db.get_agent_subscription_id("scout") is not None

    async def test_an_unrelated_setting_deletion_triggers_no_sweep(
        self, env, settings_router
    ):
        """T18's sibling — the hook is keyed on the Anthropic aliases, so every
        other setting deletion stays exactly as inert as it was."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        env.db.set_setting("some_flag", "on")

        resp = await settings_router.delete_setting(
            key="some_flag",
            request=_http_request("/api/settings/some_flag"),
            current_user=env.admin,
        )

        assert resp["deleted"] is True
        assert env.db.get_agent_subscription_id("scout") is None
        assert env.docker_calls == []  # the sweep never even started

    async def test_a_partial_catch_all_deletion_leaves_the_key_resolvable_and_adopts_nobody(
        self, env, settings_router
    ):
        """T18b — `clear_secret_setting` removes BOTH rows; the catch-all
        removes only the exact key. With a legacy cleartext row surviving, the
        credential is still resolvable, so the hook fires and correctly adopts
        nobody. (The half-done revocation is the catch-all's pre-existing leak
        over secret settings, not something this sweep introduces or can fix.)
        """
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        _set_instance_key()
        # A surviving legacy cleartext row — the state a pre-ent#435 backup
        # restore leaves behind. Written by raw SQL on purpose: ent#435's sink
        # guard refuses it through `db.set_setting`, which is the point.
        _write_raw_setting("anthropic_api_key", "sk-ant-legacy-cleartext")

        resp = await settings_router.delete_setting(
            key="anthropic_api_key_encrypted",
            request=_http_request("/api/settings/anthropic_api_key_encrypted"),
            current_user=env.admin,
        )

        assert resp["deleted"] is True
        assert env.db.get_agent_subscription_id("scout") is None

    async def test_a_noop_key_deletion_does_not_sweep(self, env, settings_router):
        """T19 — gated on the route's existing `deleted` truthiness: nothing
        changed ⇒ nothing to sweep, and no audit row either."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        # No instance key was ever stored.

        resp = await settings_router.delete_anthropic_key(
            request=_http_request("/api/settings/api-keys/anthropic"),
            current_user=env.admin,
        )

        assert resp["deleted"] is False
        assert env.docker_calls == []
        assert env.db.get_agent_subscription_id("scout") is None
        assert _audit_rows("subscription_auto_adopt") == []

    @pytest.mark.parametrize("route", ["dedicated", "catch_all"])
    async def test_a_non_admin_cannot_reach_the_sweep(self, env, settings_router, route):
        """The sweep GRANTS a credential, so it must stay human-admin-only
        (Invariant #8's grant-vs-use line). It adds no gate of its own — it
        inherits each route's existing `assert_admin`, which since #2323 is an
        allowlist that rejects agent and connector principals in the gate
        itself. This asserts the inheritance actually holds on BOTH routes."""
        from fastapi import HTTPException

        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        _set_instance_key()

        non_admin = _Admin(username="bob", role="user")

        with pytest.raises(HTTPException) as exc:
            if route == "dedicated":
                await settings_router.delete_anthropic_key(
                    request=_http_request("/api/settings/api-keys/anthropic"),
                    current_user=non_admin,
                )
            else:
                await settings_router.delete_setting(
                    key="anthropic_api_key_encrypted",
                    request=_http_request("/api/settings/anthropic_api_key_encrypted"),
                    current_user=non_admin,
                )

        assert exc.value.status_code == 403
        assert env.docker_calls == []          # the sweep never started
        assert env.db.get_agent_subscription_id("scout") is None

    @pytest.mark.parametrize("route", ["dedicated", "catch_all"])
    async def test_deleting_the_key_still_succeeds_when_the_sweep_raises(
        self, env, settings_router, monkeypatch, route
    ):
        """T20 — the mirror of T9, on both A2 routes. The sweep must never fail
        the deletion the operator asked for."""
        _seed_agent("scout")
        _claude(env, "scout")
        _make_subscription(env, "eugene-max")
        _set_instance_key()

        async def _boom(**kwargs):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(env.svc, "adopt_for_credentialless_agents", _boom)

        if route == "dedicated":
            resp = await settings_router.delete_anthropic_key(
                request=_http_request("/api/settings/api-keys/anthropic"),
                current_user=env.admin,
            )
        else:
            resp = await settings_router.delete_setting(
                key="anthropic_api_key_encrypted",
                request=_http_request("/api/settings/anthropic_api_key_encrypted"),
                current_user=env.admin,
            )

        assert resp["deleted"] is True
        assert resp["success"] is True


# ===========================================================================
# Trigger B — already shipped by #74. Pinned, not re-implemented.
# ===========================================================================

class TestTriggerBAgentCreation:

    async def test_creation_on_a_keyless_instance_still_auto_assigns(self, env):
        """T13 — the "create" half of the operator's decision needs no new code:
        `crud._apply_subscription_env` gates ONLY on `is_claude_runtime` and has
        no instance-key check anywhere in it, so an agent created after a
        subscription exists adopts one even on a keyless instance.

        Pinned by behaviour so the decision survives a future "optimization"
        that adds an instance-key guard there.
        """
        from services.agent_service.crud import _apply_subscription_env

        sub = _make_subscription(env, "eugene-max")
        config = types.SimpleNamespace(name="fresh-agent", runtime=None)
        env_vars = {"ANTHROPIC_API_KEY": ""}

        assigned = _apply_subscription_env(config, env_vars)

        assert assigned == sub.id
        assert env_vars["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-eugene-max"
        assert "ANTHROPIC_API_KEY" not in env_vars

    async def test_a_non_claude_runtime_is_still_skipped_at_creation(self, env):
        """The other half of #1187 decision 7, at the create path."""
        from services.agent_service.crud import _apply_subscription_env

        _make_subscription(env, "eugene-max")
        config = types.SimpleNamespace(name="coder", runtime="codex")
        env_vars = {"ANTHROPIC_API_KEY": "sk-ant-x"}

        assigned = _apply_subscription_env(config, env_vars)

        assert assigned is None
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_vars
