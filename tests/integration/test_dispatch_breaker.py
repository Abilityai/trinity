"""Integration tests for the dispatch circuit breaker Lua machine (#526).

The state machine lives in Redis (atomic Lua scripts) — fakeredis has no
EVALSHA, so the only faithful tests run against a real Redis, mirroring
tests/integration/test_circuit_breaker.py (the transport breaker).

Connect to a live Redis via the `backend` ACL credentials. Honors a pre-set
REDIS_URL (sibling-stack workflows / CI on alternate ports — e.g. the
`-p trinity-526-test` stack on 6390); otherwise derives from the repo .env +
localhost:6379. Each test uses a unique agent name and cleans its keys.

Covered:
- closed → open at the consecutive AUTH threshold; any success resets
- non-AUTH / ignored outcomes never move state (R2 at the Redis level)
- open denies; retry_after_seconds ~= base cooldown
- half-open probe closes on success; reopens with grown backoff
- record_failure("missed_heartbeat") seam (#307)
- get_all_dispatch_states (scan, skips probe-locks) + get_dispatch_states_for
  (pipelined) + reset_dispatch
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest
import redis as _redis

_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_env_password() -> str:
    env_path = _REPO / ".env"
    if not env_path.exists():
        pytest.skip(".env missing — cannot derive Redis credentials", allow_module_level=True)
    for line in env_path.read_text().splitlines():
        if line.startswith("REDIS_BACKEND_PASSWORD="):
            return line.split("=", 1)[1].strip()
    pytest.skip("REDIS_BACKEND_PASSWORD not found in .env", allow_module_level=True)


if "REDIS_URL" not in os.environ:
    _PW = _load_env_password()
    os.environ["REDIS_URL"] = f"redis://backend:{_PW}@localhost:6379"

# Normal import (linter-safe — no sys.modules mutation). dispatch_breaker is a
# leaf (only redis_breaker_util) so this stays cheap.
from services.dispatch_breaker import (  # noqa: E402
    DISPATCH_BASE_COOLDOWN_SECONDS,
    DISPATCH_FAILURE_THRESHOLD,
    DispatchBreaker,
    get_all_dispatch_states,
    get_dispatch_states_for,
    reset_dispatch,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_client():
    client = _redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        client.ping()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Redis unreachable at {os.environ['REDIS_URL']}: {e}")
    return client


@pytest.fixture
def agent_name(redis_client):
    name = f"db-itest-{uuid.uuid4().hex[:10]}"
    yield name
    redis_client.delete(f"agent:dispatch:{name}", f"agent:dispatch:{name}:probe-lock")


def _breaker(agent_name, redis_client):
    return DispatchBreaker(agent_name, redis_client=redis_client)


class TestStateMachine:
    def test_consecutive_auth_opens_at_threshold(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        assert b.allow_dispatch() is True
        # threshold-1 AUTH failures stay closed
        for _ in range(DISPATCH_FAILURE_THRESHOLD - 1):
            assert b.record_outcome("auth").opened is False
        assert b.to_dict()["state"] == "closed"
        # the threshold-th opens
        t = b.record_outcome("auth")
        assert t.opened is True and t.prior == "closed" and t.new == "open"
        assert b.to_dict()["state"] == "open"
        assert b.allow_dispatch() is False  # cooldown not elapsed

    def test_success_resets_counter(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        b.record_outcome("auth")
        b.record_outcome("auth")
        assert b.to_dict()["failure_count"] == 2
        b.record_outcome(None)  # success
        assert b.to_dict()["failure_count"] == 0
        # must take a fresh full run of AUTH to open again
        for _ in range(DISPATCH_FAILURE_THRESHOLD - 1):
            assert b.record_outcome("auth").opened is False

    @pytest.mark.parametrize(
        "code", ["timeout", "capacity", "network", "agent_error", "billing", "circuit_open"]
    )
    def test_ignored_outcomes_never_move_state(self, agent_name, redis_client, code):
        """R2: only AUTH counts — ignored codes leave the breaker untouched."""
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD + 2):
            assert b.record_outcome(code).changed is False
        assert b.to_dict() == {"state": "closed", "failure_count": 0, "retry_after_seconds": 0}
        assert b.allow_dispatch() is True

    def test_retry_after_seconds_when_open(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD):
            b.record_outcome("auth")
        ra = b.retry_after_seconds()
        assert 0 < ra <= int(DISPATCH_BASE_COOLDOWN_SECONDS) + 1

    def test_half_open_probe_closes_on_success(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD):
            b.record_outcome("auth")
        assert b.allow_dispatch() is False
        # Fast-forward past cooldown → one probe is admitted.
        redis_client.hset(f"agent:dispatch:{agent_name}", "next_probe_at", str(time.time() - 1))
        assert b.allow_dispatch() is True  # probe granted
        t = b.record_success()
        assert t.closed is True
        assert b.to_dict()["state"] == "closed"

    def test_probe_failure_reopens_with_grown_backoff(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD):
            b.record_outcome("auth")
        first_ra = b.retry_after_seconds()
        # probe admitted then fails again → backoff grows (exp).
        redis_client.hset(f"agent:dispatch:{agent_name}", "next_probe_at", str(time.time() - 1))
        b.allow_dispatch()
        t = b.record_outcome("auth")
        assert t.new == "open" and t.opened is False  # open→open, not a fresh open
        second_ra = b.retry_after_seconds()
        assert second_ra >= first_ra

    def test_missed_heartbeat_seam_opens(self, agent_name, redis_client):
        """#307 seam — record_failure with a non-auth reason still trips."""
        b = _breaker(agent_name, redis_client)
        b.record_failure("missed_heartbeat")
        b.record_failure("missed_heartbeat")
        t = b.record_failure("missed_heartbeat")
        assert t.opened is True

    def test_record_success_noop_skips_write_when_clean(self, agent_name, redis_client):
        """#526 Finding 4: a success on an already-closed/0-failures breaker is a
        no-op — no HASH write, so a healthy agent doesn't churn Redis on every
        successful execution. A fresh breaker has no key, so the absence of the
        key after record_success proves the guard skipped the write."""
        b = _breaker(agent_name, redis_client)
        t = b.record_success()
        assert (t.prior, t.new) == ("closed", "closed")
        assert t.closed is False
        assert redis_client.exists(f"agent:dispatch:{agent_name}") == 0

    def test_record_success_still_resets_accumulated_failures(self, agent_name, redis_client):
        """#526 Finding 4: the no-op guard must NOT skip a real reset — a closed
        breaker carrying sub-threshold failures still gets cleared on success."""
        b = _breaker(agent_name, redis_client)
        b.record_outcome("auth")  # closed, failures=1 (below threshold)
        assert b.to_dict()["failure_count"] == 1
        b.record_success()
        assert b.to_dict()["failure_count"] == 0


class TestOperatorHooks:
    def test_get_all_dispatch_states_includes_open_skips_locks(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD):
            b.record_outcome("auth")
        # opening sets a probe-lock transiently; ensure scan never returns the lock key
        states = get_all_dispatch_states()
        assert agent_name in states
        assert states[agent_name]["state"] == "open"
        assert not any(k.endswith(":probe-lock") for k in states)

    def test_get_dispatch_states_for_pipelined(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD):
            b.record_outcome("auth")
        other = f"db-itest-{uuid.uuid4().hex[:8]}"
        try:
            states = get_dispatch_states_for([agent_name, other])
            assert states[agent_name]["state"] == "open"
            assert states[other]["state"] == "closed"  # no history → closed default
        finally:
            redis_client.delete(f"agent:dispatch:{other}", f"agent:dispatch:{other}:probe-lock")

    def test_reset_dispatch_clears(self, agent_name, redis_client):
        b = _breaker(agent_name, redis_client)
        for _ in range(DISPATCH_FAILURE_THRESHOLD):
            b.record_outcome("auth")
        assert b.to_dict()["state"] == "open"
        reset_dispatch(agent_name)
        assert b.to_dict()["state"] == "closed"
        assert b.allow_dispatch() is True
