"""Unit tests for the per-agent display label (ent#181).

An agent had exactly one name — its slug — so "rename" meant the heavyweight
identity change: stop the container, rewrite ~20 tables, clear every per-agent
Redis keyspace, and still leave the volumes under the old base (the root of
#1664/#1665/#1667/#1669/#1671). This adds a label that is *rendered, never
resolved*, so the common case ("call it Marketing Bot") touches one column.

What matters here, in order:

1. **The slug never moves.** Setting a label must not touch `agent_name` or
   anything keyed on it. If that ever breaks, the feature has become the bug it
   exists to avoid.
2. **NULL means "use the slug"** — no backfill, existing agents unchanged, and
   clearing reverts rather than blanking a name.
3. **The list read is batched**, because it runs on the fleet's hottest endpoint.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

_TS = "2026-01-01T00:00:00Z"


@pytest.fixture
def agent_ops(db_backend):
    try:
        from db.agents import AgentOperations
        from db.users import UserOperations
    except ImportError:  # pragma: no cover - env guard
        pytest.skip("backend venv required")
    return AgentOperations(UserOperations())


def _seed(name, deleted_at=None):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, 1, :ts, :d)",
        n=name, ts=_TS, d=deleted_at,
    )


class TestTheSlugNeverMoves:
    """The whole point of the feature."""

    def test_setting_a_label_leaves_the_slug_untouched(self, agent_ops):
        _seed("prod-agent")
        agent_ops.set_display_label("prod-agent", "Marketing Bot")

        # The row is still keyed by the slug...
        assert agent_ops.is_agent_name_reserved("prod-agent") is True
        assert agent_ops.get_display_label("prod-agent") == "Marketing Bot"
        # ...and nothing about its volume identity changed (#1664): no pin, so
        # the volumes are still `agent-prod-agent-*`, exactly as before.
        assert agent_ops.get_volume_base_name("prod-agent") == "prod-agent"

    def test_a_label_does_not_reserve_a_name(self, agent_ops):
        """A label is presentation. Labelling agent A "beta" must not make the
        slug `beta` unavailable, nor collide with an agent actually named it."""
        _seed("alpha")
        agent_ops.set_display_label("alpha", "beta")
        assert agent_ops.is_agent_name_reserved("beta") is False
        assert agent_ops.is_volume_base_reserved("beta") is False

    def test_two_agents_may_share_a_label(self, agent_ops):
        """Labels are not identities, so uniqueness is not theirs to enforce.
        The slug keeps them apart."""
        _seed("agent-one")
        _seed("agent-two")
        assert agent_ops.set_display_label("agent-one", "Support") is True
        assert agent_ops.set_display_label("agent-two", "Support") is True
        assert agent_ops.get_display_name("agent-one") == "Support"
        assert agent_ops.get_display_name("agent-two") == "Support"


class TestNullMeansTheSlug:
    def test_unlabelled_agent_renders_its_slug(self, agent_ops):
        _seed("plain")
        assert agent_ops.get_display_label("plain") is None   # no label...
        assert agent_ops.get_display_name("plain") == "plain"  # ...renders the slug

    def test_clearing_reverts_to_the_slug(self, agent_ops):
        _seed("plain")
        agent_ops.set_display_label("plain", "Fancy Name")
        assert agent_ops.get_display_name("plain") == "Fancy Name"

        agent_ops.set_display_label("plain", None)
        assert agent_ops.get_display_label("plain") is None
        assert agent_ops.get_display_name("plain") == "plain"

    def test_blank_is_stored_as_null_not_empty_string(self, agent_ops):
        """An empty label would render a nameless agent everywhere; "clear it"
        is the only sane reading of submitting an empty box."""
        _seed("plain")
        agent_ops.set_display_label("plain", "   ")
        assert agent_ops.get_display_label("plain") is None
        assert agent_ops.get_display_name("plain") == "plain"

    def test_label_is_trimmed(self, agent_ops):
        _seed("plain")
        agent_ops.set_display_label("plain", "  Spaced Out  ")
        assert agent_ops.get_display_label("plain") == "Spaced Out"

    def test_unknown_agent_reads_as_no_label(self, agent_ops):
        assert agent_ops.get_display_label("ghost") is None
        assert agent_ops.get_display_name("ghost") == "ghost"


class TestSoftDeleteGuard:
    def test_soft_deleted_agent_cannot_be_labelled(self, agent_ops):
        """Mirrors the other settings setters — a row on its way out is not
        editable."""
        _seed("gone", deleted_at="2026-02-01T00:00:00Z")
        assert agent_ops.set_display_label("gone", "Nope") is False
        assert agent_ops.get_display_label("gone") is None

    def test_set_on_missing_agent_returns_false(self, agent_ops):
        assert agent_ops.set_display_label("never-existed", "x") is False


class TestBatchRead:
    def test_returns_only_labelled_agents(self, agent_ops):
        """Absent = "no label" = render the slug, so an empty map means the
        fleet looks exactly as it does today."""
        _seed("one")
        _seed("two")
        _seed("three")
        agent_ops.set_display_label("one", "First")
        agent_ops.set_display_label("three", "Third")

        got = agent_ops.get_display_labels_for_agents(["one", "two", "three"])
        assert got == {"one": "First", "three": "Third"}

    def test_empty_input_does_not_query(self, agent_ops):
        assert agent_ops.get_display_labels_for_agents([]) == {}

    def test_excludes_soft_deleted(self, agent_ops):
        _seed("live")
        _seed("dead", deleted_at="2026-02-01T00:00:00Z")
        _hrun("UPDATE agent_ownership SET display_label = 'Ghost' WHERE agent_name = 'dead'")
        agent_ops.set_display_label("live", "Alive")

        got = agent_ops.get_display_labels_for_agents(["live", "dead"])
        assert got == {"live": "Alive"}

    def test_unknown_names_are_simply_absent(self, agent_ops):
        _seed("real")
        agent_ops.set_display_label("real", "Real")
        assert agent_ops.get_display_labels_for_agents(["real", "nope"]) == {"real": "Real"}


class TestEndpoints:
    """The router half: owner-gated write, honest read shape."""

    def _mod(self):
        import importlib
        return importlib.import_module("routers.agents")

    @pytest.mark.asyncio
    async def test_get_returns_label_slug_and_resolved_name(self):
        from unittest.mock import MagicMock, patch
        mod = self._mod()
        db = MagicMock()
        db.get_display_label.return_value = "Marketing Bot"
        with patch.object(mod, "db", db):
            out = await mod.get_agent_label_endpoint("prod-agent", MagicMock())
        # `label` is None-able and NOT coerced to the slug — the UI needs to
        # tell "no label" from "label equals the slug" to show an empty field.
        assert out == {
            "agent_name": "prod-agent",
            "label": "Marketing Bot",
            "display_name": "Marketing Bot",
        }

    @pytest.mark.asyncio
    async def test_get_unlabelled_resolves_to_the_slug(self):
        from unittest.mock import MagicMock, patch
        mod = self._mod()
        db = MagicMock()
        db.get_display_label.return_value = None
        with patch.object(mod, "db", db):
            out = await mod.get_agent_label_endpoint("plain", MagicMock())
        assert out["label"] is None and out["display_name"] == "plain"

    @pytest.mark.asyncio
    async def test_put_sets_and_broadcasts(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        mod = self._mod()
        db = MagicMock()
        db.set_display_label.return_value = True
        db.get_display_label.return_value = "Marketing Bot"
        ws = MagicMock()
        ws.broadcast = AsyncMock()
        body = MagicMock()
        body.label = "Marketing Bot"
        with patch.object(mod, "db", db), patch.object(mod, "manager", ws):
            out = await mod.set_agent_label_endpoint("prod-agent", body, MagicMock())
        assert out["display_name"] == "Marketing Bot"
        db.set_display_label.assert_called_once_with("prod-agent", "Marketing Bot")
        ws.broadcast.assert_awaited_once()
        import json as _json
        payload = _json.loads(ws.broadcast.await_args.args[0])
        # `event` is what the frontend WS client switches on; `data.name` is the
        # slug (it never moves). Both must be present or the broadcast is dead.
        assert payload["event"] == "agent_label_changed"
        assert payload["data"]["name"] == "prod-agent"
        assert payload["data"]["display_label"] == "Marketing Bot"

    @pytest.mark.asyncio
    async def test_put_null_clears(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        mod = self._mod()
        db = MagicMock()
        db.set_display_label.return_value = True
        db.get_display_label.return_value = None
        ws = MagicMock(); ws.broadcast = AsyncMock()
        body = MagicMock(); body.label = None
        with patch.object(mod, "db", db), patch.object(mod, "manager", ws):
            out = await mod.set_agent_label_endpoint("plain", body, MagicMock())
        assert out["label"] is None and out["display_name"] == "plain"

    @pytest.mark.asyncio
    async def test_put_404_when_the_row_is_gone(self):
        from unittest.mock import MagicMock, patch
        from fastapi import HTTPException
        mod = self._mod()
        db = MagicMock()
        db.set_display_label.return_value = False  # soft-deleted / vanished
        body = MagicMock(); body.label = "x"
        with patch.object(mod, "db", db), patch.object(mod, "manager", None):
            with pytest.raises(HTTPException) as exc:
                await mod.set_agent_label_endpoint("ghost", body, MagicMock())
        assert exc.value.status_code == 404
