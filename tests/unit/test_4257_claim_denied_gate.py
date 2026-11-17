"""The denied-claim counter is gated on proof, not on the refusal (#4257).

`routers/internal.py::_note_claim_denied` hangs off the `/next-task` 403
branch. That branch is reached BEFORE any credential is read — `_pull_authorized`
answers False for any non-pilot name first — so the diagnostic it carries (a 24 h
Redis key, an entry in a process-lifetime set, a WARNING line) was reachable by
an unauthenticated caller with an agent name of its choosing: unbounded state
growth plus raw attacker text in the log pipeline.

These tests execute the real HTTP path (`pull_router` under TestClient) and the
real helper, and assert the two halves of the gate:
  * the counter runs ONLY when the caller presented its OWN validated agent key
    (the de-piloted-but-still-polling worker the counter exists for);
  * the agent name is shape-checked before it becomes a Redis key or a log
    argument.

Closes review finding C1 and test gap I4.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "unit-secret")
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")  # nobody is a pilot -> 403
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.internal import pull_router

    app = FastAPI()
    app.include_router(pull_router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _clean_noted():
    """The warn-once set is process-global; isolate each test from the others."""
    from routers import internal

    internal._CLAIM_DENIED_NOTED.clear()
    yield
    internal._CLAIM_DENIED_NOTED.clear()


@pytest.fixture
def redis():
    """Patch the Redis the counter reaches for, and hand back the mock."""
    fake = MagicMock()
    with patch("services.slot_service.get_slot_service",
               return_value=MagicMock(redis=fake)):
        yield fake


def _key(agent_name, scope="agent"):
    """A validate_mcp_api_key() result shape."""
    return {"scope": scope, "agent_name": agent_name, "key_id": "k1", "user_id": "u"}


class TestUnauthenticatedCallerWritesNothing:
    """The refusal alone proves nothing about who asked."""

    def test_no_credential_at_all_leaves_redis_untouched(self, client, redis, caplog):
        with caplog.at_level(logging.WARNING, logger="routers.internal"), \
             patch("routers.internal.db.validate_mcp_api_key", return_value=None):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "attacker-chosen-name", "worker_id": "w"},
            )
        assert r.status_code == 403
        redis.incr.assert_not_called()
        redis.expire.assert_not_called()
        assert "attacker-chosen-name" not in caplog.text

    def test_a_flood_of_distinct_names_grows_no_state(self, client, redis):
        from routers import internal

        with patch("routers.internal.db.validate_mcp_api_key", return_value=None):
            for i in range(25):
                r = client.get(
                    "/api/internal/next-task",
                    params={"agent_name": f"flood-{i}", "worker_id": "w"},
                )
                assert r.status_code == 403
        assert internal._CLAIM_DENIED_NOTED == set()
        assert redis.incr.call_count == 0

    def test_a_key_bound_to_a_different_agent_does_not_count(self, client, redis):
        # beta's key asking about alpha: a valid credential, but not proof about
        # alpha — the same self-binding rule the seam itself enforces.
        with patch("routers.internal.db.validate_mcp_api_key",
                   return_value=_key("beta")):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "w"},
                headers={"Authorization": "Bearer trinity_mcp_beta"},
            )
        assert r.status_code == 403
        redis.incr.assert_not_called()

    def test_a_user_scoped_key_does_not_count(self, client, redis):
        with patch("routers.internal.db.validate_mcp_api_key",
                   return_value=_key("alpha", scope="user")):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "w"},
                headers={"Authorization": "Bearer trinity_mcp_user"},
            )
        assert r.status_code == 403
        redis.incr.assert_not_called()


class TestTheDePilotedWorkerIsStillCounted:
    """The case the diagnostic exists for must keep working."""

    def test_its_own_key_turned_away_by_the_allowlist_increments(self, client, redis):
        from services.pull_pilot import (
            PULL_CLAIM_DENIED_KEY_PREFIX,
            PULL_CLAIM_DENIED_TTL_SECONDS,
        )

        with patch("routers.internal.db.validate_mcp_api_key",
                   return_value=_key("alpha")):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        assert r.status_code == 403
        redis.incr.assert_called_once_with(f"{PULL_CLAIM_DENIED_KEY_PREFIX}alpha")
        redis.expire.assert_called_once_with(
            f"{PULL_CLAIM_DENIED_KEY_PREFIX}alpha", PULL_CLAIM_DENIED_TTL_SECONDS
        )

    def test_the_warning_is_once_per_process_but_the_count_is_every_poll(
        self, client, redis, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="routers.internal"), \
             patch("routers.internal.db.validate_mcp_api_key",
                   return_value=_key("alpha")):
            for _ in range(3):
                client.get(
                    "/api/internal/next-task",
                    params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                    headers={"Authorization": "Bearer trinity_mcp_alpha"},
                )
        assert redis.incr.call_count == 3
        warnings = [r for r in caplog.records if "refusing pull claims" in r.getMessage()]
        assert len(warnings) == 1

    def test_a_redis_failure_never_changes_the_answer(self, client, redis):
        redis.incr.side_effect = RuntimeError("redis down")
        with patch("routers.internal.db.validate_mcp_api_key",
                   return_value=_key("alpha")):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        assert r.status_code == 403

    def test_a_pilot_with_its_own_key_is_not_refused_and_not_counted(
        self, client, redis, monkeypatch
    ):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
        claim = {"envelope": {"id": "m1"}, "execution_id": "exec-1", "claim_token": "t"}
        with patch("routers.internal.db.validate_mcp_api_key",
                   return_value=_key("alpha")), \
             patch("services.pull_coordination_service.claim_next_task",
                   return_value=claim), \
             patch("services.pull_coordination_service.open_claim_activity",
                   new=AsyncMock()):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        assert r.status_code == 200
        redis.incr.assert_not_called()


class TestTheNameIsShapeCheckedBeforeItIsUsed:
    """Second layer: nothing attacker-shaped reaches Redis or the log line."""

    @pytest.mark.parametrize("bad", [
        "alpha\nWARNING injected log line",
        "alpha\x1b[31m",
        "Alpha Beta",
        "-leading-hyphen",
        "a" * 64,
        "",
    ])
    def test_a_name_that_is_not_an_agent_name_is_dropped(self, redis, caplog, bad):
        from routers import internal

        with caplog.at_level(logging.WARNING, logger="routers.internal"):
            internal._note_claim_denied(
                bad, {"scope": "agent", "agent_name": bad, "key_id": "k", "user_id": "u"}
            )
        redis.incr.assert_not_called()
        assert internal._CLAIM_DENIED_NOTED == set()
        assert not [r for r in caplog.records if "refusing pull claims" in r.getMessage()]

    @pytest.mark.parametrize("ok", ["alpha", "a", "my-agent.v2", "agent_1", "x" * 63])
    def test_a_real_agent_name_passes(self, redis, ok):
        from routers import internal
        from services.pull_pilot import PULL_CLAIM_DENIED_KEY_PREFIX

        internal._note_claim_denied(
            ok, {"scope": "agent", "agent_name": ok, "key_id": "k", "user_id": "u"}
        )
        redis.incr.assert_called_once_with(f"{PULL_CLAIM_DENIED_KEY_PREFIX}{ok}")
