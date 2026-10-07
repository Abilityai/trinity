"""trinity-enterprise#792 — the in-agent file guardrail reads `//` as `/` too.

`posixpath.normpath` keeps exactly two leading slashes (POSIX leaves `//`
implementation-defined). The PreToolUse file guardrail normalised `file_path`
with it and then matched the baseline's absolute patterns such as
`/home/developer/.ssh/*`, so a `//`-prefixed path matched none of them while the
filesystem writes the real file. The backend file routes had the same shape
(`services/agent_service/files.py`, KEEP IN SYNC with this hook); this pins the
agent-side copy.

Everything here runs the SHIPPED hook — `docker/base-image/hooks/file-guardrail.py`,
its `main()` through `run_hook`, against the shipped `guardrails-baseline.json`.
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
    # The hook prepends /opt/trinity/hooks to sys.path and imports `lib`; keep
    # both out of every other test.
    monkeypatch.setattr(sys, "path", list(sys.path))
    lib_spec = importlib.util.spec_from_file_location("lib", HOOKS / "lib.py")
    lib = importlib.util.module_from_spec(lib_spec)
    lib_spec.loader.exec_module(lib)
    monkeypatch.setattr(lib, "LOG_PATH", str(tmp_path / "guardrails.jsonl"))
    monkeypatch.setitem(sys.modules, "lib", lib)
    spec = importlib.util.spec_from_file_location("_file_guardrail_792", HOOKS / "file-guardrail.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # In the image the config is /opt/trinity/guardrails-{runtime,baseline}.json.
    monkeypatch.setattr(mod, "load_config", lambda: BASELINE)
    return mod


def _exit_code(hook, monkeypatch, file_path):
    payload = {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": "x"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        hook.run_hook(hook.main)
    return exc.value.code


def test_the_baseline_carries_the_absolute_patterns_these_rows_need():
    for pattern in ("/home/developer/.ssh/*", "/home/developer/.claude/settings.json", "/opt/trinity/*"):
        assert pattern in BASELINE["path_deny"]


def test_two_slashes_normalise_to_one(hook):
    assert hook._normalise("//home/developer/x") == "/home/developer/x"


@pytest.mark.parametrize("slashes", ["/", "//", "///"])
@pytest.mark.parametrize("rest", [
    "home/developer/.ssh/authorized_keys",
    "home/developer/.claude/settings.json",
    "home/developer/.claude/settings.local.json",
    "home/developer/.aws/credentials",
    "opt/trinity/hooks/lib.py",
    "etc/claude-code/managed-settings.json",
])
def test_a_protected_path_is_denied_whatever_the_leading_slashes(hook, monkeypatch, slashes, rest):
    assert _exit_code(hook, monkeypatch, slashes + rest) == 2


@pytest.mark.parametrize("path", ["//home/developer/notes.md", "//home/developer/.claude/agents/x.md"])
def test_an_ordinary_path_with_two_slashes_is_still_allowed(hook, monkeypatch, path):
    assert _exit_code(hook, monkeypatch, path) == 0
