"""
Gated skills, the in-container hook — its registration (trinity-enterprise#752).

The hook is registered in its OWN managed-settings drop-in,
``/etc/claude-code/managed-settings.d/50-skill-gate.json``, not in
``managed-settings.json``: the CLI (2.1.281) voids a whole settings file when
one hook entry in it does not load, and validates each ``.d`` file on its own —
so a rejected skill-gate entry can never take the Bash/Edit guardrails with it.

CI does not build the base image, so the shipped artifacts — the JSON file and
the Dockerfile — are what is pinned here, and the matcher is evaluated by a
PORT of the CLI's own plain-name branch (``_ho`` / ``sAe`` in 2.1.281), not by
a regex written for this test. That the built image's CLI loads the file is
proven by behaviour at verify-local (a ``skill_gate_allow`` line after a real
Skill call).
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BASE = _ROOT / "docker" / "base-image"
_DROP_IN_SRC = _BASE / "hooks" / "managed-settings.d" / "50-skill-gate.json"
_DROP_IN = "/etc/claude-code/managed-settings.d/50-skill-gate.json"
_DOCKERFILE = (_BASE / "Dockerfile").read_text()
_INSTRUCTIONS = "\n".join(line for line in _DOCKERFILE.splitlines()
                          if not line.lstrip().startswith("#"))

EXPECTED = {
    "env": {"LD_PRELOAD": "", "LD_LIBRARY_PATH": "", "LD_AUDIT": ""},
    "hooks": {"PreToolUse": [{
        "matcher": "Skill|Agent|Task",
        "hooks": [{
            "type": "command",
            "command": "/usr/bin/env",
            "args": ["-i", "/usr/local/bin/python3", "-I", "-S", "/opt/trinity/hooks/skill-gate.py"],
            "timeout": 30,
        }],
    }]},
}


def _registration():
    return json.loads(_DROP_IN_SRC.read_text())


def test_the_drop_in_is_exactly_the_reviewed_shape():
    """Schema-exact for the pinned CLI: an unknown key or a mistyped field can
    make the CLI drop the file, and a dropped file is a hook that never runs."""
    assert _registration() == EXPECTED


# --- a port of the CLI's matcher (2.1.281) ----------------------------------
# function _ho(e,n,r){if(!(n?/^[a-zA-Z0-9_|, -]+$/:/^[a-zA-Z0-9_|]+$/).test(e))return;
#   return e.split(n?/[|,]/:"|").map(g=>g.trim()).filter(Boolean).flatMap(g=>Wme(Rc(g),r))}
# function sAe(e,n,...){if(!n||n==="*")return!0;let _=_ho(n,...);
#   if(_!==void 0)return _.includes(e)||...; try{let M=new RegExp(n);...}}
# rAe (events where `,` and spaces are allowed) includes PreToolUse.
# Rc maps legacy tool names: {Task:"Agent", KillShell:"TaskStop", ...}.
_CLI_TOOL_ALIASES = {"Task": "Agent", "KillShell": "TaskStop", "KillBash": "TaskStop",
                     "ListPeers": "ListAgents", "Brief": "SendUserMessage"}


def _cli_plain_names(matcher):
    if not re.fullmatch(r"[a-zA-Z0-9_|, -]+", matcher):
        return None
    parts = [p.strip() for p in re.split(r"[|,]", matcher)]
    return [_CLI_TOOL_ALIASES.get(p, p) for p in parts if p]


def _cli_matches(tool_name, matcher):
    if not matcher or matcher == "*":
        return True
    names = _cli_plain_names(matcher)
    if names is not None:
        return tool_name in names
    return re.search(matcher, tool_name) is not None


def test_the_matcher_is_in_the_exact_name_grammar():
    """The plain-name grammar is compared as exact strings — no regex to drift
    into matching more (or less) than intended."""
    [entry] = _registration()["hooks"]["PreToolUse"]
    assert _cli_plain_names(entry["matcher"]) is not None


@pytest.mark.parametrize("tool, expected", [
    ("Skill", True), ("Agent", True),
    ("Bash", False), ("Write", False), ("Read", False), ("SkillTool", False), ("skill", False),
    ("Agents", False), ("TaskStop", False), ("mcp__trinity__skill", False), ("WebFetch", False),
])
def test_the_matcher_selects_skill_loads_only(tool, expected):
    [entry] = _registration()["hooks"]["PreToolUse"]
    assert _cli_matches(tool, entry["matcher"]) is expected


def test_the_hook_runs_in_exec_form_with_no_shell_to_wrap():
    """Shell form is wrapped by a repo-settings `CLAUDE_CODE_SHELL_PREFIX` and
    reads `BASH_ENV`; exec form (`args` present) is spawned directly."""
    for entry in _registration()["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            assert hook["type"] == "command"
            assert isinstance(hook.get("args"), list) and hook["args"]
            assert hook["command"].startswith("/")          # never PATH-resolved
            assert "/opt/trinity/hooks/skill-gate.py" in hook["args"]
            assert hook["args"][:2] == ["-i", "/usr/local/bin/python3"]   # env -i: an empty env
            assert "-I" in hook["args"] and "-S" in hook["args"]


def test_the_loader_pins_are_names_dot_env_may_never_set():
    from agent_server.services.execution_env import PROTECTED_KEYS
    env = _registration()["env"]
    assert set(env) <= PROTECTED_KEYS
    assert all(value == "" for value in env.values())


def test_the_main_registration_file_is_left_alone():
    """GUARD-002's file is not where this hook lives: one bad entry there would
    void every guardrail, and startup's legacy cleanup compares it byte for byte."""
    main = (_BASE / "hooks" / "managed-settings.json").read_text()
    assert "skill-gate" not in main
    assert "Skill" not in json.loads(main)["hooks"].get("PreToolUse", [{}])[0].get("matcher", "")


# --- the image ---------------------------------------------------------------

def test_the_scripts_ship_root_owned_beside_the_other_hooks():
    for name in ("skill-gate.py", "_skill_gate.py"):
        assert (_BASE / "hooks" / name).is_file()
        assert f"COPY ./hooks/{name} /opt/trinity/hooks/{name}" in _INSTRUCTIONS
    # The existing blanket chmod/chown covers them only if they land before it.
    copy_at = _INSTRUCTIONS.index("COPY ./hooks/_skill_gate.py")
    assert copy_at < _INSTRUCTIONS.index("chmod 0555 /opt/trinity/hooks/*.py")


def test_the_drop_in_ships_root_owned_and_read_only_in_a_read_only_directory():
    assert f"COPY ./hooks/managed-settings.d/50-skill-gate.json {_DROP_IN}" in _INSTRUCTIONS
    assert "mkdir -p /etc/claude-code/managed-settings.d" in _INSTRUCTIONS
    assert "chown -R root:root /etc/claude-code/managed-settings.d" in _INSTRUCTIONS
    assert "chmod 0755 /etc/claude-code/managed-settings.d" in _INSTRUCTIONS
    assert f"chmod 0444 {_DROP_IN}" in _INSTRUCTIONS
    assert "--chown=developer" not in _INSTRUCTIONS[_INSTRUCTIONS.index("managed-settings.d"):][:400]


def test_the_build_fails_unless_the_hook_imports_and_decides():
    assert "RUN /usr/local/bin/python3 -I -S /opt/trinity/hooks/skill-gate.py --self-test" in _INSTRUCTIONS
    # It runs after the scripts are in place.
    assert _INSTRUCTIONS.index("--self-test") > _INSTRUCTIONS.index("COPY ./hooks/_skill_gate.py")


def test_the_image_still_ends_as_the_agent_user():
    """Invariant #17: the root sections this adds must hand back."""
    assert "USER developer" in _INSTRUCTIONS[_INSTRUCTIONS.index(_DROP_IN):]
    assert re.findall(r"^USER (\S+)", _INSTRUCTIONS, re.M)[-1] == "developer"
