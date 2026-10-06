"""The agent server never writes the runtime config files through its file
routes (abilityai/trinity-enterprise#823).

`~/.claude.json` and `~/.claude/.credentials.json` join the agent server's
by-name lists. Those two, Gemini's `~/.gemini/settings.json` and Codex's
`~/.tmp/codex/` are also matched on the RESOLVED target of PUT, mkdir and
DELETE, so a link elsewhere in the home that points at one is refused too.

Loads the SHIPPED router by path (it must stay standalone-importable, #1795)
and drives its handlers through a real FastAPI app over a temporary home.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTER = _REPO_ROOT / "docker/base-image/agent_server/routers/files.py"


def _load():
    spec = importlib.util.spec_from_file_location("_test823_agent_files", str(_ROUTER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary home with the four runtime config locations populated, and
    two links into them (`scratch -> .tmp/codex`, `g -> .gemini`)."""
    mod = _load()
    base = (tmp_path / "home").resolve()
    (base / ".claude").mkdir(parents=True)
    (base / ".claude/.credentials.json").write_text("LOGIN")
    (base / ".claude/agents").mkdir()
    (base / ".claude.json").write_text("{}")
    (base / ".gemini").mkdir()
    (base / ".gemini/settings.json").write_text("{}")
    (base / ".tmp/codex").mkdir(parents=True)
    (base / ".tmp/codex/auth.json").write_text("CODEX")
    (base / "scratch").symlink_to(base / ".tmp/codex")
    (base / "g").symlink_to(base / ".gemini")
    monkeypatch.setattr(mod, "_HOME", base)
    app = FastAPI()
    app.include_router(mod.router)
    return mod, base, TestClient(app, raise_server_exceptions=True)


def test_both_claude_code_files_are_on_both_name_lists():
    mod = _load()
    for name in (".claude.json", ".credentials.json"):
        assert name in mod.PROTECTED_PATHS
        assert name in mod.EDIT_PROTECTED_PATHS


@pytest.mark.parametrize("path", ["/home/developer/.claude.json", "/home/developer/.claude/.credentials.json"])
def test_the_claude_code_files_are_protected_by_name(path):
    mod = _load()
    assert mod._is_edit_protected_path(Path(path)) is True
    assert mod._is_protected_path(Path(path)) is True


@pytest.mark.parametrize("path", ["/home/developer/.claude/skills/x/SKILL.md", "/home/developer/.claude/agents/a.md"])
def test_ordinary_claude_files_stay_editable(path):
    mod = _load()
    assert mod._is_edit_protected_path(Path(path)) is False
    assert mod._is_protected_path(Path(path)) is False


@pytest.mark.parametrize("rel", [
    "scratch/config.toml",            # a link into .tmp/codex
    "g/settings.json",                # a link into .gemini
    ".tmp/codex/auth.json",
    ".gemini/settings.json",
    ".claude.json",
    ".claude/.credentials.json",
])
def test_put_never_reaches_runtime_config(home, rel):
    mod, base, client = home
    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    r = client.put("/api/files", params={"path": rel, "platform": "true"}, json={"content": "x"})
    assert r.status_code == 403, (rel, r.status_code, r.text)
    assert {p: p.read_bytes() for p in base.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("rel", ["scratch/x", ".tmp/codex/sub", "g/settings.json"])
def test_mkdir_never_reaches_runtime_config(home, rel):
    mod, base, client = home
    r = client.post("/api/files/mkdir", params={"path": rel})
    assert r.status_code == 403, (rel, r.status_code, r.text)
    assert not (base / ".tmp/codex/x").exists() and not (base / ".tmp/codex/sub").exists()


@pytest.mark.parametrize("rel,survivor", [
    (".tmp", ".tmp/codex/auth.json"),
    (".tmp/codex", ".tmp/codex/auth.json"),
    (".claude", ".claude/.credentials.json"),
    (".gemini", ".gemini/settings.json"),
    (".claude.json", ".claude.json"),
    (".claude/.credentials.json", ".claude/.credentials.json"),
])
def test_delete_never_removes_runtime_config(home, rel, survivor):
    mod, base, client = home
    r = client.delete("/api/files", params={"path": str(base / rel)})
    assert r.status_code == 403, (rel, r.status_code, r.text)
    assert (base / survivor).exists()


def test_an_ordinary_tmp_file_is_still_writable_and_deletable(home):
    mod, base, client = home
    r = client.put("/api/files", params={"path": ".tmp/scratch.txt"}, json={"content": "hello"})
    assert r.status_code == 200, r.text
    assert (base / ".tmp/scratch.txt").read_text() == "hello"
    r = client.delete("/api/files", params={"path": str(base / ".tmp/scratch.txt")})
    assert r.status_code == 200, r.text


def test_an_ordinary_claude_file_is_still_writable(home):
    mod, base, client = home
    r = client.put("/api/files", params={"path": ".claude/agents/a.md"}, json={"content": "agent"})
    assert r.status_code == 200, r.text


# ---- cross-layer: the two Claude Code entries are in every list -----------------

def test_the_claude_code_entries_are_in_every_layer():
    from services.agent_service import files as backend

    assert ".claude.json" in backend._FILE_WRITE_DENY_PATTERNS
    assert ".claude/.credentials.json" in backend._FILE_WRITE_DENY_PATTERNS
    baseline = json.loads((_REPO_ROOT / "docker/base-image/hooks/guardrails-baseline.json").read_text())
    assert ".claude.json" in baseline["path_deny"]
    assert "/home/developer/.claude/.credentials.json" in baseline["path_deny"]
    mod = _load()
    for lst in (mod.PROTECTED_PATHS, mod.EDIT_PROTECTED_PATHS):
        assert ".claude.json" in lst and ".credentials.json" in lst
