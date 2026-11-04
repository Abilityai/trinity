"""One path policy, two copies, three consumers (abilityai/trinity-enterprise#820).

`src/backend/services/file_path_policy.py` is vendored byte-identical into the
agent server (`docker/base-image/agent_server/file_path_policy.py`) because the
agent server ships as its own image and cannot import the backend
(architecture.md Invariant #5). The file guardrail hook reads its own JSON
list (`hooks/guardrails-baseline.json::path_deny`), so that list is held to the
policy here in both directions. And the agent server's platform floor must
stay a SUBSET of the backend route floor: platform writers reach the agent
server directly and write paths the user routes refuse.
"""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src/backend/services/file_path_policy.py"
_VENDORED = _REPO / "docker/base-image/agent_server/file_path_policy.py"
_BASELINE = _REPO / "docker/base-image/hooks/guardrails-baseline.json"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


policy = _load(_BACKEND, "_test820_policy")


def test_the_vendored_copy_is_byte_identical():
    assert _VENDORED.read_bytes() == _BACKEND.read_bytes(), (
        "file_path_policy.py drifted: copy src/backend/services/file_path_policy.py "
        "to docker/base-image/agent_server/file_path_policy.py"
    )


def test_the_policy_imports_only_the_stdlib():
    import ast
    tree = ast.parse(_BACKEND.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"fnmatch", "posixpath", "re", "typing"}, imported


def test_the_agent_server_loads_the_vendored_copy():
    router = _load(_REPO / "docker/base-image/agent_server/routers/files.py", "_test820_router")
    assert Path(router._policy.__file__).resolve() == _VENDORED.resolve()


def _hook_deny():
    return json.loads(_BASELINE.read_text())["path_deny"]


# Hook entries that are not route-write-denied on purpose, with the reason.
_HOOK_ONLY = {
    # the agent's own read-only marker: the hook stops the agent editing it;
    # the routes refuse it as `.trinity/*` already (pinned below), so no gap
    "/home/developer/.trinity/read-only-config.json": "covered by .trinity/*",
}


@pytest.mark.parametrize("entry", _hook_deny())
def test_every_hook_entry_is_write_denied_by_the_policy(entry):
    """hook -> policy: the routes never write what the agent may not write."""
    example = entry.replace("*", "x")
    assert policy.write_denied(example), f"{entry} is denied to the agent but writable via the routes"


# Policy credential/runtime patterns the hook expresses in its own spelling.
_POLICY_ONLY = {
    # the hook does not guard these; each with the reason
    ".mcp.json.template": "rendered by no one yet (#2007); the route floor is enough",
    ".git/config": "the hook's bash guard owns git config",
}


@pytest.mark.parametrize("pattern", policy.CREDENTIAL_PATH_PATTERNS + policy.RUNTIME_CONFIG_PATTERNS)
def test_every_credential_pattern_has_a_hook_entry(pattern):
    """policy -> hook: a new credential class reaches the agent's own guard."""
    if pattern in _POLICY_ONLY:
        return
    deny = _hook_deny()
    assert pattern in deny or f"/home/developer/{pattern}" in deny, (
        f"{pattern} is a credential class with no guardrails-baseline.json path_deny entry"
    )


_SAMPLES = [
    ".env", ".env.local", ".mcp.json", ".mcp.json.template", ".credentials.enc", ".claude.json",
    ".claude/.credentials.json", ".trinity/x", ".trinity", ".git", ".git/config", ".gitignore",
    "workspace/r/.git/hooks/pre-commit", "workspace/bare.git/config", "a/b/.env",
    "CLAUDE.md", ".claude/settings.local.json", ".claude/skills/x", "notes.md",
]


@pytest.mark.parametrize("rel", _SAMPLES)
def test_the_platform_floor_is_a_subset_of_the_route_floor(rel):
    if policy.agent_server_edit_protected(rel):
        assert policy.write_denied(rel), f"{rel}: the agent server refuses it, the routes would not"


@pytest.mark.parametrize("rel", _SAMPLES)
def test_the_platform_delete_floor_is_a_subset_of_the_route_delete_floor(rel):
    if policy.agent_server_delete_protected(rel):
        assert policy.delete_denied(rel), rel


def test_the_platform_floor_leaves_the_platform_writers_their_paths():
    """read_only.py writes the settings file without platform=true; the
    instructions and skills are platform-written too (skill_service)."""
    for rel in (".claude/settings.local.json", "CLAUDE.md", ".claude/skills/x/SKILL.md"):
        assert not policy.agent_server_edit_protected(rel), rel
