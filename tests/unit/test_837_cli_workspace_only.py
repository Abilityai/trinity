"""trinity-enterprise#837 review: `trinity init` for a Workspace-only account.

The floor refuses a `user` account on every operator route, and the CLI is
nothing but operator routes. `init` swallowed the first refusal (provisioning
an MCP key) and printed "Trinity CLI is ready", after which every command
failed. It now names the refusal and exits non-zero.

The predicate is executed: `trinity_cli/refusals.py` imports nothing third
party. Its one call site is pinned by AST, because the unit job does not install
the CLI's own dependencies (`click`, `rich`) and an importorskip would skip
silently there (the ent#705 precedent).
"""

import ast
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "src" / "cli"
if str(CLI) not in sys.path:
    sys.path.insert(0, str(CLI))

from trinity_cli.refusals import is_workspace_only_refusal  # noqa: E402

pytestmark = pytest.mark.unit

FLOOR = {"detail": {"code": "workspace_only",
                    "message": "This account works in the Workspace. Open /workspace."}}


def test_the_floor_refusal_is_recognised():
    assert is_workspace_only_refusal(403, FLOOR) is True


@pytest.mark.parametrize("status,body", [
    (403, {"detail": "Forbidden"}),
    (403, {"mfa_required": True, "detail": "second factor required"}),
    (401, FLOOR),
    (403, None),
    (403, {"detail": {"code": "something_else"}}),
])
def test_anything_else_is_not(status, body):
    assert is_workspace_only_refusal(status, body) is False


def test_provisioning_exits_on_it_instead_of_reporting_ready():
    tree = ast.parse((CLI / "trinity_cli" / "commands" / "auth.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_provision_mcp_key")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"is_workspace_only_refusal", "_workspace_only_exit"} <= called
