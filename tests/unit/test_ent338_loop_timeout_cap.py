"""ent#338 — a loop's per-run timeout may not exceed the agent's ceiling.

`agent_ownership.execution_timeout_seconds` is the per-agent ceiling. Nothing
downstream re-applies it: `task_execution_service` reads the cap only when the
caller passed no `timeout_seconds`, so an explicit `timeout_per_run` went
straight to dispatch. A loop could run iterations LONGER than its owner's
ceiling — and `max_runs` goes to 100, so the overrun multiplies.

Mirrors #929's schedule guard (refuse, don't clamp): ent#458 shows these
guardrails on screen before Start, so a silent clamp would start a loop whose
bounds differ from the ones the user was shown.
"""
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


@pytest.fixture()
def guard(monkeypatch):
    from routers import loops as loops_router
    return loops_router


def _cap(guard, monkeypatch, value):
    monkeypatch.setattr(guard.db, "get_execution_timeout", lambda name: value)


def test_timeout_above_cap_is_refused(guard, monkeypatch):
    _cap(guard, monkeypatch, 600)
    with pytest.raises(HTTPException) as exc:
        guard._reject_timeout_above_cap("agent-a", 1200)
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert detail["error"] == "loop_timeout_exceeds_agent_cap"
    # The real bound rides the payload so a UI shows it instead of guessing.
    assert detail["agent_cap_seconds"] == 600
    assert detail["requested_seconds"] == 1200


def test_timeout_at_the_cap_is_allowed(guard, monkeypatch):
    """Boundary: equal is within the ceiling, matching #929's `>` comparison."""
    _cap(guard, monkeypatch, 600)
    guard._reject_timeout_above_cap("agent-a", 600)


def test_no_timeout_requested_is_untouched(guard, monkeypatch):
    """`None` means 'inherit the agent's own timeout' — never an error.

    Guarding on `is not None` explicitly: `None > int` raises TypeError in
    Python 3, the same trap #929's docstring calls out for schedules.
    """
    called = []
    monkeypatch.setattr(guard.db, "get_execution_timeout",
                        lambda name: called.append(name) or 600)
    guard._reject_timeout_above_cap("agent-a", None)
    assert called == [], "a None timeout must not even read the cap"


def test_cap_read_failure_fails_open(guard, monkeypatch):
    """A cap that cannot be read must not block a loop.

    Deliberately the opposite direction from a security gate: this is a
    resource ceiling, and the pre-ent#338 behaviour was no check at all, so
    degrading to that on a DB blip costs nothing that was not already the case.
    Failing closed here would take loops down whenever the settings read did.
    """
    def boom(name):
        raise RuntimeError("db down")
    monkeypatch.setattr(guard.db, "get_execution_timeout", boom)
    guard._reject_timeout_above_cap("agent-a", 999_999)


def test_guard_runs_before_the_deadline_check(guard):
    """Ordering is load-bearing, so it is pinned in source.

    #1156's deadline check compares `max_duration_seconds` against the
    effective per-run timeout. If an over-cap `timeout_per_run` reached it
    first, the user could be told to raise a deadline to match a per-run
    timeout they are not allowed to have.
    """
    import inspect
    src = inspect.getsource(guard.start_loop)
    assert src.index("_reject_timeout_above_cap") < src.index("max_duration_seconds"), (
        "the agent-cap guard must precede the #1156 deadline comparison"
    )
