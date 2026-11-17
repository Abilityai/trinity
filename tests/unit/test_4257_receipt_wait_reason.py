"""A queued receipt says how deep, since when, and waiting on what (#4257).

trinity#4257 — the async `/task` receipt said only "queued", which on a pull
pilot says nothing about load (#3625) and nothing about whether anything is
moving. These tests pin the three keys both receipts now carry, that the two
receipts agree, and that a failed diagnostic read degrades instead of breaking
a receipt that was already correct.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from services import chat_execution_service as ces  # noqa: E402
from services import queue_stall_service as qss  # noqa: E402

_QUEUED_AT = "2026-11-05T11:00:00Z"


def _fields(*, pilot=True, leased=0, max_parallel=3, ahead=2, pull=None,
            row=SimpleNamespace(queued_at=_QUEUED_AT)):
    db = MagicMock()
    db.get_execution.return_value = row
    db.count_queued_ahead.return_value = ahead
    db.count_active_leased_by_agent.return_value = {"alpha": leased}
    database = MagicMock()
    database.db = db
    with patch.dict(sys.modules, {"database": database}), \
            patch("services.pull_pilot.is_pull_pilot_agent", return_value=pilot), \
            patch("services.settings_service.get_effective_max_parallel_tasks",
                  return_value=max_parallel):
        return ces._queue_wait_fields("alpha", "e1", pull=pull), db


def test_an_idle_pilot_receipt_names_the_claim_wait_not_capacity():
    """The #4257 case: the caller must be able to tell this from a full pool."""
    fields, _ = _fields(pilot=True, leased=0, max_parallel=3)

    assert fields["wait_reason"] == qss.WAIT_WORKER_CLAIM
    assert fields["queued_at"] == _QUEUED_AT
    assert fields["queue_position"] == 2


def test_a_full_pilot_receipt_says_agent_busy():
    fields, _ = _fields(pilot=True, leased=3, max_parallel=3)

    assert fields["wait_reason"] == qss.WAIT_AGENT_BUSY


def test_a_push_overflow_receipt_says_capacity():
    """A push row only reaches the durable queue because the slot meter just
    refused it, so the receipt says capacity — not the `backlog_drain` the
    monitor will report for the same row a tick later."""
    fields, _ = _fields(pilot=False, leased=0, max_parallel=3)

    assert fields["wait_reason"] == qss.WAIT_CAPACITY


def test_the_head_of_the_queue_reports_position_zero():
    fields, _ = _fields(ahead=0)

    assert fields["queue_position"] == 0


def test_the_pull_flag_from_the_capacity_result_is_honoured():
    """The dispatch path already knows whether this was a pull queue; the
    receipt must not second-guess it with a fresh allowlist read."""
    fields, _ = _fields(pilot=False, pull=True, leased=0, max_parallel=3)

    assert fields["wait_reason"] == qss.WAIT_WORKER_CLAIM


def test_a_failed_read_degrades_instead_of_breaking_the_receipt():
    database = MagicMock()
    database.db.get_execution.side_effect = RuntimeError("db down")

    with patch.dict(sys.modules, {"database": database}):
        fields = ces._queue_wait_fields("alpha", "e1")

    assert fields == {"queued_at": None, "queue_position": None, "wait_reason": None}


def test_a_row_that_disappeared_still_yields_a_receipt():
    fields, db = _fields(row=None)

    assert fields["queued_at"] is None
    db.count_queued_ahead.assert_called_once_with("alpha", None, "e1")


# ---------------------------------------------------------------------------
# Both receipts, one shape
# ---------------------------------------------------------------------------


def _assigned_dict_keys(src: str, varname: str) -> set:
    """Keys of the dict literal assigned to `varname`, plus any `**` spread
    call names — read structurally, not by substring."""
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if varname not in targets or not isinstance(node.value, ast.Dict):
            continue
        keys = set()
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant):
                keys.add(key.value)
            elif key is None and isinstance(value, ast.Call):
                keys.add(getattr(value.func, "id", None)
                         or getattr(value.func, "attr", None))
        return keys
    return set()


def test_both_queued_receipts_spread_the_same_wait_fields():
    """A divergent pair would make the MCP client's answer depend on which
    door the caller used — the reason the sync receipt was built to mirror the
    async one in the first place."""
    src = (_BACKEND / "services" / "chat_execution_service.py").read_text()

    for var in ("_queued_payload", "receipt"):
        assert "_queue_wait_fields" in _assigned_dict_keys(src, var), (
            f"the {var} receipt no longer carries the #4257 wait fields"
        )
