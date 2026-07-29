"""
Regression + drift guard for #1811 — the recovery recreate must build the same
kind of container as creation.

`recreate_missing_container` (#1559) rebuilds an agent's container spec from
scratch instead of from the creation path, and drifted: `TEMPLATE_NAME` was
never restored (so a recovered agent reported an empty `template_name` from
`/info` and skipped the local-template branch in `startup.sh:341`), and a
literal shared `encrypted-data:/data` mount existed only on the creation side.

The two builders live ~400 lines apart in different modules and share no spec,
so nothing stopped them diverging. This asserts the parity directly, by
reading the env keys each one assigns.

Deliberately source-level (AST) rather than behavioural: driving both builders
end-to-end needs the full docker/db mock harness, and the failure mode here is
"someone adds an env var to one path and not the other" — which is visible in
the source and cheap to check on every run. It is a drift guard, not a
substitute for the create-path characterization suite (#1484).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_CRUD = _BACKEND / "services" / "agent_service" / "crud.py"
_LIFECYCLE = _BACKEND / "services" / "agent_service" / "lifecycle.py"

# Keys creation sets inline that recovery supplies through helpers instead of a
# literal — parity holds at runtime, not in the source text.
# Verified against `_apply_persisted_auth_env` (lifecycle.py), which the
# recovery path calls — these reach a recovered container at runtime even
# though they are not literals inside `recreate_missing_container`.
_SUPPLIED_BY_HELPERS = {
    "ANTHROPIC_API_KEY",          # auth env, resolved from the persisted mode
    "AGENT_GUARDRAILS",           # lifecycle.py:491 (GUARD-001)
    "AGENT_TOOL_STALL_LIMIT_S",   # lifecycle.py:501
}


def _env_keys_assigned_in(path: Path, func_name: str) -> set[str]:
    """Every string-literal key assigned into a dict inside `func_name`.

    Catches both `env_vars["X"] = ...` and `{"X": ...}` literal forms, which is
    how the two builders express their env sets.
    """
    tree = ast.parse(path.read_text())
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            target = node
            break
    assert target is not None, f"{func_name} not found in {path.name}"

    keys: set[str] = set()
    for node in ast.walk(target):
        # {"KEY": value}
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        # env_vars["KEY"] = value
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                ):
                    keys.add(tgt.slice.value)
    return keys


def _looks_like_env_var(key: str) -> bool:
    """Env vars are SHOUTY_SNAKE; dict literals in these functions also carry
    label keys ('trinity.platform'), mount specs ('bind') and JSON fields."""
    return key.isupper() and key.replace("_", "").isalnum()


def test_template_name_is_restored_on_recovery():
    """The concrete #1811 regression: recovery must set TEMPLATE_NAME."""
    recovery = _env_keys_assigned_in(_LIFECYCLE, "recreate_missing_container")
    assert "TEMPLATE_NAME" in recovery, (
        "recreate_missing_container must set TEMPLATE_NAME — startup.sh:341 gates "
        "local-template init on it and agent-server /info reports it (#1811)."
    )


def test_recovery_env_covers_creation_env():
    """Drift guard: an env var added to creation must reach recovery too."""
    created = {k for k in _env_keys_assigned_in(_CRUD, "_build_base_env") if _looks_like_env_var(k)}
    recovered = {k for k in _env_keys_assigned_in(_LIFECYCLE, "recreate_missing_container") if _looks_like_env_var(k)}

    missing = created - recovered - _SUPPLIED_BY_HELPERS
    assert not missing, (
        "These env vars are set when an agent is CREATED but not when one is "
        f"RECOVERED, so a recovered agent comes back different: {sorted(missing)}. "
        "Add them to recreate_missing_container, or to _SUPPLIED_BY_HELPERS if a "
        "helper provides them at runtime (#1811)."
    )


def test_no_shared_encrypted_data_mount_anywhere():
    """The mount was a literal name shared rw by every agent — isolation hole.

    Removed from creation rather than copied into recovery, so this asserts it
    stays gone from BOTH paths.
    """
    for path in (_CRUD, _LIFECYCLE):
        assert "'encrypted-data'" not in path.read_text(), (
            f"{path.name} mounts the shared `encrypted-data` volume. It is a literal "
            "name, so one volume would be mounted rw into every agent at once (#1811)."
        )


def test_recovery_deactivates_superseded_keys_before_minting():
    """Order matters: deactivating AFTER the mint would kill the new key too."""
    src = _LIFECYCLE.read_text()
    func_start = src.index("async def recreate_missing_container")
    body = src[func_start:]
    deactivate_at = body.find("deactivate_agent_mcp_keys")
    mint_at = body.find("create_agent_mcp_api_key")

    assert deactivate_at != -1, "recovery must deactivate superseded MCP keys (#1811)"
    assert mint_at != -1
    assert deactivate_at < mint_at, (
        "deactivate_agent_mcp_keys must run BEFORE create_agent_mcp_api_key — "
        "it flips every agent-scoped key for the agent, so running it after the "
        "mint would deactivate the key just issued (#1811)."
    )
