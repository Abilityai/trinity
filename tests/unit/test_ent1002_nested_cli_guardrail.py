"""
trinity-enterprise#1002: on an agent with gated skills, the Bash guardrail
refuses a nested coding-agent CLI.

The in-container skill check takes the run's identity from the parent claude's
launch environment, and a `/<skill>` prompt expands in a nested session with no
tool call at all. So on an agent with gates, starting `claude`, `codex` or
`gemini` from Bash is refused. An agent with no gates keeps the old behaviour:
the rows live under their own key and apply only with the root-owned marker
`/opt/trinity/skill-gates-active`.

A regex over the command string stops the ordinary path, not a deliberate
wrapper. Both directions of that are pinned below: what must be refused, what
must still pass, and what is knowingly let through or knowingly refused too.

Related flows: docs/memory/feature-flows/agent-guardrails.md, skill-gate.md
"""
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _ent787_hook_harness as H  # noqa: E402

pytestmark = pytest.mark.unit

_HOOK = "bash-guardrail.py"
_BASELINE = json.loads(H.BASELINE.read_text())
_MARKER = "/opt/trinity/skill-gates-active"


def _gated() -> list:
    return [re.compile(e["pattern"]) for e in _BASELINE["gated_bash_deny"]]


def _refused(command: str) -> bool:
    return any(p.search(command) for p in _gated())


def _run(tmp_path: Path, command: str, *, gated: bool) -> subprocess.CompletedProcess:
    marker = tmp_path / "skill-gates-active"
    if gated:
        marker.write_text("1\n")
    return H.run_hook(
        _HOOK, {"tool_name": "Bash", "tool_input": {"command": command}},
        flags=H.registered_flags(_HOOK), env=H.registered_env(_HOOK), tmp=tmp_path,
        overrides={"SKILL_GATES_MARKER": str(marker)})


# --- the hook: gated agents only -------------------------------------------

def test_a_nested_cli_is_refused_on_an_agent_with_gates(tmp_path):
    result = _run(tmp_path, 'claude -p "hello"', gated=True)
    assert result.returncode == 2, result.stderr
    assert "coding-agent CLI" in result.stderr
    [event] = H.log_events(tmp_path)
    assert event["event"] == "deny" and event["tool"] == "Bash"
    assert event["command_prefix"] == 'claude -p "hello"'


def test_a_nested_cli_still_runs_on_an_agent_with_no_gates(tmp_path):
    result = _run(tmp_path, 'claude -p "hello"', gated=False)
    assert result.returncode == 0, result.stderr
    assert H.log_events(tmp_path) == []


def test_the_ordinary_deny_list_applies_with_and_without_the_marker(tmp_path):
    for gated in (False, True):
        assert _run(tmp_path, "rm -rf /", gated=gated).returncode == 2


def test_an_ordinary_command_passes_with_the_marker(tmp_path):
    assert _run(tmp_path, "ls -la .claude", gated=True).returncode == 0


def test_the_rows_are_not_in_the_unconditional_list():
    """In `bash_deny` they would reach every agent, including a skill on an
    ungated agent that shells out to `claude -p`."""
    for row in _BASELINE["bash_deny"]:
        assert not re.search(row["pattern"], "claude -p hello"), row["reason"]


def test_the_refusal_tells_the_model_what_to_use_instead():
    [row] = _BASELINE["gated_bash_deny"]
    assert "gated skills" in row["reason"]
    assert "Agent subagent tool" in row["reason"]
    assert "chat_with_agent" in row["reason"]
    assert "Write tool" in row["reason"]  # the way out of a quoted-prose false positive


# --- a per-agent override cannot reach the rows ------------------------------

def test_a_per_agent_override_cannot_remove_the_rows(tmp_path):
    script = (H.HOOKS / "write-runtime-config.py").read_text()
    baseline = tmp_path / "baseline.json"
    runtime = tmp_path / "runtime.json"
    baseline.write_text(H.BASELINE.read_text())
    rewritten = tmp_path / "write-runtime-config.py"
    rewritten.write_text(
        script.replace('"/opt/trinity/guardrails-baseline.json"', repr(str(baseline)))
              .replace('"/opt/trinity/guardrails-runtime.json"', repr(str(runtime))))
    env = {**os.environ, "AGENT_GUARDRAILS": json.dumps({"gated_bash_deny": []})}
    subprocess.run([sys.executable, str(rewritten)], env=env, check=True)
    assert json.loads(runtime.read_text())["gated_bash_deny"] == _BASELINE["gated_bash_deny"]


# --- the marker path has one value, in five files ----------------------------

def _constant(path: Path, name: str):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


@pytest.mark.parametrize("path, name", [
    (H.HOOKS / "bash-guardrail.py", "SKILL_GATES_MARKER"),
    (H.HOOKS / "guard002-smoke.py", "SKILL_GATES_MARKER"),
    (H.HOOKS / "_skill_gate.py", "MARKER"),
    (H.HOOKS / "skill-gate.py", "MARKER"),
    (H.REPO / "src" / "backend" / "services" / "skill_gate_service.py", "MARKER"),
])
def test_every_reader_names_the_one_marker(path, name):
    assert _constant(path, name) == _MARKER


# --- the rule ----------------------------------------------------------------

@pytest.mark.parametrize("command", [
    'claude -p "hi"', "claude", "codex exec hi", "gemini -p hi", "gemini",
    # the path forms
    "/usr/local/bin/claude -p hi", "~/.local/bin/claude -p hi", "$HOME/.local/bin/codex exec hi",
    "./node_modules/.bin/claude -p hi",
    # the identity route the issue names
    "TRINITY_EXECUTION_ID=abc claude -p hi", 'TRINITY_EXECUTION_ID="a b" claude -p hi',
    "env TRINITY_EXECUTION_ID=abc claude -p hi", "env -u TRINITY_EXECUTION_ID claude -p hi",
    "env -i claude -p hi", "export TRINITY_EXECUTION_ID=abc; claude -p hi",
    "A=1 B=2 claude -p hi",
    # after another command
    "cd x && claude -p hi", "ls; claude -p hi", "false || codex exec hi", "sleep 1 & gemini -p hi",
    "echo hi | claude -p", "echo hi |claude -p", "echo hi|claude -p", "echo hi | claude",
    "cd x\nclaude -p hi", "cd x\n  claude -p hi", "foo \\\n  claude -p hi",
    # subshells and substitutions
    "(claude -p hi)", "x=$(claude -p hi)", 'echo "$(claude -p hi)"', "{ claude -p hi; }",
    # launchers
    "nohup claude -p hi &", "timeout 60 claude -p hi", "timeout 5m claude -p hi",
    "timeout -k 5 60 claude -p hi", "exec claude -p hi", "command claude -p hi", "time claude -p hi",
    "setsid claude -p hi", "setsid -f claude -p hi", "nice claude -p hi", "nice -n 5 claude -p hi",
    "stdbuf -oL claude -p hi", "sudo claude -p hi", "sudo -u developer claude -p hi",
    "sudo -E claude -p hi", "nohup timeout 60 claude -p hi",
    "ls | xargs claude -p", "ls | xargs -n1 claude -p", "ls | xargs -P4 -I{} claude -p {}",
    "ls | xargs -I {} claude -p {}",
    # shell control flow
    'for f in *; do claude -p "$f"; done', "for f in *\ndo\n  codex exec x\ndone",
    "if claude -p hi; then echo y; fi", "if true; then claude -p hi; fi",
    "if a; then b; else gemini -p hi; fi", "while true; do claude -p hi; done", "! claude -p hi",
    # a shell string
    'bash -c "claude -p hi"', "sh -c 'claude -p hi'", 'bash -lc "codex exec hi"',
    '/bin/bash -c "gemini -p hi"', "zsh -c 'claude -p hi'",
    "bash -c 'TRINITY_EXECUTION_ID=x claude -p hi'",
    # a session after an allowed command
    "claude --version && claude -p hi", "claude plugin list; claude -p hi", "codex mcp list; codex exec hi",
    # an allowed word as the prompt, not the subcommand
    "claude -p mcp", "claude -p --version", "codex exec plugin",
    # gemini has no `plugin` subcommand: that word is its [query..] prompt
    "gemini plugin list", "gemini plugin review this repo",
    # the `mcp-server` subcommand is not `mcp`
    "codex mcp-server",
    # -p or --print before any `--` skips subcommand parsing, so these are sessions
    # (run on CLI 2.1.281: each one reached the model)
    "claude mcp -p hi", "claude mcp list -p hi", "claude mcp --print hi", "claude mcp list --print hi",
    "claude plugin -p hi", "claude plugin list --print hi", "claude plugin marketplace list -p",
    "cd x && claude mcp list -p hi", "(claude mcp -p hi)",
])
def test_refused(command):
    assert _refused(command), command


@pytest.mark.parametrize("command", [
    # the three the issue's option text names
    "ls .claude", "grep claude src/x.py", "cat claude.md",
    # looking at the CLI is not starting it
    "which claude", "type codex", "command -v claude", "command -v gemini >/dev/null",
    "ps aux | grep claude", "pgrep claude", "pkill codex",
    "find . -name claude", "echo claude", "echo claude -p hi", "printf 'claude'",
    # a word in a path or a name
    "ls ~/.claude", "cat ~/.claude/settings.json", "~/.claude/run.sh", ".claude/skills/x/run.sh",
    "cd claude && ls", "mkdir claude", "cd ~/.claude; ls", "git clone https://github.com/x/claude",
    "cat x > claude", "cat x | tee claude", "python claude.py", "python3 -m claude", "./claude.py",
    "claude-usage-report", "claude_helper --x", "my-claude -p hi", "gemini.sh",
    # a launcher running something else
    "ls | xargs grep claude", "ls | xargs -0 grep claude", "ls | xargs -r grep claude",
    "time grep claude f", "timeout 5 grep claude f", "env | grep claude",
    "sudo npm i -g @anthropic-ai/claude-code", "sudo apt-get install -y claude", "nohup ./run.sh claude",
    "npm i -g @openai/codex", "pip install gemini",
    # a value, not a command
    "CONFIG=$HOME/.config/claude; ls", "X=claude ls", "export CLAUDE_DIR=/x/claude",
    # a pattern, not a pipeline
    "grep -E 'claude|codex|gemini' f", 'rg "(claude|codex)" src', 'grep -E "claude|codex" f',
    "git log --grep='claude|codex'",
    # prose
    'git commit -m "claude -p is the CLI"', 'git commit -m "docs: claude and codex"',
    "echo 'run claude -p later'",
    # a variable in an inline script
    "python3 - <<EOF\nclaude = anthropic.Anthropic()\nEOF", "python3 - <<EOF\ngemini += 1\nEOF",
])
def test_passes(command):
    assert not _refused(command), command


@pytest.mark.parametrize("command", [
    "cat > x <<EOF\nclaude is a CLI\nEOF",
    'git commit -m "docs: gemini & codex runtime"',
    'echo "see (claude -p hi)"',
    "cat > t.md <<EOF\n| model | gemini |\nEOF",
])
def test_known_limit_quoted_prose_at_a_command_position_is_refused_too(command):
    """The rule does not parse quotes, the same as the #3105 `su` row. Gated
    agents only, and the model can read the reason and rephrase. If a parser
    ever replaces the regex these cases should flip to passing."""
    assert _refused(command)


@pytest.mark.parametrize("command", [
    "python3 -c 'import subprocess; subprocess.run([\"claude\"])'",
    "./scripts/run-nested.sh", "CLI=claude; $CLI -p hi", '"$(which claude)" -p hi', "'claude' -p hi",
    "cl''aude -p hi", "\\claude -p hi", "npx @anthropic-ai/claude-code -p hi",
    "echo 'claude -p hi' | sh", "eval 'claude -p hi'", "/usr/bin/env claude -p hi",
    "find . -exec claude -p {} \\;", "A=$(cat f) claude -p hi", '"$HOME/.local/bin/claude" -p hi',
    "timeout -s KILL 60 claude -p hi", "ls | xargs -I % claude -p %",
    # The price of letting `claude = x` pass as a variable in an inline script.
    "claude = -p hi",
    # Left out on purpose: inline code in a Markdown body written through a heredoc
    # is likelier than a backtick substitution, and models write `$(...)`.
    "x=`claude -p hi`",
])
def test_known_limit_other_spellings_get_past(command):
    """Documented in keeping-agents-in-bounds.md § Honest limits, and open as
    the regex-evasion class in trinity-enterprise#167. Pinned so the docs and
    the rule cannot drift apart: tightening the rule should fail here and
    prompt a docs change."""
    assert not _refused(command)


@pytest.mark.parametrize("command", [
    "claude --version", "codex --version", "gemini --version",
    "claude --help", "codex --help", "gemini --help",
    "claude mcp list", "claude mcp add x -- y", "codex mcp list", "gemini mcp list",
    "claude plugin list --json", "codex plugin list",
    "claude --version 2>/dev/null", "/usr/local/bin/claude --version",
    # the /trinity:sync and /trinity:onboard spellings
    "claude plugin marketplace list --json; claude plugin list --json",
    "claude plugin marketplace add abilityai/abilities && claude plugin install trinity@abilityai --yes",
    "claude plugin list --json 2>/dev/null | grep -q trinity || claude plugin install trinity --yes",
    # after `--` a -p is the MCP server's own argument, not the CLI's
    "claude mcp add srv -- npx server -p 8080", "claude mcp add srv -- /bin/true --print",
    # neither of these is the -p token
    "claude mcp add -e A=-p srv -- /bin/true", "claude mcp list -pv",
])
def test_a_command_that_starts_no_session_passes(command):
    """A flag or subcommand that exits without a model turn is not a nested run,
    and the platform's own plugin skills drive `claude plugin` through Bash.
    Every spelling here was run on CLI 2.1.281 against a stub that counts model
    calls; `test_refused` holds the ones that reached it."""
    assert not _refused(command), command


# --- a hook that outlives its timeout lets the tool run ----------------------

_SIZE = 512_000


@pytest.mark.parametrize("unit", [
    "\n", " ", ";", "; ", "\n ", "x", "a/", "-", "A=", 'A="', "A=1 ", "env ", "env   ", "env -x ",
    "env A=1 ", "timeout 1 ", "do ", "sudo -u ", "env -u env ", "time env ", "bash -c '", "(", "|",
    # no inner token may swallow a command position, or every start rescans the rest
    "env -x;", "env 1;", "env -u x;", "sudo -n|", "env -x(", "time 1&",
    "A=(", "$(A=", "(A=1", "$(A=~/-x", "x=1(", "A=(A=(",
    # the -p scan after an allowed word starts over at every command position
    "sh -c 'claude mcp ", "sh -c 'claude plugin ", "; claude mcp ", "\nclaude mcp x ", "(claude mcp ",
], ids=repr)
def test_the_rule_stays_fast_on_a_large_command(unit):
    command = unit * (_SIZE // len(unit)) + " nothing"
    started = time.monotonic()
    assert not _refused(command)
    assert time.monotonic() - started < 3.0  # a quadratic rule takes minutes at this size


# --- the image build checks the same thing --------------------------------

def _load_smoke(marker: Path):
    spec = importlib.util.spec_from_file_location("guard002_smoke", H.HOOKS / "guard002-smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SKILL_GATES_MARKER = str(marker)
    return module


def _fake_hook(tmp_path: Path, marker: Path, *, honours_marker: bool) -> list:
    body = f"import os, sys; sys.exit(2 if os.path.lexists({str(marker)!r}) else 0)"
    script = tmp_path / "hook.py"
    script.write_text(body if honours_marker else "import sys; sys.exit(0)")
    return [sys.executable, str(script)]


def test_the_image_smoke_passes_a_hook_that_honours_the_marker_and_cleans_up(tmp_path):
    marker = tmp_path / "skill-gates-active"
    failures = []
    _load_smoke(marker)._check_gated_rows(_fake_hook(tmp_path, marker, honours_marker=True), failures)
    assert failures == []
    assert not marker.exists()


def test_the_image_smoke_runs_the_check_on_the_registered_bash_hook(tmp_path):
    """Through `main()`, so the call site is pinned: a hook that ignores the
    marker fails the build. `claude -p hi` allowed with the marker is the failure."""
    marker = tmp_path / "skill-gates-active"
    hook = tmp_path / "bash-guardrail.py"
    hook.write_text("import sys; sys.exit(0)")
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"command": sys.executable, "args": [str(hook)]}]}]}}))
    smoke = _load_smoke(marker)
    smoke.MANAGED = str(managed)
    smoke.READ_ONLY_CONFIG = str(tmp_path / "read-only-config.json")
    smoke.LOG_PATH = str(tmp_path / "guardrails.jsonl")
    smoke.CASES = {"bash-guardrail.py": []}
    assert smoke.main() == 1
    assert not marker.exists()


def test_the_image_smoke_fails_a_hook_that_ignores_the_marker_and_cleans_up(tmp_path):
    """The marker makes the skill-gate hook fail closed, so one left in an
    image layer would turn every agent built from it into a gated agent."""
    marker = tmp_path / "skill-gates-active"
    failures = []
    _load_smoke(marker)._check_gated_rows(_fake_hook(tmp_path, marker, honours_marker=False), failures)
    assert len(failures) == 1 and "exit 0, want 2" in failures[0]
    assert not marker.exists()
