"""
Tests for the OpenCode terminal mode (#2981, checkpoint H — H3).

Before H an `opencode` terminal request fell through to `/bin/bash`. The
mode → argv mapping is now the pure `_cli_command(mode, model)`, and the
WebSocket handler hands its result to `api_exec_create` — the last test
drives the real handler with the Docker edges stubbed, so deleting that
hand-off turns it red.

The pinned OpenCode v1.18.32 TUI (`$0 [project]`, the default command)
declares `--model`/`-m` "in the format of provider/model":
packages/opencode/src/cli/cmd/tui.ts:81-85.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


def _cli_command(mode, model=None):
    from services.agent_service.terminal import _cli_command as fn
    return fn(mode, model)


class TestCliCommand:
    def test_opencode_without_model_launches_the_bare_tui(self):
        assert _cli_command("opencode") == ["opencode"]

    def test_opencode_passes_the_provider_model_flag(self):
        assert _cli_command("opencode", "anthropic/claude-sonnet-5") == [
            "opencode", "--model", "anthropic/claude-sonnet-5"]

    @pytest.mark.parametrize("mode,model,argv", [
        ("claude", None, ["claude", "--dangerously-skip-permissions"]),
        ("claude", "sonnet", ["claude", "--dangerously-skip-permissions", "--model", "sonnet"]),
        ("gemini", None, ["gemini"]),
        ("gemini", "gemini-2.5-flash", ["gemini", "--model", "gemini-2.5-flash"]),
        ("codex", None, ["codex"]),
        ("codex", "gpt-5.1-codex", ["codex", "-m", "gpt-5.1-codex"]),
        ("bash", None, ["/bin/bash"]),
        ("bash", "sonnet", ["/bin/bash"]),
        ("unknown", "x", ["/bin/bash"]),
    ])
    def test_other_modes_are_unchanged(self, mode, model, argv):
        assert _cli_command(mode, model) == argv


class _WS:
    def __init__(self):
        self.sent = []
        self._first = True

    async def accept(self):
        pass

    async def receive_text(self):
        if self._first:
            self._first = False
            return json.dumps({"type": "auth", "token": "t"})
        raise RuntimeError("client gone")

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=None, reason=None):
        pass


class TestHandlerHandOff:
    def test_an_opencode_session_execs_the_opencode_tui(self, monkeypatch):
        import services.agent_service.terminal as terminal

        execs = []

        async def fake_exec_create(container_id, cmd, **_k):
            execs.append(cmd)
            raise RuntimeError("stop after exec_create")

        async def fake_reload(_container):
            return None

        monkeypatch.setattr(terminal, "api_exec_create", fake_exec_create)
        monkeypatch.setattr(terminal, "container_reload", fake_reload)
        monkeypatch.setattr(terminal, "get_agent_container",
                            lambda *_a, **_k: SimpleNamespace(id="c1", status="running"))
        monkeypatch.setattr(terminal.db, "can_user_share_agent", lambda *_a, **_k: True)
        decode = lambda _t: {"sub": "op", "email": "op@example.com", "role": "operator"}  # noqa: E731

        manager = terminal.TerminalSessionManager()
        asyncio.run(manager.handle_terminal_session(
            _WS(), "a1", "opencode", decode, model="anthropic/claude-sonnet-5"))
        assert execs == [["opencode", "--model", "anthropic/claude-sonnet-5"]]
        assert manager._active_sessions == {}
