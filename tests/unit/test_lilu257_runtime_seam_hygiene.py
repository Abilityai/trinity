"""Abilityai/lilu#257 step 0 — runtime seam hygiene, before any OpenCode code.

Two defects that the Codex runtime already has and that a fourth runtime would
inherit verbatim:

  1. ``plugins_reinstall`` ran on EVERY runtime. Every subprocess it runs is
     ``claude plugin ...``; on a non-Claude agent that is two guaranteed-failing
     execs per boot plus a ``withheld:`` state file describing a capability the
     runtime does not have. Gated on ``is_claude_runtime()``.
  2. The Codex ``CLAUDE.md -> AGENTS.md`` mirror was a one-shot ``cp`` guarded
     by ``[ ! -f AGENTS.md ]``, so it never ran again — every later edit to
     CLAUDE.md left Codex reading a frozen snapshot of the agent's identity.
     Now a REFRESHED COPY with a provenance record (Abilityai/lilu#627 — a
     symlink is forbidden in the agent home by ent#819), refreshed at boot and
     at every Codex/OpenCode turn.

The predicate itself is pinned too: it must be case-insensitive (the #1187
casing trap) and it must be importable BOTH as a package member and standalone,
because ``plugins_reinstall`` is a ``python3 -m`` boot hook. An import fallback
that degrades to "assume Claude" would be a gate that silently does nothing,
which is why the name sets live in a zero-dependency leaf module.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"
_STARTUP = _ROOT / "docker" / "base-image" / "startup.sh"

pytestmark = pytest.mark.unit


def _load(name: str, path: Path):
    """Load an agent-server module by path with the agent-server dir on
    ``sys.path`` — the ``test_1704_plugins_reinstall.py`` idiom."""
    sys.path.insert(0, str(_AGENT_SERVER))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(_AGENT_SERVER))


@pytest.fixture(scope="module")
def names():
    return _load("_lilu257_runtime_names", _AGENT_SERVER / "runtime_names.py")


@pytest.fixture(scope="module")
def plugins():
    return _load("_lilu257_plugins_reinstall", _AGENT_SERVER / "plugins_reinstall.py")


# --------------------------------------------------------------------------
# 1. the predicate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),          # unset → the historical default
        ("", True),            # crud.py writes "" for "pins no runtime"
        ("claude-code", True),
        ("claude", True),
        ("Claude-Code", True),  # #1187 casing trap
        ("  claude  ", True),
        ("codex", False),
        ("Codex", False),
        ("gemini-cli", False),
        ("gemini", False),
        ("opencode", False),
    ],
)
def test_is_claude_runtime_classification(names, value, expected):
    assert names.is_claude_runtime(value) is expected


def test_is_claude_runtime_reads_env_when_unspecified(names, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "codex")
    assert names.is_claude_runtime() is False
    monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
    assert names.is_claude_runtime() is True
    monkeypatch.delenv("AGENT_RUNTIME")
    assert names.is_claude_runtime() is True


def test_runtime_names_leaf_has_no_package_imports():
    """The whole point of the leaf: a boot hook loaded outside the package
    must still get the REAL predicate, never a fallback stub."""
    src = (_AGENT_SERVER / "runtime_names.py").read_text()
    assert "from ." not in src and "from .." not in src


def test_runtime_adapter_reexports_the_leaf(names):
    """Existing importers of ``runtime_adapter.KNOWN_RUNTIMES`` keep working,
    and there is exactly one definition of the name sets."""
    src = (_AGENT_SERVER / "services" / "runtime_adapter.py").read_text()
    assert "from ..runtime_names import" in src
    # No second definition of the Claude set anywhere in the adapter.
    assert not re.search(r"_CLAUDE_RUNTIMES\s*=\s*frozenset", src)


# --------------------------------------------------------------------------
# 2. the plugin-reinstall gate
# --------------------------------------------------------------------------

def _no_subprocess(monkeypatch, plugins):
    def _boom(*a, **k):  # pragma: no cover — asserts it is never reached
        raise AssertionError(f"ran a subprocess on a non-Claude runtime: {a!r}")

    monkeypatch.setattr(plugins.subprocess, "run", _boom)


@pytest.mark.parametrize("runtime", ["codex", "gemini-cli", "gemini", "opencode"])
def test_reinstall_skips_non_claude_runtimes(plugins, monkeypatch, tmp_path, runtime):
    monkeypatch.setenv("AGENT_RUNTIME", runtime)
    monkeypatch.setattr(plugins, "STATE_FILE", tmp_path / "state.json", raising=False)
    _no_subprocess(monkeypatch, plugins)

    result = plugins.reinstall(manifest={"marketplaces": {"m": "github:o/r"},
                                         "installed": ["p@m"]})

    assert result["status"] == "skipped_runtime"
    assert result["installed"] == []
    assert result["added_marketplaces"] == []
    assert result["withheld"] == {}


def test_reinstall_skip_is_a_named_status_not_a_failure(plugins, monkeypatch, tmp_path):
    """'off because this runtime has no Claude plugins' and 'it broke' must
    not look alike — the #1929 withhold-with-a-reason contract."""
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")
    monkeypatch.setattr(plugins, "STATE_FILE", tmp_path / "state.json", raising=False)
    _no_subprocess(monkeypatch, plugins)

    result = plugins.reinstall(manifest={})
    assert result["status"] == "skipped_runtime"
    assert result["status"] not in ("no_manifest", "ok", "platform_defaults_only")


def test_reinstall_still_runs_on_claude(plugins, monkeypatch, tmp_path):
    """The gate must not disable the feature it guards."""
    monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
    monkeypatch.setattr(plugins, "STATE_FILE", tmp_path / "state.json", raising=False)
    monkeypatch.setattr(plugins, "merge_platform_defaults", lambda m: m)
    seen = []

    def _fake_run(args, **kwargs):
        seen.append(list(args))
        import types
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(plugins.subprocess, "run", _fake_run)
    result = plugins.reinstall(manifest={"marketplaces": {"m": "github:o/r"},
                                         "installed": []})
    assert result["status"] != "skipped_runtime"
    assert seen, "Claude runtime must still probe the CLI"


# --------------------------------------------------------------------------
# 3. the AGENTS.md mirror is a refreshed copy (Abilityai/lilu#627)
# --------------------------------------------------------------------------

def test_startup_mirror_is_a_copy_not_a_link():
    """ent#819: the platform creates no links in an agent home."""
    src = _STARTUP.read_text()
    assert "cp /home/developer/CLAUDE.md /home/developer/AGENTS.md" in src
    assert "ln -sfn CLAUDE.md /home/developer/AGENTS.md" not in src


def _run_mirror_block(home: Path) -> str:
    """Execute just the CLAUDE.md -> AGENTS.md block against a fake $HOME.

    Extracted from startup.sh by text so the test exercises the SHIPPED shell,
    not a re-typed copy of it, with `/home/developer` repointed at a tmpdir and
    the provenance record under `<home>/.tmp`.
    """
    src = _STARTUP.read_text()
    start = src.index('    _agents_md_record="${AGENT_TMPDIR}/agents-md.sha256"')
    # The block lives inside a `case … esac` arm, so it ends at that arm's `;;`.
    end = src.index("\n    ;;", start)
    block = src[start:end].replace("/home/developer", str(home))
    proc = subprocess.run(
        ["bash", "-c", block], capture_output=True, text=True, timeout=30,
        env={**os.environ, "AGENT_TMPDIR": str(home / ".tmp")},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_mirror_creates_a_regular_copy(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("v1\n")
    _run_mirror_block(tmp_path)

    copy = tmp_path / "AGENTS.md"
    assert copy.is_file() and not copy.is_symlink()
    assert copy.read_text() == "v1\n"
    assert (tmp_path / ".tmp" / "agents-md.sha256").is_file()


def test_mirror_refreshes_its_own_stale_copy(tmp_path):
    """The defect: a copy froze at first boot. Ours is refreshed."""
    (tmp_path / "CLAUDE.md").write_text("v1\n")
    _run_mirror_block(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("v2 — edited by a git sync\n")
    _run_mirror_block(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text() == "v2 — edited by a git sync\n"


def test_mirror_adopts_a_stale_identical_copy(tmp_path):
    """Upgrade path off the copy-era image: a regular AGENTS.md that is
    byte-identical to CLAUDE.md is provably our own mirror."""
    (tmp_path / "CLAUDE.md").write_text("v1\n")
    (tmp_path / "AGENTS.md").write_text("v1\n")
    _run_mirror_block(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("v2\n")
    _run_mirror_block(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text() == "v2\n"


def test_mirror_leaves_an_authored_agents_md_alone_but_says_so(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude instructions\n")
    (tmp_path / "AGENTS.md").write_text("hand-written codex instructions\n")
    out = _run_mirror_block(tmp_path)

    assert (tmp_path / "AGENTS.md").read_text() == "hand-written codex instructions\n"
    assert "did not write" in out.lower()


def test_mirror_replaces_a_leftover_symlink_with_a_copy(tmp_path):
    """The lilu#257-era link (or a dangling one) becomes a plain copy."""
    (tmp_path / "CLAUDE.md").write_text("v1\n")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    _run_mirror_block(tmp_path)
    assert not (tmp_path / "AGENTS.md").is_symlink()
    assert (tmp_path / "AGENTS.md").read_text() == "v1\n"
    assert (tmp_path / "CLAUDE.md").read_text() == "v1\n"


def test_mirror_noops_without_claude_md(tmp_path):
    _run_mirror_block(tmp_path)
    assert not (tmp_path / "AGENTS.md").exists()
