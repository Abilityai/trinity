"""Independent-referee seam on the validation path (ent#277).

VALIDATE-001 runs its auditor as the SAME agent — same model, same provider,
same container, workspace and tools reachable. Useful for "does the file really
exist", but it shares the failure mode it exists to catch: when a provider
retunes the model, the auditor drifts with the executor. This seam lets an
entitled module register a referee that judges the same execution with a
DIFFERENT model, called by the backend with only the transcript as input.

What OSS owns, and therefore what this pins:
  * with nothing registered, behaviour is byte-for-byte what it was
  * a registered referee's verdict replaces the same-agent pass
  * a referee that cannot answer (None) degrades to the same-agent pass
  * a referee that RAISES also degrades — an execution that already succeeded
    must never be failed because its validation was unavailable
  * a failing verdict still reaches the operator queue
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _vs():
    try:
        from services import validation_service
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return validation_service


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is module-global; leaking one across tests would make the
    'OSS-only build behaves exactly as before' assertion meaningless."""
    vs = _vs()
    original = vs.get_referee()
    vs._referee = None
    yield
    vs._referee = original


@pytest.fixture()
def svc(monkeypatch):
    """A ValidationService whose DB writes and operator notifications are
    captured rather than performed."""
    vs = _vs()
    calls = {"status": [], "notified": [], "same_agent_runs": 0}

    monkeypatch.setattr(
        vs.db, "update_business_status",
        lambda *a, **k: calls["status"].append(k or a) or True)

    service = vs.ValidationService(task_execution_service=SimpleNamespace())

    async def _never_called(**kwargs):
        calls["same_agent_runs"] += 1
        raise AssertionError("same-agent auditor must not run when a referee answered")

    async def _notify(**kwargs):
        calls["notified"].append(kwargs)

    monkeypatch.setattr(service, "_notify_operator_on_failure", _notify)
    return vs, service, calls


def _verdict(vs, status):
    return vs.ValidationResult(status=status, summary="referee says so", items=[])


def test_oss_only_build_registers_no_referee():
    """The default must be 'off': an unentitled install keeps VALIDATE-001."""
    assert _vs().get_referee() is None


@pytest.mark.asyncio
async def test_referee_verdict_replaces_the_same_agent_pass(svc, monkeypatch):
    vs, service, calls = svc

    async def referee(**kwargs):
        assert kwargs["agent_name"] == "atlas"
        # Transcript only — the referee is handed no workspace handle, no tools.
        assert set(kwargs) == {"agent_name", "original_message", "execution_response"}
        return _verdict(vs, vs.ValidationStatus.PASS)

    vs.register_referee(referee)
    monkeypatch.setattr(vs.db, "create_validation_execution",
                        lambda **k: pytest.fail("no execution row for a backend-side referee"))

    out = await service.validate_execution(
        execution_id="e1", agent_name="atlas", schedule_id="s1",
        original_message="do the thing", execution_response="did the thing")

    assert out.status is vs.ValidationStatus.PASS
    assert calls["same_agent_runs"] == 0


@pytest.mark.asyncio
async def test_a_failing_verdict_still_reaches_the_operator(svc):
    """AC: the result is surfaced, never silently consumed."""
    vs, service, calls = svc

    async def referee(**kwargs):
        return _verdict(vs, vs.ValidationStatus.FAIL)

    vs.register_referee(referee)
    out = await service.validate_execution(
        execution_id="e1", agent_name="atlas", schedule_id="s1",
        original_message="m", execution_response="r")

    assert out.status is vs.ValidationStatus.FAIL
    assert len(calls["notified"]) == 1
    assert calls["notified"][0]["execution_id"] == "e1"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["none", "raises"])
async def test_unavailable_referee_degrades_to_the_same_agent_pass(svc, monkeypatch, outcome):
    """AC: 'if the validation model is unavailable, execution proceeds and the
    missing validation is reported' — no silent failure, and above all no
    failing of an execution that already succeeded."""
    vs, service, _ = svc

    async def referee(**kwargs):
        if outcome == "raises":
            raise RuntimeError("provider unreachable")
        return None

    vs.register_referee(referee)

    # Prove we fell THROUGH to the original path rather than raising.
    reached = {}

    def _record_fallthrough(**kwargs):
        reached["yes"] = True
        return None  # the original path's own "couldn't create a row" branch

    monkeypatch.setattr(vs.db, "create_validation_execution", _record_fallthrough)

    out = await service.validate_execution(
        execution_id="e1", agent_name="atlas", schedule_id="s1",
        original_message="m", execution_response="r")

    assert reached.get("yes"), "must fall through to the same-agent auditor"
    # create_validation_execution returned None → the original path's own
    # error branch, which is an ERROR verdict, not an exception.
    assert out.status is vs.ValidationStatus.ERROR


# ---------------------------------------------------------------------------
# The blocker found while building this: VALIDATE-001 never worked
# ---------------------------------------------------------------------------

def test_validation_db_methods_are_reachable_on_the_facade():
    """`services/validation_service.py` calls these on the `db` FACADE, but
    only `ScheduleCleanupMixin` defined them and nothing re-exported them —
    so `validate_execution` raised AttributeError on its very first line.

    It failed invisibly: `POST /api/internal/.../validate` answers "accepted"
    and runs validation in a background task whose blanket `except Exception`
    reduces the crash to one log line. An operator who enabled
    `validation_enabled` got an accepted request, no verdict, no operator-queue
    item, and a `business_status` never even set to `pending_validation`.

    Same class as the #1539 facade-delegation bug, and unseeable by any test
    that mocks `db` wholesale — which is why this asserts on the real facade.
    """
    try:
        from database import db
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    for method in ("update_business_status", "create_validation_execution"):
        assert hasattr(db, method), (
            f"db.{method} is called by validation_service but missing from the "
            "facade — VALIDATE-001 crashes on the first line"
        )


def test_facade_forwards_validation_args_by_keyword():
    """Positional delegation is what broke #1539; assert the values land on the
    right names rather than merely that the method exists."""
    try:
        import database as database_module
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    captured = {}

    class _Ops:
        def update_business_status(self, **kw):
            captured.update(kw)
            return True

    facade = object.__new__(database_module.DatabaseManager)
    facade._schedule_ops = _Ops()
    facade.update_business_status("e1", "validated", "v1")

    assert captured == {
        "execution_id": "e1",
        "business_status": "validated",
        "validation_execution_id": "v1",
    }
