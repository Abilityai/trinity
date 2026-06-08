"""
Unit tests for deploy-local validation hardening (#950).

Covers the two pure functions in `services/template_service.py` that the
deferred #950 acceptance items added:

  - `is_trinity_compatible`: now hard-fails a deploy whose archive is missing
    a usable CLAUDE.md (missing / empty / whitespace-only / non-UTF-8) so the
    operator gets a clean 400 instead of an empty agent on first interaction.
  - `collect_mcp_credential_warnings`: advisory warnings for MCP servers whose
    ${VAR} references have no matching credential and aren't platform-injected.

Both load `template_service.py` standalone (no backend connection) — the only
backend import it pulls is `config`, stubbed via `monkeypatch.setitem`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_template_service(monkeypatch):
    """Load template_service.py as a standalone module with `config` stubbed.

    Uses monkeypatch.setitem (not a bare `sys.modules[...] =`) so the
    tests/lint_sys_modules.py baseline check stays happy and the stub is undone
    on teardown.
    """
    if "config" not in sys.modules:
        config_mod = types.ModuleType("config")
        config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
        config_mod.GITHUB_PAT_CREDENTIAL_ID = "test-pat"
        monkeypatch.setitem(sys.modules, "config", config_mod)

    spec = importlib.util.spec_from_file_location(
        "ts_deploy_validation", _BACKEND / "services" / "template_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_VALID_TEMPLATE_YAML = """
name: test-agent
display_name: Test Agent
resources:
  cpu: "1"
  memory: "2g"
"""


def _seed_agent_dir(parent: Path, *, claude_md=None, template_yaml=_VALID_TEMPLATE_YAML):
    """Create an agent directory. `claude_md`:
      - None  -> no CLAUDE.md written
      - str   -> written as UTF-8 text
      - bytes -> written raw (for the non-UTF-8 case)
    """
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "template.yaml").write_text(template_yaml)
    if isinstance(claude_md, bytes):
        (parent / "CLAUDE.md").write_bytes(claude_md)
    elif isinstance(claude_md, str):
        (parent / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return parent


# ---------------------------------------------------------------------------
# is_trinity_compatible — CLAUDE.md hard-fail (#950)
# ---------------------------------------------------------------------------

def test_missing_claude_md_rejected(tmp_path, monkeypatch):
    """A valid template.yaml with no CLAUDE.md is no longer compatible."""
    ts = _load_template_service(monkeypatch)
    agent_dir = _seed_agent_dir(tmp_path / "agent", claude_md=None)

    is_compatible, error, data = ts.is_trinity_compatible(agent_dir)

    assert is_compatible is False
    assert data is None
    assert "CLAUDE.md" in error


def test_empty_claude_md_rejected(tmp_path, monkeypatch):
    """A zero-byte CLAUDE.md is rejected."""
    ts = _load_template_service(monkeypatch)
    agent_dir = _seed_agent_dir(tmp_path / "agent", claude_md="")

    is_compatible, error, data = ts.is_trinity_compatible(agent_dir)

    assert is_compatible is False
    assert "CLAUDE.md" in error


def test_whitespace_only_claude_md_rejected(tmp_path, monkeypatch):
    """A CLAUDE.md that is only whitespace is rejected."""
    ts = _load_template_service(monkeypatch)
    agent_dir = _seed_agent_dir(tmp_path / "agent", claude_md="   \n\t  \n")

    is_compatible, error, data = ts.is_trinity_compatible(agent_dir)

    assert is_compatible is False
    assert "CLAUDE.md" in error


def test_binary_claude_md_rejected_without_raising(tmp_path, monkeypatch):
    """A non-UTF-8 CLAUDE.md is rejected cleanly (no exception → no HTTP 500).

    This is the outside-voice [P2] guard: a binary CLAUDE.md must yield a
    clean (False, ...) result, NOT a UnicodeDecodeError bubbling up to the
    generic 500 handler in deploy.py.
    """
    ts = _load_template_service(monkeypatch)
    # 0xFF 0xFE is an invalid UTF-8 lead-byte sequence.
    agent_dir = _seed_agent_dir(tmp_path / "agent", claude_md=b"\xff\xfe\x00\x01\x02binary")

    is_compatible, error, data = ts.is_trinity_compatible(agent_dir)

    assert is_compatible is False
    assert data is None
    assert error  # a non-empty, human-readable reason


def test_present_claude_md_accepted(tmp_path, monkeypatch):
    """A valid template.yaml + non-empty CLAUDE.md is compatible."""
    ts = _load_template_service(monkeypatch)
    agent_dir = _seed_agent_dir(
        tmp_path / "agent", claude_md="# Test Agent\nDo useful things."
    )

    is_compatible, error, data = ts.is_trinity_compatible(agent_dir)

    assert is_compatible is True
    assert error is None
    assert data is not None and data.get("name") == "test-agent"


def test_unicode_claude_md_accepted(tmp_path, monkeypatch):
    """A CLAUDE.md with non-ASCII (but valid UTF-8) content is accepted."""
    ts = _load_template_service(monkeypatch)
    agent_dir = _seed_agent_dir(
        tmp_path / "agent", claude_md="# Тест 测试 🚀\nInstructions."
    )

    is_compatible, error, _ = ts.is_trinity_compatible(agent_dir)

    assert is_compatible is True
    assert error is None


# ---------------------------------------------------------------------------
# collect_mcp_credential_warnings — advisory MCP credential gaps (#950)
# ---------------------------------------------------------------------------

def _write_mcp_template(parent: Path, body: str):
    parent.mkdir(parents=True, exist_ok=True)
    (parent / ".mcp.json.template").write_text(body)


def test_no_mcp_config_no_warnings(tmp_path, monkeypatch):
    """A template with no .mcp.json[.template] produces no warnings."""
    ts = _load_template_service(monkeypatch)
    tmp_path.mkdir(parents=True, exist_ok=True)

    assert ts.collect_mcp_credential_warnings(tmp_path) == []


def test_all_credentials_satisfied_no_warnings(tmp_path, monkeypatch):
    """When every ${VAR} has a matching .env key, no warnings are emitted."""
    ts = _load_template_service(monkeypatch)
    _write_mcp_template(
        tmp_path,
        """
        {"mcpServers": {"heygen": {"command": "x", "env": {"KEY": "${HEYGEN_API_KEY}"}}}}
        """,
    )
    (tmp_path / ".env").write_text("HEYGEN_API_KEY=real-value\n")

    assert ts.collect_mcp_credential_warnings(tmp_path) == []


def test_platform_injected_vars_never_warn(tmp_path, monkeypatch):
    """Platform-injected vars (exact + prefix) must not produce false warnings.

    Exercises both the exact set (ANTHROPIC_API_KEY) and the prefix rule
    (TRINITY_MCP_API_KEY, TRINITY_GIT_BASE_URL) — the outside-voice [P2]
    false-positive guard. No .env at all: these would warn if not allowlisted.
    """
    ts = _load_template_service(monkeypatch)
    _write_mcp_template(
        tmp_path,
        """
        {"mcpServers": {"trinity": {"command": "x", "env": {
            "TRINITY_MCP_API_KEY": "${TRINITY_MCP_API_KEY}",
            "BASE": "${TRINITY_GIT_BASE_URL}",
            "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"
        }}}}
        """,
    )

    assert ts.collect_mcp_credential_warnings(tmp_path) == []


def test_unsatisfied_var_warns(tmp_path, monkeypatch):
    """A user var with no matching .env key produces one warning naming it."""
    ts = _load_template_service(monkeypatch)
    _write_mcp_template(
        tmp_path,
        """
        {"mcpServers": {"heygen": {"command": "x", "env": {"KEY": "${HEYGEN_API_KEY}"}}}}
        """,
    )
    # No .env / HEYGEN_API_KEY anywhere.

    warnings = ts.collect_mcp_credential_warnings(tmp_path)

    assert len(warnings) == 1
    assert "heygen" in warnings[0]
    assert "${HEYGEN_API_KEY}" in warnings[0]


def test_one_warning_per_unsatisfied_var(tmp_path, monkeypatch):
    """Two unsatisfied vars across servers → two warnings; satisfied ones skipped."""
    ts = _load_template_service(monkeypatch)
    _write_mcp_template(
        tmp_path,
        """
        {"mcpServers": {
            "heygen": {"command": "x", "env": {"K": "${HEYGEN_API_KEY}"}},
            "blotato": {"command": "y", "env": {"K": "${BLOTATO_API_KEY}"}},
            "ok": {"command": "z", "env": {"K": "${SATISFIED_KEY}"}}
        }}
        """,
    )
    (tmp_path / ".env").write_text("SATISFIED_KEY=present\n")

    warnings = ts.collect_mcp_credential_warnings(tmp_path)

    assert len(warnings) == 2
    joined = "\n".join(warnings)
    assert "${HEYGEN_API_KEY}" in joined
    assert "${BLOTATO_API_KEY}" in joined
    assert "SATISFIED_KEY" not in joined


def test_malicious_server_name_control_chars_stripped(tmp_path, monkeypatch):
    """An MCP server name with control chars / ANSI escapes must not appear
    raw in the operator-facing warning (#950 L1 — terminal-escape hardening).

    The server name is an arbitrary, operator-supplied JSON key. A crafted
    template could embed ESC sequences / newlines that hijack the operator's
    terminal when `/trinity:onboard` renders `warnings[]`. The warning string
    must carry only printable characters; the useful info (the var name) is
    preserved.
    """
    ts = _load_template_service(monkeypatch)
    # JSON escapes decode to *raw* control bytes after json.loads:
    #   [31m -> ESC[31m (red), \n -> newline, \r -> carriage return.
    _write_mcp_template(
        tmp_path,
        '{"mcpServers": {"evil\\u001b[31m\\r\\ninjected": '
        '{"command": "x", "env": {"K": "${UNSET_KEY}"}}}}',
    )

    warnings = ts.collect_mcp_credential_warnings(tmp_path)

    assert len(warnings) == 1
    w = warnings[0]
    assert "\x1b" not in w, f"raw ESC leaked: {w!r}"
    assert "\n" not in w, f"raw newline leaked: {w!r}"
    assert "\r" not in w, f"raw CR leaked: {w!r}"
    # The actionable detail (which credential is missing) survives sanitization.
    assert "${UNSET_KEY}" in w


def test_overlong_server_name_truncated(tmp_path, monkeypatch):
    """An absurdly long server name is bounded, not echoed whole (#950 L1 —
    output-flooding defense). MCP server names are short identifiers; a
    500-char name is hostile input."""
    ts = _load_template_service(monkeypatch)
    long_name = "a" * 500
    _write_mcp_template(
        tmp_path,
        '{"mcpServers": {"%s": {"command": "x", "env": {"K": "${UNSET_KEY}"}}}}'
        % long_name,
    )

    warnings = ts.collect_mcp_credential_warnings(tmp_path)

    assert len(warnings) == 1
    assert long_name not in warnings[0], "full 500-char name echoed unbounded"
    assert "${UNSET_KEY}" in warnings[0]


# ---------------------------------------------------------------------------
# sanitize_agent_name traversal invariant (#950 / PR #982 CodeQL py/path-injection)
#
# The deploy-local path that feeds collect_mcp_credential_warnings builds
# `dest_path = templates_dir / version_name`, where `version_name` derives from
# the operator-supplied agent name via `sanitize_agent_name`. CodeQL flags the
# downstream `.exists()` as path-injection because it cannot model the regex
# sanitizer as a barrier (deploy.py adds an explicit normalize+containment guard
# for that). These tests assert the *upstream* guarantee that makes the alert a
# false positive: sanitize_agent_name can never yield a value that traverses out
# of a single path component. If this property ever regresses, the deploy guard
# is the only thing standing between operator input and the filesystem.
#
# get_next_version_name only ever appends a `-{int}` suffix (digits + hyphen, no
# separators), so testing sanitize_agent_name alone covers the traversal surface.
# ---------------------------------------------------------------------------


def _load_helpers(monkeypatch):
    """Load utils/helpers.py standalone (stdlib-only imports, no backend deps)."""
    spec = importlib.util.spec_from_file_location(
        "helpers_deploy_validation", _BACKEND / "utils" / "helpers.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TRAVERSAL_INPUTS = [
    "../../etc/passwd",
    "a/../../b",
    "..",
    "../",
    "foo/bar",
    "foo\\bar",
    "....//....//etc",
    "/abs/path",
    "..%2f..%2fetc",
    "  ../sneaky  ",
    "名前/../etc",  # unicode + traversal
    "‮../rtl",  # right-to-left override + traversal
]


def test_sanitize_agent_name_never_yields_traversal(monkeypatch):
    """sanitize_agent_name output is always a single safe path component.

    For every hostile input, the sanitized name must contain no path separator,
    no `..` component, and equal its own basename — so `templates_dir / name`
    cannot escape templates_dir.
    """
    import os

    helpers = _load_helpers(monkeypatch)
    for raw in _TRAVERSAL_INPUTS:
        name = helpers.sanitize_agent_name(raw)
        # No separators of any flavor survive.
        assert "/" not in name, f"{raw!r} -> {name!r} kept a forward slash"
        assert "\\" not in name, f"{raw!r} -> {name!r} kept a backslash"
        assert os.sep not in name, f"{raw!r} -> {name!r} kept os.sep"
        # No path component is a parent-dir reference.
        assert ".." not in name.split("/"), f"{raw!r} -> {name!r} kept a '..' component"
        # Single component: joining onto a base cannot climb out.
        assert name == os.path.basename(name), f"{raw!r} -> {name!r} is not a basename"
        # Containment holds when joined onto a base directory.
        base = "/data/deployed-templates"
        joined = os.path.normpath(os.path.join(base, name))
        assert joined == base or joined.startswith(base + os.sep), (
            f"{raw!r} -> {name!r} escaped base when joined: {joined!r}"
        )


def test_sanitize_agent_name_preserves_legitimate_names(monkeypatch):
    """A normal agent name passes through to a clean lowercase slug (no false
    rejection of the happy path)."""
    helpers = _load_helpers(monkeypatch)
    assert helpers.sanitize_agent_name("My Cool Agent") == "my-cool-agent"
    assert helpers.sanitize_agent_name("data.pipeline_v2") == "data.pipeline_v2"
