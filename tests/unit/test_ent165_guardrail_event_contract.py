"""The §6 event contract's closed enums (trinity-enterprise#165, S1).

`kind` and `hook` are closed sets owned by the hook library
(`docker/base-image/hooks/lib.py`, slice S2); the backend keeps its own copy,
because the agent image and the backend ship separately and the backend must
not import agent-image code. A hand-synced pair drifts, so this compares them.

The hook library gains its copy in S2, which is built in parallel. Until it
lands the comparison has nothing to read and skips LOUDLY with that reason; the
frozen literal below still pins the backend side to the contract text, so S1
alone cannot drift from what S2 is building against.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_LIB = _REPO / "docker" / "base-image" / "hooks" / "lib.py"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

from services import guardrail_event_service as svc  # noqa: E402

# Plan §6.1, verbatim. Changing a member is a contract change for trinity#4281
# and trinity-enterprise#141, not a refactor.
CONTRACT_KINDS = {
    "deny", "credential_redacted", "credential_unredacted",
    "redaction_writeback_refused", "scanner_error", "hook_error", "config_error",
}
CONTRACT_HOOKS = {
    "bash_guardrail", "file_guardrail", "read_only_guard", "turn_tier_guard",
    "skill_gate", "output_scanner",
}
# The names the hook library's copies go by (agreed with slice S2).
LIB_NAMES = {"EVENT_KINDS": CONTRACT_KINDS, "EVENT_HOOKS": CONTRACT_HOOKS}


def test_the_backend_enums_are_the_contract():
    assert set(svc.KINDS) == CONTRACT_KINDS
    assert set(svc.HOOKS) == CONTRACT_HOOKS
    assert svc.OTHER not in CONTRACT_KINDS | CONTRACT_HOOKS


def _lib_literals():
    tree = ast.parse(_LIB.read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id in LIB_NAMES:
                    value = node.value
                    if isinstance(value, ast.Call) and value.args:  # frozenset({...})
                        value = value.args[0]
                    out[t.id] = set(ast.literal_eval(value))
    return out


def test_the_hook_library_and_the_backend_agree():
    found = _lib_literals()
    if not found:
        pytest.skip("docker/base-image/hooks/lib.py has no EVENT_KINDS / EVENT_HOOKS yet "
                    "(trinity-enterprise#165 slice S2 adds them); the backend side is "
                    "pinned by test_the_backend_enums_are_the_contract")
    assert set(found) == set(LIB_NAMES), f"lib.py defines only {sorted(found)}"
    assert found["EVENT_KINDS"] == set(svc.KINDS)
    assert found["EVENT_HOOKS"] == set(svc.HOOKS)
