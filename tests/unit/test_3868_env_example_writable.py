"""#3868 — an agent may write its own `.env.example`.

`.env.example` holds variable names only, and the compatibility check requires
one for every `${VAR}` an agent declares (K-001, F-004). The guardrail baseline
denies `.env.*`, which matched it too, so a builder agent could not produce the
file the platform demands. `path_allow` lifts the basename deny for that one
name; real `.env` files, absolute protected dirs and operator rules still deny.

Everything here runs the SHIPPED hook (`docker/base-image/hooks/file-guardrail.py`)
against the shipped `guardrails-baseline.json`.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "hooks"
BASELINE = json.loads((HOOKS / "guardrails-baseline.json").read_text())


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))
    lib_spec = importlib.util.spec_from_file_location("lib", HOOKS / "lib.py")
    lib = importlib.util.module_from_spec(lib_spec)
    lib_spec.loader.exec_module(lib)
    monkeypatch.setattr(lib, "LOG_PATH", str(tmp_path / "guardrails.jsonl"))
    monkeypatch.setitem(sys.modules, "lib", lib)
    spec = importlib.util.spec_from_file_location("_file_guardrail_3868", HOOKS / "file-guardrail.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "load_config", lambda: BASELINE)
    monkeypatch.setenv("HOME", "/home/developer")
    return mod


def _exit_code(hook, monkeypatch, file_path, tool="Write"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    payload = {"tool_name": tool, "tool_input": {key: file_path}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        hook.run_hook(hook.main)
    return exc.value.code


def test_the_baseline_allows_env_example():
    assert BASELINE["path_allow"] == [".env.example"]


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
@pytest.mark.parametrize("path", [
    ".env.example",
    "~/.env.example",
    "/home/developer/.env.example",
    "/home/developer/agents/new-agent/.env.example",
])
def test_env_example_is_writable(hook, monkeypatch, tool, path):
    assert _exit_code(hook, monkeypatch, path, tool) == 0


@pytest.mark.parametrize("path", [
    ".env",
    ".env.local",
    ".env.production",
    ".env.example.bak",
    "/home/developer/sub/.env.local",
    # an absolute protected dir still wins over the allowed name
    "/home/developer/.ssh/.env.example",
    "/opt/trinity/.env.example",
    "/etc/claude-code/.env.example",
])
def test_other_env_files_and_protected_dirs_stay_denied(hook, monkeypatch, path):
    assert _exit_code(hook, monkeypatch, path) == 2


def test_an_operator_rule_still_denies_env_example(hook, monkeypatch):
    monkeypatch.setattr(hook, "load_config", lambda: {**BASELINE, "extra_path_deny": [".env.example"]})
    assert _exit_code(hook, monkeypatch, "/home/developer/.env.example") == 2


def test_no_path_allow_in_config_means_deny(hook, monkeypatch):
    config = {k: v for k, v in BASELINE.items() if k != "path_allow"}
    monkeypatch.setattr(hook, "load_config", lambda: config)
    assert _exit_code(hook, monkeypatch, "/home/developer/.env.example") == 2


def test_a_symlinked_env_example_is_denied(hook, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SECRET=x\n")
    link = tmp_path / ".env.example"
    link.symlink_to(tmp_path / ".env")
    assert _exit_code(hook, monkeypatch, str(link)) == 2


@pytest.mark.parametrize("path", [".ENV.EXAMPLE", "/home/developer/.env.example/notes.md"])
def test_lookalikes_keep_their_existing_verdict(hook, monkeypatch, path):
    # fnmatch is case-sensitive on Linux and a dir's children match by their own
    # basename, so neither was ever denied; the allow adds no new door here.
    assert _exit_code(hook, monkeypatch, path) == 0
