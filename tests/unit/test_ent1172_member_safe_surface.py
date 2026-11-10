"""ent#1172 — member-safe conversation surface.

Backend contracts (ACs 2, 5):

- ``PortalAgentCard`` carries ``is_member_agent`` (default ``False``), so
  the roster payload can carry the flag to the frontend.
- ``_member_agent_names()`` fails open to an empty frozenset when the enterprise
  module is absent — a roster must never 500 over a missing capability.
- ``_row_to_card`` marks a member agent and suppresses ``model_default`` for
  non-owner viewers (share-tier), even when ``is_platform=True``.
- An owner viewing their own member agent keeps ``model_default``.
- A non-member agent's ``model_default`` is unaffected.

PATCHING RULE (the same constraint test_ent403_workspace_model.py documents):
``client_portal.service`` resolves its dependencies via function-local
``from services.x import y``, which walks ``sys.modules`` at call time.
Patch the seam on ``client_portal.service``, never on ``services.x`` alone —
both paths must agree or the stub may land on the one the product code skips.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def svc():
    from client_portal import service as m
    return m


# ---------------------------------------------------------------------------
# 1. PortalAgentCard carries the flag
# ---------------------------------------------------------------------------

def test_portal_agent_card_carries_is_member_agent_field():
    """The roster payload exposes is_member_agent so the frontend can gate on it."""
    from client_portal.models import PortalAgentCard
    card = PortalAgentCard(name="test-agent")
    assert card.is_member_agent is False


# ---------------------------------------------------------------------------
# 2. _member_agent_names() fails open when enterprise is absent
# ---------------------------------------------------------------------------

def test_member_agent_names_fails_open_on_missing_enterprise(svc, monkeypatch):
    """A roster load on an OSS install (no enterprise submodule) must not 500."""
    import builtins
    original = builtins.__import__

    def _raise_on_enterprise(name, *args, **kwargs):
        if "enterprise" in name and "member_agents" in name:
            raise ImportError(f"stubbed out: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _raise_on_enterprise)
    result = svc._member_agent_names()
    assert result == frozenset()
    assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# 3. _row_to_card suppresses model_default for non-owner member agent viewers
# ---------------------------------------------------------------------------

def _card(svc, *, agent_name="regent-11", is_platform=True, runtime="claude-code",
          owned=False, member_agent_names=frozenset(), ctx=None):
    """Build a card via the real _row_to_card for the model_default and
    is_member_agent assertions."""
    row = {
        "agent_name": agent_name,
        "public_channel_model": None,
        "owned": owned,
    }
    return svc._row_to_card(
        row, False, None,
        availability="ready",
        is_platform=is_platform,
        runtime=runtime,
        model_context=ctx if ctx is not None else svc._model_context(),
        stt_ready=True,
        member_agent_names=member_agent_names,
    )


def test_member_agent_non_owner_has_no_model_default(svc):
    """Share-tier viewer of a member agent: model picker must not render (AC 2)."""
    card = _card(svc, agent_name="regent-11", is_platform=True, owned=False,
                 member_agent_names=frozenset({"regent-11"}))
    assert card.model_default is None


def test_member_agent_non_owner_is_flagged(svc):
    """The card carries is_member_agent=True so the frontend can suppress the briefing."""
    card = _card(svc, agent_name="regent-11", owned=False,
                 member_agent_names=frozenset({"regent-11"}))
    assert card.is_member_agent is True


def test_member_agent_owner_keeps_model_default(svc):
    """The hub-steward owner of a member agent keeps the model picker (AC 5 boundary)."""
    card = _card(svc, agent_name="regent-11", is_platform=True, owned=True,
                 member_agent_names=frozenset({"regent-11"}))
    assert card.model_default is not None


def test_non_member_agent_model_default_unchanged(svc):
    """Agents not in member_agent_names are not affected."""
    card = _card(svc, agent_name="regular-agent", is_platform=True, owned=False,
                 member_agent_names=frozenset({"regent-11"}))  # different set
    assert card.model_default is not None


def test_non_member_agent_is_not_flagged(svc):
    card = _card(svc, agent_name="regular-agent", is_platform=True, owned=False,
                 member_agent_names=frozenset())
    assert card.is_member_agent is False


def test_member_agent_non_platform_session_still_no_model_default(svc):
    """Portal-token viewers of member agents: model_default is None (unchanged from
    the existing is_platform=False path)."""
    card = _card(svc, agent_name="regent-11", is_platform=False, owned=False,
                 member_agent_names=frozenset({"regent-11"}))
    assert card.model_default is None
