"""#4053 — the platform never ends a must-act alert, whatever the call site.

A kind registered `clears="person"` (the poison-parked task, the portal inbox
collision, the gitignore change set, the Workspace problem report) is ended by
a person only. `platform_alerts.clear()` and `reconcile()` ended any kind they
were given; only a static scan of literal call sites kept a person kind out.
Both now refuse one at runtime: nothing is ended, 0 is returned, and the
refusal is logged at ERROR. Driven at the seam against the unit island's real
SQLite.
"""
import logging
import uuid

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db():
    from database import db
    return db


@pytest.fixture
def pa(monkeypatch):
    from services import platform_alerts, operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    return platform_alerts


@pytest.fixture
def agent():
    return f"agent-4053-{uuid.uuid4().hex[:6]}"


def _person_kinds():
    from services import platform_alerts as pa
    return sorted(k.name for k in pa.KINDS.values() if k.clears == pa.CLEARS_PERSON)


def test_the_built_in_person_kinds_are_covered():
    assert {"poison", "portal_inbox_collision", "gitignore_untracked",
            "workspace_problem_report"} <= set(_person_kinds())


def _file(pa, agent, kind):
    key = None if pa.KINDS[kind].event else "the-key"
    assert pa.observe(agent, kind, key, title=kind, question="q") == pa.OBSERVED_CREATED
    return key


def _refusals(caplog, verb, agent, kind):
    return [r for r in caplog.records if r.levelno == logging.ERROR
            and f"refusing to {verb} {agent}/{kind}" in r.getMessage()]


@pytest.mark.parametrize("kind", _person_kinds())
def test_clear_never_ends_a_person_kind(db, pa, agent, kind, caplog):
    key = _file(pa, agent, kind)
    [row] = db.list_operator_queue_items(agent_name=agent, status="pending")

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        assert pa.clear(agent, kind, key) == 0

    assert db.get_operator_queue_item(row["id"])["status"] == "pending"
    assert len(_refusals(caplog, "clear", agent, kind)) == 1


@pytest.mark.parametrize("kind", _person_kinds())
def test_reconcile_never_ends_a_person_kind(db, pa, agent, kind, caplog):
    _file(pa, agent, kind)
    [row] = db.list_operator_queue_items(agent_name=agent, status="pending")

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        assert pa.reconcile(agent, kind, live_keys=[]) == 0

    assert db.get_operator_queue_item(row["id"])["status"] == "pending"
    assert len(_refusals(caplog, "reconcile", agent, kind)) == 1


def test_a_kind_named_through_a_variable_is_refused_too(db, pa, agent, caplog):
    """The AST scan sees only a literal kind; the runtime guard sees every call."""
    kinds = [name for name in _person_kinds() if not pa.KINDS[name].event]
    for kind in kinds:
        _file(pa, agent, kind)

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        ended = sum(pa.clear(agent, kind, "the-key") for kind in kinds)

    assert ended == 0
    assert len(db.list_operator_queue_items(agent_name=agent, status="pending")) == len(kinds)


def test_a_hook_kind_is_still_cleared(db, pa, agent, caplog):
    """Control: the guard refuses person kinds only."""
    pa.observe(agent, "circuit_dormant", agent, title="t", question="q")

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        assert pa.clear(agent, "circuit_dormant", agent) == 1

    assert db.list_operator_queue_items(agent_name=agent, status="pending") == []
    assert not [r for r in caplog.records if "refusing to" in r.getMessage()]
