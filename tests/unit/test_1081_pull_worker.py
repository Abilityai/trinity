"""Agent-side pull worker pool — Phase 2 (#946 / #1081).

Unit-tests the worker machinery with the runtime turn and all HTTP MOCKED (local
Trinity has no Claude auth, so a real single-agent pilot cannot run here). Maps to
the five required proofs:

  1. Flag OFF ⇒ push path unchanged — ``schedule_pull_workers`` registers no
     startup handler, no worker loop runs (the load-bearing safety property).
  2. Flag ON ⇒ a worker claims a queued task via ``next-task``, runs it (mocked
     turn), and POSTs a well-formed §3.3 result carrying the correct claim_token.
  3. The pool respects its size bound (never more than ``max_parallel_tasks``
     concurrent claims/runs).
  4. Empty claim (§3.2 null / 204 / non-200) is handled gracefully — back off,
     no crash.
  5. Result POST failure/retry mirrors ``result_callback`` backoff (transient →
     retry, permanent 4xx → stop, deadline → give up to the reaper, Retry-After
     honored) — no lost terminal.

Async bodies run via ``asyncio.run`` inside sync tests (repo pattern —
pytest-asyncio auto-mode is not wired for the unit tier; mirrors
``test_1083_result_callback.py``). The unit conftest preloads the real
``docker/base-image/agent_server`` package.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import pull_worker as pw  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class _FakeClient:
    """Scripted GET (claim) + POST (result) client. Exhausted list repeats last."""

    def __init__(self, get_responses=None, post_responses=None):
        self._get = list(get_responses or [])
        self._post = list(post_responses or [])
        self.gets = []
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        self.gets.append((url, params, headers))
        r = self._get[min(len(self.gets) - 1, len(self._get) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        r = self._post[min(len(self.posts) - 1, len(self._post) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class _FakeApp:
    def __init__(self):
        self.handlers = {"startup": [], "shutdown": []}

    def on_event(self, name):
        def deco(fn):
            self.handlers[name].append(fn)
            return fn

        return deco


# ===========================================================================
# Proof 1 — flag OFF is a provable no-op
# ===========================================================================
class TestDefaultOff:
    def test_pull_mode_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("TRINITY_PULL_MODE", raising=False)
        assert pw._pull_mode_enabled() is False

    def test_flag_off_registers_no_handlers(self, monkeypatch):
        """The safety property: with the flag unset, schedule_pull_workers wires
        NOTHING — no startup handler, no shutdown handler, no worker task. The
        agent behaves exactly as today (push path)."""
        monkeypatch.delenv("TRINITY_PULL_MODE", raising=False)
        app = _FakeApp()
        pw.schedule_pull_workers(app)
        assert app.handlers["startup"] == []
        assert app.handlers["shutdown"] == []

    def test_flag_on_but_creds_missing_registers_no_handlers(self, monkeypatch):
        """Fail-safe: PULL_MODE on but no backend URL / scoped MCP key ⇒ still
        no pool (never a crash-loop of unauthenticated polls)."""
        monkeypatch.setenv("TRINITY_PULL_MODE", "true")
        monkeypatch.delenv("TRINITY_BACKEND_URL", raising=False)
        monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
        app = _FakeApp()
        pw.schedule_pull_workers(app)
        assert app.handlers["startup"] == []


# ===========================================================================
# Proof 2 — claim → run → well-formed §3.3 result with claim_token
# ===========================================================================
class TestRunAndReport:
    def test_worker_runs_turn_and_posts_result(self, monkeypatch):
        md = SimpleNamespace(
            model_dump=lambda: {
                "cost_usd": 0.02, "output_tokens": 1234, "context_window": 200000,
            }
        )
        runtime = SimpleNamespace(
            execute_headless=AsyncMock(return_value=("all done", [{"t": 1}], md, "sess-9"))
        )
        # Stub the runtime module so the local import in _run_and_report resolves
        # without executing the real (heavy) runtime_adapter.
        stub = types.ModuleType("agent_server.services.runtime_adapter")
        stub.get_runtime = lambda: runtime
        monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)

        client = _FakeClient(post_responses=[_FakeResp(200, {"applied": True})])
        claim = {
            "execution_id": "exec-7",
            "claim_token": "tok-abc",
            "envelope": {"payload": {"message": "run recon", "session_id": None}},
        }
        asyncio.run(pw._run_and_report(claim, client, "http://backend:8000", "sekret", "alpha"))

        # The claimed message reached the runtime, keyed by the execution_id.
        runtime.execute_headless.assert_awaited_once()
        kwargs = runtime.execute_headless.call_args.kwargs
        assert kwargs["prompt"] == "run recon"
        assert kwargs["execution_id"] == "exec-7"

        # Exactly one §3.3 result POST, to the right URL, authed with the agent's
        # OWN scoped MCP key as a Bearer token (proof 7) — NOT the master secret.
        assert len(client.posts) == 1
        url, body, headers = client.posts[0]
        assert url.endswith("/api/internal/tasks/exec-7/result")
        assert headers["Authorization"] == "Bearer sekret"
        assert "X-Internal-Secret" not in headers
        # Well-formed reply payload carrying the claim_token from the claim.
        assert body["claim_token"] == "tok-abc"
        assert body["status"] == "success"
        assert body["content"] == "all done"
        assert body["error_code"] is None
        assert body["cost"] == 0.02
        assert body["tokens"] == 1234
        assert body["session_id"] == "sess-9"
        assert body["metadata"]["context_window"] == 200000

    def test_http_failure_maps_to_typed_error_code(self, monkeypatch):
        runtime = SimpleNamespace(
            execute_headless=AsyncMock(side_effect=HTTPException(status_code=503, detail="no sub"))
        )
        stub = types.ModuleType("agent_server.services.runtime_adapter")
        stub.get_runtime = lambda: runtime
        monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)

        client = _FakeClient(post_responses=[_FakeResp(200, {"applied": True})])
        claim = {
            "execution_id": "exec-9", "claim_token": "t2",
            "envelope": {"payload": {"message": "m"}},
        }
        asyncio.run(pw._run_and_report(claim, client, "http://b", "s", "alpha"))
        _url, body, _h = client.posts[0]
        assert body["status"] == "failed"
        assert body["error_code"] == "auth"        # 503 → auth (§4 taxonomy)
        assert body["claim_token"] == "t2"


# ===========================================================================
# Proof 3 — the pool never exceeds its size bound
# ===========================================================================
class TestPoolBound:
    def test_pool_size_reads_and_clamps(self, monkeypatch):
        monkeypatch.setenv("TRINITY_MAX_PARALLEL_TASKS", "4")
        assert pw._pool_size() == 4
        monkeypatch.setenv("TRINITY_MAX_PARALLEL_TASKS", "0")   # clamp up to 1
        assert pw._pool_size() == 1
        monkeypatch.setenv("TRINITY_MAX_PARALLEL_TASKS", "999")  # clamp to 32
        assert pw._pool_size() == 32
        monkeypatch.setenv("TRINITY_MAX_PARALLEL_TASKS", "junk")  # default
        assert pw._pool_size() == pw._DEFAULT_POOL_SIZE

    def test_startup_spawns_exactly_pool_size_workers(self, monkeypatch):
        monkeypatch.setenv("TRINITY_PULL_MODE", "true")
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
        monkeypatch.setenv("TRINITY_MAX_PARALLEL_TASKS", "4")
        app = _FakeApp()
        pw.schedule_pull_workers(app)
        assert len(app.handlers["startup"]) == 1

        created = []

        def fake_create_task(coro):
            coro.close()  # don't actually run the infinite worker loop
            t = MagicMock()
            created.append(t)
            return t

        monkeypatch.setattr(asyncio, "create_task", fake_create_task)
        asyncio.run(app.handlers["startup"][0]())
        assert len(created) == 4  # == _pool_size()

    def test_concurrency_never_exceeds_n(self, monkeypatch):
        """N workers, each claim always returns work, each run blocks on an event:
        exactly N turns run concurrently — never N+1. This is the real bound proof
        (each worker awaits its turn inline, so the pool size IS the ceiling)."""
        N = 3

        async def _go():
            state = {"concurrent": 0, "max": 0}
            release = asyncio.Event()
            entered = asyncio.Semaphore(0)

            async def fake_claim_once(*a, **k):
                return {"execution_id": "e", "claim_token": "t",
                        "envelope": {"payload": {"message": "m"}}}

            async def fake_run_and_report(claim, client, *a, **k):
                state["concurrent"] += 1
                state["max"] = max(state["max"], state["concurrent"])
                entered.release()
                await release.wait()
                state["concurrent"] -= 1

            monkeypatch.setattr(pw, "_claim_once", fake_claim_once)
            monkeypatch.setattr(pw, "_run_and_report", fake_run_and_report)

            workers = [
                asyncio.create_task(pw.run_worker(f"a#w{i}", "u", "s", "a")) for i in range(N)
            ]
            try:
                for _ in range(N):  # wait until all N are inside a run
                    await asyncio.wait_for(entered.acquire(), timeout=2)
                await asyncio.sleep(0.05)  # give a (wrongly-spawned) N+1 a chance
                assert state["max"] == N
                assert state["concurrent"] == N
            finally:
                release.set()
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        asyncio.run(_go())


# ===========================================================================
# Proof 4 — empty claim handled gracefully
# ===========================================================================
class TestEmptyClaim:
    def test_claim_once_treats_empty_variants_as_none(self):
        async def _go():
            assert await pw._claim_once(
                _FakeClient(get_responses=[_FakeResp(200, {"envelope": None})]), "u", "s", "a", "w"
            ) is None
            assert await pw._claim_once(
                _FakeClient(get_responses=[_FakeResp(204, None)]), "u", "s", "a", "w"
            ) is None
            assert await pw._claim_once(
                _FakeClient(get_responses=[_FakeResp(503, None)]), "u", "s", "a", "w"
            ) is None

        asyncio.run(_go())

    def test_claim_once_returns_real_claim(self):
        async def _go():
            return await pw._claim_once(
                _FakeClient(get_responses=[
                    _FakeResp(200, {"envelope": {"payload": {}}, "execution_id": "e"})
                ]),
                "u", "s", "a", "w",
            )

        got = asyncio.run(_go())
        assert got is not None and got["execution_id"] == "e"

    def test_worker_backs_off_on_empty_without_crashing(self, monkeypatch):
        calls = {"n": 0}

        async def fake_claim(*a, **k):
            calls["n"] += 1
            return None

        sleeps = []

        async def fake_sleep(d):
            sleeps.append(d)
            if len(sleeps) >= 3:
                raise asyncio.CancelledError  # break the infinite loop

        monkeypatch.setattr(pw, "_claim_once", fake_claim)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(pw.run_worker("a#w1", "u", "s", "a"))
        assert calls["n"] >= 3            # kept polling, didn't die
        assert all(d >= 0 for d in sleeps)  # each empty poll backed off


# ===========================================================================
# Proof 5 — result POST retry mirrors result_callback backoff (no lost terminal)
# ===========================================================================
class TestDeliverResult:
    def test_transient_then_success_retries(self, monkeypatch):
        client = _FakeClient(post_responses=[_FakeResp(500, None), _FakeResp(200, {"applied": True})])
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ok = asyncio.run(pw._deliver_result(
            client, "e", {"claim_token": "t", "status": "success"}, "u", "s",
            time.monotonic() + 1000,
        ))
        assert ok is True
        assert len(client.posts) == 2  # retried the transient 500 once

    def test_permanent_4xx_stops_immediately(self, monkeypatch):
        client = _FakeClient(post_responses=[_FakeResp(409, None)])  # stale/wrong token
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ok = asyncio.run(pw._deliver_result(
            client, "e", {"claim_token": "bad", "status": "success"}, "u", "s",
            time.monotonic() + 1000,
        ))
        assert ok is True               # gave up (no point retrying a permanent 4xx)
        assert len(client.posts) == 1   # did NOT retry

    def test_gives_up_at_deadline_for_reaper(self, monkeypatch):
        client = _FakeClient(post_responses=[_FakeResp(500, None)])  # always transient
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ok = asyncio.run(pw._deliver_result(
            client, "e", {"claim_token": "t", "status": "success"}, "u", "s",
            time.monotonic() - 1,  # already past → lease reaper is the backstop
        ))
        assert ok is False
        assert len(client.posts) == 1

    def test_retry_after_header_is_honored_as_floor(self, monkeypatch):
        client = _FakeClient(post_responses=[
            _FakeResp(503, None, headers={"Retry-After": "7"}),  # #1085 governor
            _FakeResp(200, {"applied": True}),
        ])
        sleeps = []

        async def fake_sleep(d):
            sleeps.append(d)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        ok = asyncio.run(pw._deliver_result(
            client, "e", {"claim_token": "t", "status": "success"}, "u", "s",
            time.monotonic() + 1000,
        ))
        assert ok is True
        assert max(sleeps) >= 7  # Retry-After floor respected


# ===========================================================================
# #2317 — the claimed row's own per-task settings reach the runtime
# ===========================================================================
class TestTaskOverridesReachTheRuntime:
    """The backend records a concrete per-task `model` / `allowed_tools` /
    `max_turns` / `timeout_seconds` on every queued row and the PUSH path
    enforces them. Before #2317 the claim envelope carried none of it (it read a
    `task_overrides` key no producer writes), so this pool silently ran every
    pulled turn on agent/global defaults — including `TRINITY_PULL_TURN_TIMEOUT`
    in place of the row's own turn budget."""

    @staticmethod
    def _stub_runtime(monkeypatch, runtime):
        stub = types.ModuleType("agent_server.services.runtime_adapter")
        stub.get_runtime = lambda: runtime
        monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)

    def test_resolve_turn_timeout_prefers_the_rows_budget(self, monkeypatch):
        monkeypatch.setenv("TRINITY_PULL_TURN_TIMEOUT", "900")
        assert pw._resolve_turn_timeout({"timeout_seconds": 60}) == 60
        assert pw._resolve_turn_timeout({"timeout_seconds": "120"}) == 120

    def test_resolve_turn_timeout_falls_back_when_absent_or_junk(self, monkeypatch):
        monkeypatch.setenv("TRINITY_PULL_TURN_TIMEOUT", "900")
        assert pw._resolve_turn_timeout({}) == 900                        # absent
        assert pw._resolve_turn_timeout({"timeout_seconds": None}) == 900  # null
        assert pw._resolve_turn_timeout({"timeout_seconds": "soon"}) == 900
        assert pw._resolve_turn_timeout({"timeout_seconds": 0}) == 900     # non-positive
        assert pw._resolve_turn_timeout({"timeout_seconds": -5}) == 900

    def test_every_override_is_forwarded_to_execute_headless(self, monkeypatch):
        monkeypatch.setenv("TRINITY_PULL_TURN_TIMEOUT", "900")
        md = SimpleNamespace(model_dump=lambda: {"cost_usd": 0.0})
        runtime = SimpleNamespace(
            execute_headless=AsyncMock(return_value=("done", [], md, "sess-1"))
        )
        self._stub_runtime(monkeypatch, runtime)

        client = _FakeClient(post_responses=[_FakeResp(200, {"applied": True})])
        claim = {
            "execution_id": "exec-2317",
            "claim_token": "tok",
            "envelope": {"payload": {
                "message": "run recon",
                "session_id": "claude-sess-7",
                "task_overrides": {
                    "model": "opus",
                    "allowed_tools": ["mcp__trinity__report"],
                    "system_prompt": "PLATFORM::CALLER",
                    "max_turns": 7,
                    "timeout_seconds": 60,
                },
            }},
        }
        asyncio.run(pw._run_and_report(claim, client, "http://b", "s", "alpha"))

        kwargs = runtime.execute_headless.call_args.kwargs
        assert kwargs["model"] == "opus"
        assert kwargs["allowed_tools"] == ["mcp__trinity__report"]
        assert kwargs["system_prompt"] == "PLATFORM::CALLER"
        assert kwargs["max_turns"] == 7
        assert kwargs["timeout_seconds"] == 60          # the ROW's budget, not 900
        assert kwargs["resume_session_id"] == "claude-sess-7"
        assert kwargs["persist_session"] is True

    def test_short_row_timeout_does_not_shrink_the_delivery_window(self, monkeypatch):
        """A 60s turn budget must not cut the result-POST retry window down to
        60s+buffer — the backend lease derives from the agent's
        execution_timeout, not the row's, so the reaper would re-run a turn whose
        terminal was still being retried."""
        monkeypatch.setenv("TRINITY_PULL_TURN_TIMEOUT", "900")
        md = SimpleNamespace(model_dump=lambda: {})
        runtime = SimpleNamespace(
            execute_headless=AsyncMock(return_value=("done", [], md, None))
        )
        self._stub_runtime(monkeypatch, runtime)

        seen = {}

        async def _capture_deadline(client, execution_id, body, url, key, deadline):
            seen["deadline"] = deadline
            return True

        monkeypatch.setattr(pw, "_deliver_result", _capture_deadline)
        claim = {
            "execution_id": "exec-2317", "claim_token": "tok",
            "envelope": {"payload": {
                "message": "m", "task_overrides": {"timeout_seconds": 60},
            }},
        }
        before = time.monotonic()
        asyncio.run(pw._run_and_report(claim, _FakeClient(), "http://b", "s", "alpha"))
        # 900 (pool budget) + 300 (slot-TTL buffer), NOT 60 + 300.
        assert seen["deadline"] - before >= 900 + pw._SLOT_TTL_BUFFER_SECONDS - 5
