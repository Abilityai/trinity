"""The two event-loop blocks the probe measured are off the loop (#4674).

Run on a live instance, `services/loop_health.py` named two things blocking a
worker (each line carried the pid, the duration and the frame):

- `run_heartbeat_watch_loop → process_watch_tick → list_all_agents_fast`:
  208 ms with 55 containers, every 5 s, in every worker. A `/health` sample
  taken at the same second read 236 ms.
- `login → authenticate_user → verify_password`: 176–200 ms of bcrypt per
  login, successful or not.

Both now run in a thread. The tests assert the THREAD the blocking function
ran on — not that some wrapper was called — and that the loop stayed free to
run other work while it did.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


async def _loop_stayed_free_while(coro, blocked_s: float) -> int:
    """Run `coro` beside a 5 ms ticker; return how many times the ticker ran.
    A blocked loop runs it about zero times; a free one, many."""
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await coro
    finally:
        done.set()
        await task
    assert ticks >= (blocked_s / 0.005) / 4, f"the loop ran other work only {ticks} time(s)"
    return ticks


class TestHeartbeatWatch:
    def test_the_tick_runs_off_the_event_loop(self, monkeypatch):
        from services import heartbeat_service as hb

        seen = SimpleNamespace(thread=None, alerts=[])

        def slow_tick():
            seen.thread = threading.get_ident()
            time.sleep(0.2)                 # a Docker listing of a large fleet
            return [("agent-a", "degraded")]

        async def emit(name, kind):
            seen.alerts.append((name, kind, threading.get_ident()))
            raise asyncio.CancelledError    # one tick is enough

        monkeypatch.setattr(hb, "process_watch_tick", slow_tick)
        monkeypatch.setattr(hb, "_emit_heartbeat_alert", emit)

        async def scenario():
            loop_thread = threading.get_ident()
            with pytest.raises(asyncio.CancelledError):
                await _loop_stayed_free_while(hb.run_heartbeat_watch_loop(interval_seconds=0), 0.2)
            return loop_thread

        loop_thread = asyncio.run(scenario())
        assert seen.thread is not None and seen.thread != loop_thread
        # The alert — an awaited broadcast — still runs on the loop.
        assert seen.alerts == [("agent-a", "degraded", loop_thread)]

    def test_a_failing_tick_still_cannot_kill_the_loop(self, monkeypatch):
        from services import heartbeat_service as hb

        calls = []

        def tick():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("docker down")
            raise asyncio.CancelledError

        monkeypatch.setattr(hb, "process_watch_tick", tick)

        async def scenario():
            with pytest.raises(asyncio.CancelledError):
                await hb.run_heartbeat_watch_loop(interval_seconds=0)

        asyncio.run(scenario())
        assert len(calls) == 2, "the loop survived the first tick's exception"

    def test_run_blocking_uses_the_docker_executor(self):
        from services import docker_utils

        async def scenario():
            return await docker_utils.run_blocking(lambda a, b: (threading.current_thread().name, a + b), 2, 3)

        name, total = asyncio.run(scenario())
        assert name.startswith("docker-") and total == 5


class TestLogin:
    def test_bcrypt_runs_off_the_event_loop(self, monkeypatch):
        from routers import auth

        seen = SimpleNamespace(thread=None, args=None)

        def slow_authenticate(username, password):
            seen.thread = threading.get_ident()
            seen.args = (username, password)
            time.sleep(0.2)                 # bcrypt
            return None                     # a FAILED attempt blocks just the same

        monkeypatch.setattr(auth, "authenticate_user", slow_authenticate)
        monkeypatch.setattr(auth, "check_login_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(auth, "is_setup_completed", lambda: True)
        monkeypatch.setattr(auth, "record_login_attempt", lambda *a, **k: None)

        audited = []

        async def audit(**kw):
            audited.append(kw["event_action"])

        monkeypatch.setattr(auth.platform_audit_service, "log", audit)
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={},
                                  state=SimpleNamespace(), scope={"path": "/api/token"})
        form = SimpleNamespace(username="admin", password="wrong")

        async def scenario():
            loop_thread = threading.get_ident()
            with pytest.raises(auth.HTTPException) as refused:
                await _loop_stayed_free_while(auth.login(request, form, None), 0.2)
            return loop_thread, refused.value.status_code

        loop_thread, status_code = asyncio.run(scenario())
        assert seen.args == ("admin", "wrong")
        assert seen.thread is not None and seen.thread != loop_thread
        assert status_code == 401, "a wrong password is still refused"
        assert audited == ["login_failed"], "and still audited"
