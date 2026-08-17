"""#2015 — the audit hash chain must survive a restart, and be one chain.

Enabling it set `self._hash_chain_enabled` and wrote nothing. Nothing restored
it at boot either, so every backend restart silently switched the integrity
control back off — and restarts are routine (CLAUDE.md documents that users
re-login after one). An install could sit unhashed indefinitely with the
feature still showing as available, and #1985's `verified_partial` verdict
reports `valid: true` for a range that is mostly unverifiable.

The chain head had the same shape of defect one level down: `self._last_hash`
made the chain a property of one PROCESS. With more than one worker each kept
its own head and wrote `previous_hash` values pointing into a different
worker's sequence, so `verify_chain`'s link check reported an untampered log as
**tampered** — the loud false positive its own docstring warns against.

Both are now DB-backed: the flag in `system_settings`, the head read inside the
insert's transaction.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


@pytest.fixture
def mod():
    try:
        import services.platform_audit_service as m
    except ImportError:  # pragma: no cover — backend venv required
        pytest.skip("backend venv required")
    return m


@pytest.fixture
def store(mod, monkeypatch):
    """A stand-in `system_settings` shared by every service instance.

    Shared on purpose: it is what makes "a new process" testable — two
    instances over one store is exactly the relationship two workers, or the
    same worker before and after a restart, have with the database.
    """
    values: dict = {}

    monkeypatch.setattr(mod.db, "set_setting",
                        lambda k, v: values.__setitem__(k, v), raising=False)
    monkeypatch.setattr(mod.db, "get_setting_value",
                        lambda k, default=None: values.get(k, default),
                        raising=False)
    return values


# ---------------------------------------------------------------------------
# THE bug
# ---------------------------------------------------------------------------

class TestEnablementIsDurable:

    def test_enabling_survives_a_restart(self, mod, store):
        """A fresh instance IS the process after a restart."""
        mod.PlatformAuditService().enable_hash_chain(True)

        after_restart = mod.PlatformAuditService()

        assert after_restart.hash_chain_enabled is True

    def test_disabling_survives_a_restart_too(self, mod, store):
        mod.PlatformAuditService().enable_hash_chain(True)
        mod.PlatformAuditService().enable_hash_chain(False)
        assert mod.PlatformAuditService().hash_chain_enabled is False

    def test_default_is_off(self, mod, store):
        assert mod.PlatformAuditService().hash_chain_enabled is False

    def test_every_worker_sees_the_same_answer(self, mod, store):
        """Two instances over one store — the `--workers 2` relationship.

        Before this, worker B kept writing unhashed rows after worker A enabled
        the chain, producing a permanently interleaved partial log.
        """
        worker_a, worker_b = mod.PlatformAuditService(), mod.PlatformAuditService()

        worker_a.enable_hash_chain(True)

        assert worker_b.hash_chain_enabled is True

    def test_it_is_read_live_not_cached(self, mod, store):
        """A cache would let a worker keep hashing after an admin turned it
        off, which is the `_resolve_bool_flag` reasoning applied here."""
        svc = mod.PlatformAuditService()
        svc.enable_hash_chain(True)
        assert svc.hash_chain_enabled is True

        store[mod.PlatformAuditService.HASH_CHAIN_SETTING] = "false"

        assert svc.hash_chain_enabled is False

    @pytest.mark.parametrize("stored,expected", [
        ("true", True), ("True", True), ("1", True), ("yes", True),
        ("false", False), ("0", False), ("", False), ("nonsense", False),
        (None, False),
    ])
    def test_stored_value_parsing(self, mod, store, stored, expected):
        if stored is not None:
            store[mod.PlatformAuditService.HASH_CHAIN_SETTING] = stored
        assert mod.PlatformAuditService().hash_chain_enabled is expected

    def test_a_settings_read_failure_reads_as_OFF(self, mod, monkeypatch):
        """Fail-CLOSED, unlike the sibling feature flags.

        They fail open because an exception would zero every flag in the UI.
        This one decides whether an integrity record is written, and a
        settings-read failure is not a reason to claim one exists.
        """
        def boom(*a, **k):
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr(mod.db, "get_setting_value", boom, raising=False)
        assert mod.PlatformAuditService().hash_chain_enabled is False

    def test_the_process_no_longer_carries_the_answer(self, mod, store):
        """Pins the mechanism: an instance attribute would reintroduce exactly
        the divergence this fixes, and every behavioural test above would still
        pass for a single long-lived process."""
        svc = mod.PlatformAuditService()
        svc.enable_hash_chain(True)
        assert not hasattr(svc, "_hash_chain_enabled")
        assert not hasattr(svc, "_last_hash")


# ---------------------------------------------------------------------------
# The chain head
# ---------------------------------------------------------------------------

class TestTheChainHeadComesFromTheDatabase:

    def test_log_uses_the_chained_writer_when_enabled(self, mod, store, monkeypatch):
        calls = {"chained": 0, "plain": 0}
        monkeypatch.setattr(mod.db, "create_audit_entry_chained",
                            lambda e, f: calls.__setitem__("chained", calls["chained"] + 1),
                            raising=False)
        monkeypatch.setattr(mod.db, "create_audit_entry",
                            lambda e: calls.__setitem__("plain", calls["plain"] + 1),
                            raising=False)

        svc = mod.PlatformAuditService()
        svc.enable_hash_chain(True)
        asyncio.run(svc.log(
            event_type=mod.AuditEventType.CONFIGURATION, event_action="x",
            source="api", actor_agent_name="probe",
        ))

        assert (calls["chained"], calls["plain"]) == (1, 0)

    def test_log_uses_the_plain_writer_when_disabled(self, mod, store, monkeypatch):
        calls = {"chained": 0, "plain": 0}
        monkeypatch.setattr(mod.db, "create_audit_entry_chained",
                            lambda e, f: calls.__setitem__("chained", calls["chained"] + 1),
                            raising=False)
        monkeypatch.setattr(mod.db, "create_audit_entry",
                            lambda e: calls.__setitem__("plain", calls["plain"] + 1),
                            raising=False)

        asyncio.run(mod.PlatformAuditService().log(
            event_type=mod.AuditEventType.CONFIGURATION, event_action="x",
            source="api", actor_agent_name="probe",
        ))

        assert (calls["chained"], calls["plain"]) == (0, 1)

    def test_two_instances_produce_ONE_chain(self, mod, store, monkeypatch):
        """The multi-worker false-tamper case, end to end.

        Two services append alternately against one shared table. Previously
        each carried its own head and the resulting `previous_hash` links
        pointed across sequences; `verify_chain` then reported `tampered` on a
        log nobody had touched.
        """
        rows: list = []

        def chained(entry, compute_hash):
            head = next((r["entry_hash"] for r in reversed(rows)
                         if r.get("entry_hash")), None)
            entry["previous_hash"] = head
            entry["entry_hash"] = compute_hash(entry)
            entry["id"] = len(rows) + 1
            rows.append(dict(entry))

        monkeypatch.setattr(mod.db, "create_audit_entry_chained", chained,
                            raising=False)
        monkeypatch.setattr(mod.db, "get_audit_entries_range",
                            lambda s, e: rows, raising=False)

        a, b = mod.PlatformAuditService(), mod.PlatformAuditService()
        a.enable_hash_chain(True)
        for i, svc in enumerate([a, b, a, b, a]):
            asyncio.run(svc.log(
                event_type=mod.AuditEventType.CONFIGURATION,
                event_action=f"action-{i}", source="api",
                actor_agent_name="probe",
            ))

        result = asyncio.run(a.verify_chain(1, 100))
        assert result["valid"] is True, result
        assert result["checked"] == 5

    def test_the_head_is_read_inside_the_insert_transaction(self):
        """Pins the atomicity, which no behavioural test can observe.

        Reading the head before the transaction would pass every test above and
        reintroduce a race: two appends read the same head, and the second
        writes a `previous_hash` that no longer points at the row before it.

        Asserted structurally, not by substring: my first version checked that
        `ast.dump(with_node)` contained "entry_hash" and "insert", which stayed
        true when I hoisted the read out of the `with` — the strings were still
        somewhere in the block. It now walks the tree and requires BOTH the
        SELECT and the INSERT to be inside the transaction body, and no head
        assignment outside it.
        """
        import ast
        import inspect
        import textwrap

        from db.audit import PlatformAuditOperations

        src = textwrap.dedent(
            inspect.getsource(PlatformAuditOperations.create_audit_entry_chained)
        )
        fn = ast.parse(src).body[0]
        withs = [n for n in fn.body if isinstance(n, ast.With)]
        assert len(withs) == 1, "expected exactly one transaction block"
        block = withs[0]

        def calls_named(node, name):
            return [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and ((isinstance(n.func, ast.Name) and n.func.id == name)
                     or (isinstance(n.func, ast.Attribute) and n.func.attr == name))
            ]

        assert calls_named(block, "select"), (
            "the chain head is no longer SELECTed inside the transaction — a "
            "concurrent append can orphan the link (#2015)"
        )
        assert calls_named(block, "_insert_stmt") or calls_named(block, "insert"), (
            "the INSERT is no longer inside the transaction that read the head"
        )

        outside = [n for n in fn.body if not isinstance(n, ast.With)]
        for node in outside:
            assert not calls_named(node, "select"), (
                "the head is read OUTSIDE the insert transaction — that is the "
                "race this test exists to prevent (#2015)"
            )


# ---------------------------------------------------------------------------
# The consumer that read the old private attribute
# ---------------------------------------------------------------------------

def test_the_retention_warning_still_fires(mod, store, monkeypatch):
    """`audit_retention_service` gated on `_hash_chain_enabled` via
    `getattr(..., False)` — a default that would have degraded silently to
    "never warn" the moment the attribute moved. The new-producer /
    forgotten-consumer class, caught because the sweep was grepped for."""
    import services.audit_retention_service as ret

    monkeypatch.setattr(ret.db, "prune_audit_log", lambda days: 7, raising=False)
    monkeypatch.setattr(ret.platform_audit_service.__class__, "hash_chain_enabled",
                        property(lambda self: True), raising=False)

    out = asyncio.run(ret.AuditRetentionService().prune())
    assert out["removed"] == 7


# ---------------------------------------------------------------------------
# The append must be serialised, not merely wrapped in one transaction
# ---------------------------------------------------------------------------

class TestConcurrentAppendsDoNotForkTheChain:
    """One transaction around SELECT-then-INSERT is NOT enough on either backend.

    pysqlite defers the real ``BEGIN`` until it sees DML, so the head SELECT ran
    in autocommit and the transaction opened at the INSERT; PostgreSQL is READ
    COMMITTED and a bare ``SELECT … ORDER BY id DESC LIMIT 1`` takes no lock and
    cannot lock rows that do not exist yet. Both let two appenders read the same
    head and both insert — silently, with zero errors, which is how the first
    version of this fix passed review.

    These run against a real SQLite file, since the defect is in the driver's
    transaction timing and a mocked connection cannot express it.
    """

    @staticmethod
    def _hash(entry):
        import hashlib
        import json
        payload = json.dumps(
            {k: entry.get(k) for k in ("event_id", "previous_hash")}, sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _row(i):
        return {
            "event_id": f"e{i}", "event_type": "t", "event_action": "a",
            "actor_type": "user", "timestamp": "2026-08-10T00:00:00Z", "source": "api",
        }

    def _run(self, tmp_path, monkeypatch, threads=4, per=15):
        import threading

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chain.db'}")
        import db.engine as engine_mod
        from db.audit import PlatformAuditOperations
        from db.tables import audit_log, metadata

        if hasattr(engine_mod, "reset_engine"):
            engine_mod.reset_engine()
        engine = engine_mod.get_engine()
        metadata.create_all(engine, tables=[audit_log])
        ops = PlatformAuditOperations()

        # Pre-seed so this is not a cold-start artifact.
        for i in range(3):
            ops.create_audit_entry_chained(self._row(f"seed{i}"), self._hash)

        errors = []

        def worker(t):
            for i in range(per):
                try:
                    ops.create_audit_entry_chained(self._row(f"{t}-{i}"), self._hash)
                except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                    errors.append(repr(exc))

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        from sqlalchemy import select as sa_select
        with engine.connect() as conn:
            rows = conn.execute(
                sa_select(audit_log.c.previous_hash, audit_log.c.entry_hash)
                .order_by(audit_log.c.id)
            ).all()
        return rows, errors

    def test_every_link_points_at_its_predecessor(self, tmp_path, monkeypatch):
        rows, errors = self._run(tmp_path, monkeypatch)
        assert not errors, f"appends failed: {errors[:2]}"

        broken = [
            i for i, (prev, _) in enumerate(rows)
            if i and prev != rows[i - 1][1]
        ]
        assert not broken, (
            f"{len(broken)}/{len(rows)} rows link to something other than their "
            "predecessor — concurrent appends forked the chain, and verify_chain "
            "reports that untampered log as tampered (#2015)"
        )

    def test_no_two_rows_share_a_previous_hash(self, tmp_path, monkeypatch):
        """The fork itself, stated directly: a head may be consumed once."""
        rows, _ = self._run(tmp_path, monkeypatch)
        heads = [prev for prev, _ in rows]
        dupes = {h for h in heads if heads.count(h) > 1}
        assert not dupes, (
            f"{len(dupes)} chain head(s) consumed by more than one row — two "
            "appenders read the same head and both inserted (#2015)"
        )


def test_the_append_lock_is_taken_before_the_head_is_read():
    """Structural: the lock call is the FIRST statement in the transaction.

    Ordering is the whole property — a lock taken after the SELECT serialises
    nothing. Asserted over the AST because the docstring above it explains the
    locking at length, so a text scan matches the prose either way.
    """
    import ast
    import inspect
    import textwrap

    from db.audit import PlatformAuditOperations

    tree = ast.parse(
        textwrap.dedent(
            inspect.getsource(PlatformAuditOperations.create_audit_entry_chained)
        )
    )
    with_blocks = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    assert with_blocks, "the transaction block is gone"
    first = with_blocks[0].body[0]
    calls = [
        n for n in ast.walk(first)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_lock_chain_for_append"
    ]
    assert calls, (
        "the append lock is not the first statement inside the transaction — "
        "a lock taken after the head SELECT serialises nothing (#2015)"
    )
