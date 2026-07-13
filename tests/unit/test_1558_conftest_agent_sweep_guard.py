"""Regression guard for #1558 — the conftest agent-deletion blast radius.

Before this fix, the session-scoped ``api_client`` fixture deleted EVERY agent
named ``test-*`` on the target instance at startup — silent, unprompted data
loss that destroyed a developer's real ``test-agent-2``. These tests pin the
new fail-closed policy in ``tests/utils/cleanup.py`` (pure, no network):

- the sweep NEVER returns a pre-existing ``test-``-prefixed user agent,
- it is empty unless explicitly enabled AND the target is localhost,
- ``cleanup_test_agent(require_suite_owned=True)`` refuses a name the session
  cannot prove it created (no stop/delete call is made).
"""
import os
import sys

import pytest

# The `utils` name is contested: backend has `src/backend/utils/` (helpers, …)
# and the suite has `tests/utils/` (api_client, cleanup). When this file is
# collected in isolation, a backend import can bind `sys.modules["utils"]` to
# the backend package first, hiding `tests/utils/cleanup.py`. Make the tests
# package win here (mirrors the conftest's own sys.modules surgery, #762 family).
_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
_u = sys.modules.get("utils")
if _u is not None and _TESTS_DIR not in (getattr(_u, "__file__", "") or ""):
    # Drop the backend `utils` parent binding so `utils.cleanup` re-resolves
    # under tests/utils. (Backend `utils.helpers` stays cached — it's unrelated.)
    sys.modules.pop("utils", None)

from utils.cleanup import (
    EPHEMERAL_AGENT_PREFIX,
    is_local_target,
    is_suite_owned_agent,
    register_created_agent,
    select_sweepable_agents,
    cleanup_test_agent,
    _SESSION_CREATED_AGENTS,
)

pytestmark = pytest.mark.unit

LOCAL = "http://localhost:8000"


class TestSweepPolicy:
    def test_preexisting_test_agent_is_never_swept(self):
        """The exact #1558 scenario: a real `test-agent-2` must survive."""
        names = ["test-agent-2", "testfix", "my-real-agent",
                 f"{EPHEMERAL_AGENT_PREFIX}agent-abc123"]
        got = select_sweepable_agents(names, LOCAL, enabled=True)
        assert got == [f"{EPHEMERAL_AGENT_PREFIX}agent-abc123"]
        assert "test-agent-2" not in got

    def test_disabled_sweeps_nothing(self):
        names = [f"{EPHEMERAL_AGENT_PREFIX}agent-x"]
        assert select_sweepable_agents(names, LOCAL, enabled=False) == []

    def test_non_local_target_refuses_even_when_enabled(self):
        names = [f"{EPHEMERAL_AGENT_PREFIX}agent-x"]
        for url in (
            "https://trinity.example.com",
            "http://10.0.0.5:8000",
            "http://staging:8000",
        ):
            assert select_sweepable_agents(names, url, enabled=True) == []

    def test_only_ephemeral_prefixed_returned_on_local_enabled(self):
        names = [
            f"{EPHEMERAL_AGENT_PREFIX}a", f"{EPHEMERAL_AGENT_PREFIX}b",
            "test-c", "prod-d",
        ]
        got = select_sweepable_agents(names, LOCAL, enabled=True)
        assert set(got) == {f"{EPHEMERAL_AGENT_PREFIX}a", f"{EPHEMERAL_AGENT_PREFIX}b"}

    def test_session_registered_name_is_reclaimable_even_without_prefix(self):
        """A registered name is provably suite-created, so it's sweepable."""
        odd = "weirdly-named-agent-#1558-test"
        register_created_agent(odd)
        try:
            got = select_sweepable_agents([odd, "test-real"], LOCAL, enabled=True)
            assert odd in got
            assert "test-real" not in got
        finally:
            _SESSION_CREATED_AGENTS.discard(odd)


class TestIsLocalTarget:
    @pytest.mark.parametrize("url", [
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost", "http://0.0.0.0:8000", "http://[::1]:8000",
    ])
    def test_local(self, url):
        assert is_local_target(url) is True

    @pytest.mark.parametrize("url", [
        "https://trinity.example.com", "http://staging.internal:8000",
        "http://10.0.0.5", "http://192.168.1.10:8000",
    ])
    def test_remote(self, url):
        assert is_local_target(url) is False


class TestSuiteOwnership:
    def test_ephemeral_prefix_is_not_the_broad_test_prefix(self):
        # The whole point of #1558: the suite prefix must not collide with a
        # human's `test-*` agent.
        assert not EPHEMERAL_AGENT_PREFIX.startswith("test-")
        assert is_suite_owned_agent(f"{EPHEMERAL_AGENT_PREFIX}x") is True
        assert is_suite_owned_agent("test-agent-2") is False


class TestCleanupRefusal:
    def test_require_suite_owned_refuses_unknown_name_without_calling_api(self):
        class ExplodingClient:
            def post(self, *a, **k):
                raise AssertionError("stop must not be called for a refused name")

            def delete(self, *a, **k):
                raise AssertionError("delete must not be called for a refused name")

        assert cleanup_test_agent(
            ExplodingClient(), "test-agent-2", require_suite_owned=True
        ) is False

    def test_require_suite_owned_allows_ephemeral_name(self):
        calls = {"post": 0, "delete": 0}

        class RecordingClient:
            def post(self, *a, **k):
                calls["post"] += 1
                return type("R", (), {"status_code": 200})()

            def delete(self, *a, **k):
                calls["delete"] += 1
                return type("R", (), {"status_code": 204})()

        ok = cleanup_test_agent(
            RecordingClient(), f"{EPHEMERAL_AGENT_PREFIX}agent-z",
            require_suite_owned=True,
        )
        assert ok is True
        assert calls["delete"] == 1
