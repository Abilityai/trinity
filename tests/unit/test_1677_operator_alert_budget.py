"""#1677 — budget for agent-influenceable platform operator-alert emitters.

#1632 capped the agent-authored FILE seam; platform direct-DB creates are
exempt by construction. One exempt emitter — ``task_execution_service.
_alert_skill_not_found`` (#1410) — is agent-influenceable: per-COMMAND dedup +
a timestamped id meant distinct unknown slash-commands each minted a new
``priority:"high"`` pending row + a notification, uncapped. These tests pin:

  - the ``create_bounded_alert`` four-outcome contract (created / at-cap /
    count-raise / create-raise), every failure arm fail-CLOSED with an
    ``[#1677 budget]`` ERROR marker and NO episode alert;
  - the ``_BUDGETED_ALERT_TYPES`` registry gate (unregistered type → refused);
  - the episode alert: once per cooldown window, deterministic bucketed
    ``alert-budget-{agent}-{type}-b{bucket}`` id, content hygiene (no ``held``,
    no agent-controlled command text), direct create (never re-enters the
    budget), swallowed WS/create failures;
  - ``_alert_skill_not_found`` ordering (dedup → budget → both creates), the
    notification suppressed with the queue item, the literal
    ``"skill_not_found"`` type, and command truncation on every echo surface;
  - the typed pending count through the REAL db layer and the REAL
    ``DatabaseManager`` facade (a missed ``item_type`` delegation + the
    helper's swallow silently voids the whole budget — learnings 2026-07-06);
  - the ``create_item`` execution_id COLUMN belt (non-str / >512 → None).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_service import (  # noqa: E402
    OPERATOR_ALERT_MAX_PENDING_PER_TYPE,
    create_bounded_alert,
)

pytestmark = pytest.mark.unit

_CAP = OPERATOR_ALERT_MAX_PENDING_PER_TYPE


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_budget_state():
    """The #1677 cooldown map is module-level state shared across tests."""
    oqs.reset_alert_budget_state()
    saved_ws = oqs._websocket_manager
    yield
    oqs.reset_alert_budget_state()
    oqs._websocket_manager = saved_ws


def _budget_db(monkeypatch, pending=0, count_side_effect=None, create_side_effect=None):
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.return_value = pending
    if count_side_effect is not None:
        db.count_operator_queue_pending_for_agent.side_effect = count_side_effect
    if create_side_effect is not None:
        db.create_operator_queue_item.side_effect = create_side_effect
    monkeypatch.setattr(oqs, "db", db)
    monkeypatch.setattr(oqs, "_websocket_manager", None)
    return db


def _skill_item(agent="a", command="/x", triggered_by="schedule"):
    return {
        "id": f"skill-not-found-{agent}-2026-01-01T00:00:00Z",
        "agent_name": agent,
        "type": "skill_not_found",
        "status": "pending",
        "priority": "high",
        "title": "Scheduled command references a missing skill",
        "question": f"command '{command}' did not resolve",
        "context": {"command": command, "triggered_by": triggered_by, "execution_id": ""},
        "created_at": "2026-01-01T00:00:00Z",
    }


def _budget_alerts(db):
    return [
        c.args[1]
        for c in db.create_operator_queue_item.call_args_list
        if str(c.args[1].get("id", "")).startswith("alert-budget-")
    ]


def _non_budget_creates(db):
    return [
        c.args[1]
        for c in db.create_operator_queue_item.call_args_list
        if not str(c.args[1].get("id", "")).startswith("alert-budget-")
    ]


# ---------------------------------------------------------------------------
# 1. create_bounded_alert — the four-outcome contract
# ---------------------------------------------------------------------------

class TestBudgetArms:
    def test_admits_under_cap_returns_true(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP - 1)
        ok = asyncio.run(create_bounded_alert("a", _skill_item()))
        assert ok is True
        assert len(_non_budget_creates(db)) == 1
        assert _budget_alerts(db) == []
        # The count is TYPED — item_type derived from item["type"], never a
        # separate parameter (S5/F2: two sources of one fact void the bound).
        db.count_operator_queue_pending_for_agent.assert_called_once_with(
            "a", item_type="skill_not_found"
        )

    def test_refuses_at_cap_with_one_episode_alert(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        ok = asyncio.run(create_bounded_alert("a", _skill_item()))
        assert ok is False
        assert _non_budget_creates(db) == []       # the item itself never created
        assert len(_budget_alerts(db)) == 1        # one episode alert

    def test_unregistered_type_fail_closed(self, monkeypatch, caplog):
        db = _budget_db(monkeypatch, pending=0)
        with caplog.at_level(logging.ERROR, logger="services.operator_queue_service"):
            ok = asyncio.run(create_bounded_alert("a", {"type": "made_up_type"}))
        assert ok is False
        db.create_operator_queue_item.assert_not_called()
        db.count_operator_queue_pending_for_agent.assert_not_called()
        assert any("[#1677 budget]" in r.message for r in caplog.records)

    def test_absent_type_fail_closed(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=0)
        assert asyncio.run(create_bounded_alert("a", {"id": "x"})) is False
        db.create_operator_queue_item.assert_not_called()

    def test_non_dict_item_fail_closed_never_raises(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=0)
        for bad in (None, "a string", ["list"], 42):
            assert asyncio.run(create_bounded_alert("a", bad)) is False
        db.create_operator_queue_item.assert_not_called()

    def test_count_raise_fail_closed_no_episode(self, monkeypatch, caplog):
        db = _budget_db(monkeypatch, count_side_effect=Exception("db down"))
        with caplog.at_level(logging.ERROR, logger="services.operator_queue_service"):
            ok = asyncio.run(create_bounded_alert("a", _skill_item()))
        assert ok is False
        # Fail-closed: NO create attempt AND NO episode alert — an unreadable
        # count is "DB broken", not "at cap" (Gemini MF2; the #1632 mirror).
        db.create_operator_queue_item.assert_not_called()
        assert any("[#1677 budget]" in r.message for r in caplog.records)

    def test_count_non_int_fail_closed(self, monkeypatch):
        # int(None) raises inside the guarded read — same fail-closed arm.
        db = _budget_db(monkeypatch, pending=None)
        assert asyncio.run(create_bounded_alert("a", _skill_item())) is False
        db.create_operator_queue_item.assert_not_called()

    def test_create_raise_returns_false_no_episode(self, monkeypatch, caplog):
        db = _budget_db(monkeypatch, pending=0, create_side_effect=Exception("boom"))
        with caplog.at_level(logging.ERROR, logger="services.operator_queue_service"):
            ok = asyncio.run(create_bounded_alert("a", _skill_item()))
        assert ok is False
        # One attempted create (the item), zero episode alerts.
        assert db.create_operator_queue_item.call_count == 1
        assert _budget_alerts(db) == []
        assert any("[#1677 budget]" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. The episode alert
# ---------------------------------------------------------------------------

class TestEpisodeAlert:
    def test_emitted_once_per_cooldown_window(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        asyncio.run(create_bounded_alert("a", _skill_item()))
        asyncio.run(create_bounded_alert("a", _skill_item(command="/y")))
        assert len(_budget_alerts(db)) == 1

    def test_first_episode_emits_when_monotonic_below_cooldown(self, monkeypatch):
        # The #1632 `None`-sentinel regression, re-pinned for the new map.
        db = _budget_db(monkeypatch, pending=_CAP)
        monkeypatch.setattr(oqs.time, "monotonic", lambda: 5.0)
        asyncio.run(create_bounded_alert("a", _skill_item()))
        assert len(_budget_alerts(db)) == 1

    def test_deterministic_bucketed_id_with_reserved_prefix(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        monkeypatch.setattr(oqs.time, "time", lambda: 1000.0)
        asyncio.run(create_bounded_alert("a", _skill_item()))
        (alert,) = _budget_alerts(db)
        bucket = int(1000.0 // oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS)
        assert alert["id"] == f"alert-budget-a-skill_not_found-b{bucket}"
        # The prefix is platform-reserved so an agent can't pre-create (and via
        # on_conflict_do_nothing suppress) the alert the bucketed id relies on.
        assert alert["id"].startswith("alert-budget-")
        assert "alert-budget-" in oqs._RESERVED_ID_PREFIXES

    def test_content_hygiene_no_held_no_command_text(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        secret_cmd = "/exfil-credentials-now-9000"
        asyncio.run(create_bounded_alert("a", _skill_item(command=secret_cmd)))
        (alert,) = _budget_alerts(db)
        assert alert["type"] == "alert"
        assert alert["priority"] == "high"
        # No untruthful `held` count (S6/F4) and NO agent-controlled text (G-04).
        assert "held" not in alert["context"]
        assert secret_cmd not in json.dumps(alert)
        assert alert["context"]["reason"] == "platform_alert_budget"
        assert alert["context"]["alert_type"] == "skill_not_found"
        assert alert["context"]["cap"] == _CAP

    def test_last_triggered_by_platform_enum_included(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        asyncio.run(create_bounded_alert("a", _skill_item(triggered_by="a2a")))
        (alert,) = _budget_alerts(db)
        assert alert["context"]["last_triggered_by"] == "a2a"

    @pytest.mark.parametrize("bad", ["Schedule!", "x" * 100, 123, None, "/cmd"])
    def test_last_triggered_by_shape_guard_excludes_non_enum(self, monkeypatch, bad):
        db = _budget_db(monkeypatch, pending=_CAP)
        item = _skill_item()
        item["context"]["triggered_by"] = bad
        asyncio.run(create_bounded_alert("a", item))
        (alert,) = _budget_alerts(db)
        assert "last_triggered_by" not in alert["context"]

    def test_episode_is_direct_create_never_reenters_budget(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        asyncio.run(create_bounded_alert("a", _skill_item()))
        # Exactly one typed count (for the refused item) — the episode alert's
        # own create consults no budget, so it cannot recurse into itself.
        assert db.count_operator_queue_pending_for_agent.call_count == 1
        assert len(_budget_alerts(db)) == 1

    def test_ws_broadcast_failure_swallowed(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        ws = MagicMock()
        ws.broadcast = AsyncMock(side_effect=Exception("ws down"))
        monkeypatch.setattr(oqs, "_websocket_manager", ws)
        asyncio.run(create_bounded_alert("a", _skill_item()))  # must not raise
        assert len(_budget_alerts(db)) == 1
        assert ws.broadcast.await_count == 1

    def test_episode_create_failure_swallowed_cooldown_prestamped(self, monkeypatch):
        # The cooldown is stamped BEFORE the create, so a persistently-failing
        # episode create backs off for the window instead of retrying on every
        # refusal (#1632 shape).
        db = _budget_db(monkeypatch, pending=_CAP, create_side_effect=Exception("down"))
        asyncio.run(create_bounded_alert("a", _skill_item()))
        asyncio.run(create_bounded_alert("a", _skill_item(command="/y")))
        assert db.create_operator_queue_item.call_count == 1  # one attempt, then cooldown

    def test_cooldown_is_per_agent_and_type(self, monkeypatch):
        db = _budget_db(monkeypatch, pending=_CAP)
        asyncio.run(create_bounded_alert("a", _skill_item(agent="a")))
        asyncio.run(create_bounded_alert("b", _skill_item(agent="b")))
        assert len(_budget_alerts(db)) == 2  # distinct agents, distinct episodes


# ---------------------------------------------------------------------------
# 3. _alert_skill_not_found integration (dedup → budget → both creates)
# ---------------------------------------------------------------------------

import services.task_execution_service as tes  # noqa: E402


def _wire_skill_alert(monkeypatch, pending=0, existing=None):
    """One MagicMock db shared by task_execution_service (dedup list +
    notification) and operator_queue_service (count + create)."""
    db = MagicMock()
    db.list_operator_queue_items.return_value = list(existing or [])
    db.count_operator_queue_pending_for_agent.return_value = pending
    monkeypatch.setattr(tes, "db", db)
    monkeypatch.setattr(oqs, "db", db)
    monkeypatch.setattr(oqs, "_websocket_manager", None)
    return db


class TestSkillNotFoundIntegration:
    def test_under_cap_creates_item_and_notification(self, monkeypatch):
        db = _wire_skill_alert(monkeypatch, pending=0)
        asyncio.run(tes._alert_skill_not_found("a", "/gen", "exec-1", "schedule"))
        (item,) = _non_budget_creates(db)
        assert item["id"].startswith("skill-not-found-a-")   # reserved prefix kept
        assert item["type"] == "skill_not_found"             # the registered literal (MF3/S5)
        assert item["context"]["command"] == "/gen"
        assert db.create_notification.call_count == 1

    def test_dedup_runs_before_budget(self, monkeypatch):
        # A repeat of an already-pending command returns BEFORE the budget:
        # no count, no create, no notification.
        existing = [{"context": {"command": "/gen"}}]
        db = _wire_skill_alert(monkeypatch, pending=0, existing=existing)
        asyncio.run(tes._alert_skill_not_found("a", "/gen", "exec-1", "schedule"))
        db.create_operator_queue_item.assert_not_called()
        db.create_notification.assert_not_called()
        db.count_operator_queue_pending_for_agent.assert_not_called()

    def test_at_cap_suppresses_item_and_notification(self, monkeypatch):
        db = _wire_skill_alert(monkeypatch, pending=_CAP)
        asyncio.run(tes._alert_skill_not_found("a", "/gen", "exec-1", "schedule"))
        assert _non_budget_creates(db) == []          # no skill-not-found item
        assert len(_budget_alerts(db)) == 1           # the episode alert instead
        db.create_notification.assert_not_called()    # notification dies WITH the item

    def test_repeat_at_cap_dedup_wins_no_episode_alert(self, monkeypatch):
        # F3 pin: a benign repeat of an already-pending command at cap must be
        # a silent no-op — never a false "compromised" episode alert.
        existing = [{"context": {"command": "/gen"}}]
        db = _wire_skill_alert(monkeypatch, pending=_CAP, existing=existing)
        asyncio.run(tes._alert_skill_not_found("a", "/gen", "exec-1", "schedule"))
        db.create_operator_queue_item.assert_not_called()
        assert _budget_alerts(db) == []
        db.create_notification.assert_not_called()

    def test_long_command_truncated_on_every_surface(self, monkeypatch):
        db = _wire_skill_alert(monkeypatch, pending=0)
        long_cmd = "/" + "x" * 500
        asyncio.run(tes._alert_skill_not_found("a", long_cmd, "exec-1", "schedule"))
        (item,) = _non_budget_creates(db)
        truncated = item["context"]["command"]
        assert len(truncated) == tes._COMMAND_DISPLAY_MAX
        assert truncated.endswith(oqs._TRUNC_MARKER)
        assert long_cmd not in item["question"]
        assert truncated in item["question"]
        # The notification echoes the same truncated form.
        notif = db.create_notification.call_args.args[1]
        assert notif.metadata["command"] == truncated
        assert long_cmd not in notif.message

    def test_dedup_matches_on_truncated_form(self, monkeypatch):
        # Two occurrences of the same >200-char command truncate identically,
        # so the per-command dedup still collapses them.
        long_cmd = "/" + "x" * 500
        from services.operator_queue_service import _truncate_with_marker
        truncated = _truncate_with_marker(long_cmd, tes._COMMAND_DISPLAY_MAX)
        existing = [{"context": {"command": truncated}}]
        db = _wire_skill_alert(monkeypatch, pending=0, existing=existing)
        asyncio.run(tes._alert_skill_not_found("a", long_cmd, "exec-1", "schedule"))
        db.create_operator_queue_item.assert_not_called()

    def test_never_raises_even_when_everything_breaks(self, monkeypatch):
        db = _wire_skill_alert(monkeypatch, pending=0)
        db.list_operator_queue_items.side_effect = Exception("db gone")
        asyncio.run(tes._alert_skill_not_found("a", "/gen", "e", "schedule"))  # no raise


# ---------------------------------------------------------------------------
# 4. Typed count through the REAL layers (db_harness) + the REAL facade
# ---------------------------------------------------------------------------

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401


def _insert(item_id: str, *, agent="a1", type_="skill_not_found", status="pending"):
    _hrun(
        "INSERT INTO operator_queue "
        "(id, agent_name, request_id, type, status, priority, title, question, created_at) "
        "VALUES (:id, :ag, :rid, :ty, :st, 'high', 't', 'q', '2026-01-01T00:00:00Z')",
        id=item_id, ag=agent, rid=item_id, ty=type_, st=status,
    )


def _real_ops():
    from db.operator_queue import OperatorQueueOperations
    return OperatorQueueOperations()


def _seed_typed_rows():
    _insert("s1")
    _insert("s2")
    _insert("s3")
    _insert("q1", type_="question")
    _insert("q2", type_="question")
    _insert("s-resp", status="responded")             # non-pending — never counted
    _insert("s-other", agent="a2")                    # other agent — never counted


class TestTypedCountRealLayers:
    def test_item_type_filters_where_clause(self, db_backend):
        _seed_typed_rows()
        ops = _real_ops()
        assert ops.count_pending_for_agent("a1") == 5                              # all types (existing #1632 read)
        assert ops.count_pending_for_agent("a1", item_type="skill_not_found") == 3
        assert ops.count_pending_for_agent("a1", item_type="question") == 2
        assert ops.count_pending_for_agent("a1", item_type="alert") == 0
        assert ops.count_pending_for_agent("a2", item_type="skill_not_found") == 1

    def test_real_facade_passes_item_type_through(self, db_backend):
        """Drive the REAL DatabaseManager facade — a missed item_type
        delegation (TypeError swallowed by the helper) voids the entire budget
        while monkeypatched tests stay green (learnings 2026-07-06). This test
        goes red both when the facade signature lacks the param AND when it
        accepts-but-drops it (the typed count would come back as 5, not 3)."""
        _seed_typed_rows()
        import database
        assert database.db.count_operator_queue_pending_for_agent("a1") == 5
        assert (
            database.db.count_operator_queue_pending_for_agent(
                "a1", item_type="skill_not_found"
            )
            == 3
        )


# ---------------------------------------------------------------------------
# 5. create_item execution_id COLUMN belt (real engine)
# ---------------------------------------------------------------------------

class TestExecutionIdColumnBelt:
    def _column(self, request_id):
        return _hscalar(
            "SELECT execution_id FROM operator_queue WHERE request_id = :r", r=request_id
        )

    def test_oversize_execution_id_column_nulled_context_untouched(self, db_backend):
        long_id = "E" * 600  # > _DB_BELT_ID_MAX (512), context still < 64 KiB
        _real_ops().create_item("a1", {
            "id": "belt-1", "type": "alert", "title": "t", "question": "q",
            "context": {"execution_id": long_id},
        })
        assert self._column("belt-1") is None
        ctx = json.loads(_hscalar(
            "SELECT context FROM operator_queue WHERE request_id = :r", r="belt-1"
        ))
        assert ctx["execution_id"] == long_id  # blob untouched — only the COLUMN belted

    def test_non_str_execution_id_column_nulled(self, db_backend):
        _real_ops().create_item("a1", {
            "id": "belt-2", "type": "alert", "title": "t", "question": "q",
            "context": {"execution_id": 12345},
        })
        assert self._column("belt-2") is None

    def test_valid_execution_id_kept(self, db_backend):
        _real_ops().create_item("a1", {
            "id": "belt-3", "type": "alert", "title": "t", "question": "q",
            "context": {"execution_id": "exec-1"},
        })
        assert self._column("belt-3") == "exec-1"

    def test_empty_string_execution_id_passes_unchanged(self, db_backend):
        # `""` is what existing callers send (`execution_id or ""`) — preserved.
        _real_ops().create_item("a1", {
            "id": "belt-4", "type": "alert", "title": "t", "question": "q",
            "context": {"execution_id": ""},
        })
        assert self._column("belt-4") == ""
