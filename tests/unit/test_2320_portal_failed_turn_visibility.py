"""A Workspace turn that fails before or at start now SAYS SO (#2320).

The client's give-up path was built on absence. A streamed turn's 202 goes out
long before the turn ends, so a failure inside the background task has no
request left to raise into: it wrote `schedule_executions.error` and stopped
there. The client, polling, saw a thread with its own message, no reply, and no
in-flight marker — and reported "we've lost track of this turn" for a turn the
backend had diagnosed precisely, seconds earlier, as "your subscription is out
of credit".

So the tests worth writing are not "does Redis round-trip a dict". They are
about the four decisions this change actually makes:

  * **Where the verdict is decided.** `category` and `retryable` are arguments
    at the RAISE SITE, not something inferred later from the row. Only the raise
    site knows whether anything reached the agent — `_fail_unstarted_execution`
    is reached from both the `ClientPortalError` branch (genuinely pre-start)
    and the generic `except Exception` (which can fire after `execute_task`
    already returned and billed).

  * **That `unbilled` and `retryable` are two questions, not one.** The
    `agent_unavailable` refusal is the case that proves it: nothing was
    dispatched, yet a Retry is still wrong, because ent#286 settled that
    retrying a stopped agent cannot work. Deriving one bit from the other would
    read as an obvious simplification and would silently reintroduce either the
    #2120 double-billing or the #2150 no-Retry-where-Retry-is-correct bug.

  * **Ordering.** The outcome is written BEFORE the in-flight marker is
    cleared. The client's give-up timer starts the instant the marker vanishes,
    so an outcome written after it races a window in which the client sees
    neither a turn nor a reason — i.e. exactly the pre-#2320 behaviour, but
    intermittently, which is worse.

  * **What a client may be told.** The raw `type(exc).__name__: exc` text stays
    on the operator surface. A crash gets one fixed sentence.

Plus one guard that no service-layer test can supply: the field has to be
DECLARED on `PortalHistory`, or FastAPI's `response_model` strips it and the
whole feature is a no-op on the wire while every test here still passes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2320.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2320-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "bob@example.com"
SESSION = "sess-1"
EXEC_ID = "exec-abc"
OUTCOME_KEY = f"portal_turn_outcome:{SESSION}"
INFLIGHT_KEY = f"portal_inflight:{SESSION}"
SERVICE_SRC = _BACKEND / "client_portal" / "service.py"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeRedis:
    """This file's OWN fake, extended over test_ent286's and test_2133's.

    Neither of those records the ORDER of operations, and neither should grow
    it: the ordering of the outcome SET against the marker DEL is this file's
    subject (`test_the_verdict_is_written_before_the_marker_is_cleared`), and a
    shared fake would make that pin depend on a sibling file's harness.

    `ops` is an append-only log of `(verb, key)`; `broken` makes every call
    raise, which is how the best-effort contract is exercised.
    """

    def __init__(self, data=None, broken=False):
        self.data = dict(data or {})
        self.ex = {}
        self.ops = []
        self.broken = broken

    def _guard(self):
        if self.broken:
            raise RuntimeError("redis down")

    def set(self, k, v, ex=None):
        self._guard()
        self.ops.append(("set", k))
        self.data[k] = v
        self.ex[k] = ex

    def get(self, k):
        self._guard()
        self.ops.append(("get", k))
        return self.data.get(k)

    def delete(self, k):
        self._guard()
        self.ops.append(("delete", k))
        self.data.pop(k, None)
        self.ex.pop(k, None)

    def ttl(self, k):
        self._guard()
        if k not in self.data:
            return -2
        e = self.ex.get(k)
        return int(e) if e is not None else -1

    # -- assertions read through these ------------------------------------
    def outcome(self):
        raw = self.data.get(OUTCOME_KEY)
        return json.loads(raw) if raw is not None else None

    def index_of(self, verb, key):
        for i, op in enumerate(self.ops):
            if op == (verb, key):
                return i
        return -1


@pytest.fixture()
def redis_stub(monkeypatch):
    import redis_breaker_util
    fake = _FakeRedis()
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: fake)
    return fake


class _Result:
    """A `TaskExecutionResult` shaped just enough for the classifier.

    `error_code` defaults to None on purpose: that is the pre-#2320 world, and
    every substring case has to keep classifying in it.
    """

    def __init__(self, status="success", response="ok", error=None, error_code=None):
        self.status = status
        self.response = response
        self.error = error
        self.error_code = error_code
        self.session_id = None
        self.cost = 0.01


def _code(name):
    """The real enum member, by name — never a hand-rolled stand-in.

    `TaskExecutionErrorCode` is a `@dataclass`-decorated str-Enum, whose
    generated `__eq__` compares an empty field tuple and therefore reports every
    member equal to every other (pinned below). A fake would not reproduce that,
    and reproducing it is the entire reason `_error_code_name` reads `.name`.
    """
    from services.task_execution_service import TaskExecutionErrorCode
    return getattr(TaskExecutionErrorCode, name)


@pytest.fixture()
def chat(monkeypatch):
    """The REAL `portal_chat` with its boundaries stubbed.

    What stays real: the roster gate, the availability gate, and the whole
    failure classifier — the code that decides the two bits under test.
    `run_resumable_turn` is the seam, patched rather than driven through the
    execution engine, because this file needs to inject a `ResumeLockBusy` and
    an arbitrary `error_code` and cares about neither the lock nor the engine.
    """
    from client_portal import db as portal_db
    from client_portal import service as svc
    from services import session_turn_service as sts

    state = types.SimpleNamespace(
        availability="ready", on_roster=True, lock_busy=False,
        result=_Result(), turn_calls=[],
    )

    monkeypatch.setattr(svc, "agent_on_roster",
                        lambda a, e, include_owned=False: state.on_roster)

    async def _availability(name):
        return state.availability

    monkeypatch.setattr(svc, "_agent_availability", _availability)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s: s or SESSION)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda a, e: None)
    monkeypatch.setattr(svc, "_spawn_title_generation", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_persist_user_turn", lambda *a, **kw: None)

    async def _no_inbox(agent, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _no_inbox)

    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: [])
    monkeypatch.setattr(portal_db, "add_portal_message", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id", lambda sid: None)
    monkeypatch.setattr(portal_db, "update_cached_claude_session_id", lambda sid, u: None)

    monkeypatch.setattr(sts, "supports_session_resume", lambda a: True)
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 600)

    async def _turn(**kwargs):
        state.turn_calls.append(kwargs)
        if state.lock_busy:
            raise sts.ResumeLockBusy(f"portal:{AGENT}:{SESSION}")
        return sts.ResumableTurn(result=state.result, real_uuid=None)

    monkeypatch.setattr(sts, "run_resumable_turn", _turn)
    return svc, state


def _refuse(svc, state, **overrides):
    """Drive `portal_chat` into its refusal and hand back the error."""
    for k, v in overrides.items():
        setattr(state, k, v)
    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))
    return excinfo.value


# The full raise-site taxonomy, one row per branch in `portal_chat`. Written as
# data rather than as N tests so that a NEW branch added without a category is a
# visibly missing row, not an invisible absence.
RAISE_SITES = [
    # id                    setup                                          status  category             retryable
    ("off_roster",          {"on_roster": False},                          404,   "agent_unavailable", False),
    ("stopped_agent",       {"availability": "stopped"},                   502,   "agent_unavailable", False),
    ("containerless_agent", {"availability": "unavailable"},               502,   "agent_unavailable", False),
    ("resume_lock_busy",    {"lock_busy": True},                           429,   "busy",              True),
]


@pytest.mark.parametrize("label,setup,status,category,retryable",
                         RAISE_SITES, ids=[r[0] for r in RAISE_SITES])
def test_each_pre_dispatch_raise_site_declares_its_own_category_and_retryability(
        chat, label, setup, status, category, retryable):
    """The two bits travel WITH the refusal, from the only place that knows.

    Downstream, `_run` cannot tell these apart: they all arrive as one
    `ClientPortalError` and all write the same kind of terminal row. If the
    classification were done there it would have to be guessed from the status
    code — and 502 alone covers both "the agent is stopped" (nothing ran) and
    "the turn came back empty" (it ran).
    """
    svc, state = chat
    err = _refuse(svc, state, **setup)

    assert err.status_code == status
    assert err.category == category
    assert err.retryable is retryable
    assert err.detail, "a client-facing refusal must carry copy"


TERMINAL_SITES = [
    # id                  result kwargs                                                   status category   retryable
    ("auth_code",         {"error_code": "AUTH", "error": "no api key"},                   502,  "auth",      False),
    ("billing_code",      {"error_code": "BILLING", "error": "credit balance too low"},    502,  "auth",      False),
    ("capacity_code",     {"error_code": "CAPACITY", "error": "no slots"},                 429,  "capacity",  True),
    ("capacity_substr",   {"error_code": None, "error": "Agent is at capacity"},           429,  "capacity",  True),
    ("timeout_code",      {"error_code": "TIMEOUT", "error": "gave up"},                   504,  "timeout",   False),
    ("timeout_substr",    {"error_code": None, "error": "Execution timed out"},            504,  "timeout",   False),
    ("generic_failed",    {"error_code": None, "error": "something went wrong"},           502,  "agent_error", False),
    # ent#155 review (NEW-1): a cancellation is not a failure. It used to fall
    # through this ladder to the generic 502/agent_error, and `_run` recorded
    # that DURABLY — so a client who stopped their own turn saw "Something went
    # wrong" again on the next reload. The classifier now answers it ahead of
    # the failure arms, and retryable is TRUE: re-asking is exactly what someone
    # who changed their mind may want.
    ("generic_cancelled", {"error_code": None, "error": None, "status": "cancelled"},      409,  "cancelled",   False),
    # AGENT_ERROR is a real code with no branch of its own — it must land on the
    # generic arm rather than fall through the classifier untagged.
    ("agent_error_code",  {"error_code": "AGENT_ERROR", "error": "exit 1"},                502,  "agent_error", False),
]


@pytest.mark.parametrize("label,kwargs,status,category,retryable",
                         TERMINAL_SITES, ids=[r[0] for r in TERMINAL_SITES])
def test_each_failed_terminal_is_classified_with_its_own_category_and_retryability(
        chat, label, kwargs, status, category, retryable):
    svc, state = chat
    kwargs = dict(kwargs)
    code = kwargs.pop("error_code", None)
    result = _Result(status=kwargs.pop("status", "failed"),
                     error_code=_code(code) if code else None, **kwargs)
    err = _refuse(svc, state, result=result)

    assert err.status_code == status
    assert err.category == category
    assert err.retryable is retryable


def test_the_only_retryable_verdicts_are_the_two_where_nothing_reached_the_agent(chat):
    """The whole rule in one assertion, over EVERY raise site at once.

    Stated as a set rather than as per-case `is False` lines because the failure
    this guards is additive: someone adds a branch, copies a neighbouring
    `retryable=True`, and a turn that ran and billed grows a Retry button. A
    per-case test would stay green; this one names the new category.
    """
    svc, state = chat
    retryable = set()

    for label, setup, _status, category, _r in RAISE_SITES:
        err = _refuse(svc, state, **setup)
        if err.retryable:
            retryable.add(category)
        # reset the mutated field so the next row starts clean
        for k in setup:
            setattr(state, k, {"on_roster": True, "availability": "ready",
                               "lock_busy": False}[k])

    for label, kwargs, _status, category, _r in TERMINAL_SITES:
        kwargs = dict(kwargs)
        code = kwargs.pop("error_code", None)
        err = _refuse(svc, state, result=_Result(
            status=kwargs.pop("status", "failed"),
            error_code=_code(code) if code else None, **kwargs))
        if err.retryable:
            retryable.add(category)

    assert retryable == {"busy", "capacity"}


def test_a_turn_that_ran_is_never_retryable(chat):
    """The #2120/#2133 rule, kept intact: `timeout` and `agent_error` describe a
    turn that reached the agent and was billed. `auth` is not billed but
    re-fails identically, so it joins them — the pool is exhausted, not busy."""
    svc, state = chat

    for code, err_text in (("TIMEOUT", "gave up"), ("AGENT_ERROR", "exit 1"),
                           ("AUTH", "no api key"), ("BILLING", "credit low")):
        err = _refuse(svc, state, result=_Result(
            status="failed", error=err_text, error_code=_code(code)))
        assert err.retryable is False, f"{code} offered a Retry"


def test_unbilled_and_retryable_are_two_different_questions(chat):
    """The case that forbids deriving one bit from the other.

    A stopped agent and a held resume lock are BOTH refused before anything
    reaches the agent — `run_resumable_turn` is not even called for the first —
    so on "was this billed?" they are identical. On "should the client re-send?"
    they are opposites: ent#286 settled that retrying a stopped agent cannot
    work (and pins its copy against the words "try again"), while the busy copy
    has invited a retry all along.

    So `retryable` cannot be `not billed`, and it cannot be read off the status
    code either — 429 and 502 both appear on each side across the full table.
    """
    svc, state = chat

    refusal = _refuse(svc, state, availability="stopped")
    assert refusal.category == "agent_unavailable"
    assert state.turn_calls == [], "a refused turn must not reach the agent"
    assert refusal.retryable is False
    assert "try again" not in refusal.detail.lower()

    state.availability = "ready"
    busy = _refuse(svc, state, lock_busy=True)
    assert busy.category == "busy"
    assert busy.retryable is True
    assert "try again" in busy.detail.lower()


def test_the_subscription_limit_case_is_classified_instead_of_falling_through(chat):
    """The originally-reported bug, named.

    An exhausted subscription answers with `error_code=AUTH` and an error string
    that matches neither "at capacity" nor "timed out", so before #2320 it fell
    all the way to the generic 502 — "The agent couldn't respond. Please try
    again." — which is both the wrong diagnosis and the wrong instruction.
    """
    svc, state = chat
    generic = _refuse(svc, state, result=_Result(
        status="failed", error="Claude usage limit reached"))

    state.result = _Result(status="failed", error="Claude usage limit reached",
                           error_code=_code("AUTH"))
    classified = _refuse(svc, state)

    assert generic.category == "agent_error"
    assert classified.category == "auth"
    assert classified.detail != generic.detail
    assert "usage limit" in classified.detail.lower()
    # Operator remediation ("add an API key", "register a subscription") stays
    # on the Executions surface — a client cannot act on it.
    assert "api key" not in classified.detail.lower()
    assert "subscription" not in classified.detail.lower()


# ---------------------------------------------------------------------------
# Reading the engine's verdict
# ---------------------------------------------------------------------------


def test_the_error_code_enum_cannot_be_compared_with_equality(chat):
    """Why `_error_code_name` reads `.name` instead of the obvious `==`.

    `TaskExecutionErrorCode` is decorated `@dataclass`, so it carries a
    generated `__eq__` over an EMPTY field tuple — every member compares equal
    to every other (the #1085 footgun). A `code == TaskExecutionErrorCode.AUTH`
    test would therefore classify a TIMEOUT as an auth failure, and read as
    perfectly idiomatic while doing it.
    """
    from services.task_execution_service import TaskExecutionErrorCode as E

    assert E.AUTH == E.TIMEOUT, "the footgun is gone — this guard can be simplified"
    assert E.AUTH is not E.TIMEOUT
    assert E.AUTH.name != E.TIMEOUT.name


def test_error_code_name_reads_the_engines_verdict():
    from client_portal import service as svc

    assert svc._error_code_name(_Result(error_code=_code("AUTH"))) == "AUTH"
    assert svc._error_code_name(_Result(error_code=_code("CAPACITY"))) == "CAPACITY"


def test_error_code_name_is_none_when_there_is_no_code():
    """The pre-#2320 world, and every runtime that reports no code."""
    from client_portal import service as svc

    assert svc._error_code_name(_Result(error_code=None)) is None
    # A result object that does not carry the attribute at all — an older
    # engine, or a stand-in from another surface.
    assert svc._error_code_name(types.SimpleNamespace(status="failed")) is None


def test_an_unreadable_code_degrades_to_text_rather_than_raising():
    """A code object with no usable `.name` must not blow up the classifier.

    It falls back to `str(code)`, which will not match any branch name — so the
    turn lands on the substring fallback and then the generic arm, exactly as it
    did before this change. What must NOT happen is a raise: this runs inside
    `portal_chat`'s failure path, where an exception would replace a diagnosed
    refusal with an uncategorised crash.
    """
    from client_portal import service as svc

    class _Opaque:
        def __str__(self):
            return "opaque-code"

    got = svc._error_code_name(_Result(error_code=_Opaque()))
    assert got == "opaque-code"
    assert got not in ("AUTH", "BILLING", "CAPACITY", "TIMEOUT")


def test_the_substring_fallback_still_classifies_when_the_code_is_none(chat):
    """The change is ADDITIVE: everything that classified before still does.

    The `code == X or "substring" in err` shape is what makes it so, and it is
    easy to "tidy" into an if/elif on the code alone — which would silently
    un-classify every runtime that reports no `error_code`.
    """
    svc, state = chat

    capacity = _refuse(svc, state, result=_Result(
        status="failed", error="Agent is AT CAPACITY right now", error_code=None))
    assert (capacity.status_code, capacity.category) == (429, "capacity")

    timeout = _refuse(svc, state, result=_Result(
        status="failed", error="Execution timed out after 600s", error_code=None))
    assert (timeout.status_code, timeout.category) == (504, "timeout")
    # #2214's copy survives the reclassification.
    assert "10-minute" in timeout.detail


# ---------------------------------------------------------------------------
# What the background task records
# ---------------------------------------------------------------------------


@pytest.fixture()
def streaming(monkeypatch, redis_stub):
    """`start_portal_turn` with `portal_chat` faked — the ent#286 harness shape.

    `state.raise_with` is what the faked turn does: None for a clean turn, or an
    exception instance to raise. The `_run` wrapper around it is the code under
    test.
    """
    from client_portal import service as svc
    from database import db as core_db

    state = types.SimpleNamespace(raise_with=None, chat_calls=[], terminals=[],
                                  redis=redis_stub, ops_at_chat=None)

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s: s or SESSION)

    async def _ready(name):
        return "ready"

    monkeypatch.setattr(svc, "_agent_availability", _ready)
    monkeypatch.setattr(core_db, "create_task_execution",
                        lambda **kw: types.SimpleNamespace(id=EXEC_ID))
    monkeypatch.setattr(core_db, "get_agent_subscription_id", lambda a: "sub-1")

    def _terminal(execution_id, status, error=None, **kw):
        state.terminals.append({"execution_id": execution_id, "status": status,
                                "error": error})
        return True

    monkeypatch.setattr(core_db, "update_execution_status", _terminal)

    from services import session_turn_service as sts
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 600)

    async def _fake_chat(agent_name, message, email, session_id=None,
                         include_owned=False, execution_id=None,
                         turn_timeout_seconds=None, availability=None):
        state.chat_calls.append(execution_id)
        # Snapshot the op log the moment the turn starts, so "did a DEL happen
        # AFTER the turn" is answerable without guessing at indices.
        state.ops_at_chat = len(redis_stub.ops)
        if state.raise_with is not None:
            raise state.raise_with
        return {"response": "done", "cost": 0.0, "session_id": session_id}

    monkeypatch.setattr(svc, "portal_chat", _fake_chat)
    return svc, state


async def _drain(coro):
    """Await the dispatch, then let its background task run to completion."""
    out = await coro
    await asyncio.sleep(0)
    from client_portal import service as svc
    while svc._INFLIGHT_TURNS:
        await asyncio.sleep(0.01)
    return out


def test_a_diagnosed_failure_is_recorded_verbatim_for_the_client(streaming):
    """The whole feature: the refusal's own copy is what the client is handed.

    Not a re-worded summary and not the status code — `_run` has no vocabulary
    of its own, and inventing one here is how the client's message drifts away
    from the one `/chat` (the non-streaming path) returns for the same failure.
    """
    svc, state = streaming
    state.raise_with = svc.ClientPortalError(
        504, "The request timed out after the agent's 10-minute limit.",
        category="timeout", retryable=False)

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    outcome = state.redis.outcome()
    assert outcome == {
        "execution_id": EXEC_ID,
        "category": "timeout",
        "message": "The request timed out after the agent's 10-minute limit.",
        "retryable": False,
    }


def test_the_recorded_retryability_is_the_one_the_raise_site_declared(streaming):
    """`_run` copies the bit, it does not re-derive it — a 429 is not
    automatically retryable and a 502 is not automatically not."""
    svc, state = streaming
    state.raise_with = svc.ClientPortalError(
        429, "This conversation is already handling a message. Please try again shortly.",
        category="busy", retryable=True)

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert state.redis.outcome()["retryable"] is True
    assert state.redis.outcome()["category"] == "busy"


def test_an_uncategorised_crash_tells_the_client_one_fixed_sentence(streaming):
    """AC 2: the raw exception text is OPERATOR-only.

    It is unbounded, attacker-influencable and routinely carries a file path, a
    connection string or a stack-shaped fragment. It still goes to the log and
    to `schedule_executions.error`, where operators already read it — and
    nowhere a client can see.
    """
    svc, state = streaming
    secret = "SUPER-SECRET-INTERNAL-abc123"
    state.raise_with = RuntimeError(secret)

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    outcome = state.redis.outcome()
    assert outcome["category"] == "internal"
    assert outcome["message"] == svc.INTERNAL_FAILURE_DETAIL
    assert outcome["retryable"] is False, (
        "this branch can fire AFTER execute_task returned, so the turn may "
        "already have been billed"
    )

    # The raw text is nowhere in what the client receives...
    assert secret not in json.dumps(outcome)
    assert secret not in state.redis.data[OUTCOME_KEY]
    # ...and IS on the operator surface.
    assert state.terminals == [{"execution_id": EXEC_ID, "status": "failed",
                                "error": f"RuntimeError: {secret}"}]


def test_the_verdict_is_written_before_the_marker_is_cleared(streaming):
    """The load-bearing ordering contract.

    The client polls history and reads two things from one response: the
    in-flight marker and the outcome. Its give-up timer starts the moment the
    marker is gone. Writing the outcome after `clear_turn_inflight` would leave
    a window — a whole poll interval — in which the thread looks idle with no
    explanation, which is precisely the pre-#2320 bug, now intermittent and
    therefore much harder to see.

    Asserted on the observed Redis op ORDER, not on the source line order: the
    two calls live in different `try` clauses (`except` vs `finally`), so
    "which line comes first" is not the same question as "which runs first".
    """
    svc, state = streaming
    state.raise_with = svc.ClientPortalError(502, "nope", category="agent_error")

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    wrote_outcome = state.redis.ops.index(("set", OUTCOME_KEY))
    cleared_marker = state.redis.ops.index(("delete", INFLIGHT_KEY))
    assert wrote_outcome < cleared_marker, (
        "the client's give-up timer starts when the marker vanishes; a verdict "
        "written after it races that timer"
    )
    # ...and the marker really was cleared. A test that only proved ordering
    # would also pass if the `finally` disappeared entirely.
    assert svc.get_turn_inflight(SESSION) is None


def test_a_turn_that_answered_clears_the_slate(streaming):
    """The `else:` branch. The reply IS the outcome; a stale record would
    outlive it for the whole TTL and shadow the NEXT turn's give-up."""
    svc, state = streaming
    state.redis.data[OUTCOME_KEY] = json.dumps(
        {"execution_id": "older", "category": "timeout", "message": "stale",
         "retryable": False})

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert state.redis.outcome() is None
    # Proven to be the success branch, not merely the dispatch-time clear: a
    # delete lands AFTER the turn started running.
    after_turn = [op for op in state.redis.ops[state.ops_at_chat:]]
    assert ("delete", OUTCOME_KEY) in after_turn


def test_a_new_turn_never_inherits_the_previous_turns_verdict(streaming):
    """Cleared at DISPATCH, before the marker lands.

    Without it, a client that sends turn N+1 on a thread whose turn N failed is
    handed N's verdict on its very first poll — the marker is up, the outcome is
    there, and the id check is the only thing between that and reporting a
    live turn as already failed. Belt (the id match) and braces (this).
    """
    svc, state = streaming
    state.redis.data[OUTCOME_KEY] = json.dumps(
        {"execution_id": "older", "category": "auth", "message": "stale",
         "retryable": False})

    _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))

    cleared = state.redis.index_of("delete", OUTCOME_KEY)
    marked = state.redis.index_of("set", INFLIGHT_KEY)
    assert cleared >= 0 and marked >= 0
    assert cleared < marked, "the stale verdict outlived the new marker"


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_an_unknown_category_is_coerced_rather_than_stored(redis_stub):
    """The taxonomy is closed at the WRITE, not at the read.

    A client switches on `category` to decide how to render; an unrecognised
    token would fall through every branch and render as nothing at all. Coercing
    to `internal` costs precision and keeps the message — which is the part the
    user reads — intact.
    """
    from client_portal import service as svc

    svc.record_turn_outcome(SESSION, EXEC_ID, category="wat",
                            message="something", retryable=False)
    assert redis_stub.outcome()["category"] == "internal"
    assert redis_stub.outcome()["message"] == "something"

    # ...and a legitimate one is stored verbatim.
    for known in svc.PORTAL_FAILURE_CATEGORIES:
        svc.record_turn_outcome(SESSION, EXEC_ID, category=known,
                                message="m", retryable=False)
        assert redis_stub.outcome()["category"] == known


def test_the_record_expires_rather_than_accumulating(redis_stub):
    """It is a hand-off to a client that is already polling, not a history.

    The durable record is `schedule_executions` (status + error + cost), which
    retention governs. An unexpiring key would invent a second, unswept log of
    every failure in Redis.
    """
    from client_portal import service as svc

    svc.record_turn_outcome(SESSION, EXEC_ID, category="timeout",
                            message="m", retryable=False)
    assert redis_stub.ex[OUTCOME_KEY] == svc.TURN_OUTCOME_TTL_SECONDS
    assert 0 < svc.TURN_OUTCOME_TTL_SECONDS <= 3600


def test_reading_a_verdict_that_is_not_there_says_so(redis_stub, monkeypatch):
    """Every unreadable shape means "we cannot say why", and the honest answer
    to that is None — which drops the client back to its pre-#2320 lost/idle
    handling rather than rendering a half-parsed verdict."""
    import redis_breaker_util
    from client_portal import service as svc

    assert svc.get_turn_outcome(SESSION) is None                    # absent key

    redis_stub.data[OUTCOME_KEY] = "{not json"
    assert svc.get_turn_outcome(SESSION) is None                    # unparseable

    redis_stub.data[OUTCOME_KEY] = json.dumps(["a", "list"])
    assert svc.get_turn_outcome(SESSION) is None                    # parses, wrong shape

    redis_stub.data[OUTCOME_KEY] = json.dumps("just a string")
    assert svc.get_turn_outcome(SESSION) is None

    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: None)
    assert svc.get_turn_outcome(SESSION) is None                    # no client at all


def test_a_stored_verdict_round_trips_including_bytes(redis_stub):
    """redis-py returns bytes unless `decode_responses` is set, and the shared
    breaker client is not this module's to configure."""
    from client_portal import service as svc

    svc.record_turn_outcome(SESSION, EXEC_ID, category="capacity",
                            message="The agent is busy.", retryable=True)
    stored = redis_stub.data[OUTCOME_KEY]
    redis_stub.data[OUTCOME_KEY] = stored.encode()

    assert svc.get_turn_outcome(SESSION) == {
        "execution_id": EXEC_ID, "category": "capacity",
        "message": "The agent is busy.", "retryable": True,
    }


def test_every_helper_is_best_effort_when_redis_is_down(monkeypatch):
    """Redis down degrades the client to the pre-#2320 message. It must never
    turn a failed turn into a crashed background task, or a successful turn into
    a failed one — `clear_turn_outcome` runs on the SUCCESS path too."""
    import redis_breaker_util
    from client_portal import service as svc

    for client in (_FakeRedis(broken=True), None):
        monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda c=client: c)
        svc.record_turn_outcome(SESSION, EXEC_ID, category="timeout",
                                message="m", retryable=False)
        svc.clear_turn_outcome(SESSION)
        assert svc.get_turn_outcome(SESSION) is None


def test_a_broken_redis_does_not_stop_the_turn_from_finalizing(streaming, monkeypatch):
    """End to end over the degraded path: the terminal row is still written and
    the background task still exits cleanly."""
    import redis_breaker_util
    svc, state = streaming
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: _FakeRedis(broken=True))
    state.raise_with = svc.ClientPortalError(502, "nope", category="agent_error")

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert state.terminals == [{"execution_id": EXEC_ID, "status": "failed",
                                "error": "nope"}]


# ---------------------------------------------------------------------------
# It reaches the client
# ---------------------------------------------------------------------------


@pytest.fixture()
def history(monkeypatch, redis_stub):
    from client_portal import db as portal_db
    from client_portal import service as svc

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: [])
    monkeypatch.setattr(portal_db, "get_latest_portal_session_id", lambda *a, **kw: None)
    return svc, redis_stub


def test_the_verdict_rides_the_poll_the_client_is_already_making(history):
    """No extra request: `awaitPersistedReply` reads this exact response."""
    svc, redis_stub = history
    svc.record_turn_outcome(SESSION, EXEC_ID, category="auth",
                            message="The agent has reached its usage limit.",
                            retryable=False)

    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)

    assert out["last_turn_outcome"] == {
        "execution_id": EXEC_ID, "category": "auth",
        "message": "The agent has reached its usage limit.", "retryable": False,
    }


def test_a_thread_with_no_verdict_reports_none(history):
    svc, _ = history
    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)
    assert out["last_turn_outcome"] is None


def test_a_client_with_no_thread_at_all_is_not_a_lookup(history):
    """`get_turn_outcome(None)` would key on the literal string "None" and
    could, in principle, read another surface's key. The guard is the `if
    session_id` — asserted here rather than assumed."""
    svc, redis_stub = history
    out = svc.get_history(AGENT, EMAIL, session_id=None)

    assert out["session_id"] is None
    assert out["last_turn_outcome"] is None
    assert [k for (_verb, k) in redis_stub.ops if "turn_outcome" in k] == []


def test_the_field_is_declared_on_the_response_model():
    """The trap this whole file could otherwise walk into.

    The history route is declared `response_model=PortalHistory`, and FastAPI
    strips any key the model does not declare — silently, with a 200. So every
    service-layer assertion above would stay green while the field never left
    the server. This is the only test that can catch that, and it is the same
    failure `in_flight_wait_budget_seconds` (#2214) hit before it.
    """
    from client_portal.models import PortalHistory, PortalTurnOutcome

    assert "last_turn_outcome" in PortalHistory.model_fields

    # Optional, so an older client and a thread that answered are unaffected.
    empty = PortalHistory(agent_name=AGENT, session_id=None, messages=[])
    assert empty.last_turn_outcome is None

    # And the nested model declares every key the client reads.
    assert set(PortalTurnOutcome.model_fields) == {
        "execution_id", "category", "message", "retryable"}
    assert PortalTurnOutcome(execution_id="e", category="internal",
                             message="m").retryable is False


def test_the_route_still_declares_the_response_model_the_field_lives_on():
    """Belt for the test above: it proves the model carries the field, not that
    the route uses that model. Both halves are needed for the field to ship."""
    src = SERVICE_SRC.parent.joinpath("router.py").read_text()
    assert re.search(
        r'@router\.get\(\s*"/agents/\{agent_name\}/history",\s*response_model=PortalHistory',
        src), "the history route no longer serializes through PortalHistory"


def test_a_verdict_survives_the_serialization_boundary():
    """The dict `get_history` returns must actually validate as the model —
    a key-name or type mismatch would be dropped or 500 at response time, not
    caught by either test above."""
    from client_portal.models import PortalHistory

    parsed = PortalHistory(agent_name=AGENT, session_id=SESSION, messages=[],
                           last_turn_outcome={
                               "execution_id": EXEC_ID, "category": "busy",
                               "message": "already handling a message",
                               "retryable": True})
    assert parsed.last_turn_outcome.retryable is True
    assert parsed.model_dump()["last_turn_outcome"]["category"] == "busy"


# ---------------------------------------------------------------------------
# The taxonomy has no dead entries
# ---------------------------------------------------------------------------


def test_every_declared_category_is_actually_raised_somewhere():
    """A closed taxonomy is only useful while it is closed in BOTH directions.

    An unused token invites a client-side branch for a state that can never
    happen; a raise site using a token that is not declared is silently coerced
    to `internal` by `record_turn_outcome`, so the failure would show up as
    "every failure reads as internal" rather than as an error anywhere.
    """
    from client_portal import service as svc

    declared = set(svc.PORTAL_FAILURE_CATEGORIES)
    used = set(re.findall(r'category=["\']([a-z_]+)["\']', SERVICE_SRC.read_text()))

    assert used - declared == set(), "a raise site uses an undeclared category"
    assert declared - used == set(), "a declared category is never raised"


def test_the_default_is_the_unprivileged_answer():
    """A raise site that forgets to declare gets `internal` / not-retryable.

    The cost of a wrong `True` is a turn dispatched and billed twice; the cost
    of a wrong `False` is a user retyping a message. The default must be the
    second one.
    """
    from client_portal import service as svc

    err = svc.ClientPortalError(500, "boom")
    assert err.category == "internal"
    assert err.retryable is False
    # Still an ordinary ClientPortalError for every pre-#2320 caller.
    assert err.status_code == 500 and str(err) == "boom"
