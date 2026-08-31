"""ent#430 — an answer given in the Workspace resumes the agent (slice 5, the gate).

Until this, `answer_ask` wrote the answer and returned. The operator route called
`operator_resume_service.spawn_resume_dispatch`; the client route did not. So an
ask addressed to a Workspace client — the whole point of ent#364/#428/#429 — was
recorded, reached the agent's queue file in about three seconds, and then nothing
re-triggered the agent. Measured on a live instance before this change.

WHAT THIS DELIBERATELY DOES NOT ADD. ent#430's own body: "a second dispatch
surface for the same event is how the cost, trigger-label and loop-prevention
questions get answered twice, differently." So the opt-in gate, the idempotency
key, the audit row and the failure handling all stay inside
`maybe_dispatch_resume` — this path only reaches it. The tests below assert that
by asserting the CALL, not a re-implementation of what it does.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def asks():
    from client_portal.asks import service as m
    return m


ITEM = {
    "id": "q1",
    "agent_name": "agent-a",
    "type": "approval",
    "status": "pending",
    "options": ["Ship it", "Revise"],
    "addressed_to_email": "x@example.com",
}


@pytest.fixture()
def wired(monkeypatch, asks):
    """Drive `answer_ask` with everything below it stubbed, and record the call."""
    calls = []

    class _Db:
        def get_operator_queue_item(self, item_id):
            return dict(ITEM)

        def respond_to_operator_queue_item(self, **kw):
            return {**ITEM, "status": "responded", **kw}

        def get_operator_resume_enabled(self, agent_name):
            return True

    monkeypatch.setattr(asks, "db", _Db(), raising=False)
    monkeypatch.setattr(asks, "_on_roster", lambda *a, **k: True, raising=False)

    import services.operator_resume_service as ors
    monkeypatch.setattr(
        ors, "spawn_resume_dispatch",
        lambda item, **kw: calls.append((item, kw)), raising=False,
    )
    return calls


def test_an_answer_from_the_workspace_dispatches_the_resume(asks, wired):
    """AC #1 — the answer reaches the agent and it resumes."""
    asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)

    assert len(wired) == 1, "the client path did not reach ent#329's dispatch"
    item, kw = wired[0]
    assert item["agent_name"] == "agent-a"
    assert kw["response"] == "Ship it"
    assert kw["responded_by_email"] == "x@example.com", (
        "the resume must be attributed to the person who actually answered, not "
        "to the agent's owner — the audit row is the only record of who spent this"
    )


def test_the_dispatch_gets_the_UPDATED_row_not_the_one_read_first(asks, wired):
    """The row handed to the dispatch must carry the answer.

    `item` is the pre-answer read; `updated` is the CAS result. Passing the
    former looks identical in a green test and hands the resume an ask that
    still reads `pending`.
    """
    asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)
    item, _ = wired[0]
    assert item["status"] == "responded"


def test_a_lost_race_dispatches_nothing(asks, monkeypatch):
    """Hung off the CAS WIN only, exactly as the operator route is.

    Two people answering at once must not produce two resumes — and the loser's
    answer was never recorded, so resuming on it would act on nothing.
    """
    calls = []

    class _Db:
        def get_operator_queue_item(self, item_id):
            return dict(ITEM)

        def respond_to_operator_queue_item(self, **kw):
            return None            # someone else won

        def get_operator_resume_enabled(self, agent_name):
            return True

    monkeypatch.setattr(asks, "db", _Db(), raising=False)
    monkeypatch.setattr(asks, "_on_roster", lambda *a, **k: True, raising=False)
    import services.operator_resume_service as ors
    monkeypatch.setattr(ors, "spawn_resume_dispatch",
                        lambda item, **kw: calls.append(kw), raising=False)

    with pytest.raises(asks.AskError):
        asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)
    assert calls == []


def test_a_refused_choice_dispatches_nothing(asks, wired):
    """#2376's validator runs first, so an invalid answer cannot spend."""
    with pytest.raises(asks.AskError):
        asks.answer_ask("q1", "x@example.com", False, response="Delete everything", response_text=None)
    assert wired == []


def test_a_dispatch_failure_never_loses_the_answer(asks, monkeypatch):
    """The answer is committed before the dispatch and must survive it.

    `spawn_resume_dispatch` is fire-and-forget, but a raise on the calling line
    would still propagate — and returning 500 after the CAS landed would tell
    the client their answer failed when it is recorded and already on its way to
    the agent's queue file.
    """
    class _Db:
        def get_operator_queue_item(self, item_id):
            return dict(ITEM)

        def respond_to_operator_queue_item(self, **kw):
            return {**ITEM, "status": "responded"}

        def get_operator_resume_enabled(self, agent_name):
            return True

    monkeypatch.setattr(asks, "db", _Db(), raising=False)
    monkeypatch.setattr(asks, "_on_roster", lambda *a, **k: True, raising=False)
    import services.operator_resume_service as ors

    def _boom(item, **kw):
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(ors, "spawn_resume_dispatch", _boom, raising=False)

    out = asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)
    # The answer survived the spawn failure — that is the point of the test.
    # `answered`, not `pending`: the row IS answered, and reporting otherwise
    # beside `resume_requested` was the contradiction this assertion used to
    # paper over ("projection maps DB status; the point is it returned at all").
    assert out.status == "answered"
    assert out.resume_requested is False, "a spawn that raised must not claim a resume"


# ---------------------------------------------------------------------------
# AC #5 — the caller can tell whether anything was set in motion
# ---------------------------------------------------------------------------
def test_the_response_says_whether_a_resume_was_requested(asks, wired):
    """So a surface can say "answered" without implying work started.

    Read from the SAME accessor the dispatch gates on. It is a report of intent,
    not a promise of success: the dispatch is backgrounded, and a failure after
    this point lands as a FAILED execution row plus an `operator_resume_dispatch`
    audit entry (ent#329).
    """
    out = asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)
    assert out.resume_requested is True


def test_resume_requested_is_false_when_the_agent_has_not_opted_in(asks, monkeypatch):
    """AC #3 — per-agent, never per-answer. Hosting asks must not hand a client
    a spend button; the owner opts in, and the answer is still recorded."""
    class _Db:
        def get_operator_queue_item(self, item_id):
            return dict(ITEM)

        def respond_to_operator_queue_item(self, **kw):
            return {**ITEM, "status": "responded"}

        def get_operator_resume_enabled(self, agent_name):
            return False

    monkeypatch.setattr(asks, "db", _Db(), raising=False)
    monkeypatch.setattr(asks, "_on_roster", lambda *a, **k: True, raising=False)
    out = asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)
    assert out.resume_requested is False


def test_resume_requested_fails_closed(asks, monkeypatch):
    """An unreadable flag claims nothing. Over-claiming here is the failure mode
    AC #5 names — an ask that reads as acted upon while nothing happened."""
    class _Db:
        def get_operator_queue_item(self, item_id):
            return dict(ITEM)

        def respond_to_operator_queue_item(self, **kw):
            return {**ITEM, "status": "responded"}

        def get_operator_resume_enabled(self, agent_name):
            raise RuntimeError("db down")

    monkeypatch.setattr(asks, "db", _Db(), raising=False)
    monkeypatch.setattr(asks, "_on_roster", lambda *a, **k: True, raising=False)
    import services.operator_resume_service as ors
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda *a, **k: None, raising=False)

    out = asks.answer_ask("q1", "x@example.com", False, response="Ship it", response_text=None)
    assert out.resume_requested is False


# ---------------------------------------------------------------------------
# AC #2 — one dispatch surface, not two
# ---------------------------------------------------------------------------
def _code_only(src: str) -> str:
    """Source with comments and docstring prose stripped.

    Without this the checks below match the EXPLANATION of the rule rather than
    the rule: the comment in `answer_ask` names `maybe_dispatch_resume` while
    saying the code must not call it, and a bare substring test reads that as a
    violation. The mirror of the same trap in #2415, where a docstring saying
    "deliberately does NOT call X" satisfied an assertion looking for X.
    """
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)


def test_no_workspace_specific_execution_path(asks):
    """The client route must reach ent#329's dispatch and nothing else.

    Calling `execute_task` here would answer the cost, trigger-label and
    loop-prevention questions a second time and differently — which is the one
    thing ent#430's body rules out.
    """
    src = _code_only(inspect.getsource(asks.answer_ask))
    assert "spawn_resume_dispatch" in src
    for forbidden in ("execute_task", "create_task_execution", "maybe_dispatch_resume"):
        assert forbidden not in src, (
            f"answer_ask reaches {forbidden} directly; the whole point is that it "
            "goes through spawn_resume_dispatch like the operator route does"
        )


def test_both_answer_routes_use_the_same_dispatch(asks):
    """Operator and client answers must not drift apart."""
    from routers import operator_queue as opq

    operator_src = inspect.getsource(opq)
    client_src = inspect.getsource(asks)
    assert "spawn_resume_dispatch" in operator_src
    assert "spawn_resume_dispatch" in client_src


# ---------------------------------------------------------------------------
# The projected status (review pass 2, non-blocking)
# ---------------------------------------------------------------------------
def test_an_answered_ask_does_not_come_back_as_pending(asks):
    """`status: "pending"` beside `resume_requested: true` is one row saying both
    that nobody has answered it and that answering it started work."""
    assert asks._status_of({"status": "responded"}) == "answered"
    assert asks._status_of({"status": "acknowledged"}) == "answered"


def test_the_listing_statuses_are_unchanged(asks):
    """`answered` is reachable only from the answer response — the listing
    carries pending and expired rows and must keep reading exactly as before."""
    assert asks._status_of({"status": "pending"}) == "pending"
    assert asks._status_of({"status": "pending",
                            "expires_at": "2000-01-01T00:00:00Z"}) == "expired"
    assert asks._status_of({}) == "pending"


def test_an_expired_answer_still_reads_as_answered(asks):
    """An answer that landed is a fact; an `expires_at` that has since passed
    does not un-answer it. Ordering, pinned — the obvious refactor is to test
    expiry first, which would make a slow client's own answer vanish."""
    assert asks._status_of({"status": "responded",
                            "expires_at": "2000-01-01T00:00:00Z"}) == "answered"
