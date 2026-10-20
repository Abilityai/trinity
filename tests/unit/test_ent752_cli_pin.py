"""
Gated skills, the in-container hook — the CLI it was verified against
(trinity-enterprise#752).

The hook's fail-closed property rests on facts read from ONE Claude Code
binary (2.1.281): exec-form hooks are not wrapped, a plain-name matcher is an
exact comparison, the `Skill` tool's input is `{skill, args}`, exit 2 blocks
while exit 1 / a crash / a timeout let the tool run, managed `.d` files are
validated one by one, policy `env` is applied last, the `skill__` tools are
compiled off, the hook's parent process is claude itself. A newer CLI may
change any of them, so moving the image's pin fails this test until someone
re-reads them (the list lives beside `SKILL_GATE_CLI_VERIFIED` in the hook).
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _ROOT / "docker" / "base-image" / "hooks" / "_skill_gate.py"
_DOCKERFILE = _ROOT / "docker" / "base-image" / "Dockerfile"


def _module_constant(name):
    for node in ast.parse(_HOOK.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not defined in {_HOOK.name}")


def test_the_hook_was_verified_against_the_cli_the_image_pins():
    pinned = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", _DOCKERFILE.read_text(), re.M).group(1)
    verified = _module_constant("SKILL_GATE_CLI_VERIFIED")
    assert verified == pinned, (
        f"The image now pins Claude Code {pinned}, but the skill-gate hook's CLI facts were "
        f"read from {verified}. Re-audit every item in CLI_FACTS_TO_REAUDIT against {pinned} "
        f"(docker/base-image/hooks/_skill_gate.py), then move SKILL_GATE_CLI_VERIFIED.")


def test_the_reaudit_list_names_what_to_check():
    facts = _module_constant("CLI_FACTS_TO_REAUDIT")
    assert isinstance(facts, tuple) and len(facts) >= 8
    text = " ".join(facts).lower()
    for topic in ("exec form", "matcher", "skill", "exit 2", "managed-settings.d", "env", "parent"):
        assert topic in text, topic
