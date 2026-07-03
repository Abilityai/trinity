"""
Unit tests for #28 — persisted per-agent voice + VoIP enable/disable toggle.

Covers:
- db/agents.py  get_voice_name / set_voice_name: unset→default fallback,
  roundtrip, invalid-persisted→default (reviewer M1), clear→default.
- db/voip.py    set_enabled + the create_binding "preserve enabled on re-PUT"
  fix (reviewer H3) + 404-shaped no-op on a missing binding.
- config.py     GEMINI_VOICE_NAMES default + parity with the frontend
  src/constants/voices.js list (reviewer M2 drift guard).

Runs on the db_harness backends (SQLite always; PostgreSQL when
TEST_POSTGRES_URL is set). No Docker, no API, no Redis.

Modules: src/backend/db/agents.py, src/backend/db/voip.py, src/backend/config.py
"""

import os
import re
import secrets
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, engine_conn, seed_agent, seed_user  # noqa: E402,F401


@pytest.fixture
def agent_ops(db_backend):
    """AgentOperations on the active backend, with one agent_ownership row."""
    from db.agents import AgentOperations
    from db.users import UserOperations

    seed_user()
    seed_agent("agent-voice")
    yield AgentOperations(UserOperations())


@pytest.fixture
def voip_ops(db_backend):
    from db.voip import VoipOperations

    yield VoipOperations()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    # create_binding encrypts the AuthToken via AES-256-GCM (needs a key).
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


# ---------------------------------------------------------------------------
# Persisted per-agent voice (db/agents.py)
# ---------------------------------------------------------------------------

class TestVoiceName:
    def test_unset_falls_back_to_default(self, agent_ops):
        # Fresh agent has no voice_name → the historical 'Kore' default.
        assert agent_ops.get_voice_name("agent-voice") == "Kore"

    def test_set_then_get_roundtrip(self, agent_ops):
        assert agent_ops.set_voice_name("agent-voice", "Puck") is True
        assert agent_ops.get_voice_name("agent-voice") == "Puck"

    def test_invalid_persisted_value_falls_back_to_default(self, agent_ops):
        # A value no longer in GEMINI_VOICE_NAMES (e.g. removed by Google) must
        # not reach the call path — get_voice_name fails closed to the default.
        agent_ops.set_voice_name("agent-voice", "RetiredVoice")
        assert agent_ops.get_voice_name("agent-voice") == "Kore"

    def test_clear_reverts_to_default(self, agent_ops):
        agent_ops.set_voice_name("agent-voice", "Charon")
        assert agent_ops.get_voice_name("agent-voice") == "Charon"
        assert agent_ops.set_voice_name("agent-voice", None) is True
        assert agent_ops.get_voice_name("agent-voice") == "Kore"

    def test_set_on_missing_agent_is_noop(self, agent_ops):
        assert agent_ops.set_voice_name("nope", "Puck") is False


# ---------------------------------------------------------------------------
# VoIP enable/disable toggle (db/voip.py)
# ---------------------------------------------------------------------------

class TestVoipEnabled:
    def _make_binding(self, ops):
        return ops.create_binding(
            agent_name="agent-voip",
            account_sid="AC" + "0" * 32,
            auth_token="FAKE-TOKEN-not-real",
            from_number="+14155550100",
        )

    def test_new_binding_is_enabled(self, voip_ops):
        b = self._make_binding(voip_ops)
        assert b["enabled"] is True

    def test_toggle_disable_then_enable(self, voip_ops):
        self._make_binding(voip_ops)
        assert voip_ops.set_enabled("agent-voip", False) is True
        assert voip_ops.get_binding_by_agent("agent-voip")["enabled"] is False
        assert voip_ops.set_enabled("agent-voip", True) is True
        assert voip_ops.get_binding_by_agent("agent-voip")["enabled"] is True

    def test_resave_credentials_preserves_disabled_state(self, voip_ops):
        # Reviewer H3: re-PUTting credentials on a disabled binding must NOT
        # silently re-enable it.
        self._make_binding(voip_ops)
        voip_ops.set_enabled("agent-voip", False)
        # Owner edits the from-number / re-validates creds via the same upsert.
        voip_ops.create_binding(
            agent_name="agent-voip",
            account_sid="AC" + "0" * 32,
            auth_token="FAKE-TOKEN-not-real",
            from_number="+14155550199",
        )
        b = voip_ops.get_binding_by_agent("agent-voip")
        assert b["from_number"] == "+14155550199"  # edit landed
        assert b["enabled"] is False               # but stayed disabled

    def test_set_enabled_on_missing_binding_returns_false(self, voip_ops):
        assert voip_ops.set_enabled("no-such-agent", True) is False


# ---------------------------------------------------------------------------
# Voice list: backend default + frontend parity (no DB)
# ---------------------------------------------------------------------------

class TestVoiceConstants:
    def test_backend_defaults(self):
        import config

        assert config.DEFAULT_VOICE_NAME == "Kore"
        assert config.DEFAULT_VOICE_NAME in config.GEMINI_VOICE_NAMES
        assert config.GEMINI_VOICE_NAMES == (
            "Kore", "Zephyr", "Puck", "Aoede", "Charon", "Fenrir", "Gacrux",
        )

    def test_frontend_voice_list_matches_backend(self):
        """The frontend src/constants/voices.js VOICE ids must equal the
        backend GEMINI_VOICE_NAMES — the two are mirrored across the language
        boundary and a drift would silently desync the picker from validation
        (reviewer M2)."""
        import config

        js = (
            _BACKEND.parent / "frontend" / "src" / "constants" / "voices.js"
        ).read_text()
        # Pull each `{ id: 'X', ... }` in declaration order.
        ids = re.findall(r"id:\s*'([^']+)'", js)
        assert tuple(ids) == config.GEMINI_VOICE_NAMES
        # And the JS default agrees with the backend default.
        m = re.search(r"DEFAULT_VOICE_NAME\s*=\s*'([^']+)'", js)
        assert m and m.group(1) == config.DEFAULT_VOICE_NAME
