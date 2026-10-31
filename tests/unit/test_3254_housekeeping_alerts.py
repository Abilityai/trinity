"""
#3254 (S5) — the boot housekeeping alarms report through the platform alert
seam.

Two start-time sweeps raise operator alarms:

* the first-run seed (`system_seed_service`): kind `system_seed`, one subject
  per seed kind (`failed`, `error`, `override-unreadable`, `partial`,
  `cornelius-failed`) on `trinity-system`;
* the ent#615 token sweep (`git_service/token_scrub.py`): kinds
  `git_token_scrub_refused` and `git_token_scrub_unreadable`, one subject per
  agent.

Each is one pending row updated in place, and the place the code has POSITIVE
evidence the condition is over ends it as the platform. An inconclusive result
(a skipped or failed Cornelius pass, a sweep that could not see the tree) ends
nothing. Against a fresh database per test (db_harness), asserting the rows:
`observe` never raises, so a failure would only log.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from db_harness import db_backend  # noqa: F401

import services.system_seed_service as sss
import services.system_service as system_service
from models import SystemDeployFailure, SystemDeployResponse

pytestmark = pytest.mark.unit

HOST = "trinity-system"
ADMIN = {"id": 1, "username": "admin", "email": "admin@example.com", "role": "admin"}
MANIFEST = """
name: testsys
agents:
  one:
    template: local:one
  two:
    template: local:two
"""
SEED_KINDS = ("failed", "error", "override-unreadable", "partial", "cornelius-failed")
NET = timedelta(days=30)  # `system_seed` is LIFETIME_NET
DEFAULT = timedelta(days=14)  # both scrub kinds are LIFETIME_DEFAULT


# ---------------------------------------------------------------------------
# Fixtures and row helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db(db_backend, monkeypatch):  # noqa: F811
    from database import db as database
    from services import operator_resume_service

    # The seam's broadcast and the answer sink's audit hop to a loop; the rows
    # are what these tests assert.
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    monkeypatch.delenv("OPERATOR_PLATFORM_ALERT_LIFETIME_DAYS", raising=False)
    monkeypatch.delenv("OPERATOR_PLATFORM_ALERT_SNOOZE_DAYS", raising=False)
    return database


def _resp(status, created, failed=()):
    return SystemDeployResponse(
        status=status,
        system_name="testsys",
        agents_created=list(created),
        prompt_updated=False,
        warnings=[],
        failed=[
            SystemDeployFailure(
                name=n,
                short_name=n.split("-", 1)[1],
                template="local:x",
                reason="boom",
                status_code=500,
            )
            for n in failed
        ],
    )


@pytest.fixture
def seed(db, monkeypatch, tmp_path):
    """A fresh, set-up install: Docker up, admin present, Redis down (locks
    fail open), the real settings table, a deploy that fully succeeds."""
    # The seeder imports deploy_manifest at call time; own that module so a
    # sibling's stub cannot leak in (learnings 2026-07-12).
    monkeypatch.setitem(sys.modules, "services.system_service", system_service)
    # Bind the seeder to the SAME db the seam writes through (sibling suites
    # evict and re-import `database`, so the module-level name can be stale).
    monkeypatch.setattr(sss, "db", db)
    monkeypatch.setattr(db, "get_user_by_username", lambda username: dict(ADMIN))
    monkeypatch.setattr(db, "is_agent_name_reserved", lambda name: False)
    monkeypatch.setattr(sss, "docker_client", object())
    monkeypatch.setattr(sss, "get_breaker_redis", lambda: None)
    monkeypatch.delenv(sss.MANIFEST_ENV_VAR, raising=False)
    bundled = tmp_path / "default-system.yaml"
    bundled.write_text(MANIFEST, encoding="utf-8")
    monkeypatch.setattr(sss, "BUNDLED_MANIFEST_PATH", str(bundled))
    deploy = AsyncMock(return_value=_resp("deployed", ["testsys-one", "testsys-two"]))
    monkeypatch.setattr(system_service, "deploy_manifest", deploy)
    return types.SimpleNamespace(
        db=db, deploy=deploy, monkeypatch=monkeypatch, tmp=tmp_path
    )


def _seed_pass(fresh=True):
    return asyncio.run(sss.system_seed_service.ensure_seeded(fresh=fresh))


def _boot(seed, cornelius_action="none"):
    """One full first-run pass (both seeders), Cornelius stubbed to `action`."""

    async def cornelius(fresh=None):
        return {
            "agent_name": "cornelius",
            "action": cornelius_action,
            "status": "error" if cornelius_action == "create_failed" else "skipped",
            "message": "Failed to seed Cornelius: boom",
        }

    seed.monkeypatch.setattr(sss.cornelius_agent_service, "ensure_seeded", cornelius)
    seed.monkeypatch.setattr(sss, "_connect_seeded_agents", lambda *results: None)
    return asyncio.run(sss.ensure_first_run_seeded())


def _raise_seed_alarm(kind):
    sss.SystemSeedService._notify_operator(kind, f"t {kind}", f"q {kind}", {"k": kind})


def _rows(db, host, prefix):
    """Every row on `host` whose request id carries `prefix`, any status."""
    return [
        r
        for r in db.list_operator_queue_items(agent_name=host, limit=1000)
        if (r.get("request_id") or "").startswith(prefix)
    ]


def _subject_rows(db, host, subject):
    return [
        r
        for r in db.list_operator_queue_items(agent_name=host, limit=1000)
        if r.get("subject") == subject
    ]


def _pending(db, host, subject):
    return [r for r in _subject_rows(db, host, subject) if r["status"] == "pending"]


def _seed_row(db, kind):
    rows = _subject_rows(db, HOST, f"system_seed:{kind}")
    assert len(rows) == 1, rows
    return rows[0]


def _ended_by_platform(row):
    return (row["status"], row["disposed_by"], row["disposition_reason"]) == (
        "cancelled",
        "platform",
        "condition_cleared",
    )


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _answer(row):
    """A person acknowledges the alarm through the real answer path (#2372)."""
    from services import ask_service

    return ask_service.answer(
        row,
        response="acknowledged",
        response_text=None,
        actor=ask_service.Actor(email="op@example.com"),
    ).rows[0]


def _assert_two_readings_one_row(first, second, subject, lifetime):
    """The seam property (plan §14): the row carries the subject; the second
    reading bumps `seen_count`, moves `last_seen_at` and moves the deadline."""
    assert second["id"] == first["id"]
    assert second["subject"] == subject
    assert second["status"] == "pending"
    assert second["context"]["seen_count"] == 2
    assert _ts(second["last_seen_at"]) > _ts(first["last_seen_at"])
    assert _ts(second["expires_at"]) > _ts(first["expires_at"])
    gap = _ts(second["expires_at"]) - _ts(second["last_seen_at"])
    assert abs(gap - lifetime) < timedelta(minutes=1), gap


# ---------------------------------------------------------------------------
# The seed: one row per seed kind
# ---------------------------------------------------------------------------


def _drive(seed, kind):
    """Produce one reading of `kind` through the real emitter path."""
    if kind == "override-unreadable":
        seed.monkeypatch.setenv(sss.MANIFEST_ENV_VAR, str(seed.tmp / "missing.yaml"))
        _seed_pass()
    elif kind == "failed":
        seed.deploy.return_value = _resp(
            "failed", [], failed=["testsys-one", "testsys-two"]
        )
        _seed_pass()
    elif kind == "error":
        seed.deploy.side_effect = RuntimeError("docker exploded")
        _seed_pass()
    elif kind == "partial":
        seed.deploy.return_value = _resp(
            "partial", ["testsys-one"], failed=["testsys-two"]
        )
        # A partial deploy latches the seed flag; unset it so the next pass
        # reaches the emitter again.
        seed.db.set_setting(sss._SEEDED_FLAG, "false")
        _seed_pass()
    elif kind == "cornelius-failed":
        _boot(seed, "create_failed")
    else:  # pragma: no cover - guard
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", SEED_KINDS)
def test_each_seed_kind_is_one_row_updated_in_place(seed, db, kind):
    _drive(seed, kind)
    first = _rows(db, HOST, "system-seed-")
    assert len(first) == 1, first
    _drive(seed, kind)
    second = _rows(db, HOST, "system-seed-")
    # The deterministic legacy `system-seed-<kind>` id also kept ONE row, so
    # the assertion is the subject and the in-place update, not the count.
    assert len(second) == 1, second
    _assert_two_readings_one_row(first[0], second[0], f"system_seed:{kind}", NET)
    assert second[0]["request_id"].startswith(f"system-seed-{kind}-")
    assert second[0]["type"] == "alert" and second[0]["priority"] == "high"


def test_two_seed_kinds_are_two_subjects(seed, db):
    _drive(seed, "failed")
    _drive(seed, "override-unreadable")
    subjects = sorted(r["subject"] for r in _rows(db, HOST, "system-seed-"))
    assert subjects == ["system_seed:failed", "system_seed:override-unreadable"]


# ---------------------------------------------------------------------------
# The seed: what ends which subject
# ---------------------------------------------------------------------------


def _raise_all_seed_alarms(db):
    for kind in SEED_KINDS:
        _raise_seed_alarm(kind)
    return {kind: _seed_row(db, kind) for kind in SEED_KINDS}


def _state(db):
    """kind -> 'ended' (by the platform) | 'pending'."""
    out = {}
    for kind in SEED_KINDS:
        row = _seed_row(db, kind)
        if _ended_by_platform(row):
            out[kind] = "ended"
        else:
            assert row["status"] == "pending", row
            out[kind] = "pending"
    return out


def test_a_full_deploy_ends_every_fleet_alarm_but_not_cornelius(seed, db):
    _raise_all_seed_alarms(db)
    result = _seed_pass()
    assert result["status"] == "deployed"
    assert _state(db) == {
        "failed": "ended",
        "error": "ended",
        "override-unreadable": "ended",
        "partial": "ended",
        "cornelius-failed": "pending",
    }


def test_a_partial_deploy_ends_the_no_fleet_alarms_and_re_reads_partial(seed, db):
    _raise_all_seed_alarms(db)
    seed.deploy.return_value = _resp("partial", ["testsys-one"], failed=["testsys-two"])
    _seed_pass()
    assert _state(db) == {
        "failed": "ended",
        "error": "ended",
        "override-unreadable": "ended",
        "partial": "pending",
        "cornelius-failed": "pending",
    }
    assert _seed_row(db, "partial")["context"]["seen_count"] == 2


def test_the_full_fleet_converge_ends_every_fleet_alarm(seed, db):
    """The existence backstop's `already_exists` with every name reserved is the
    same conclusion as a full deploy (Codex C5)."""
    _raise_all_seed_alarms(db)
    seed.monkeypatch.setattr(db, "is_agent_name_reserved", lambda name: True)
    result = _seed_pass()
    assert result["action"] == "already_exists"
    seed.deploy.assert_not_awaited()
    assert _state(db) == {
        "failed": "ended",
        "error": "ended",
        "override-unreadable": "ended",
        "partial": "ended",
        "cornelius-failed": "pending",
    }


def test_a_partial_converge_keeps_partial(seed, db):
    _raise_all_seed_alarms(db)
    seed.monkeypatch.setattr(
        db, "is_agent_name_reserved", lambda name: name == "testsys-one"
    )
    result = _seed_pass()
    assert result["action"] == "already_exists"
    assert _state(db) == {
        "failed": "ended",
        "error": "ended",
        "override-unreadable": "ended",
        "partial": "pending",
        "cornelius-failed": "pending",
    }
    assert _seed_row(db, "partial")["context"]["seen_count"] == 2


def _docker_down(seed):
    seed.monkeypatch.setattr(sss, "docker_client", None)


def _already_seeded(seed):
    seed.db.set_setting(sss._SEEDED_FLAG, "true")


def _disabled(seed):
    seed.monkeypatch.setenv(sss.MANIFEST_ENV_VAR, "disabled")


def _pre_setup(seed):
    seed.monkeypatch.setattr(seed.db, "get_user_by_username", lambda username: None)


def _lock_held(seed):
    seed.monkeypatch.setattr(sss.SystemSeedService, "_acquire_lock", lambda self: None)


def _deploy_fails(seed):
    seed.deploy.return_value = _resp(
        "failed", [], failed=["testsys-one", "testsys-two"]
    )


def _deploy_raises(seed):
    seed.deploy.side_effect = RuntimeError("docker exploded")


@pytest.mark.parametrize(
    "arrange, fresh",
    [
        (_docker_down, True),
        (_already_seeded, True),
        (_disabled, True),
        (lambda seed: None, None),  # verdict undetermined (skipped_error)
        (lambda seed: None, False),  # established install (skipped_not_fresh)
        (_pre_setup, True),
        (_lock_held, True),
        (_deploy_fails, True),
        (_deploy_raises, True),
    ],
    ids=[
        "docker_unavailable",
        "already_seeded",
        "disabled",
        "verdict_undetermined",
        "not_fresh",
        "pre_setup",
        "lock_held",
        "deploy_failed",
        "deploy_raised",
    ],
)
def test_an_inconclusive_seed_pass_ends_nothing(seed, db, arrange, fresh):
    _raise_all_seed_alarms(db)
    arrange(seed)
    _seed_pass(fresh=fresh)
    assert set(_state(db).values()) == {"pending"}


# ---------------------------------------------------------------------------
# Cornelius: only a result that shows the agent exists ends its alarm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["created", "already_exists"])
def test_a_present_cornelius_ends_its_alarm_and_nothing_else(seed, db, action):
    _raise_all_seed_alarms(db)
    seed.monkeypatch.setattr(
        sss.system_seed_service,
        "ensure_seeded",
        AsyncMock(return_value={"action": "none"}),
    )
    _boot(seed, action)
    assert _state(db) == {
        "failed": "pending",
        "error": "pending",
        "override-unreadable": "pending",
        "partial": "pending",
        "cornelius-failed": "ended",
    }


@pytest.mark.parametrize(
    "action",
    [
        "docker_unavailable",
        "skipped_error",
        "skipped_not_fresh",
        "skipped_locked",
        "deferred",
        "none",
        None,
        "create_failed",
        "create_blocked",
    ],
)
def test_an_inconclusive_cornelius_result_keeps_its_alarm(seed, db, action):
    _raise_seed_alarm("cornelius-failed")
    seed.monkeypatch.setattr(
        sss.system_seed_service,
        "ensure_seeded",
        AsyncMock(return_value={"action": "none"}),
    )
    _boot(seed, action)
    row = _seed_row(db, "cornelius-failed")
    assert row["status"] == "pending", row


def test_a_raising_cornelius_seeder_keeps_its_alarm(seed, db):
    _raise_seed_alarm("cornelius-failed")

    async def boom(fresh=None):
        raise RuntimeError("cornelius exploded")

    seed.monkeypatch.setattr(sss.cornelius_agent_service, "ensure_seeded", boom)
    seed.monkeypatch.setattr(
        sss.system_seed_service,
        "ensure_seeded",
        AsyncMock(return_value={"action": "none"}),
    )
    seed.monkeypatch.setattr(sss, "_connect_seeded_agents", lambda *results: None)
    asyncio.run(sss.ensure_first_run_seeded())
    row = _seed_row(db, "cornelius-failed")
    assert row["status"] == "pending" and row["context"]["seen_count"] == 2


def test_a_missing_cornelius_result_keeps_its_alarm(seed, db):
    """No result at all (the seeder returned None, not a dict) says nothing."""
    _raise_seed_alarm("cornelius-failed")

    async def nothing(fresh=None):
        return None

    seed.monkeypatch.setattr(sss.cornelius_agent_service, "ensure_seeded", nothing)
    seed.monkeypatch.setattr(
        sss.system_seed_service,
        "ensure_seeded",
        AsyncMock(return_value={"action": "none"}),
    )
    seed.monkeypatch.setattr(sss, "_connect_seeded_agents", lambda *results: None)
    asyncio.run(sss.ensure_first_run_seeded())
    row = _seed_row(db, "cornelius-failed")
    assert row["status"] == "pending" and row["context"]["seen_count"] == 1


# ---------------------------------------------------------------------------
# A person's ending is never overwritten (AC 7 / #2372, through the answer path)
# ---------------------------------------------------------------------------


def test_a_person_ending_a_seed_alarm_stands_and_snoozes_it(seed, db):
    _drive(seed, "failed")
    out = _answer(_seed_row(db, "failed"))
    assert out["status"] == "acknowledged"
    # Inside the snooze window the next failing reading files nothing.
    _drive(seed, "failed")
    assert _pending(db, HOST, "system_seed:failed") == []
    # A successful seed's clear never overwrites the person's ending.
    seed.deploy.return_value = _resp("deployed", ["testsys-one", "testsys-two"])
    _seed_pass()
    rows = _subject_rows(db, HOST, "system_seed:failed")
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["disposed_by"]) == ("acknowledged", "person")


# ---------------------------------------------------------------------------
# Other shape: a fresh install
# ---------------------------------------------------------------------------


def test_fresh_install_first_boot_fails_and_the_next_boot_ends_it(seed, db):
    """Nothing in the queue; the first boot's seed fails → one row on
    `trinity-system`; the next boot's seed succeeds → the platform ends it."""
    assert db.list_operator_queue_items(agent_name=HOST, limit=1000) == []

    seed.deploy.return_value = _resp(
        "failed", [], failed=["testsys-one", "testsys-two"]
    )
    first = _boot(seed, "created")
    assert first["action"] == "create_failed"
    rows = db.list_operator_queue_items(agent_name=HOST, limit=1000)
    assert [(r["subject"], r["status"]) for r in rows] == [
        ("system_seed:failed", "pending")
    ]

    seed.deploy.return_value = _resp("deployed", ["testsys-one", "testsys-two"])
    second = _boot(seed, "none")
    assert second["action"] == "created"
    rows = db.list_operator_queue_items(agent_name=HOST, limit=1000)
    assert len(rows) == 1 and _ended_by_platform(rows[0]), rows
    assert rows[0]["subject"] == "system_seed:failed"


# ---------------------------------------------------------------------------
# The ent#615 token sweep
# ---------------------------------------------------------------------------


@pytest.fixture
def sweep(db, monkeypatch):
    """Run the real `scrub_git_remote_tokens` against a stubbed exec that
    returns the given report; yields (run, agent)."""
    from services.git_service import token_scrub

    agent = f"agent-3254ts-{uuid.uuid4().hex[:6]}"

    def run(
        *, refused=0, root_readable=1, root_traversable=1, git_present=1, exit_code=0
    ):
        async def fake_exec(
            container_name, command, timeout=60, *, environment=None, user="developer"
        ):
            if "base64 -d" in command and "sh -s" in command:
                return {
                    "exit_code": exit_code,
                    "output": (
                        f"TRINITY_SCRUB_REPORT remotes_scrubbed=0 harvested=0 seeded=0 "
                        f"refused={refused} gitmodules_hits=0 helper_ok=1 "
                        f"competing_helpers=0 root_readable={root_readable} "
                        f"root_traversable={root_traversable} git_present={git_present}"
                    ),
                }
            return {"exit_code": 0, "output": ""}  # the helper install

        monkeypatch.setattr(token_scrub, "execute_command_in_container", fake_exec)
        return asyncio.run(
            token_scrub.scrub_git_remote_tokens(agent, git_dir="/home/developer")
        )

    return run, agent


REFUSING = dict(refused=2)
UNREADABLE = dict(root_readable=0, root_traversable=0, git_present=0)


def _scrub(db, agent, kind):
    rows = _subject_rows(db, agent, f"{kind}:{agent}")
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.parametrize(
    "kind, reading",
    [
        ("git_token_scrub_refused", REFUSING),
        ("git_token_scrub_unreadable", UNREADABLE),
    ],
)
def test_each_scrub_alarm_is_one_row_per_agent(sweep, db, kind, reading):
    run, agent = sweep
    run(**reading)
    first = _rows(db, agent, "ent615-git-token-scrub-")
    assert len(first) == 1, first
    run(**reading)
    second = _rows(db, agent, "ent615-git-token-scrub-")
    assert len(second) == 1, second
    _assert_two_readings_one_row(first[0], second[0], f"{kind}:{agent}", DEFAULT)
    assert second[0]["priority"] == "medium"


def test_the_refusal_keeps_its_counts_only_context(sweep, db):
    run, agent = sweep
    run(**REFUSING)
    ctx = _scrub(db, agent, "git_token_scrub_refused")["context"]
    assert ctx["refused"] == 2 and ctx["success"] is True
    assert all(isinstance(v, (int, bool)) for k, v in ctx.items() if k != "seen_count")


def _raise_both(run):
    run(**REFUSING)
    run(**UNREADABLE)


def test_an_inspected_clean_sweep_ends_both_alarms(sweep, db):
    run, agent = sweep
    _raise_both(run)
    run()  # read ROOT's repo, refused nothing
    assert _ended_by_platform(_scrub(db, agent, "git_token_scrub_refused"))
    assert _ended_by_platform(_scrub(db, agent, "git_token_scrub_unreadable"))


@pytest.mark.parametrize(
    "reading",
    [
        dict(
            root_readable=0, root_traversable=0, git_present=0
        ),  # ROOT not traversable
        dict(
            root_readable=0, root_traversable=1, git_present=1
        ),  # repo there, unreadable
        dict(exit_code=1),  # the sweep failed
        # No repo at ROOT (#2930) is also what a fallback ROOT reads: a failed
        # toplevel probe sends the sweep to /home/developer while a legacy
        # repo under workspace/ still holds the token.
        dict(root_readable=0, root_traversable=1, git_present=0),
    ],
    ids=["not_traversable", "present_unreadable", "sweep_failed",
         "no_repository_or_fallback_root"],
)
def test_zero_refusals_without_inspection_evidence_end_neither(sweep, db, reading):
    run, agent = sweep
    _raise_both(run)
    report = run(**reading)
    assert report["refused"] == 0
    assert _scrub(db, agent, "git_token_scrub_refused")["status"] == "pending"
    assert _scrub(db, agent, "git_token_scrub_unreadable")["status"] == "pending"


def test_a_readable_refusing_sweep_ends_only_the_unreadable_alarm(sweep, db):
    run, agent = sweep
    _raise_both(run)
    run(**REFUSING)
    refused = _scrub(db, agent, "git_token_scrub_refused")
    assert refused["status"] == "pending" and refused["context"]["seen_count"] == 2
    assert _ended_by_platform(_scrub(db, agent, "git_token_scrub_unreadable"))


def test_a_person_ending_a_scrub_alarm_stands_and_snoozes_it(sweep, db):
    run, agent = sweep
    run(**REFUSING)
    out = _answer(_scrub(db, agent, "git_token_scrub_refused"))
    assert out["status"] == "acknowledged"
    run(**REFUSING)
    assert _pending(db, agent, f"git_token_scrub_refused:{agent}") == []
    run()  # a clean, inspected sweep: the clear finds no pending row
    row = _scrub(db, agent, "git_token_scrub_refused")
    assert (row["status"], row["disposed_by"]) == ("acknowledged", "person")


def test_a_sweep_that_never_raised_an_alarm_writes_nothing(sweep, db):
    run, agent = sweep
    run()
    assert db.list_operator_queue_items(agent_name=agent, limit=1000) == []


# ---------------------------------------------------------------------------
# R1: the key each emitter passes is the subject the upgrade sweep derives
# from that kind's pre-move rows (so the second sweep stamps the same subject)
# ---------------------------------------------------------------------------


@pytest.fixture
def readings(monkeypatch):
    """Record what the emitters hand the seam, without writing. Patched on the
    module object each emitter holds (R6): a sibling suite that re-imports
    `services.platform_alerts` would otherwise detach the patch."""
    from services.git_service import token_scrub

    seen = []

    def record(host, kind, key=None, **kw):
        seen.append((host, kind, key))

    for module in {id(m): m for m in (sss.platform_alerts, token_scrub.platform_alerts)}.values():
        monkeypatch.setattr(module, "observe", record)
    return seen


@pytest.mark.parametrize("seed_kind", SEED_KINDS)
def test_r1_system_seed_key_matches_the_legacy_parser(readings, seed_kind):
    from services import platform_alerts as pa

    _raise_seed_alarm(seed_kind)
    [(host, kind, key)] = readings
    # the pre-move emitter: `system-seed-<kind>` on trinity-system
    legacy = pa.derive_legacy_subject(f"system-seed-{seed_kind}", {"k": seed_kind})
    assert host == HOST
    assert (legacy.kind, legacy.subject) == (kind, pa.subject_for(kind, key))


def test_r1_scrub_refused_key_matches_the_legacy_parser(readings):
    from services import platform_alerts as pa
    from services.git_service import token_scrub

    report = {"remotes_scrubbed": 0, "refused": 1, "root_readable": 1, "success": True}
    token_scrub._alarm_git_token_scrub_refused("agent-a", report)
    [(host, kind, key)] = readings
    # the pre-move emitter: `ent615-git-token-scrub-<agent>-<UTC day>` on the agent
    legacy = pa.derive_legacy_subject(
        "ent615-git-token-scrub-agent-a-2026-09-01", report
    )
    assert host == "agent-a"
    assert (legacy.kind, legacy.subject) == (kind, pa.subject_for(kind, key))


# Agent names that collide with the nested `ent615-git-token-scrub-unreadable-`
# prefix: the legacy parser reads the row's context to tell the two kinds apart.
COLLIDING_AGENTS = ("unreadable-agent-a", "unreadable", "unreadable-unreadable-x")


@pytest.mark.parametrize("agent", COLLIDING_AGENTS)
def test_r1_scrub_refused_key_for_a_colliding_agent_name(readings, agent):
    from services import platform_alerts as pa
    from services.git_service import token_scrub

    report = {"remotes_scrubbed": 0, "refused": 1, "root_readable": 1, "success": True}
    token_scrub._alarm_git_token_scrub_refused(agent, report)
    [(host, kind, key)] = readings
    legacy = pa.derive_legacy_subject(f"ent615-git-token-scrub-{agent}-2026-09-01", report)
    assert host == agent
    assert (legacy.kind, legacy.subject) == (kind, pa.subject_for(kind, key))


@pytest.mark.parametrize("agent", COLLIDING_AGENTS)
def test_r1_scrub_unreadable_key_for_a_colliding_agent_name(readings, agent):
    from services import platform_alerts as pa
    from services.git_service import token_scrub

    token_scrub._alarm_git_token_scrub_unreadable(agent)
    [(host, kind, key)] = readings
    legacy = pa.derive_legacy_subject(
        f"ent615-git-token-scrub-unreadable-{agent}-2026-09-01", {"reason": "root_readable=0"}
    )
    assert host == agent
    assert (legacy.kind, legacy.subject) == (kind, pa.subject_for(kind, key))


def test_the_upgrade_sweep_ends_a_colliding_agents_legacy_refusal_duplicate():
    """The consequence the misread had: next to the seam's row, the pre-move
    refusal row of an `unreadable-…` agent formed its own group and survived."""
    from datetime import timezone

    from services import platform_alerts as pa

    agent = "unreadable-agent-a"
    report = {"remotes_scrubbed": 0, "refused": 1, "root_readable": 1, "success": True}
    rows = [
        {"id": "legacy", "agent_name": agent, "request_id": f"ent615-git-token-scrub-{agent}-2026-10-20",
         "status": "pending", "created_at": "2026-10-20T00:00:00.000000Z", "expires_at": None,
         "context": report, "subject": None, "raised_by": None},
        {"id": "seam", "agent_name": agent,
         "request_id": f"ent615-git-token-scrub-{agent}-2026-10-27T00:00:00.000000Z",
         "status": "pending", "created_at": "2026-10-27T00:00:00.000000Z",
         "expires_at": "2026-11-10T00:00:00.000000Z", "context": report,
         "subject": pa.subject_for("git_token_scrub_refused", agent), "raised_by": None},
    ]
    plan = pa.plan_sweep(rows, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert plan.ended_ids == ("legacy",)
    assert plan.survivor_stamps == () and plan.lifetime_stamps == ()


def test_r1_scrub_unreadable_key_matches_the_legacy_parser(readings):
    from services import platform_alerts as pa
    from services.git_service import token_scrub

    token_scrub._alarm_git_token_scrub_unreadable("agent-a")
    [(host, kind, key)] = readings
    # the pre-move emitter: `ent615-git-token-scrub-unreadable-<agent>-<UTC day>`
    legacy = pa.derive_legacy_subject(
        "ent615-git-token-scrub-unreadable-agent-a-2026-09-01",
        {"reason": "root_readable=0"},
    )
    assert host == "agent-a"
    assert (legacy.kind, legacy.subject) == (kind, pa.subject_for(kind, key))
