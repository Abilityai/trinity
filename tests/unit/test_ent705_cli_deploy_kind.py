"""`trinity deploy --repo` creates a DEPLOYMENT (trinity-enterprise#705).

`--repo` deploys a codebase from GitHub. Since ent#705 the create default is
`kind: "agent"` (a working branch plus auto-push when the repo is the creator's
own), so the command must say what it is — otherwise deploying your own product
repo would start pushing an agent's workspace to it (PR #3020 review).

Static (AST) on purpose: the unit job does not install the CLI's own deps
(`click`, `rich`), and an importorskip would skip silently there.
"""

import ast
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "src/cli/trinity_cli/commands/deploy.py"


def _create_payloads():
    for node in ast.walk(ast.parse(DEPLOY.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "post" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "/api/agents"):
            payload = next(k.value for k in node.keywords if k.arg == "json")
            yield {k.value: v for k, v in zip(payload.keys, payload.values)}


def test_deploy_repo_asks_for_a_deployment():
    payloads = list(_create_payloads())
    assert payloads, "deploy --repo no longer POSTs /api/agents"
    for payload in payloads:
        assert "kind" in payload, "deploy --repo must send kind"
        assert ast.literal_eval(payload["kind"]) == "deployment"
