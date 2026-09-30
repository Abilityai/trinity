"""#3105: the Bash guardrail denies sudo routes to the guardrail registration,
and agent /health re-checks the registration on every request.

`developer` holds NOPASSWD sudo by design, so these are a speed bump against a
determined agent (variables, base64 or a script file get past a regex). They
exist so the obvious spellings are denied and logged.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BASE = Path(__file__).resolve().parents[2] / "docker" / "base-image"
_BASELINE = json.loads((_BASE / "hooks" / "guardrails-baseline.json").read_text())
_PATTERNS = [re.compile(e["pattern"]) for e in _BASELINE["bash_deny"]]


def _denied(command: str) -> bool:
    return any(p.search(command) for p in _PATTERNS)


@pytest.mark.parametrize("command", [
    "echo {} | sudo tee /etc/claude-code/managed-settings.json",
    "sudo rm /etc/claude-code/managed-settings.json",
    "sudo chmod 666 /etc/claude-code/managed-settings.json",
    "sudo cp evil.py /opt/trinity/hooks/bash-guardrail.py",
    "sudo sed -i s/a/b/ /opt/trinity/hooks/lib.py",
    "cd /etc/claude-code && sudo tee managed-settings.json",
    "echo 'x ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/x",
    "sudo visudo",
    "sudo sh -c '\n  echo {} > /etc/claude-code/managed-settings.json'",
])
def test_sudo_touching_the_registration_is_denied(command):
    assert _denied(command)


@pytest.mark.parametrize("command", [
    "sudo -i", "sudo -s", "sudo -is", "sudo -E -s", "sudo -u root -i",
    "sudo --login", "sudo --shell", "sudo -i tee /tmp/x",
    "sudo su", "sudo su -", "sudo -u root su",
    "sudo bash", "sudo bash -i", "sudo sh", "sudo zsh -l",
    "sudo /bin/bash", "sudo /usr/bin/su -", "/bin/su -",
    "su", "su -", "su root", "echo developer | su -c id", "ls; su -",
])
def test_root_shells_are_denied(command):
    assert _denied(command)


@pytest.mark.parametrize("command", [
    "sudo apt-get install -y jq",
    "sudo apt-get install -s jq",
    "sudo pip install -i https://example.com/simple pkg",
    "sudo bash script.sh",
    "sudo sh -c 'apt-get update'",
    "sudo -u postgres psql",
    "ls /opt/trinity/hooks",
    "cat /etc/claude-code/managed-settings.json",
    "git commit -m 'sum of parts'",
    "grep -r su src/",
])
def test_ordinary_commands_still_pass(command):
    assert not _denied(command)


# --- /health registration check -------------------------------------------

def _load_check(path: str):
    """Pull `_guardrails_registration` out of info.py without importing the
    agent server (its imports need the container)."""
    src = (_BASE / "agent_server" / "routers" / "info.py").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_guardrails_registration")
    ns = {"os": os, "GUARDRAIL_REGISTRATION": path}
    exec(compile(ast.Module([fn], []), "info.py", "exec"), ns)
    return ns["_guardrails_registration"]


def test_health_exposes_the_check():
    src = (_BASE / "agent_server" / "routers" / "info.py").read_text()
    assert '"guardrails_registration": _guardrails_registration()' in src
    assert 'GUARDRAIL_REGISTRATION = "/etc/claude-code/managed-settings.json"' in src


def test_missing(tmp_path):
    assert _load_check(str(tmp_path / "nope.json"))() == "missing"


def test_not_root_owned(tmp_path):
    f = tmp_path / "m.json"
    f.write_text("{}")
    if os.getuid() == 0:
        pytest.skip("running as root")
    assert _load_check(str(f))() == "not_root_owned"


def test_writable_and_ok(tmp_path, monkeypatch):
    f = tmp_path / "m.json"
    f.write_text("{}")
    real_stat = os.stat

    class _RootOwned:
        def __init__(self, st):
            self.st_uid = 0

    monkeypatch.setattr(os, "stat", lambda p, *a, **k: _RootOwned(real_stat(p)))
    check = _load_check(str(f))
    f.chmod(0o644)
    assert check() == "writable"
    f.chmod(0o444)
    if os.getuid() != 0:
        assert check() == "ok"


@pytest.mark.parametrize("command", [
    "sudo " + "-u " * 5000 + "true; sudo -i",
    "sudo " * 40000,
    "sudo " + "-E " * 5000 + "x",
])
def test_patterns_do_not_backtrack(command):
    """A hook that runs past its timeout does not deny, so match time must stay
    linear. Each sudo option token must have exactly one way to match; do not
    let `-u` match both the argument-taking and the bare-flag alternative."""
    import time
    start = time.monotonic()
    _denied(command)
    assert time.monotonic() - start < 1.0
