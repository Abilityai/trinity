"""Unit tests for #894 — per-agent public-channel model override.

Covers:
- services/settings_service.py  is_valid_public_channel_model / PUBLIC_CHANNEL_MODELS.
- db/agents.py  get_public_channel_model / set_public_channel_model: unset→None,
  roundtrip, invalid-persisted→None (defense-in-depth), clear→None, missing-agent no-op.
- A static guard that the three public-facing call sites pass the per-agent
  override into execute_task (resolution wiring can't silently regress).

Runs on the db_harness backends (SQLite always; PostgreSQL when TEST_POSTGRES_URL
is set). No Docker, no API, no Redis.

Modules: src/backend/services/settings_service.py, src/backend/db/agents.py
"""

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _PROJECT_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, engine_conn, seed_agent, seed_user  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_ops(db_backend):
    """AgentOperations on the active backend, with one agent_ownership row."""
    from db.agents import AgentOperations
    from db.users import UserOperations

    seed_user()
    seed_agent("agent-pcm")
    yield AgentOperations(UserOperations())


# ---------------------------------------------------------------------------
# Whitelist / validator (settings_service)
# ---------------------------------------------------------------------------

class TestValidator:
    def test_current_gen_models_are_valid(self):
        from services.settings_service import is_valid_public_channel_model
        for m in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
            assert is_valid_public_channel_model(m), m

    def test_unknown_or_bare_alias_is_invalid(self):
        from services.settings_service import is_valid_public_channel_model
        for m in ("gpt-4", "sonnet", "opus", "", "claude-opus-4-20250514"):
            assert not is_valid_public_channel_model(m), m

    def test_whitelist_is_non_empty_frozenset(self):
        from services.settings_service import PUBLIC_CHANNEL_MODELS
        assert isinstance(PUBLIC_CHANNEL_MODELS, frozenset)
        assert "claude-sonnet-4-6" in PUBLIC_CHANNEL_MODELS


# ---------------------------------------------------------------------------
# Persisted per-agent override (db/agents.py)
# ---------------------------------------------------------------------------

class TestPublicChannelModel:
    def test_unset_is_none(self, agent_ops):
        # Fresh agent has no override → None (caller inherits platform default).
        assert agent_ops.get_public_channel_model("agent-pcm") is None

    def test_set_then_get_roundtrip(self, agent_ops):
        assert agent_ops.set_public_channel_model("agent-pcm", "claude-opus-4-8") is True
        assert agent_ops.get_public_channel_model("agent-pcm") == "claude-opus-4-8"

    def test_invalid_persisted_value_falls_back_to_none(self, agent_ops):
        # A value no longer in the whitelist (e.g. retired model) must not reach
        # the call path — the getter fails closed to None (→ platform default).
        agent_ops.set_public_channel_model("agent-pcm", "claude-opus-4-20250514")
        assert agent_ops.get_public_channel_model("agent-pcm") is None

    def test_clear_reverts_to_none(self, agent_ops):
        agent_ops.set_public_channel_model("agent-pcm", "claude-haiku-4-5-20251001")
        assert agent_ops.get_public_channel_model("agent-pcm") == "claude-haiku-4-5-20251001"
        assert agent_ops.set_public_channel_model("agent-pcm", None) is True
        assert agent_ops.get_public_channel_model("agent-pcm") is None

    def test_set_on_missing_agent_is_noop(self, agent_ops):
        assert agent_ops.set_public_channel_model("nope", "claude-opus-4-8") is False


# ---------------------------------------------------------------------------
# Resolution wiring — the three public-facing call sites must pass the override
# ---------------------------------------------------------------------------

class TestCallSiteWiring:
    SITES = {
        "src/backend/routers/public.py": 2,          # sync + async-background paths
        "src/backend/adapters/message_router.py": 1,
        "src/backend/routers/paid.py": 1,
    }

    @pytest.mark.parametrize("relpath,expected", SITES.items())
    def test_call_site_passes_override(self, relpath, expected):
        src = (_PROJECT_ROOT / relpath).read_text()
        hits = len(re.findall(
            r"model=db\.get_public_channel_model\(agent_name\)", src
        ))
        assert hits == expected, (
            f"{relpath}: expected {expected} execute_task call(s) to pass the "
            f"#894 per-agent override, found {hits}"
        )
