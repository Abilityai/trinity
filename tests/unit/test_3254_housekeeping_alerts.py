"""
#3254 (S5) — the first-run seed's alarms report through the platform alert
seam.

`system_seed_service` raises kind `system_seed`, one subject per seed kind
(`failed`, `error`, `override-unreadable`, `partial`, `cornelius-failed`) on
`trinity-system`. Each is one pending row updated in place, and the place the
code has POSITIVE evidence the condition is over ends it as the platform. An
inconclusive result (a skipped or failed Cornelius pass, a seed pass that
deployed nothing) ends nothing. Against a fresh database per test
(db_harness), asserting the rows: `observe` never raises, so a failure would
only log.
"""

from __future__ import annotations

import asyncio
import sys
import types
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
# R1: the key each emitter passes is the subject the upgrade sweep derives
# from that kind's pre-move rows (so the second sweep stamps the same subject)
# ---------------------------------------------------------------------------


@pytest.fixture
def readings(monkeypatch):
    """Record what the emitters hand the seam, without writing."""
    from services import platform_alerts as pa

    seen = []
    monkeypatch.setattr(
        pa, "observe", lambda host, kind, key=None, **kw: seen.append((host, kind, key))
    )
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

