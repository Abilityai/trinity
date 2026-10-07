"""
GUARD-002 hooks: no agent-writable input reaches them (trinity-enterprise#787).

The four GUARD-002 hooks (bash-guardrail, file-guardrail, read-only-guard,
output-scanner) used to register in SHELL form under `/usr/bin/python3` with no
`-I`, so three things the agent's own workspace controls reached them with no
`sudo`: `CLAUDE_CODE_SHELL_PREFIX` (settings `env` or `.env`) wrapped the hook
command, `PYTHONPATH` from settings `env` reached the interpreter, and the user
site (`usercustomize`, `.pth`) ran inside the hook. Separately, read-only mode
is switched by `~/.trinity/read-only-config.json`, a file in the agent's own
home, which the agent can delete.

What is pinned here, and how:
  * The registration's exact shape, and a PORT of CLI 2.1.281's hook spawn
    branch (`_F`: exec form is spawned with no shell; the prefix wraps only an
    entry with no `args`, and only when it is truthy). Moving the image's CLI
    pin fails `test_the_spawn_facts_were_read_from_the_cli_the_image_pins`.
  * Each hook RUN under its registered flags and env: it still denies and
    allows what its tables say.
  * A hostile `PYTHONPATH` and a user-site `usercustomize.py` flip a deny to an
    allow under the OLD invocation (the precondition, asserted) and do not
    under the registered flags.
  * Read-only config lives at a root-owned path that, when present, decides
    for the hook and the Codex runtime whatever the agent-owned home copy
    says; the home copy decides only while the root file is missing (after a
    recreate, before the start-time sync). The backend writes the root file
    through a root exec whose argv carries the config as data.
  * startup.sh's legacy cleanup, extracted and run under bash, removes every
    shipped `claude-settings.json` and nothing else.

CI does not build the image. The build runs the hooks under the registered
argv (Dockerfile smoke) and /verify-local proves the CLI loads the file.
Related flow: docs/memory/feature-flows/agent-guardrails.md
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _ent787_hook_harness as H  # noqa: E402

_BACKEND = H.REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

_DOCKERFILE = (H.BASE / "Dockerfile").read_text()
_STARTUP = (H.BASE / "startup.sh").read_text()
_SKILL_GATE_DROP_IN = H.HOOKS / "managed-settings.d" / "50-skill-gate.json"
_FIXTURES = _HERE / "fixtures" / "ent787"

# The CLI build every fact in this file was read from (offsets in the 2.1.281
# binary: hook spawn `_F`, prefix gate `Yt?n9(Yt,Pt):Pt`, Bash-tool `if(gt)`).
_CLI_VERIFIED = "2.1.281"

ROOT_READ_ONLY_CONFIG = "/opt/trinity/read-only-config.json"
LEGACY_READ_ONLY_CONFIG = "/home/developer/.trinity/read-only-config.json"


def _argv(hook):
    return ["-i", "HOME=/home/developer", "/usr/local/bin/python3", "-I", "-S",
            f"/opt/trinity/hooks/{hook}"]


def _entry(hook):
    return {"type": "command", "command": "/usr/bin/env", "args": _argv(hook), "timeout": 30}


EXPECTED = {
    "env": {"CLAUDE_CODE_SHELL_PREFIX": ""},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [_entry("bash-guardrail.py")]},
            {"matcher": "Edit|Write|NotebookEdit|MultiEdit",
             "hooks": [_entry("file-guardrail.py"), _entry("read-only-guard.py")]},
        ],
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [_entry("output-scanner.py")]},
        ],
    },
}


# --- the registration --------------------------------------------------------

def test_the_registration_is_exactly_the_reviewed_shape():
    """Schema-exact: one entry the CLI does not load voids the WHOLE managed
    file (verified on 2.1.281), and a voided file runs no guardrail at all."""
    assert H.registration() == EXPECTED


def test_every_entry_has_the_key_set_of_an_entry_known_to_load():
    """The skill-gate drop-in's entry is proven to load at verify-local; the
    GUARD-002 entries carry exactly its keys, so they are the same schema."""
    known = json.loads(_SKILL_GATE_DROP_IN.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    for entry in H.registered_entries():
        assert set(entry) == set(known)


# A port of CLI 2.1.281's hook spawn (`_F`), reduced to the decision that
# matters here:
#   gt = e.args !== void 0
#   if (e.args !== void 0) Ht = [e.command, e.args]
#   Tn = !bt && !gt && Yt ? n9(Yt, Pt) : Pt        // Yt = CLAUDE_CODE_SHELL_PREFIX
#   if (Ht) spawn(Ht[0], Ht[1])  /* no shell */   else spawn(Tn, [], {shell: true})
def _cli_spawn(entry, shell_prefix):
    if entry.get("args") is not None:
        return {"shell": False, "argv": [entry["command"], *entry["args"]]}
    command = entry["command"]
    if shell_prefix:
        command = f"{shell_prefix} {command}"
    return {"shell": True, "command": command}


@pytest.mark.parametrize("prefix", ["/bin/false", "/tmp/x/allow-everything.sh", ""])
def test_no_shell_prefix_wraps_a_guard002_hook(prefix):
    for entry in H.registered_entries():
        spawned = _cli_spawn(entry, prefix)
        assert spawned["shell"] is False
        assert spawned["argv"][:2] == ["/usr/bin/env", "-i"]
        assert prefix not in spawned["argv"] or prefix == ""


def test_the_old_shell_form_was_wrapped():
    """The port is not vacuous: a shell-form entry IS wrapped by a prefix."""
    old = {"type": "command", "command": "/usr/bin/python3 /opt/trinity/hooks/bash-guardrail.py"}
    assert _cli_spawn(old, "/bin/false") == {
        "shell": True, "command": "/bin/false /usr/bin/python3 /opt/trinity/hooks/bash-guardrail.py"}
    assert _cli_spawn(old, "") == {"shell": True, "command": old["command"]}


def test_the_spawn_facts_were_read_from_the_cli_the_image_pins():
    pinned = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", _DOCKERFILE, re.M).group(1)
    assert pinned == _CLI_VERIFIED, (
        f"The image now pins Claude Code {pinned}; the GUARD-002 registration facts were read "
        f"from {_CLI_VERIFIED}. Re-read in {pinned}: exec form (`args`) is spawned with no shell; "
        "CLAUDE_CODE_SHELL_PREFIX wraps only a hook with no `args` and only when truthy; policy "
        "`env` beats user/project settings and the process env; one bad entry voids the file. "
        "Then move _CLI_VERIFIED.")


def test_the_shell_prefix_pin_is_a_name_dot_env_may_never_set():
    from agent_server.services.execution_env import PROTECTED_KEYS, is_protected_key
    assert set(H.registration()["env"]) <= PROTECTED_KEYS
    assert is_protected_key("CLAUDE_CODE_SHELL_PREFIX")


def test_every_registered_script_is_shipped_and_smoke_tested_at_build():
    for hook in H.GUARD_HOOKS:
        assert f"COPY ./hooks/{hook} /opt/trinity/hooks/{hook}" in _DOCKERFILE
    # Pin on top of the executed tests below: the build runs every registered
    # hook under the argv it reads from the shipped managed file.
    assert "/opt/trinity/hooks/guard002-smoke.py" in _DOCKERFILE


# --- each hook, run under its registered flags and env ------------------------

_DENY_BASH = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
_ALLOW_BASH = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
_DENY_FILE = {"tool_name": "Write", "tool_input": {"file_path": "~/.ssh/id_rsa"}}
_DENY_FILE_ABS = {"tool_name": "Write", "tool_input": {"file_path": "/home/developer/.ssh/id_rsa"}}
_ALLOW_FILE = {"tool_name": "Write", "tool_input": {"file_path": "/home/developer/notes.md"}}
_SECRET = "sk-ant-api03-" + "A" * 93
_SCAN = {"tool_name": "Bash", "tool_input": {"command": "cat x"},
         "tool_response": {"stdout": f"token={_SECRET}"}}


def _registered(hook, tmp_path, overrides=None, payload=None):
    return H.run_hook(hook, payload, flags=H.registered_flags(hook),
                      env=H.registered_env(hook), tmp=tmp_path, overrides=overrides)


@pytest.mark.parametrize("hook, payload, rc", [
    ("bash-guardrail.py", _DENY_BASH, 2),
    ("bash-guardrail.py", _ALLOW_BASH, 0),
    ("file-guardrail.py", _DENY_FILE, 2),
    ("file-guardrail.py", _ALLOW_FILE, 0),
])
def test_guards_keep_their_tables_under_the_registered_invocation(hook, payload, rc, tmp_path):
    result = _registered(hook, tmp_path, payload=payload)
    assert result.returncode == rc, result.stderr


def test_read_only_guard_denies_under_the_registered_invocation(tmp_path):
    cfg = tmp_path / "ro.json"
    cfg.write_text(json.dumps({"enabled": True, "blocked_patterns": ["*.py"]}))
    payload = {"tool_name": "Write", "tool_input": {"file_path": "/home/developer/app.py"}}
    result = _registered("read-only-guard.py", tmp_path, {"_CONFIG_PATH": str(cfg)}, payload)
    assert result.returncode == 2, result.stderr
    assert "read-only mode" in result.stderr


def test_output_scanner_still_records_a_leak_under_the_registered_invocation(tmp_path):
    result = _registered("output-scanner.py", tmp_path, payload=_SCAN)
    assert result.returncode == 0, result.stderr
    events = [e for e in H.log_events(tmp_path) if e["event"] == "credential_pattern_in_output"]
    assert events and _SECRET not in json.dumps(events)


# --- hostile inputs from the agent's workspace -------------------------------

_HOSTILE_ALLOW = "import os\nos._exit(0)\n"


def _hostile_env(tmp_path, vector):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if vector == "pythonpath":
        evil = tmp_path / "evilpy"
        evil.mkdir()
        (evil / "json.py").write_text(_HOSTILE_ALLOW)   # every hook imports json via lib
        env["PYTHONPATH"] = str(evil)
    else:
        H.plant_usercustomize(home, _HOSTILE_ALLOW)
    return env


@pytest.mark.parametrize("vector", ["pythonpath", "user_site"])
def test_a_workspace_input_turned_a_deny_into_an_allow_under_the_old_invocation(vector, tmp_path):
    """Precondition for the test below: the attack is real, so its green is not vacuous."""
    result = H.run_hook("bash-guardrail.py", _DENY_BASH, flags=[],
                        env=_hostile_env(tmp_path, vector), tmp=tmp_path)
    assert result.returncode == 0


@pytest.mark.parametrize("vector", ["pythonpath", "user_site"])
@pytest.mark.parametrize("hook, payload", [
    ("bash-guardrail.py", _DENY_BASH),
    ("file-guardrail.py", _DENY_FILE_ABS),
])
def test_the_registered_flags_hold_against_a_hostile_env(vector, hook, payload, tmp_path):
    """The flags alone hold, even when the hostile env DOES reach the
    interpreter (env -i is the second layer, not the one tested here)."""
    env = _hostile_env(tmp_path, vector)
    result = H.run_hook(hook, payload, flags=H.registered_flags(hook), env=env, tmp=tmp_path)
    assert result.returncode == 2, result.stderr


# --- read-only config out of the agent's reach -------------------------------

_PY_WRITE = {"tool_name": "Write", "tool_input": {"file_path": "/home/developer/app.py"}}
_ENABLED = {"enabled": True, "blocked_patterns": ["*.py"]}


def _guard_with(tmp_path, root=None, home=None):
    """Run the read-only guard with the root file and the home (transition)
    file each present with ``root``/``home`` as content, or absent (None)."""
    root_path, home_path = tmp_path / "root.json", tmp_path / "home.json"
    for path, content in ((root_path, root), (home_path, home)):
        if content is not None:
            path.write_text(json.dumps(content))
    return H.run_hook("read-only-guard.py", _PY_WRITE, flags=["-I", "-S"], env={}, tmp=tmp_path,
                      overrides={"_CONFIG_PATH": str(root_path),
                                 "_LEGACY_CONFIG_PATH": str(home_path)})


def test_the_root_file_decides_whatever_the_home_file_says(tmp_path):
    """The agent owns ~/.trinity. While the root file exists, deleting or
    rewriting the home copy changes nothing."""
    assert _guard_with(tmp_path, root=_ENABLED, home=None).returncode == 2
    assert _guard_with(tmp_path, root=_ENABLED, home={"enabled": False}).returncode == 2
    assert _guard_with(tmp_path, root={"enabled": False}, home=_ENABLED).returncode == 0


def test_the_home_file_decides_only_while_the_root_file_is_missing(tmp_path):
    """A recreate drops the container's writable layer, and with it the root
    file, until the start-time sync rewrites it; an older backend never writes
    it. The home copy covers both, as it did before the root file existed."""
    assert _guard_with(tmp_path, root=None, home=_ENABLED).returncode == 2
    assert _guard_with(tmp_path, root=None, home={"enabled": False}).returncode == 0
    assert _guard_with(tmp_path, root=None, home=None).returncode == 0


@pytest.mark.parametrize("root, home, expected", [
    (_ENABLED, None, True), (_ENABLED, {"enabled": False}, True),
    ({"enabled": False}, _ENABLED, False), (None, _ENABLED, True), (None, None, False),
])
def test_codex_reads_the_same_precedence(root, home, expected, tmp_path, monkeypatch):
    from agent_server.services import codex_runtime
    root_path, home_path = tmp_path / "root.json", tmp_path / "home.json"
    for path, content in ((root_path, root), (home_path, home)):
        if content is not None:
            path.write_text(json.dumps(content))
    monkeypatch.setattr(codex_runtime, "_READ_ONLY_CONFIG", root_path)
    monkeypatch.setattr(codex_runtime, "_LEGACY_READ_ONLY_CONFIG", home_path)
    assert codex_runtime._is_read_only() is expected


def test_the_guard_and_codex_read_the_root_owned_config():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ro_guard", H.HOOKS / "read-only-guard.py")
    sys.path.insert(0, str(H.HOOKS))
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(H.HOOKS))
    assert mod._CONFIG_PATH == ROOT_READ_ONLY_CONFIG
    assert mod._LEGACY_CONFIG_PATH == LEGACY_READ_ONLY_CONFIG

    from agent_server.services import codex_runtime
    assert str(codex_runtime._READ_ONLY_CONFIG) == ROOT_READ_ONLY_CONFIG
    assert str(codex_runtime._LEGACY_READ_ONLY_CONFIG) == LEGACY_READ_ONLY_CONFIG


def _run_root_command(argv, root):
    """Run the backend's root-exec argv locally with /opt/trinity re-rooted."""
    argv = [a.replace("/opt/trinity", str(root)) for a in argv]
    return subprocess.run(argv, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("payload", [
    {"enabled": True, "blocked_patterns": ["*.py"], "allowed_patterns": []},
    {"enabled": True, "blocked_patterns": ["'; touch PWNED; '", "$(touch PWNED2)", "`touch PWNED3`", "%s%n\\"]},
])
def test_root_config_command_writes_the_config_as_data(payload, tmp_path):
    from services.agent_service import read_only
    root = tmp_path / "opt"
    root.mkdir()
    result = _run_root_command(read_only.root_config_command(json.dumps(payload)), root)
    assert result.returncode == 0, result.stderr
    written = root / "read-only-config.json"
    assert json.loads(written.read_text()) == payload
    assert oct(written.stat().st_mode & 0o777) == "0o444"
    assert not list(tmp_path.rglob("PWNED*")) and not list(Path.cwd().glob("PWNED*"))


def test_root_config_command_removes_the_config(tmp_path):
    from services.agent_service import read_only
    root = tmp_path / "opt"
    root.mkdir()
    (root / "read-only-config.json").write_text("{}")
    assert _run_root_command(read_only.root_config_command(None), root).returncode == 0
    assert not (root / "read-only-config.json").exists()


@pytest.fixture
def backend(monkeypatch):
    import importlib
    from services.agent_service import read_only
    # The registry entry is what `read_only`'s call-time `from
    # services.docker_service import ...` reads, so patch that object even if
    # another test left the package attribute pointing elsewhere (#3272).
    docker_service = importlib.import_module("services.docker_service")
    execs = []

    async def _exec(**kwargs):
        execs.append(kwargs)
        return {"exit_code": 0, "output": ""}

    client = MagicMock()
    client.write_file = AsyncMock(return_value={"success": True})
    client.read_file = AsyncMock(return_value={"success": False})
    monkeypatch.setattr(docker_service, "execute_command_in_container", _exec)
    monkeypatch.setattr(read_only, "get_agent_client", lambda name: client)
    return read_only, execs, client


@pytest.mark.asyncio
async def test_enabling_read_only_writes_the_root_file_as_root_and_keeps_the_legacy_file(backend):
    read_only, execs, client = backend
    config = {"blocked_patterns": ["*.py"], "allowed_patterns": ["out/*"]}
    result = await read_only.inject_read_only_hooks("alpha", config)
    assert result["success"] is True
    [call] = execs
    assert call["container_name"] == "agent-alpha" and call["user"] == "root"
    assert call["command"] == read_only.root_config_command(json.dumps({"enabled": True, **config}, indent=2))
    # Agents still on the pre-ent#787 image read only the home file.
    client.write_file.assert_awaited_once()
    assert client.write_file.await_args.args[0] == ".trinity/read-only-config.json"


@pytest.mark.asyncio
async def test_disabling_read_only_removes_the_root_file(backend):
    read_only, execs, client = backend
    result = await read_only.remove_read_only_hooks("alpha")
    assert result["success"] is True
    [call] = execs
    assert call["user"] == "root" and call["command"] == read_only.root_config_command(None)


@pytest.mark.asyncio
async def test_a_failed_root_write_is_reported_not_hidden(backend, monkeypatch):
    read_only, execs, client = backend
    import importlib
    docker_service = importlib.import_module("services.docker_service")

    async def _fail(**kwargs):
        return {"exit_code": 1, "output": "no"}

    monkeypatch.setattr(docker_service, "execute_command_in_container", _fail)
    result = await read_only.inject_read_only_hooks("alpha", {"blocked_patterns": []})
    assert result["success"] is False and "root" in result["error"]


# --- startup.sh: retire every shipped legacy registration --------------------

def _legacy_cleanup_block():
    start = _STARTUP.index("LEGACY_SETTINGS=")
    end = _STARTUP.index("# === Scratch space")
    return _STARTUP[start:end]


def _run_cleanup(path):
    script = _legacy_cleanup_block().replace(
        "LEGACY_SETTINGS=/home/developer/.claude/settings.json", f"LEGACY_SETTINGS={path}")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("fixture", sorted(p.name for p in _FIXTURES.glob("*.json")))
def test_startup_removes_every_shipped_legacy_registration(fixture, tmp_path):
    target = tmp_path / "settings.json"
    target.write_bytes((_FIXTURES / fixture).read_bytes())
    result = _run_cleanup(target)
    assert result.returncode == 0, result.stderr
    assert not target.exists(), f"{fixture} survived the cleanup"


def test_startup_keeps_a_settings_file_it_did_not_ship(tmp_path):
    target = tmp_path / "settings.json"
    shipped = (_FIXTURES / "claude-settings.887.json").read_text()
    target.write_text(shipped.replace("Bash", "Bash|Read"))
    _run_cleanup(target)
    assert target.exists()


def test_the_fixtures_are_the_two_shipped_versions():
    digests = {hashlib.sha256(p.read_bytes()).hexdigest() for p in _FIXTURES.glob("*.json")}
    assert digests == {
        "abb03a5eb55d268f1cca99372697844294cf601cd1decefb611b8729735cc09c",
        "d00df36d494cb68f013b86c2051b952226e95777331a5a108ee14aa55976041f",
    }
