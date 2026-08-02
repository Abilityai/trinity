"""Agent MCP key — detection, self-heal and deliberate rotation (#1854).

Nothing validated that an agent container authenticates with its OWN
``scope='agent'`` key. A user-scoped key pasted into the agent's ``.mcp.json``
is accepted silently and the agent operates with the *owner's* identity, so the
``agent_permissions`` matrix is bypassed. There was also no route to mint or
rotate an agent-scoped key, and no signal anywhere that said what a container is
actually configured with.

This suite pins the three pieces that ship together — **detect** (container
config-truth probe), **self-heal** (start-time drift predicate) and **rotate**
(owner-visible key + deliberate regeneration) — plus the ordering/concurrency
contract that keeps a half-landed rotation from 401-ing the heartbeat, the
result callback, the pull worker and the MCP client all at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

_LIFECYCLE_SRC = Path(_BACKEND) / "services" / "agent_service" / "lifecycle.py"
_SERVICE_SRC = Path(_BACKEND) / "services" / "agent_mcp_key_service.py"
_ROUTER_SRC = Path(_BACKEND) / "routers" / "agent_mcp_key.py"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def keys_db(tmp_path, monkeypatch):
    """Isolated SQLite carrying just the tables this feature reads/writes."""
    db_file = tmp_path / "trinity-1854.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m,
        agent_ownership,
        users,
        mcp_api_keys,
        schedule_executions,
    )
    m.create_all(
        get_engine(),
        tables=[agent_ownership, users, mcp_api_keys, schedule_executions],
    )

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="owner", email="owner@example.com", role="admin",
            created_at="t", updated_at="t",
        ))
    yield str(db_file)


def _mk_agent(name: str, **cols):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership
    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name=name, owner_id=1, created_at="t", **cols))


def _mk_key(key_id: str, agent: str | None, scope: str = "agent",
            active: int = 1, key_hash: str | None = None,
            last_used_at: str | None = None, created_at: str = "2026-01-01T00:00:00Z"):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import mcp_api_keys
    with get_engine().begin() as conn:
        conn.execute(insert(mcp_api_keys).values(
            id=key_id, name=key_id, key_prefix=f"trinity_mcp_{key_id}",
            key_hash=key_hash or f"h-{key_id}", created_at=created_at,
            last_used_at=last_used_at, usage_count=0,
            is_active=active, user_id=1, agent_name=agent, scope=scope))


def _key_ids() -> set:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT id FROM mcp_api_keys"))}


def _user(**kw):
    from models import User
    base = dict(id=1, username="owner", email="owner@example.com", role="admin")
    base.update(kw)
    return User(**base)


def _container(env: dict, status: str = "running"):
    return SimpleNamespace(
        status=status,
        attrs={"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}},
    )


async def _noop_log(**kwargs):
    return None


# --------------------------------------------------------------------------- #
# 14 — create_agent_mcp_api_key must set is_active explicitly
# --------------------------------------------------------------------------- #
def test_14_create_agent_key_sets_is_active_explicitly(keys_db):
    """`tables.py` declares a bare `Column("is_active", Integer)` with NO default,
    so a metadata-built DB yields NULL — which `validate_mcp_api_key` treats as
    revoked. On the rotation path that is a brick (new key inactive, old deleted)."""
    from database import db
    from sqlalchemy import text
    from db.engine import get_engine

    _mk_agent("scout")
    key = db.create_agent_mcp_api_key("scout", "owner")
    assert key is not None

    with get_engine().connect() as conn:
        active = conn.execute(
            text("SELECT is_active FROM mcp_api_keys WHERE id = :i"), {"i": key.id}
        ).scalar()
    assert active == 1, "a freshly minted agent key must be born ACTIVE"
    assert db.validate_mcp_api_key(key.api_key) is not None


# --------------------------------------------------------------------------- #
# 1 / 2 / 13 — captured-id deletion, scope='agent' only
# --------------------------------------------------------------------------- #
def test_01_regenerate_deletes_the_captured_superseded_ids(keys_db):
    from database import db

    _mk_agent("scout")
    _mk_key("k-old-1", "scout")
    _mk_key("k-old-2", "scout")
    captured = set(db.list_active_agent_key_ids("scout"))
    assert captured == {"k-old-1", "k-old-2"}

    new = db.create_agent_mcp_api_key("scout", "owner")
    removed = db.delete_superseded_agent_keys("scout", new.id, captured)

    assert removed == 2
    assert _key_ids() == {new.id}


def test_02_connector_key_survives_regeneration(keys_db):
    """`deactivate_agent_mcp_keys` / `set_agent_keys_active` span
    ('agent','connector') — reusing either would silently revoke the owner's
    MCP connector key."""
    from database import db

    _mk_agent("scout")
    _mk_key("k-agent", "scout", scope="agent")
    _mk_key("k-connector", "scout", scope="connector")

    captured = set(db.list_active_agent_key_ids("scout"))
    assert captured == {"k-agent"}, "capture must be scope='agent'-only"

    new = db.create_agent_mcp_api_key("scout", "owner")
    db.delete_superseded_agent_keys("scout", new.id, captured)

    assert "k-connector" in _key_ids()


def test_13_racing_concurrent_mint_is_not_collateral_damage(keys_db):
    """There is no per-agent start lock, so `recreate_missing_container` can mint
    K3 mid-flight. `id != new_id` deletion would remove the key that is actually
    in the container."""
    from database import db

    _mk_agent("scout")
    _mk_key("k-old", "scout")
    captured = set(db.list_active_agent_key_ids("scout"))

    new = db.create_agent_mcp_api_key("scout", "owner")       # our K2
    racer = db.create_agent_mcp_api_key("scout", "owner")     # concurrent K3

    db.delete_superseded_agent_keys("scout", new.id, captured)

    survivors = _key_ids()
    assert "k-old" not in survivors
    assert new.id in survivors
    assert racer.id in survivors, "a concurrent mint must never be collateral"


def test_13b_delete_superseded_never_removes_the_keep_id(keys_db):
    from database import db

    _mk_agent("scout")
    new = db.create_agent_mcp_api_key("scout", "owner")
    # A caller that mistakenly captured the new id must still not delete it.
    db.delete_superseded_agent_keys("scout", new.id, {new.id})
    assert new.id in _key_ids()


def test_13c_delete_superseded_is_scoped_to_the_agent(keys_db):
    from database import db

    _mk_agent("scout")
    _mk_agent("other")
    _mk_key("k-other", "other", scope="agent")
    new = db.create_agent_mcp_api_key("scout", "owner")

    db.delete_superseded_agent_keys("scout", new.id, {"k-other"})
    assert "k-other" in _key_ids(), "another agent's key must never be reachable"


# --------------------------------------------------------------------------- #
# 15 — DELETE, not deactivate: recover must not resurrect a rotated-out key
# --------------------------------------------------------------------------- #
def test_15_rotated_key_is_not_resurrected_by_agent_recovery(keys_db):
    """`recover_agent_ownership` reactivates EVERY inactive per-agent row with no
    notion of 'superseded by rotation' — so deactivating instead of deleting
    makes rotation non-durable."""
    from database import db

    _mk_agent("scout")
    old = db.create_agent_mcp_api_key("scout", "owner")
    old_plaintext = old.api_key
    captured = set(db.list_active_agent_key_ids("scout"))

    new = db.create_agent_mcp_api_key("scout", "owner")
    db.delete_superseded_agent_keys("scout", new.id, captured)

    db.delete_agent_ownership("scout")
    db.recover_agent_ownership("scout")

    assert db.validate_mcp_api_key(old_plaintext) is None, (
        "the rotated-out key came back alive after a soft-delete/recover cycle"
    )


# --------------------------------------------------------------------------- #
# 3 / 4 — spawned_by_key_id reconcile
# --------------------------------------------------------------------------- #
def test_03_spawn_key_id_reconciled_idempotently(keys_db):
    from database import db

    _mk_agent("parent")
    _mk_agent("child-a", spawned_by_agent="parent", spawned_by_key_id="k-old")
    _mk_agent("child-b", spawned_by_agent="parent", spawned_by_key_id="k-ancient")
    _mk_agent("cousin", spawned_by_agent="other-parent", spawned_by_key_id="k-other")
    _mk_agent("orphan")

    changed = db.reconcile_spawn_key_id("parent", "k-new")
    assert changed == 2

    assert db.get_agent_ephemeral_info("child-a")["spawned_by_key_id"] == "k-new"
    assert db.get_agent_ephemeral_info("child-b")["spawned_by_key_id"] == "k-new", (
        "a child stranded on an OLDER superseded id must also be repaired — "
        "an `= :old_id` form can never reach it"
    )
    assert db.get_agent_ephemeral_info("cousin")["spawned_by_key_id"] == "k-other"
    assert db.get_agent_ephemeral_info("orphan")["spawned_by_key_id"] is None

    assert db.reconcile_spawn_key_id("parent", "k-new") == 0, "must be idempotent"


def test_04_no_403_window_for_a_child_across_rotation(keys_db, monkeypatch):
    """`enforce_agent_spawn_scope` compares newest-active-key vs stored id. The
    reconcile must run BEFORE delivery, or the gate 403s for the whole recreate."""
    from fastapi import HTTPException
    from database import db
    from dependencies import enforce_agent_spawn_scope

    _mk_agent("parent")
    old = db.create_agent_mcp_api_key("parent", "owner")
    _mk_agent("child", spawned_by_agent="parent", spawned_by_key_id=old.id)

    parent_principal = _user(agent_name="parent")
    enforce_agent_spawn_scope(parent_principal, "child")  # baseline: allowed

    captured = set(db.list_active_agent_key_ids("parent"))
    new = db.create_agent_mcp_api_key("parent", "owner")

    # The instant the mint commits, get_agent_mcp_api_key returns the NEW key.
    db.reconcile_spawn_key_id("parent", new.id)
    enforce_agent_spawn_scope(parent_principal, "child")

    db.delete_superseded_agent_keys("parent", new.id, captured)
    enforce_agent_spawn_scope(parent_principal, "child")

    # And the gate itself is unchanged — a foreign parent is still denied.
    with pytest.raises(HTTPException) as exc:
        enforce_agent_spawn_scope(_user(agent_name="stranger"), "child")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# 17 — drift predicate
# --------------------------------------------------------------------------- #
def test_17_drift_predicate(keys_db):
    from database import db
    from services.agent_service.helpers import check_agent_mcp_key_matches

    _mk_agent("scout")
    key = db.create_agent_mcp_api_key("scout", "owner")
    url = "http://mcp-server:8080/mcp"

    good = _container({"TRINITY_MCP_API_KEY": key.api_key, "TRINITY_MCP_URL": url})
    assert check_agent_mcp_key_matches(good, "scout") is True

    assert check_agent_mcp_key_matches(_container({}), "scout") is False, "absent env"
    assert check_agent_mcp_key_matches(
        _container({"TRINITY_MCP_API_KEY": key.api_key}), "scout"
    ) is False, "TRINITY_MCP_URL absent — injection early-returns without BOTH"
    assert check_agent_mcp_key_matches(
        _container({"TRINITY_MCP_API_KEY": "trinity_mcp_bogus", "TRINITY_MCP_URL": url}),
        "scout",
    ) is False, "hash matches no active scope='agent' row"


def test_17b_drift_predicate_exempts_system_and_ephemeral(keys_db):
    from database import db
    from db.agents import SYSTEM_AGENT_NAME
    from services.agent_service.helpers import check_agent_mcp_key_matches

    _mk_agent(SYSTEM_AGENT_NAME, is_system=1)
    _mk_agent("ghost", is_ephemeral=1)

    empty = _container({})
    assert check_agent_mcp_key_matches(empty, SYSTEM_AGENT_NAME) is True, (
        "the system key is scope='system' — minting an agent-scoped replacement "
        "is an irreversible privilege downgrade"
    )
    assert check_agent_mcp_key_matches(empty, "ghost") is True, (
        "ghosts are volume-less; a recreate destroys the workspace mid-budget"
    )
    assert db.get_agent_ephemeral_info("ghost")["is_ephemeral"] is True


def test_17c_drift_predicate_is_fail_safe_on_db_error(keys_db, monkeypatch):
    """A transient DB error must not turn every start into a container recreate."""
    from database import db
    from services.agent_service import helpers

    _mk_agent("scout")
    monkeypatch.setattr(helpers.db, "get_agent_ephemeral_info",
                        lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    assert helpers.check_agent_mcp_key_matches(_container({}), "scout") is True


# --------------------------------------------------------------------------- #
# 7 — THE headline AC: both env vars land when the container had NEITHER
# --------------------------------------------------------------------------- #
def test_07_env_overrides_carry_both_key_and_url(keys_db, monkeypatch):
    """`trinity_mcp.inject_trinity_mcp_if_configured` early-returns unless BOTH
    TRINITY_MCP_URL and TRINITY_MCP_API_KEY are set — and a swallowed mint at
    creation drops both together. Baking only the key leaves the injection still
    short-circuiting: the headline AC would fail on exactly the incident
    population while the heartbeat starts working."""
    from database import db
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    overrides = svc.build_mcp_key_env_overrides("scout", description="test")

    assert set(overrides) >= {"TRINITY_MCP_API_KEY", "TRINITY_MCP_URL"}
    assert overrides["TRINITY_MCP_API_KEY"].startswith("trinity_mcp_")
    assert overrides["TRINITY_MCP_URL"]
    assert db.validate_mcp_api_key(overrides["TRINITY_MCP_API_KEY"]) is not None


def test_07b_recreate_bakes_both_vars_into_a_container_that_had_neither(monkeypatch):
    """End-to-end at the lifecycle seam: an OLD container carrying NEITHER var
    (the mint-failure population) must come back carrying BOTH."""
    from services.agent_service import lifecycle

    captured = {}

    async def _fake_provision(agent_name, **kw):
        captured.update(kw)
        return SimpleNamespace(name=f"agent-{agent_name}")

    old = SimpleNamespace(
        attrs={
            "Config": {"Env": ["AGENT_NAME=scout"], "Image": "trinity-agent-base:latest",
                       "Labels": {"trinity.ssh-port": "2222"}},
            "HostConfig": {"RestartPolicy": {}},
            "Mounts": [],
        },
        status="running",
    )
    assert not any(e.startswith("TRINITY_MCP_") for e in old.attrs["Config"]["Env"]), (
        "precondition: the old container has NEITHER var"
    )

    monkeypatch.setattr(lifecycle, "_provision_folders_and_run_agent_container", _fake_provision)
    monkeypatch.setattr(lifecycle, "validate_base_image", lambda image: None)
    monkeypatch.setattr(lifecycle, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(lifecycle, "get_agent_default_resources", lambda: {"cpu": "2", "memory": "4g"})

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(lifecycle, "container_stop", _noop)
    monkeypatch.setattr(lifecycle, "container_remove", _noop)

    async def _img(_i):
        return SimpleNamespace(labels={})
    monkeypatch.setattr(lifecycle, "image_get", _img)

    for name, val in (
        ("get_agent_subscription_id", None), ("get_resource_limits", None),
        ("get_guardrails_config", None), ("get_agent_github_pat", None),
        ("get_git_config", None), ("get_public_mount_path", "/home/developer/public"),
    ):
        monkeypatch.setattr(lifecycle.db, name, (lambda v: (lambda *a, **k: v))(val))

    asyncio.run(lifecycle.recreate_container_with_updated_config(
        "scout", old, "system",
        env_overrides={"TRINITY_MCP_API_KEY": "trinity_mcp_NEW",
                       "TRINITY_MCP_URL": "http://mcp-server:8080/mcp"},
    ))

    env = captured["env_vars"]
    assert env["TRINITY_MCP_API_KEY"] == "trinity_mcp_NEW"
    assert env["TRINITY_MCP_URL"] == "http://mcp-server:8080/mcp"


def test_07c_env_overrides_are_applied_last(monkeypatch):
    """~20 derived mutations sit between the env copy and the handoff (subscription
    juggling, PAT, guardrails, stall limit, auth token, pull-mode pop+update).
    An override applied at the copy point would be silently clobbered."""
    src = _LIFECYCLE_SRC.read_text()
    start = src.index("async def recreate_container_with_updated_config")
    end = src.index("async def _provision_folders_and_run_agent_container")
    body = src[start:end]

    override_at = body.rindex("env_overrides")
    handoff_at = body.index("return await _provision_folders_and_run_agent_container")
    copy_at = body.index('old_config.get("Env", [])')

    assert copy_at < override_at < handoff_at, (
        "env_overrides must be applied LAST — immediately before the handoff"
    )


# --------------------------------------------------------------------------- #
# 18 — verify probe verdicts (pure interpretation, no Docker)
# --------------------------------------------------------------------------- #
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def test_18_probe_verdicts(keys_db):
    from database import db
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    _mk_agent("sibling")
    mine = db.create_agent_mcp_api_key("scout", "owner")
    theirs = db.create_agent_mcp_api_key("sibling", "owner")
    _mk_key("k-user", None, scope="user", key_hash=_digest("trinity_mcp_userkey"))

    def _probe(entries, present=True):
        return {"schema": 1, "present": present, "entries": entries}

    ok = svc.interpret_probe_payload(
        _probe([{"name": "trinity", "is_trinity": True, "digest": _digest(mine.api_key)}]),
        "scout",
    )
    assert ok.verdict == "ok"

    foreign_user = svc.interpret_probe_payload(
        _probe([{"name": "trinity", "is_trinity": True, "digest": _digest("trinity_mcp_userkey")}]),
        "scout",
    )
    assert foreign_user.verdict == "foreign_user_key"
    assert "permissions matrix" in (foreign_user.message or "").lower()

    foreign_agent = svc.interpret_probe_payload(
        _probe([{"name": "trinity", "is_trinity": True, "digest": _digest(theirs.api_key)}]),
        "scout",
    )
    assert foreign_agent.verdict == "foreign_agent_key"

    unknown = svc.interpret_probe_payload(
        _probe([{"name": "trinity", "is_trinity": True, "digest": _digest("nope")}]),
        "scout",
    )
    assert unknown.verdict == "unknown_key"

    none_at_all = svc.interpret_probe_payload(_probe([]), "scout")
    assert none_at_all.verdict == "not_configured"

    shadow = svc.interpret_probe_payload(
        _probe([
            {"name": "trinity", "is_trinity": True, "digest": _digest(mine.api_key)},
            {"name": "trinity-platform", "is_trinity": True, "digest": _digest("trinity_mcp_userkey")},
        ]),
        "scout",
    )
    assert shadow.verdict == "shadow_entry", (
        "a second Trinity-pointing entry under a non-`trinity` name is never "
        "touched by re-injection and is not fixed by rotating the key"
    )


def test_18b_stopped_container_degrades_to_unavailable(keys_db, monkeypatch):
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    monkeypatch.setattr(svc, "get_agent_container",
                        lambda name: _container({}, status="exited"))
    out = asyncio.run(svc.verify_agent_mcp_key("scout"))
    assert out.verdict == "unavailable"


def test_18c_probe_returns_only_digests_never_tokens(keys_db, monkeypatch):
    """The digest is computed INSIDE the container; no secret crosses the
    boundary, and the `.mcp.json` body is never returned."""
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    secret = "trinity_mcp_SUPERSECRET_VALUE"
    payload = {
        "schema": 1, "present": True,
        "entries": [{"name": "trinity", "is_trinity": True, "digest": _digest(secret)}],
    }

    monkeypatch.setattr(svc, "get_agent_container", lambda name: _container({}))

    async def _exec(container_name, command, timeout=0):
        assert "sha256" in svc.PROBE_SCRIPT, "the digest must be computed in-container"
        return {"exit_code": 0, "output": json.dumps(payload)}

    monkeypatch.setattr(svc, "execute_command_in_container", _exec)
    out = asyncio.run(svc.verify_agent_mcp_key("scout"))

    blob = json.dumps(out.model_dump(), default=str)
    assert secret not in blob
    assert "Authorization" not in blob
    # The in-container script must never emit the token or the raw file body.
    assert "mcpServers" not in blob


# --------------------------------------------------------------------------- #
# 19 — health, including `stale`
# --------------------------------------------------------------------------- #
def test_19_health_stale_fires_when_key_predates_recent_executions(keys_db):
    """The incident's signature is verbatim 'the key sat unused for months' —
    non-NULL but old. A binary used/unused predicate renders that as green."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    _mk_key("k", "scout", last_used_at="2026-01-01T00:00:00Z")
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id="e1", schedule_id="s", agent_name="scout", status="success",
            started_at="2026-07-30T00:00:00Z", message="m", triggered_by="schedule"))

    status = svc.get_agent_mcp_key_status("scout")
    assert status.health == "stale"


def test_19b_health_states(keys_db):
    from services import agent_mcp_key_service as svc
    from utils.helpers import utc_now_iso

    _mk_agent("nokey")
    assert svc.get_agent_mcp_key_status("nokey").health == "missing"

    _mk_agent("fresh")
    _mk_key("k-fresh", "fresh", last_used_at=None)
    assert svc.get_agent_mcp_key_status("fresh").health == "never_used"

    _mk_agent("busy")
    _mk_key("k-busy", "busy", last_used_at=utc_now_iso())
    assert svc.get_agent_mcp_key_status("busy").health == "active"


def test_19c_system_agent_is_not_a_false_missing(keys_db):
    """`get_agent_mcp_api_key` filters scope=='agent'; the orchestrator's key is
    scope='system'. A permanent false warning on the platform orchestrator is the
    fastest way to train operators to ignore the signal."""
    from db.agents import SYSTEM_AGENT_NAME
    from services import agent_mcp_key_service as svc

    _mk_agent(SYSTEM_AGENT_NAME, is_system=1)
    _mk_key("k-sys", SYSTEM_AGENT_NAME, scope="system")

    status = svc.get_agent_mcp_key_status(SYSTEM_AGENT_NAME)
    assert status.health == "exempt"


# --------------------------------------------------------------------------- #
# 10 / 11 — refusals and the fail-CLOSED lock
# --------------------------------------------------------------------------- #
def _regen(agent_name, **kw):
    from services import agent_mcp_key_service as svc
    return asyncio.run(svc.regenerate_agent_mcp_key(agent_name, _user(), **kw))


def test_10_system_and_ghost_are_refused_409_before_any_mutation(keys_db, monkeypatch):
    from fastapi import HTTPException
    from db.agents import SYSTEM_AGENT_NAME
    from services import agent_mcp_key_service as svc

    _mk_agent(SYSTEM_AGENT_NAME, is_system=1)
    _mk_agent("ghost", is_ephemeral=1)

    def _boom(*a, **kw):
        raise AssertionError("refusal must precede ANY mutation")
    monkeypatch.setattr(svc.db, "create_agent_mcp_api_key", _boom)
    monkeypatch.setattr(svc, "_acquire_regen_lock", _boom)

    for name in (SYSTEM_AGENT_NAME, "ghost"):
        with pytest.raises(HTTPException) as exc:
            _regen(name)
        assert exc.value.status_code == 409, name


def test_11_redis_down_fails_closed_with_503(keys_db, monkeypatch):
    """#1644 doctrine: a guard that fails open manufactures confidence. Two
    interleaved rotations under a failed-open lock end at 'container holds K1,
    the only active row is K2' — permanently 401-ing four subsystems, with the
    surviving plaintext unrecoverable."""
    from fastapi import HTTPException
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    monkeypatch.setattr(svc, "_regen_lock_client", lambda: None)
    monkeypatch.setattr(svc.db, "create_agent_mcp_api_key",
                        lambda *a, **k: pytest.fail("must not mint without the lock"))

    with pytest.raises(HTTPException) as exc:
        _regen("scout")
    assert exc.value.status_code == 503


def test_11b_lock_contention_returns_409(keys_db, monkeypatch):
    from fastapi import HTTPException
    from services import agent_mcp_key_service as svc

    _mk_agent("scout")
    monkeypatch.setattr(svc, "_regen_lock_client",
                        lambda: SimpleNamespace(set=lambda *a, **k: False,
                                                get=lambda k: None,
                                                delete=lambda k: None))
    with pytest.raises(HTTPException) as exc:
        _regen("scout")
    assert exc.value.status_code == 409


# --------------------------------------------------------------------------- #
# 8 / 9 / 12 — delivery paths
# --------------------------------------------------------------------------- #
class _FakeLock:
    def __init__(self):
        self.store = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return False
        self.store[k] = v
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)


@pytest.fixture()
def regen_harness(keys_db, monkeypatch):
    from services import agent_mcp_key_service as svc

    monkeypatch.setattr(svc, "_regen_lock_client", lambda: _FakeLock())
    monkeypatch.setattr(svc.platform_audit_service, "log", _noop_log)
    monkeypatch.setattr(svc, "clear_agent_breakers", lambda name: None)
    return svc


def test_09_stopped_agent_stays_stopped_and_takes_the_db_only_path(regen_harness, monkeypatch):
    """Stopping an agent is the standard containment response to a suspected
    compromise, and rotating its key is what you do next. `containers_run`
    CREATES AND STARTS — a recreate would silently resume schedules and spend."""
    svc = regen_harness
    _mk_agent("quarantined")
    _mk_key("k-old", "quarantined")

    monkeypatch.setattr(svc, "get_agent_container",
                        lambda name: _container({}, status="exited"))

    async def _must_not_recreate(*a, **kw):
        raise AssertionError("a stopped agent must NOT be recreated")
    monkeypatch.setattr(svc, "_recreate_with_env", _must_not_recreate)

    out = _regen("quarantined")
    assert out.delivery == "db_only"
    assert "k-old" not in _key_ids()
    assert out.key_id in _key_ids()


def test_08_post_removal_failure_keeps_superseded_keys_and_names_the_state(
    regen_harness, monkeypatch
):
    """The old container is stopped and REMOVED before the replacement is
    created, so on failure the agent has no container at all. Claiming
    continuity would be a lie."""
    from fastapi import HTTPException

    svc = regen_harness
    _mk_agent("scout")
    _mk_key("k-old", "scout")

    monkeypatch.setattr(svc, "get_agent_container", lambda name: _container({}))

    async def _explode(*a, **kw):
        raise RuntimeError("docker said no")
    monkeypatch.setattr(svc, "_recreate_with_env", _explode)

    with pytest.raises(HTTPException) as exc:
        _regen("scout")
    assert exc.value.status_code == 500
    detail = str(exc.value.detail).lower()
    assert "start" in detail and ("down" in detail or "no container" in detail)
    assert "k-old" in _key_ids(), "superseded keys must survive a failed delivery"
    assert "docker said no" not in str(exc.value.detail), "no raw exception text"


def test_12_409_adoption_postcondition_blocks_deletion(regen_harness, monkeypatch):
    """`recreate_container_with_updated_config` adopts a container SOMEONE ELSE
    created on a 409 name conflict — with someone else's env."""
    from fastapi import HTTPException

    svc = regen_harness
    _mk_agent("scout")
    _mk_key("k-old", "scout")

    monkeypatch.setattr(svc, "get_agent_container", lambda name: _container({}))

    async def _adopt_a_stranger(agent_name, container, env_overrides):
        # The adopted container carries a DIFFERENT key.
        return _container({"TRINITY_MCP_API_KEY": "trinity_mcp_SOMEONEELSE",
                           "TRINITY_MCP_URL": "http://mcp-server:8080/mcp"})
    monkeypatch.setattr(svc, "_recreate_with_env", _adopt_a_stranger)

    with pytest.raises(HTTPException) as exc:
        _regen("scout")
    assert exc.value.status_code >= 500
    assert "k-old" in _key_ids(), "nothing may be deleted when the post-condition fails"


def test_running_agent_happy_path(regen_harness, monkeypatch):
    svc = regen_harness
    _mk_agent("scout")
    _mk_key("k-old", "scout")
    _mk_agent("child", spawned_by_agent="scout", spawned_by_key_id="k-old")
    monkeypatch.setattr(svc, "get_agent_container", lambda name: _container({}))

    baked = {}

    async def _recreate(agent_name, container, env_overrides):
        baked.update(env_overrides)
        return _container(dict(env_overrides))
    monkeypatch.setattr(svc, "_recreate_with_env", _recreate)

    out = _regen("scout")
    assert out.delivery == "recreated"
    assert out.children_repointed == 1
    assert "k-old" not in _key_ids()
    assert set(baked) >= {"TRINITY_MCP_API_KEY", "TRINITY_MCP_URL"}

    from database import db
    assert db.get_agent_ephemeral_info("child")["spawned_by_key_id"] == out.key_id


# --------------------------------------------------------------------------- #
# 16 — clear_agent_breakers before the recreate (source-order guard)
# --------------------------------------------------------------------------- #
def test_16_clear_agent_breakers_runs_before_the_recreate():
    """A 'repaired' agent that inherits a wedged predecessor's breaker verdict is
    fast-failed without ever being contacted — read as 'the rotation broke my
    agent'. `learnings.md` names this call site explicitly."""
    src = _SERVICE_SRC.read_text()
    body = src[src.index("async def _regenerate_locked"):]
    clear_at = body.index("clear_agent_breakers(")
    recreate_at = body.index("_recreate_with_env(")
    assert clear_at < recreate_at, (
        "the name-keyed heartbeat + both breakers must be cleared BEFORE the "
        "replacement comes up — `_recreate_with_env` starts it via "
        "containers_run(detach=True), so clearing afterwards leaves a window in "
        "which a concurrent dispatch reads the predecessor's verdict against a "
        "container that is already live (#1560)"
    )


# --------------------------------------------------------------------------- #
# 5 / 6 — auth allowlist and no-plaintext
# --------------------------------------------------------------------------- #
def test_05_reject_non_interactive_principal_is_an_allowlist():
    """A two-item denylist (agent + connector) leaves scope='system' walking
    through BOTH — `agent_name` and `connector_agent` are None for it — and
    `can_user_share_agent` is True fleet-wide on an admin-owned install. `scope`
    is free-text with no CHECK constraint, so only an allowlist is fail-closed."""
    from fastapi import HTTPException
    from dependencies import reject_non_interactive_principal

    reject_non_interactive_principal(_user())  # JWT: mcp_scope is None → pass

    for scope in ("user", "agent", "system", "connector", "portal_delegate", "scope_from_2027"):
        with pytest.raises(HTTPException) as exc:
            reject_non_interactive_principal(_user(mcp_scope=scope))
        assert exc.value.status_code == 403, scope


def test_05b_mcp_scope_is_populated_for_every_mcp_key_principal(monkeypatch):
    import dependencies as deps

    monkeypatch.setattr(deps.db, "validate_mcp_api_key", lambda token: {
        "key_id": "k1", "key_name": "n", "user_id": "owner",
        "user_email": "owner@example.com", "agent_name": None, "scope": "system",
    })
    monkeypatch.setattr(deps.db, "get_user_by_email", lambda e: {
        "id": 1, "username": "owner", "email": e, "role": "admin"})

    request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/agents/x/mcp-key"))
    user = asyncio.run(deps.get_current_user(request=request, token="trinity_mcp_x"))
    assert user.mcp_scope == "system"
    assert user.agent_name is None and user.connector_agent is None, (
        "precondition: the two existing denylist guards are both no-ops here"
    )


def test_05c_routes_use_owned_agent_by_name():
    src = _ROUTER_SRC.read_text()
    assert src.count("OwnedAgentByName") >= 3, "uniform-404 path auth on all three routes"
    assert "can_user_access_agent" not in src, "no inline 404-then-403 split (#186)"
    assert src.count("reject_non_interactive_principal") >= 3


def test_06_no_plaintext_is_returned_or_audited(regen_harness, monkeypatch, caplog):
    svc = regen_harness
    _mk_agent("scout")
    monkeypatch.setattr(svc, "get_agent_container", lambda name: _container({}))

    minted = {}

    async def _recreate(agent_name, container, env_overrides):
        minted["plaintext"] = env_overrides["TRINITY_MCP_API_KEY"]
        return _container(dict(env_overrides))
    monkeypatch.setattr(svc, "_recreate_with_env", _recreate)

    audited = []

    async def _log(**kwargs):
        audited.append(kwargs)
    monkeypatch.setattr(svc.platform_audit_service, "log", _log)

    with caplog.at_level("DEBUG"):
        out = _regen("scout")

    secret = minted["plaintext"]
    assert secret

    body = json.dumps(out.model_dump(), default=str)
    assert secret not in body
    assert "api_key" not in body and "plaintext" not in body

    audit_blob = json.dumps(audited, default=str)
    assert secret not in audit_blob
    assert hashlib.sha256(secret.encode()).hexdigest() not in audit_blob, (
        "audit_log is append-only with a 365-day no-delete trigger — the hash is "
        "a credential fingerprint and must not be permanent"
    )
    assert "TRINITY_MCP_API_KEY" not in audit_blob

    assert secret not in caplog.text


# --------------------------------------------------------------------------- #
# 20 — rate limit
# --------------------------------------------------------------------------- #
def test_20_regenerate_is_rate_limited(keys_db, monkeypatch):
    """For an admin, `can_user_share_agent` is True fleet-wide — an unthrottled
    loop is a scripted fleet-wide container-recreate storm."""
    from fastapi import HTTPException
    from services import rate_limiter
    from routers import agent_mcp_key as router_mod

    _mk_agent("scout")
    monkeypatch.setattr(
        rate_limiter, "check",
        lambda key, limit, window: rate_limiter.RateLimitResult(False, 0, 30, limit),
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(path="/api/agents/scout/mcp-key/regenerate"),
        state=SimpleNamespace(request_id="r1"),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router_mod.regenerate_agent_mcp_key_endpoint(
            agent_name="scout", request=request, current_user=_user()))
    assert exc.value.status_code == 429


# --------------------------------------------------------------------------- #
# FR-7 — adjacent principal guards (in blast radius)
# --------------------------------------------------------------------------- #
def test_fr7_key_revoke_delete_and_connector_mint_reject_agent_principals():
    """`db.revoke_mcp_api_key` skips the ownership check entirely for admins, so
    on a default admin-owned install ANY agent key could delete EVERY MCP key in
    the instance — a one-request fleet-wide auth wipe."""
    router_src = (Path(_BACKEND) / "routers" / "mcp_keys.py").read_text()
    connector_src = (Path(_BACKEND) / "routers" / "connector.py").read_text()

    for fn in ("revoke_mcp_api_key_endpoint", "delete_mcp_api_key_endpoint"):
        start = router_src.index(f"async def {fn}")
        body = router_src[start:start + 1400]
        assert "reject_agent_principal(current_user)" in body, fn
        assert "_reject_connector_principal(current_user)" in body, fn

    start = connector_src.index("async def regenerate_connector_key")
    body = connector_src[start:start + 1400]
    assert "reject_agent_principal(current_user)" in body
    assert "_reject_connector_principal(current_user)" in body
