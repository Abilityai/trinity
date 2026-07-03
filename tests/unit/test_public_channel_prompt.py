"""Unit tests for per-agent public/channel custom instructions (#1205).

Covers:
- DB get/set on `agent_ownership.public_channel_system_prompt` (set strips,
  empty clears, unset is None) — on SQLite and, when TEST_POSTGRES_URL is set,
  PostgreSQL.
- `platform_prompt_service.build_public_channel_caller_prompt` composition:
  public fragment first, then the MEM-001 memory block; strict no-op when the
  public prompt is unset; never raises on a DB error.

Module: src/backend/services/platform_prompt_service.py
        src/backend/db/agents.py
Issue:  https://github.com/abilityai/trinity/issues/1205
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend, seed_user, seed_agent  # noqa: E402,F401


class TestPublicChannelPromptDB:
    def test_unset_is_none(self, db_backend):
        from database import db
        seed_user()
        seed_agent("a1")
        assert db.get_public_channel_system_prompt("a1") is None

    def test_set_strips_and_get(self, db_backend):
        from database import db
        seed_user()
        seed_agent("a1")
        assert db.set_public_channel_system_prompt("a1", "  Be formal.  ") is True
        assert db.get_public_channel_system_prompt("a1") == "Be formal."

    def test_empty_clears(self, db_backend):
        from database import db
        seed_user()
        seed_agent("a1")
        db.set_public_channel_system_prompt("a1", "Something")
        assert db.get_public_channel_system_prompt("a1") == "Something"
        db.set_public_channel_system_prompt("a1", "   ")
        assert db.get_public_channel_system_prompt("a1") is None

    def test_isolated_per_agent(self, db_backend):
        from database import db
        seed_user()
        seed_agent("a1")
        seed_agent("a2")
        db.set_public_channel_system_prompt("a1", "Only A1")
        assert db.get_public_channel_system_prompt("a2") is None


class TestBuildPublicChannelCallerPrompt:
    def _patch(self, monkeypatch, public_value):
        import services.platform_prompt_service as svc
        monkeypatch.setattr(
            svc.db, "get_public_channel_system_prompt", lambda name: public_value
        )
        return svc

    def test_public_and_memory_compose_public_first(self, monkeypatch):
        svc = self._patch(monkeypatch, "PUBLIC RULES")
        out = svc.build_public_channel_caller_prompt("a1", "MEMORY BLOCK")
        assert out == "PUBLIC RULES\n\nMEMORY BLOCK"

    def test_public_only(self, monkeypatch):
        svc = self._patch(monkeypatch, "PUBLIC RULES")
        assert svc.build_public_channel_caller_prompt("a1", None) == "PUBLIC RULES"

    def test_memory_only_is_noop_for_public(self, monkeypatch):
        svc = self._patch(monkeypatch, None)
        assert svc.build_public_channel_caller_prompt("a1", "MEMORY BLOCK") == "MEMORY BLOCK"

    def test_neither_returns_none(self, monkeypatch):
        svc = self._patch(monkeypatch, None)
        assert svc.build_public_channel_caller_prompt("a1", None) is None

    def test_db_error_degrades_to_memory(self, monkeypatch):
        import services.platform_prompt_service as svc
        def boom(name):
            raise RuntimeError("db down")
        monkeypatch.setattr(svc.db, "get_public_channel_system_prompt", boom)
        # never raises; falls back to just the memory block
        assert svc.build_public_channel_caller_prompt("a1", "MEMORY") == "MEMORY"
        assert svc.build_public_channel_caller_prompt("a1", None) is None
