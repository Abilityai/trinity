"""
Gated skills, the in-container hook — is it in force? (trinity-enterprise#752)

Target: the agent server's ``/health`` field ``skill_gate_hook``
(``docker/base-image/agent_server/routers/info.py``). A gated agent running an
image without the hook, or a runtime with no hooks at all (Codex, Gemini — D4),
is not enforced in-container; ent#753's UI needs a way to say so. Enum only,
like ``guardrails_registration`` (#3105): ``/health`` is unauthenticated and
reachable by every peer on the agent network.

The real module is imported (the unit conftest installs the agent server);
root ownership cannot be created without root, so the stat is shimmed for the
two cases that need it, the #3105 way.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import asyncio
import os

import pytest

from agent_server.routers import info
from agent_server.state import agent_state

pytestmark = pytest.mark.unit


@pytest.fixture
def files(tmp_path, monkeypatch):
    d = tmp_path / "managed-settings.d"
    d.mkdir()
    reg = d / "50-skill-gate.json"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    boot, module = hooks / "skill-gate.py", hooks / "_skill_gate.py"
    for f in (reg, boot, module):
        f.write_text("x")
    monkeypatch.setattr(info, "SKILL_GATE_REGISTRATION", str(reg))
    monkeypatch.setattr(info, "SKILL_GATE_HOOK_FILES", (str(boot), str(module)))
    monkeypatch.setattr(agent_state, "agent_runtime", "claude-code")
    yield {"dir": d, "reg": reg, "boot": boot, "module": module}
    d.chmod(0o755)


def _root_owned(monkeypatch):
    real = os.stat

    class _St:
        def __init__(self, st):
            self.st_uid = 0
            self.st_mode = st.st_mode

    monkeypatch.setattr(os, "stat", lambda p, *a, **k: _St(real(p, *a, **k)))


def test_health_reports_the_field():
    body = asyncio.run(info.health_check())
    assert body["skill_gate_hook"] == info._skill_gate_hook()
    assert body["skill_gate_hook"] in {"ok", "missing", "not_root_owned", "writable",
                                       "unsupported_runtime"}


@pytest.mark.parametrize("which", ["reg", "boot", "module"])
def test_any_missing_piece_is_missing(files, which):
    files[which].unlink()
    assert info._skill_gate_hook() == "missing"


def test_files_the_agent_owns_are_not_a_registration(files):
    if os.getuid() == 0:
        pytest.skip("running as root")
    assert info._skill_gate_hook() == "not_root_owned"


def test_a_writable_piece_or_directory_is_reported(files, monkeypatch):
    _root_owned(monkeypatch)
    for f in (files["reg"], files["boot"], files["module"]):
        f.chmod(0o444)
    files["dir"].chmod(0o555)
    if os.getuid() == 0:
        pytest.skip("root can write anything")
    assert info._skill_gate_hook() == "ok"
    files["dir"].chmod(0o755)
    assert info._skill_gate_hook() == "writable"
    files["dir"].chmod(0o555)
    files["boot"].chmod(0o644)
    assert info._skill_gate_hook() == "writable"


@pytest.mark.parametrize("runtime", ["claude", "Claude-Code"])
def test_every_spelling_of_the_claude_runtime_is_checked(files, monkeypatch, runtime):
    """`AGENT_RUNTIME=claude` runs Claude Code (runtime_adapter._CLAUDE_RUNTIMES)."""
    monkeypatch.setattr(agent_state, "agent_runtime", runtime)
    assert info._skill_gate_hook() != "unsupported_runtime"


@pytest.mark.parametrize("runtime", ["gemini-cli", "codex"])
def test_a_runtime_without_hooks_says_so(files, monkeypatch, runtime):
    monkeypatch.setattr(agent_state, "agent_runtime", runtime)
    assert info._skill_gate_hook() == "unsupported_runtime"


def test_the_paths_are_where_the_image_installs_them():
    assert info.SKILL_GATE_REGISTRATION == "/etc/claude-code/managed-settings.d/50-skill-gate.json"
    assert info.SKILL_GATE_HOOK_FILES == ("/opt/trinity/hooks/skill-gate.py",
                                          "/opt/trinity/hooks/_skill_gate.py")
