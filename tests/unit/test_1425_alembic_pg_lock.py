"""Alembic-on-PG migration serialisation (#1425).

Regression guard for the multi-worker deadlock observed on the PostgreSQL first
boot: two uvicorn workers both entered ``upgrade_to_head()`` → ``command.upgrade()``
concurrently and deadlocked inside a revision. The SQLite flock
(``db/migration_lock.py``, #1160) never reached this path; the fix takes a
PostgreSQL **session advisory lock** around the whole stamp+upgrade so only one
worker migrates at a time.

These are pure unit tests — no live database. They assert the *shape* the fix
guarantees (advisory lock acquired before the upgrade, released after, the
connection closed, and skipped for a non-PG backend) by faking the engine +
Alembic ``command``. The end-to-end "two concurrent boots don't deadlock" proof
lives in the skip-gated ``tests/integration/test_alembic_postgres.py``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from db import alembic_runner  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self, events):
        self._events = events

    def exec_driver_sql(self, sql, params=None):
        self._events.append(("sql", sql, params))

    def close(self):
        self._events.append(("close", None, None))


class _FakeEngine:
    def __init__(self, events, dialect_name):
        self._events = events
        self.dialect = types.SimpleNamespace(name=dialect_name)

    def connect(self):
        self._events.append(("connect", None, None))
        return _FakeConn(self._events)


def _install_fakes(monkeypatch, events, *, dialect_name, has_version, has_core):
    engine = _FakeEngine(events, dialect_name)
    monkeypatch.setattr(alembic_runner, "get_engine", lambda: engine)
    monkeypatch.setattr(
        alembic_runner,
        "inspect",
        lambda e: types.SimpleNamespace(
            has_table=lambda t: has_version if t == "alembic_version" else has_core
        ),
    )
    monkeypatch.setattr(alembic_runner, "_config", lambda: object())

    class _Command:
        @staticmethod
        def stamp(cfg, rev):
            events.append(("stamp", rev, None))

        @staticmethod
        def upgrade(cfg, rev):
            events.append(("upgrade", rev, None))

    monkeypatch.setattr(alembic_runner, "command", _Command)
    return engine


def _idx(events, predicate):
    return next(i for i, e in enumerate(events) if predicate(e))


def test_pg_upgrade_wraps_command_in_advisory_lock(monkeypatch):
    """On PostgreSQL, pg_advisory_lock is taken BEFORE command.upgrade and
    released AFTER — the serialisation the deadlock fix depends on (#1425)."""
    events: list = []
    _install_fakes(monkeypatch, events, dialect_name="postgresql", has_version=False, has_core=False)

    alembic_runner.upgrade_to_head()

    lock_i = _idx(events, lambda e: e[0] == "sql" and "pg_advisory_lock" in e[1])
    upgrade_i = _idx(events, lambda e: e[0] == "upgrade")
    unlock_i = _idx(events, lambda e: e[0] == "sql" and "pg_advisory_unlock" in e[1])
    close_i = _idx(events, lambda e: e[0] == "close")

    assert lock_i < upgrade_i < unlock_i < close_i
    # Both lock ops use the same fixed key.
    lock_ev = events[lock_i]
    unlock_ev = events[unlock_i]
    assert lock_ev[2] == (alembic_runner._ADVISORY_LOCK_KEY,)
    assert unlock_ev[2] == (alembic_runner._ADVISORY_LOCK_KEY,)


def test_pg_pre_alembic_stamp_runs_under_the_lock(monkeypatch):
    """The fresh-vs-pre-Alembic detection + stamp must also be inside the lock,
    else two workers could both stamp the baseline (secondary race)."""
    events: list = []
    _install_fakes(monkeypatch, events, dialect_name="postgresql", has_version=False, has_core=True)

    alembic_runner.upgrade_to_head()

    lock_i = _idx(events, lambda e: e[0] == "sql" and "pg_advisory_lock" in e[1])
    stamp_i = _idx(events, lambda e: e[0] == "stamp")
    upgrade_i = _idx(events, lambda e: e[0] == "upgrade")
    unlock_i = _idx(events, lambda e: e[0] == "sql" and "pg_advisory_unlock" in e[1])

    assert lock_i < stamp_i < upgrade_i < unlock_i
    assert events[stamp_i][1] == alembic_runner._BASELINE_REVISION


def test_non_postgres_backend_skips_advisory_lock(monkeypatch):
    """A non-PG server backend degrades to the prior unlocked behaviour — no
    PG-only SQL is issued — but still upgrades."""
    events: list = []
    _install_fakes(monkeypatch, events, dialect_name="mysql", has_version=True, has_core=True)

    alembic_runner.upgrade_to_head()

    assert not any(e[0] == "sql" for e in events)
    assert not any(e[0] == "connect" for e in events)
    assert any(e[0] == "upgrade" for e in events)


def test_advisory_lock_key_in_signed_bigint_range():
    """pg_advisory_lock takes a signed 64-bit key — the constant must fit."""
    assert -(2 ** 63) <= alembic_runner._ADVISORY_LOCK_KEY < 2 ** 63
