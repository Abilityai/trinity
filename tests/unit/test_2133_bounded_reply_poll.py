"""The reply poll is bounded, and the bound is the agent's own timeout (#2214,
successor of the #2133 pins — filename kept for lineage).

Two failures sit on opposite sides of one number, which is why it is worth
pinning rather than eyeballing:

* **Too large** — an orphaned marker (hard backend kill skips the `finally`
  that clears it, and `except Exception` never catches `CancelledError`) leaves
  the client polling a dead turn and the composer disabled until the TTL.

* **Too small** — and this is the one that costs money. `run_resumable_turn`
  re-runs the WHOLE turn when a resume finds no JSONL, so a legitimate turn can
  take two full attempts. A bound of one attempt expires the marker while the
  retry is still running, the client is told "nothing is running", and a live,
  already-billed turn is declared not delivered with a Retry beside it. That is
  precisely the double-billing #2120 fixed, reappearing on exactly the
  cold-retry path that fix existed for.

#2133 sized the bound as `2 × attempt ceiling + slack` off a flat 300s turn;
#2214 makes the turn bound the agent's `execution_timeout_seconds` (TIMEOUT-001)
and these pins follow: the marker, the 202 budget and the dispatch must all be
derived from ONE per-turn resolution, and the client is told the budget on both
the dispatch (202) and the reattach (history response) rather than guessing.
"""
from __future__ import annotations

import asyncio
import os
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
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2133.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2133-logs"))

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "bob@example.com"
SESSION = "sess-1"
EXEC_ID = "exec-abc"
STUBBED_TIMEOUT = 1234   # deliberately none of the platform's round numbers


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeRedis:
    """This file's OWN fake — extended over test_ent286's, on purpose.

    That file's `_FakeRedis` discards `ex` and has no `ttl()`, and it is not
    the place to grow either: the TTL semantics are this file's subject.
    Records every SET (key, value, ex) so an assertion survives the marker
    being cleared later, and serves `ttl()` with the real client's -1/-2
    vocabulary.
    """

    def __init__(self):
        self.data = {}
        self.ex = {}
        self.set_calls = []       # (key, value, ex) in order
        self.ttl_answers = {}     # key → forced TTL answer (overrides derived)
        self.raise_on_ttl = False

    def set(self, k, v, ex=None):
        self.data[k] = v
        self.ex[k] = ex
        self.set_calls.append((k, v, ex))

    def get(self, k):
        return self.data.get(k)

    def delete(self, k):
        self.data.pop(k, None)
        self.ex.pop(k, None)

    def ttl(self, k):
        if self.raise_on_ttl:
            raise RuntimeError("redis down")
        if k in self.ttl_answers:
            return self.ttl_answers[k]
        if k not in self.data:
            return -2
        e = self.ex.get(k)
        return int(e) if e is not None else -1


@pytest.fixture()
def redis_stub(monkeypatch):
    import redis_breaker_util
    fake = _FakeRedis()
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: fake)
    return fake


def _stub_timeout(monkeypatch, value_or_exc):
    """Point the engine's `db.get_execution_timeout` at a stub."""
    from services import session_turn_service as sts

    if isinstance(value_or_exc, int):
        stub = types.SimpleNamespace(get_execution_timeout=lambda a: value_or_exc)
    else:
        def _boom(a):
            raise value_or_exc
        stub = types.SimpleNamespace(get_execution_timeout=_boom)
    monkeypatch.setattr(sts, "db", stub)


@pytest.fixture()
def portal(monkeypatch, redis_stub):
    """`start_portal_turn` with its boundaries stubbed and `portal_chat` faked.

    What stays real: the per-turn timeout resolution, the derived arithmetic,
    the marker write, and the 202 — the coupling under test.
    """
    from client_portal import service as svc
    from database import db as core_db

    state = types.SimpleNamespace(chat_calls=[], redis=redis_stub)

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)
    # #2219 moved the availability gate to the async `_agent_availability`;
    # `_agent_is_running` survives DEFINED BUT UNCALLED, so patching it would
    # not raise and would silently leave the real gate reaching Docker.
    async def _ready(name):
        return "ready"

    monkeypatch.setattr(svc, "_agent_availability", _ready)
    monkeypatch.setattr(core_db, "create_task_execution",
                        lambda **kw: types.SimpleNamespace(id=EXEC_ID))
    monkeypatch.setattr(core_db, "get_agent_subscription_id", lambda a: "sub-1")

    async def _fake_chat(agent_name, message, email, session_id=None,
                         include_owned=False, execution_id=None,
                         turn_timeout_seconds=None, availability=None, **kw):
        state.chat_calls.append({
            "execution_id": execution_id,
            "turn_timeout_seconds": turn_timeout_seconds,
        })
        return {"response": "done", "cost": 0.0, "session_id": session_id}

    monkeypatch.setattr(svc, "portal_chat", _fake_chat)
    _stub_timeout(monkeypatch, STUBBED_TIMEOUT)
    return svc, state


async def _drain(coro):
    """Await the start, then let its background task run to completion."""
    out = await coro
    await asyncio.sleep(0)
    from client_portal import service as svc
    while svc._INFLIGHT_TURNS:
        await asyncio.sleep(0.01)
    return out


@pytest.fixture()
def real_chat(monkeypatch):
    """The REAL `portal_chat` down to `execute_task`, everything else stubbed.

    Modeled on test_ent358's harness: portal persistence and the resume lock
    are boundaries; the resolution + dispatch + error mapping are the code
    under test. `state.results` feeds the recorder (default: success).
    """
    from client_portal import service as svc
    from client_portal import db as portal_db
    from services import session_turn_service as sts

    class _Result:
        def __init__(self, status="success", response="ok", error=None):
            self.status = status
            self.response = response
            self.error = error
            self.session_id = None
            self.cost = 0.01

    state = types.SimpleNamespace(calls=[], results=[], Result=_Result)

    class _Recorder:
        async def execute_task(self, **kwargs):
            state.calls.append(kwargs)
            return state.results.pop(0) if state.results else _Result()

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda a, e: None)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: SESSION)
    monkeypatch.setattr(svc, "_spawn_title_generation", lambda *a, **kw: None)

    async def _no_inbox(agent, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _no_inbox)

    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: [])
    monkeypatch.setattr(portal_db, "add_portal_message", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id", lambda sid: None)
    monkeypatch.setattr(portal_db, "update_cached_claude_session_id", lambda sid, u: None)

    async def _ready(name):
        return "ready"

    monkeypatch.setattr(svc, "_agent_availability", _ready)

    monkeypatch.setattr(sts, "supports_session_resume", lambda a: True)
    monkeypatch.setattr(sts, "resolve_lock_ttl", lambda a: 60)
    _stub_timeout(monkeypatch, STUBBED_TIMEOUT)

    class _NoLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(sts, "ResumeLock", _NoLock)

    rec = _Recorder()
    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: rec)
    monkeypatch.setattr(sts, "get_task_execution_service", lambda: rec, raising=False)

    return svc, state


# ---------------------------------------------------------------------------
# The derived arithmetic (pure functions since #2214)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t", [60, 300, 3600, 7200])
def test_the_bound_covers_two_full_turns(t):
    """One timeout is not enough: the cold retry re-runs the whole turn —
    at EVERY point of the agent-timeout range, not just the old 300."""
    from client_portal import service as svc

    assert svc.portal_max_turn_seconds(t) > 2 * t


@pytest.mark.parametrize("t", [60, 300, 3600, 7200])
def test_one_attempt_is_not_bounded_by_timeout_seconds(t):
    """The first version of this fix assumed it was, and was therefore still too
    small. `execute_task` dispatches with `timeout_seconds + 10`, and the #678
    reader-race auto-retry adds a whole second HTTP call on top of whatever
    attempt 1 already burned — capped at `_AUTO_RETRY_MAX_TIMEOUT_S`, not at the
    remaining budget.

    Read from the REAL retry constant (which the derivation now imports rather
    than mirrors), so a refactor that drops the addend fails here rather than
    silently shrinking the marker below a live turn.
    """
    from client_portal import service as svc
    from services import task_execution_service as tes

    worst_attempt = t + 10 + tes._AUTO_RETRY_MAX_TIMEOUT_S
    assert svc.portal_attempt_ceiling_seconds(t) >= worst_attempt
    assert svc.portal_max_turn_seconds(t) >= 2 * worst_attempt


def test_the_marker_is_bounded_by_the_agent_timeout_range(monkeypatch):
    """DELIBERATE SUPERSESSION of `test_the_bound_is_nowhere_near_the_old_hour`
    (#2214, plan D3).

    That pin (`marker TTL < 3600`) encoded a real hazard — a hard backend kill
    orphans the marker and one thread's composer stays disabled until the TTL —
    but holding it also pinned the bug: every agent's Workspace turn ran at
    300s no matter what the operator configured. The marker must cover the
    turn's worst LEGITIMATE life, and that follows from the operator's own cap
    (TIMEOUT-001, PUT-validated 60–7200), not from an independent hour-scale
    literal. So the bound here is derived, leaving no free number to drift.

    The accepted exposure, stated: the orphan case is dominant-case hard-kill
    (graceful shutdown clears the marker in `finally`; SIGKILL/OOM/host loss do
    not); the absolute worst marker is `portal_max_turn_seconds(7200)` =
    15,080s (~4.2h); the Session surface's own in-flight sentinel
    (`resolve_lock_ttl`, ≤7230s ≈ 2h) is the precedent for the sentinel CLASS —
    cited for the class, not the magnitude, since this reaches ~4.2h; and the
    reply-poll backoff (`REPLY_POLL_STEPS`) is what keeps the 4× longer tail
    affordable (~1,000 history reads per tab, not 5,100). Operator escape:
    `DEL portal_inflight:{session}`.
    """
    from client_portal import service as svc
    from services import session_turn_service as sts

    # Read-side clamp: garbage stored rows land inside TIMEOUT-001's own range.
    _stub_timeout(monkeypatch, 999999)
    assert sts.resolve_turn_timeout(AGENT) == 7200
    _stub_timeout(monkeypatch, 10)
    assert sts.resolve_turn_timeout(AGENT) == 60
    _stub_timeout(monkeypatch, RuntimeError("db down"))
    assert sts.resolve_turn_timeout(AGENT) == 3600   # the platform DEFAULT, not the cap

    # The derived absolute ceiling: 2 × (7200 + 10 + retry cap) + 60.
    assert svc.portal_max_turn_seconds(7200) == 15080


# ---------------------------------------------------------------------------
# One resolution per turn: marker TTL == 202 budget == dispatch timeout
# ---------------------------------------------------------------------------


def test_the_marker_ttl_is_that_bound(portal):
    """The client waits for as long as the marker claims a turn is running, so
    the marker TTL and the 202 budget must be the SAME number — proven by
    observation of one dispatch, not by comparing two module constants."""
    svc, state = portal

    out = _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    expected = svc.portal_max_turn_seconds(STUBBED_TIMEOUT)
    marker_sets = {k: ex for (k, v, ex) in state.redis.set_calls}
    assert marker_sets[f"portal_inflight:{SESSION}"] == expected
    assert marker_sets[f"portal_inflight_exec:{EXEC_ID}"] == expected
    assert out["wait_budget_seconds"] == expected

    # ...and the turn itself was handed the SAME resolved value the marker and
    # budget were sized from — one read per turn, by construction.
    assert state.chat_calls == [{
        "execution_id": EXEC_ID,
        "turn_timeout_seconds": STUBBED_TIMEOUT,
    }]


def test_the_turn_is_dispatched_with_the_timeout_the_bound_is_built_from(real_chat):
    """Successor of the old source-text pin (`timeout_seconds=PORTAL_TURN_
    TIMEOUT_SECONDS in src`): the dispatch must carry the value the bound is
    computed from — now the agent's own resolved timeout, asserted on the real
    `execute_task` call rather than on source text."""
    svc, state = real_chat

    _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))

    assert len(state.calls) == 1
    assert state.calls[0]["timeout_seconds"] == STUBBED_TIMEOUT


def test_resolver_fails_open_to_the_platform_default(portal, monkeypatch):
    """A DB hiccup must never produce a 0s or crashed turn: the dispatch
    proceeds at the platform default (3600 — the TIMEOUT-001 default, NOT the
    cap: the turn allows billable work, so 'assume the default' beats 'assume
    the maximum')."""
    svc, state = portal
    _stub_timeout(monkeypatch, RuntimeError("db down"))

    out = _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert out["wait_budget_seconds"] == svc.portal_max_turn_seconds(3600)
    assert state.chat_calls[0]["turn_timeout_seconds"] == 3600


# ---------------------------------------------------------------------------
# The budget travels to the client — dispatch AND reattach
# ---------------------------------------------------------------------------


def test_the_202_carries_the_budget_to_the_client():
    """The client must not invent its own ceiling: the server owns the timeout.
    A frontend constant drifts the next time it changes."""
    from client_portal.models import PortalTurnStarted

    assert "wait_budget_seconds" in PortalTurnStarted.model_fields


def test_the_budget_field_is_optional_so_an_older_client_is_unaffected():
    from client_portal.models import PortalTurnStarted

    started = PortalTurnStarted(execution_id="e1")
    assert started.wait_budget_seconds is None


def test_reattach_budget_rides_the_history_response(monkeypatch, redis_stub):
    """A reloading client gets its wait budget on the SAME fetch it already
    makes on mount — the marker's REMAINING TTL, not a fresh full budget
    (which would over-wait by the turn's elapsed time). Without this, a client
    reloading into a long turn gave up at the frozen fallback (~21 min) while
    the turn was alive and billed."""
    from client_portal import service as svc
    from client_portal import db as portal_db

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: [])
    _stub_timeout(monkeypatch, STUBBED_TIMEOUT)

    key = f"portal_inflight:{SESSION}"

    # Marker present → the budget IS the remaining TTL.
    redis_stub.set(key, EXEC_ID, ex=555)
    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)
    assert out["in_flight_execution_id"] == EXEC_ID
    assert out["in_flight_wait_budget_seconds"] == 555

    # No marker → nothing in flight, no budget.
    redis_stub.delete(key)
    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)
    assert out["in_flight_execution_id"] is None
    assert out["in_flight_wait_budget_seconds"] is None

    # TTL answers -2: the marker vanished between the GET and the TTL read —
    # its budget is exhausted, and "nothing running" is what a GET 1ms later
    # would have said. Both fields None; the idle-give-up resolves the client
    # in seconds instead of a whole extra budget.
    redis_stub.set(key, EXEC_ID, ex=555)
    redis_stub.ttl_answers[key] = -2
    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)
    assert out["in_flight_execution_id"] is None
    assert out["in_flight_wait_budget_seconds"] is None

    # TTL answers -1 (no expiry — unexpected, every writer sets ex=): genuinely
    # unknown state → fail OPEN to the full per-agent budget. Over-waiting is
    # the safe direction: a `lost` verdict never retries.
    redis_stub.ttl_answers[key] = -1
    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)
    assert out["in_flight_execution_id"] == EXEC_ID
    assert out["in_flight_wait_budget_seconds"] == svc.portal_max_turn_seconds(STUBBED_TIMEOUT)

    # TTL read raises → keep today's id behavior, budget None → the client
    # falls back to its frozen literal.
    del redis_stub.ttl_answers[key]
    redis_stub.raise_on_ttl = True
    out = svc.get_history(AGENT, EMAIL, session_id=SESSION)
    assert out["in_flight_execution_id"] == EXEC_ID
    assert out["in_flight_wait_budget_seconds"] is None


# ---------------------------------------------------------------------------
# Honest failure (AC #6)
# ---------------------------------------------------------------------------


def test_timeout_error_names_the_agents_bound(real_chat):
    """A turn that hits the bound must say WHICH bound — the agent's own — with
    honest rounding: minutes for real bounds, seconds below 120 (a 90s bound
    reported as '1-minute' would be a lie)."""
    svc, state = real_chat

    # 1234s → round(1234/60) = 21 minutes.
    state.results = [state.Result(status="failed", error="Execution timed out")]
    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION, turn_timeout_seconds=1234))
    assert excinfo.value.status_code == 504
    assert "21-minute" in excinfo.value.detail

    # 90s speaks in seconds.
    state.results = [state.Result(status="failed", error="Execution timed out")]
    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION, turn_timeout_seconds=90))
    assert excinfo.value.status_code == 504
    assert "90-second" in excinfo.value.detail
