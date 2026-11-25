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


class _FakeRedis:
    """Just enough of the sync client for the login counters."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def ttl(self, key):
        return 900

    def delete(self, key):
        self.data.pop(key, None)

    def pipeline(self):
        outer = self

        class _Pipe:
            def incr(self, key):
                outer.data[key] = str(int(outer.data.get(key, 0)) + 1)

            def expire(self, key, seconds):
                pass

            def execute(self):
                pass

        return _Pipe()


def _login_rig(monkeypatch, verify):
    """The real `login`, the real limit check and record, a fake Redis."""
    from routers import auth

    fake = _FakeRedis()
    monkeypatch.setattr(auth, "get_redis_client", lambda: fake)
    monkeypatch.setattr(auth, "authenticate_user", verify)
    monkeypatch.setattr(auth, "is_setup_completed", lambda: True)

    async def audit(**kw):
        return None

    monkeypatch.setattr(auth.platform_audit_service, "log", audit)

    def attempt(username, ip="127.0.0.1"):
        request = SimpleNamespace(client=SimpleNamespace(host=ip), headers={},
                                  state=SimpleNamespace(), scope={"path": "/api/token"})
        return auth.login(request, SimpleNamespace(username=username, password="wrong"), None)

    return auth, fake, attempt


async def _statuses(coros):
    results = await asyncio.gather(*coros, return_exceptions=True)
    return sorted(r.status_code for r in results)


async def _statuses_or_errors(coros):
    return await asyncio.gather(*coros, return_exceptions=True)


class TestLoginLockoutUnderConcurrency:
    """The verify is awaited now, so the lockout must hold without the event
    loop serialising the handler."""

    def test_concurrent_wrong_passwords_verify_only_up_to_the_account_limit(self, monkeypatch):
        verified = []

        def wrong(username, password):
            verified.append(username)
            time.sleep(0.02)
            return None

        auth, fake, attempt = _login_rig(monkeypatch, wrong)
        # One address per request, so only the per-account bucket can refuse.
        codes = asyncio.run(_statuses(attempt("admin", ip=f"10.0.0.{i}") for i in range(40)))

        limit = auth.LOGIN_ACCOUNT_LIMIT
        assert len(verified) == limit, f"{len(verified)} passwords were verified, the limit is {limit}"
        assert codes == [401] * limit + [429] * (40 - limit)
        assert fake.data["login_attempts_acct:admin"] == str(limit)
        assert auth._verify_locks == {}, "no lock entry outlives its requests"

    def test_the_account_is_normalised_before_it_is_locked(self, monkeypatch):
        verified = []

        def wrong(username, password):
            verified.append(username)
            time.sleep(0.02)
            return None

        auth, _, attempt = _login_rig(monkeypatch, wrong)
        names = ["admin", "ADMIN", " Admin "] * 6
        asyncio.run(_statuses(attempt(n, ip=f"10.0.1.{i}") for i, n in enumerate(names)))
        assert len(verified) == auth.LOGIN_ACCOUNT_LIMIT

    def test_one_account_does_not_delay_another(self, monkeypatch):
        running = SimpleNamespace(now=0, peak=0)
        guard = threading.Lock()

        def wrong(username, password):
            with guard:
                running.now += 1
                running.peak = max(running.peak, running.now)
            time.sleep(0.05)
            with guard:
                running.now -= 1
            return None

        _, _, attempt = _login_rig(monkeypatch, wrong)
        asyncio.run(_statuses(attempt(f"user{i}", ip=f"10.0.2.{i}") for i in range(3)))
        assert running.peak == 3, "different accounts verify in parallel"

    def test_parallel_verifies_are_capped(self, monkeypatch):
        running = SimpleNamespace(now=0, peak=0)
        guard = threading.Lock()

        def wrong(username, password):
            with guard:
                running.now += 1
                running.peak = max(running.peak, running.now)
            time.sleep(0.05)
            with guard:
                running.now -= 1
            return None

        auth, _, attempt = _login_rig(monkeypatch, wrong)
        asyncio.run(_statuses(attempt(f"user{i}", ip=f"10.0.3.{i}") for i in range(12)))
        assert running.peak == auth.LOGIN_VERIFY_CONCURRENCY

    def test_one_address_trying_many_accounts_is_held_to_the_ip_limit(self, monkeypatch):
        """The per-account lock does not cover this: every request is for a
        different account, so each holds its own lock."""
        verified = []

        def wrong(username, password):
            verified.append(username)
            time.sleep(0.02)
            return None

        auth, fake, attempt = _login_rig(monkeypatch, wrong)
        codes = asyncio.run(_statuses(attempt(f"user{i}", ip="203.0.113.7") for i in range(200)))

        limit = auth.LOGIN_IP_LIMIT
        assert len(verified) == limit, f"{len(verified)} passwords were verified, the per-IP limit is {limit}"
        assert codes == [401] * limit + [429] * (200 - limit)
        assert fake.data["login_attempts_ip:203.0.113.7"] == str(limit)
        assert auth._ip_verifies_pending == {}, "no in-flight count outlives its requests"

    def test_failures_already_recorded_count_with_the_ones_in_flight(self, monkeypatch):
        verified = []

        def wrong(username, password):
            verified.append(username)
            time.sleep(0.02)
            return None

        auth, fake, attempt = _login_rig(monkeypatch, wrong)
        fake.data["login_attempts_ip:203.0.113.8"] = str(auth.LOGIN_IP_LIMIT - 5)
        asyncio.run(_statuses(attempt(f"user{i}", ip="203.0.113.8") for i in range(40)))
        assert len(verified) == 5

    def test_other_addresses_are_not_held_back(self, monkeypatch):
        verified = []

        def wrong(username, password):
            verified.append(username)
            time.sleep(0.01)
            return None

        auth, _, attempt = _login_rig(monkeypatch, wrong)
        busy = [attempt(f"user{i}", ip="203.0.113.9") for i in range(60)]
        other = [attempt(f"other{i}", ip=f"198.51.100.{i}") for i in range(10)]
        asyncio.run(_statuses(busy + other))
        assert sum(1 for u in verified if u.startswith("other")) == 10
        assert sum(1 for u in verified if u.startswith("user")) == auth.LOGIN_IP_LIMIT

    def test_correct_passwords_in_flight_give_their_place_back(self, monkeypatch):
        """A success never becomes a failure: once it lands, the address has
        its whole budget again."""
        def right(username, password):
            time.sleep(0.01)
            return None if username.startswith("bad") else {"username": username, "role": "admin", "id": 1}

        auth, fake, attempt = _login_rig(monkeypatch, right)

        class _Stop(Exception):
            pass

        def after_login(*a, **k):
            raise _Stop

        monkeypatch.setattr(auth.db, "update_last_login", after_login)

        async def scenario():
            first = await asyncio.gather(*(attempt(f"good{i}", ip="203.0.113.10") for i in range(10)),
                                         return_exceptions=True)
            second = await asyncio.gather(*(attempt(f"bad{i}", ip="203.0.113.10") for i in range(3)),
                                          return_exceptions=True)
            return first, second

        first, second = asyncio.run(scenario())
        assert all(isinstance(r, _Stop) for r in first), "ten correct logins from one address all got in"
        assert sorted(r.status_code for r in second) == [401, 401, 401]
        assert auth._ip_verifies_pending == {}

    def test_a_verify_that_raises_still_gives_its_place_back(self, monkeypatch):
        def broken(username, password):
            raise RuntimeError("database down")

        auth, _, attempt = _login_rig(monkeypatch, broken)
        results = asyncio.run(_statuses_or_errors(attempt(f"user{i}", ip="203.0.113.11") for i in range(5)))
        assert all(isinstance(r, RuntimeError) for r in results)
        assert auth._ip_verifies_pending == {}
        assert auth._verify_locks == {}

    def test_a_correct_password_clears_the_counter_and_logs_in(self, monkeypatch):
        from routers import auth as auth_module

        def right(username, password):
            return {"username": username, "role": "admin", "id": 1}

        auth, fake, attempt = _login_rig(monkeypatch, right)
        fake.data["login_attempts_acct:admin"] = "3"
        class _Stop(Exception):
            pass

        def after_login(*a, **k):
            raise _Stop          # past the lock; the rest of the handler is not under test

        monkeypatch.setattr(auth_module.db, "update_last_login", after_login)
        with pytest.raises(_Stop):
            asyncio.run(attempt("admin"))
        assert "login_attempts_acct:admin" not in fake.data
        assert auth._verify_locks == {}
