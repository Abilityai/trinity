"""The payer→task binding, against the real SQL (abilityai/trinity-enterprise#679 I6).

`db/nevermined.py::payer_owns_execution` is the whole of T5: it decides whether
an anonymous x402 caller may `tasks/get` / `tasks/cancel` a task on the A2A
payment door. Every gate test stubs `db.nevermined_payer_owns_execution`, so the
`select … where lower(subscriber_address) == …` never ran in CI — the one new
query on the money path, and the one carrying a security property, was covered
only by its callers' stand-in.

This file runs the method itself against a throwaway SQLite carrying the real
`nevermined_payment_log` table, in the shape of the other `db/` tests. The
properties pinned are the ones an authorization predicate is judged on:

* it matches the payer's own row, and matches it across the facilitator's
  checksum casing (a verify and a settle are not guaranteed to agree on it);
* it is False for another payer, another agent, and an unknown execution — the
  three ways one payer could reach another's task;
* the payer string is a BOUND parameter, so a SQL-shaped wallet is just a wallet
  that matches nothing;
* missing arguments answer False rather than matching anything (fail closed).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

AGENT = "priced-agent"
EXEC = "exec-abc123"
PAYER = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"


@pytest.fixture()
def nevermined_db(tmp_path, monkeypatch):
    """A throwaway SQLite carrying the real payment-log table."""
    db_file = tmp_path / "trinity-ent679-i6.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata, nevermined_payment_log

    metadata.create_all(get_engine(), tables=[nevermined_payment_log])
    yield get_engine()


def _log_row(engine, *, agent, execution_id, payer, action="settle", row_id=None):
    from sqlalchemy import insert
    from db.tables import nevermined_payment_log

    with engine.begin() as conn:
        conn.execute(insert(nevermined_payment_log).values(
            id=row_id or f"{agent}:{execution_id}:{payer}:{action}",
            agent_name=agent,
            execution_id=execution_id,
            action=action,
            subscriber_address=payer,
            credits_amount=1,
            success=1,
            created_at="2026-10-03T00:00:00Z",
        ))


def _ops():
    from db.nevermined import NeverminedOperations
    return NeverminedOperations()


@pytest.mark.parametrize("agent,execution_id,payer,expected,why", [
    (AGENT, EXEC, PAYER, True, "the payer's own settled task"),
    (AGENT, EXEC, PAYER.lower(), True, "checksum casing is not stable across verify/settle"),
    (AGENT, EXEC, "0x" + "9" * 40, False, "another wallet must not reach this task"),
    ("other-agent", EXEC, PAYER, False, "the binding is scoped to one agent"),
    (AGENT, "exec-nope", PAYER, False, "an execution this payer never paid for"),
    (AGENT, EXEC, "' OR '1'='1", False, "the wallet is a bound parameter, not SQL"),
])
def test_payer_owns_execution_binding(nevermined_db, agent, execution_id, payer,
                                      expected, why):
    _log_row(nevermined_db, agent=AGENT, execution_id=EXEC, payer=PAYER)

    assert _ops().payer_owns_execution(agent, execution_id, payer) is expected, why


def test_missing_arguments_fail_closed(nevermined_db):
    """An empty wallet/agent/execution is not "match anything" — it is no."""
    _log_row(nevermined_db, agent=AGENT, execution_id=EXEC, payer=PAYER)
    ops = _ops()

    assert ops.payer_owns_execution("", EXEC, PAYER) is False
    assert ops.payer_owns_execution(AGENT, "", PAYER) is False
    assert ops.payer_owns_execution(AGENT, EXEC, "") is False


def test_a_settle_failed_row_also_binds_the_payer(nevermined_db):
    """A delivered-but-unsettled turn still owes the payer its task.

    `settle_failed` carries both columns, so the payer who was served but whose
    burn did not complete keeps access to the task — otherwise the one caller
    with a reason to poll is the one locked out.
    """
    _log_row(nevermined_db, agent=AGENT, execution_id=EXEC, payer=PAYER,
             action="settle_failed")

    assert _ops().payer_owns_execution(AGENT, EXEC, PAYER) is True


def test_a_verify_row_carrying_the_execution_binds_mid_turn(nevermined_db):
    """The I4 row, read by the predicate that consumes it.

    `payer_owns_execution` is action-agnostic by design: it asks "did this
    wallet pay for this execution", and the mid-turn `verify` row written once
    the execution id exists answers that while the turn is still running — which
    is the only window in which polling or cancelling a task is useful.
    """
    _log_row(nevermined_db, agent=AGENT, execution_id=EXEC, payer=PAYER,
             action="verify")

    assert _ops().payer_owns_execution(AGENT, EXEC, PAYER) is True
    # Still scoped: the row binds THIS payer to THIS agent's execution only.
    assert _ops().payer_owns_execution(AGENT, EXEC, "0x" + "9" * 40) is False
    assert _ops().payer_owns_execution("other-agent", EXEC, PAYER) is False
