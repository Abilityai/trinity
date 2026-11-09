"""Socket doors apply the same account-state checks as HTTP requests
(abilityai/trinity-enterprise#863).

The agent terminal and the system-agent terminal authenticate through
`dependencies.decode_token`, the shared socket decoder, not through
`get_current_user`. These tests drive the real decoder and both real door
handlers. A deactivated account (`users.suspended_at` set, #995) is refused
before the door looks for a container. The same account with the column cleared
(`None`, and the empty string a hand edit can leave) is admitted, so the refusal
is caused by that one field and not by an ambient failure.

Related requirement: docs/memory/requirements/auth.md §2.10 (account state on
socket doors).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Values the real system would never hand back on its own, so an identity that
# carries them proves the stubbed account row is the one the decoder read.
_DEACTIVATED_AT = "2026-11-01T09:30:00Z"
_EMAIL = "door-owner-ent863@example.test"


class _AccountRows:
    """The two `db` reads the doors make before they look for a container."""

    def __init__(self, row: dict):
        self.row = row
        self.lookups: list[str] = []

    def get_user_by_username(self, username: str):
        self.lookups.append(username)
        return dict(self.row) if username == self.row["username"] else None

    def can_user_share_agent(self, _email, _agent_name):
        return True  # the account owns the agent


def _row(username: str, role: str, suspended_at) -> dict:
    return {
        "id": 7,
        "username": username,
        "email": _EMAIL,
        "role": role,
        "suspended_at": suspended_at,
    }


def _wire(monkeypatch, decoder, row: dict) -> tuple[_AccountRows, str]:
    """Point the decoder's own module globals at a fake account store.

    Patched through `decoder.__globals__`, the namespace the function really
    resolves `db` in, so a re-imported `dependencies` module elsewhere in the
    island cannot leave the stub unreached. Redis is absent, so the #187 and
    #3825 checks take their documented fail-open branch and say nothing about
    account state.
    """
    rows = _AccountRows(row)
    monkeypatch.setitem(decoder.__globals__, "db", rows)
    monkeypatch.setitem(decoder.__globals__, "get_breaker_redis", lambda: None)
    token = decoder.__globals__["create_access_token"](
        {"sub": row["username"]}, expires_delta=timedelta(hours=1)
    )
    return rows, token


class _FakeSocket:
    def __init__(self, token: str):
        self._inbox = [json.dumps({"type": "auth", "token": token})]
        self.sent: list[dict] = []
        self.closed = None

    async def accept(self):
        return None

    async def receive_text(self):
        return self._inbox.pop(0)

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def _decoder():
    import dependencies

    return dependencies.decode_token


# --------------------------------------------------------------------------- #
# T1.1 — the shared decoder
# --------------------------------------------------------------------------- #

class TestTheSocketDecoder:
    @pytest.mark.parametrize("role", ["creator", "admin"])
    def test_a_deactivated_account_gets_no_identity(self, monkeypatch, role):
        decode = _decoder()
        rows, token = _wire(monkeypatch, decode, _row(f"dec-{role}", role, _DEACTIVATED_AT))

        assert decode(token) is None
        assert rows.lookups == [f"dec-{role}"], "the refusal must come from the account row"

    @pytest.mark.parametrize("cleared", [None, ""])
    @pytest.mark.parametrize("role", ["creator", "admin"])
    def test_a_reactivated_account_gets_its_identity(self, monkeypatch, role, cleared):
        decode = _decoder()
        rows, token = _wire(monkeypatch, decode, _row(f"act-{role}", role, cleared))

        identity = decode(token)

        assert identity is not None
        assert (identity["sub"], identity["email"], identity["role"]) == (f"act-{role}", _EMAIL, role)
        assert rows.lookups == [f"act-{role}"]


# --------------------------------------------------------------------------- #
# T1.2 — the agent terminal door
# --------------------------------------------------------------------------- #

def _run_agent_terminal(monkeypatch, row: dict):
    import services.agent_service.terminal as terminal

    decode = _decoder()
    rows, token = _wire(monkeypatch, decode, row)
    monkeypatch.setattr(terminal, "db", rows)
    reached: list[str] = []
    monkeypatch.setattr(
        terminal, "get_agent_container", lambda name: reached.append(name)
    )
    sock = _FakeSocket(token)
    manager = terminal.TerminalSessionManager()
    asyncio.run(manager.handle_terminal_session(sock, "atlas-ent863", "bash", decode))
    return sock, reached, manager


class TestTheAgentTerminal:
    @pytest.mark.parametrize("role", ["creator", "admin"])
    def test_a_deactivated_account_gets_no_terminal(self, monkeypatch, role):
        sock, reached, manager = _run_agent_terminal(
            monkeypatch, _row(f"term-dec-{role}", role, _DEACTIVATED_AT)
        )

        assert sock.closed is not None and sock.closed[0] == 4001
        assert sock.sent and sock.sent[-1].get("close") is True
        assert reached == [], "refused before the container lookup"
        assert manager._active_sessions == {}

    @pytest.mark.parametrize("cleared", [None, ""])
    def test_a_reactivated_owner_reaches_the_container_lookup(self, monkeypatch, cleared):
        sock, reached, manager = _run_agent_terminal(
            monkeypatch, _row("term-act", "creator", cleared)
        )

        # Past every auth gate: the next refusal is the (stubbed) missing container.
        assert reached == ["atlas-ent863"]
        assert sock.closed is not None and sock.closed[0] == 4004
        assert manager._active_sessions == {}


def test_the_agent_terminal_route_authenticates_through_the_shared_decoder(monkeypatch):
    """The route itself, not only the session manager: the WebSocket endpoint
    hands the manager the shared decoder, so a deactivated account is refused
    there too."""
    import routers.agents as agents
    import services.agent_service.terminal as terminal

    rows, token = _wire(monkeypatch, agents.decode_token, _row("route-dec", "creator", _DEACTIVATED_AT))
    monkeypatch.setattr(terminal, "db", rows)
    reached: list[str] = []
    monkeypatch.setattr(terminal, "get_agent_container", lambda name: reached.append(name))
    manager = terminal.TerminalSessionManager()
    monkeypatch.setattr(agents, "_terminal_manager", manager)
    sock = _FakeSocket(token)

    asyncio.run(agents.agent_terminal(sock, "atlas-ent863", mode="bash", model=None))

    assert sock.closed is not None and sock.closed[0] == 4001
    assert reached == [], "refused before the container lookup"
    assert rows.lookups == ["route-dec"], "the refusal must come from the account row"
    assert manager._active_sessions == {}


# --------------------------------------------------------------------------- #
# T1.3 — the system-agent terminal door
# --------------------------------------------------------------------------- #

def _run_system_agent_terminal(monkeypatch, row: dict):
    import routers.system_agent as sa

    rows, token = _wire(monkeypatch, sa.decode_token, row)
    reached: list[str] = []
    monkeypatch.setattr(sa, "get_agent_container", lambda name: reached.append(name))
    sock = _FakeSocket(token)
    asyncio.run(sa.system_agent_terminal(sock, mode="bash"))
    return sock, reached, sa


class TestTheSystemAgentTerminal:
    def test_a_deactivated_admin_gets_no_terminal(self, monkeypatch):
        sock, reached, sa = _run_system_agent_terminal(
            monkeypatch, _row("sys-dec-admin", "admin", _DEACTIVATED_AT)
        )

        assert sock.closed is not None and sock.closed[0] == 4001
        assert sock.sent and sock.sent[-1].get("close") is True
        assert reached == [], "refused before the container lookup"
        assert _EMAIL not in sa._active_terminal_sessions

    @pytest.mark.parametrize("cleared", [None, ""])
    def test_a_reactivated_admin_reaches_the_container_lookup(self, monkeypatch, cleared):
        sock, reached, sa = _run_system_agent_terminal(
            monkeypatch, _row("sys-act-admin", "admin", cleared)
        )

        assert reached == [sa.SYSTEM_AGENT_NAME]
        assert sock.closed is not None and sock.closed[0] == 4004
        assert _EMAIL not in sa._active_terminal_sessions
