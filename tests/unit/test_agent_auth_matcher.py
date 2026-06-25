"""Unit tests for the recreate matcher check_agent_auth_token_env_matches (#1159).

The matcher is what wires the token into the existing needs_recreation machinery
(lifecycle.py): a container whose TRINITY_AGENT_AUTH_TOKEN is missing or stale
(old image, or a renamed container still carrying derive(old_name)) must be
recreated; a container carrying derive(current_name) must NOT (loop-safety —
the recreate inject writes exactly the value this checks).
"""
import pytest

from services.agent_auth import derive_agent_token
from services.agent_service.helpers import check_agent_auth_token_env_matches


SECRET = "f" * 64


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_SECRET", SECRET)


class _Container:
    """Minimal stand-in for a docker SDK container: only `.attrs` is read."""

    def __init__(self, env_list):
        self.attrs = {"Config": {"Env": env_list}}


def _with_token(token):
    return _Container([f"TRINITY_AGENT_AUTH_TOKEN={token}", "OTHER=keep"])


def test_correct_token_matches():
    c = _with_token(derive_agent_token("alpha"))
    assert check_agent_auth_token_env_matches(c, "alpha") is True


def test_missing_token_triggers_recreate():
    # Old image / pre-#1159 container — no token env at all.
    c = _Container(["OTHER=keep"])
    assert check_agent_auth_token_env_matches(c, "alpha") is False


def test_empty_token_triggers_recreate():
    c = _with_token("")
    assert check_agent_auth_token_env_matches(c, "alpha") is False


def test_renamed_container_is_stale_and_triggers_recreate():
    # Container carries the OLD name's token but is now queried under the new
    # name → mismatch → recreate (the Codex #4 rename fix at the unit level).
    c = _with_token(derive_agent_token("old-name"))
    assert check_agent_auth_token_env_matches(c, "new-name") is False


def test_loop_safe_after_recreate_inject():
    # After a recreate injects derive(new-name), the matcher is satisfied — so
    # needs_recreation converges in one pass (no infinite recreate loop).
    c = _with_token(derive_agent_token("new-name"))
    assert check_agent_auth_token_env_matches(c, "new-name") is True
