"""Pull claim order (#2842) and one turn per conversation (#2843).

* #2842 — a pull worker claims interactive rows before every autonomous row,
  oldest first within each group. Strict precedence, no anti-starvation rule.
  The push drain (no worker, no trigger set) keeps plain oldest-first.
* #2843 — a pull claim never starts a second turn of a conversation that is
  already running; it skips that row and takes the next one. The partial
  unique index ``idx_executions_one_running_turn`` stops two concurrent
  claimers of one conversation (Postgres test below).

Runs against the real db layer through db_harness (SQLite; Postgres too when
TEST_POSTGRES_URL is set).
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    "db.connection",
    "db.schedules",
    "db.agent_settings.resources",
    "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def ops(db_backend):
    for mod in ("db.connection", "db.schedules", "db.agent_settings.resources", "database"):
        sys.modules.pop(mod, None)
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


_T0 = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def _q(agent: str, trigger: str, minute: int, key: str | None = None) -> str:
    """Insert a queued row ``minute`` minutes after _T0. Returns its id."""
    eid = f"{trigger}-{minute}-{key or 'nokey'}"
    qa = (_T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, conversation_key) "
        "VALUES (:id, '__manual__', :a, 'queued', :qa, :qa, 'm', :t, :k)",
        id=eid, a=agent, qa=qa, t=trigger, k=key,
    )
    return eid


def _pull(ops, agent="alpha", worker="alpha#w0"):
    from services.pull_pilot import INTERACTIVE_TRIGGERS

    row = ops.claim_next_queued(
        agent, worker_id=worker, lease_seconds=900,
        interactive_triggers=INTERACTIVE_TRIGGERS,
    )
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# #2842 — ordering
# ---------------------------------------------------------------------------


def test_interactive_turn_is_claimed_before_the_autonomous_backlog(ops):
    for m in range(5):
        _q("alpha", "schedule", m)
    chat = _q("alpha", "chat", 30)

    assert _pull(ops) == chat
    assert _pull(ops) == "schedule-0-nokey"


def test_oldest_first_within_each_group(ops):
    _q("alpha", "webhook", 0)
    later = _q("alpha", "slack", 20)
    earlier = _q("alpha", "mcp", 10)

    assert [_pull(ops), _pull(ops), _pull(ops)] == [earlier, later, "webhook-0-nokey"]


def test_unlisted_trigger_is_not_interactive(ops):
    old = _q("alpha", "validation", 0)
    _q("alpha", "self_task", 1)

    assert _pull(ops) == old


def test_push_drain_keeps_plain_oldest_first(ops):
    """No worker, no trigger set: the backend drain path, unchanged."""
    first = _q("alpha", "schedule", 0)
    _q("alpha", "chat", 5)

    row = ops.claim_next_queued("alpha")
    assert row["id"] == first


def test_backlog_drain_passes_no_trigger_set():
    src = (Path(_BACKEND_STR) / "services" / "backlog_service.py").read_text()
    assert "db.claim_next_queued(agent_name)" in src


def test_claim_next_task_passes_the_interactive_set():
    from services import pull_coordination_service as pcs
    from services.pull_pilot import INTERACTIVE_TRIGGERS

    with patch.object(pcs, "db") as db, patch.object(pcs, "record_worker_poll"):
        db.get_execution_timeout.return_value = 900
        db.claim_next_queued.return_value = None
        assert pcs.claim_next_task("alpha", "alpha#w0") is None
    assert db.claim_next_queued.call_args.kwargs["interactive_triggers"] is INTERACTIVE_TRIGGERS
    assert db.claim_next_queued.call_args.kwargs["waiting_conversation_prefix"] == "session:"


def test_waiting_conversation_is_claimed_with_interactive_turns(ops):
    """#3127: an agent-to-agent /chat turn (trigger ``agent``, autonomous) has a
    caller blocked on it. Its ``session:`` key puts it ahead of batch work."""
    from services.pull_pilot import INTERACTIVE_TRIGGERS, WAITING_CONVERSATION_PREFIX

    _q("alpha", "schedule", 0)
    _q("alpha", "agent", 1)  # an async agent /task: no key, stays batch
    chat = _q("alpha", "agent", 30, key="session:chat:s1")

    row = ops.claim_next_queued(
        "alpha", worker_id="alpha#w0", lease_seconds=900,
        interactive_triggers=INTERACTIVE_TRIGGERS,
        waiting_conversation_prefix=WAITING_CONVERSATION_PREFIX,
    )
    assert row["id"] == chat
    assert _pull(ops) == "schedule-0-nokey"


def test_interactive_and_autonomous_sets_overlap_only_where_documented():
    """The two sets answer different questions, so an overlap must be argued for.

    ``INTERACTIVE_TRIGGERS`` = a caller is blocked on the reply (claim priority
    + claim budget). ``_AUTONOMOUS_TRIGGERS`` = no PERSON on this install is
    reading it (alert the operator instead of relying on them seeing the text).

    ``a2a`` is both, and the only one: an inbound A2A request is held open for
    the whole turn while its answer leaves over the wire
    (abilityai/trinity-enterprise#679 T6). The assertion is narrowed rather
    than deleted so a FOURTH set membership — or a second trigger added to both
    without the argument — still fails here.
    """
    from services.pull_pilot import INTERACTIVE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert INTERACTIVE_TRIGGERS & _AUTONOMOUS_TRIGGERS == {"a2a"}


def test_every_channel_adapter_trigger_is_interactive():
    """message_router dispatches with ``triggered_by=channel``."""
    import re

    from services.pull_pilot import INTERACTIVE_TRIGGERS

    adapters = Path(_BACKEND_STR) / "adapters"
    found = set()
    for f in adapters.glob("*_adapter.py"):
        m = re.search(r"def channel_type\(self\) -> str:\s+return \"(\w+)\"", f.read_text())
        if m:
            found.add(m.group(1))
    assert {"slack", "telegram", "whatsapp"} <= found
    assert found <= INTERACTIVE_TRIGGERS


# ---------------------------------------------------------------------------
# #2843 — one turn per conversation
# ---------------------------------------------------------------------------


def test_running_conversation_is_skipped_not_blocking(ops):
    t1 = _q("alpha", "session", 0, key="conv-A")
    t2 = _q("alpha", "session", 1, key="conv-A")
    other = _q("alpha", "schedule", 2)

    assert _pull(ops, worker="w1") == t1
    assert _pull(ops, worker="w2") == other  # t2 skipped, queue not blocked
    assert _pull(ops, worker="w3") is None   # only t2 left, its conversation runs

    _hrun("UPDATE schedule_executions SET status = 'success' WHERE id = :id", id=t1)
    assert _pull(ops, worker="w3") == t2


def test_other_conversations_and_keyless_rows_still_run_in_parallel(ops):
    a = _q("alpha", "chat", 0, key="conv-A")
    b = _q("alpha", "chat", 1, key="conv-B")
    n1 = _q("alpha", "chat", 2)
    n2 = _q("alpha", "chat", 3)

    assert [_pull(ops, worker=f"w{i}") for i in range(4)] == [a, b, n1, n2]


def test_lease_expiry_requeue_releases_the_conversation(ops):
    t1 = _q("alpha", "session", 0, key="conv-A")
    t2 = _q("alpha", "session", 1, key="conv-A")
    assert _pull(ops, worker="w1") == t1

    _hrun(
        "UPDATE schedule_executions SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = :id",
        id=t1,
    )
    assert ops.requeue_expired_lease(t1)
    # The conversation is free again. The requeue restamps t1's queued_at, so
    # t2 is now older and runs; t1 waits for it.
    assert _pull(ops, worker="w2") == t2
    assert _pull(ops, worker="w3") is None


def test_unique_index_rejects_a_second_running_turn(ops):
    from sqlalchemy.exc import IntegrityError

    _q("alpha", "session", 0, key="conv-A")
    _q("alpha", "session", 1, key="conv-A")
    with pytest.raises(IntegrityError):
        _hrun("UPDATE schedule_executions SET status = 'running' WHERE agent_name = 'alpha'")


def test_concurrent_claims_never_run_one_conversation_twice(ops, db_backend):
    """The race is real only on Postgres (SQLite serialises writers)."""
    if db_backend != "postgres":
        pytest.skip("Postgres-only race. Set TEST_POSTGRES_URL to run.")

    N = 8
    for it in range(5):
        _hrun("DELETE FROM schedule_executions WHERE agent_name = 'alpha'")
        # 4 conversations x 2 turns: at most 4 claims can succeed.
        for c in range(4):
            _q("alpha", "chat", 2 * c, key=f"it{it}-c{c}")
            _q("alpha", "chat", 2 * c + 1, key=f"it{it}-c{c}")

        barrier = threading.Barrier(N)
        results: list = [None] * N
        errors: list = [None] * N

        def _worker(i: int) -> None:
            try:
                barrier.wait(timeout=30)
                results[i] = _pull(ops, worker=f"w{i}")
            except Exception as e:  # noqa: BLE001
                errors[i] = e

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not any(errors), errors
        claimed = [r for r in results if r]
        assert len(claimed) == len(set(claimed)), f"double claim: {results}"
        running = _scalar(
            "SELECT COUNT(*) FROM schedule_executions WHERE agent_name = 'alpha' AND status = 'running'"
        )
        distinct = _scalar(
            "SELECT COUNT(DISTINCT conversation_key) FROM schedule_executions "
            "WHERE agent_name = 'alpha' AND status = 'running'"
        )
        assert running == distinct == len(claimed), f"one conversation ran twice: {results}"


def test_canary_collector_reports_running_conversations(ops):
    from canary.snapshot import _collect_executions

    q = _q("alpha", "session", 1, key="conv-A")
    _q("alpha", "session", 0, key="conv-A")
    assert _pull(ops) == "session-0-conv-A"

    out = _collect_executions("alpha")
    assert out["running_conversation_keys"] == {"conv-A"}
    assert out["queued_meta"][q]["conversation_key"] == "conv-A"
