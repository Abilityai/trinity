"""The ledger read selects every column the turn rating reads
(abilityai/trinity-enterprise#1148, decision 9).

`turn_tier_service.turn_actor` rates a turn from the execution row on two paths:
the row `db.get_execution` returns (the header path) and the rows
`db.get_running_turn_attribution` returns (the ledger path, which the agent's
own key calls meet on every request). A column the rule reads that the ledger
SELECT does not carry makes the two paths disagree: the header path rated a
delegated turn OWNER while the ledger still rated it SHARE and fenced the
callee's own calls. `LEDGER_COLUMNS` in the service names what the rule reads;
this guard holds the SELECT to it, and runs the real read on the unit island's
SQLite so a column that is named but not returned fails too.
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import uuid
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

from services import turn_tier_service as tts  # noqa: E402

pytestmark = pytest.mark.unit

_SERVICE = _BACKEND / "services" / "turn_tier_service.py"


def _fields_the_rule_reads() -> set:
    """Every `_field(<row>, "<name>")` literal in the service, for the execution
    row (not for a schedule row, which is a different table)."""
    reads = set()
    for node in ast.walk(ast.parse(_SERVICE.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_field"):
            continue
        if len(node.args) != 2 or not isinstance(node.args[1], ast.Constant):
            continue
        target = node.args[0]
        if isinstance(target, ast.Name) and target.id in ("execution", "row"):
            reads.add(node.args[1].value)
    return reads


def test_ledger_columns_cover_every_field_the_rule_reads():
    reads = _fields_the_rule_reads()
    assert reads, "the AST walk found no `_field(execution, ...)` reads: the guard's matched set is empty"
    assert "source_agent_name" in reads, "rule A reads the calling agent off the row"
    missing = reads - set(tts.LEDGER_COLUMNS)
    assert not missing, f"turn_actor reads {sorted(missing)} but LEDGER_COLUMNS does not name them"


def test_the_ledger_select_names_every_ledger_column():
    from db.schedules.executions import ScheduleExecutionsMixin
    source = inspect.getsource(ScheduleExecutionsMixin.get_running_turn_attribution)
    missing = [c for c in tts.LEDGER_COLUMNS if f"schedule_executions.c.{c}" not in source]
    assert not missing, f"get_running_turn_attribution does not select {missing}"


def test_the_real_ledger_read_returns_every_ledger_column():
    """The live consumer: the real read on the unit island's SQLite."""
    pytest.importorskip("sqlalchemy")
    from database import db as real
    from db.write_params import TaskExecutionFields

    agent = f"led-{uuid.uuid4().hex[:6]}"
    real.create_task_execution(agent_name=agent, message="hi", triggered_by="agent",
                               fields=TaskExecutionFields(source_user_id=9, source_agent_name="caller-x"))
    rows = real.get_running_turn_attribution(agent)
    assert len(rows) == 1
    missing = set(tts.LEDGER_COLUMNS) - set(rows[0])
    assert not missing, f"the ledger row lacks {sorted(missing)}"
    assert rows[0]["source_agent_name"] == "caller-x"
