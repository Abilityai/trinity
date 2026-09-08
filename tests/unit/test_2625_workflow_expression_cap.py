"""#2625 — no expression-bearing string in any workflow may approach GitHub's 21,000-character cap.

GitHub evaluates a workflow string that contains ANY ``${{ … }}`` as ONE expression
and refuses the whole file at parse time when that string exceeds 21,000 characters:

    Invalid workflow file: (Line: 50, Col: 19): Exceeded max expression length 21000

That is how the first push to ``dev`` after #2622 died. ``deploy-dev.yml``'s ssh-action
``script:`` embeds three expressions, so its ~400-line script is one expression; the
#2578 comments took it from 17,837 to 21,923 characters, the run came up named after
the file with ZERO jobs, and — because there were no jobs — the ``notify-failure``
job that files the #2204 incident never ran either. PyYAML and ``bash -n`` both accept
such a file, and a workflow that triggers only on push to ``dev`` is never parsed
before merge, so the red run was the first signal.

This guard walks EVERY string in EVERY workflow (learnings 2026-07-30: a guard whose
scope is a hand-written list validates the fix, not the codebase) and fails when a
string that carries an expression passes the cap minus a margin. A long string WITHOUT
an expression is not capped and is deliberately left alone. The structural fix for the
one block that lives near the cap — passing the values through ``env:`` + ``envs:`` so
the script carries no expression at all — is trinity#2626; until then the margin is the
budget every edit to that script must fit in.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"

EXPRESSION_CAP = 21_000
EXPRESSION_CAP_MARGIN = 1_000
LIMIT = EXPRESSION_CAP - EXPRESSION_CAP_MARGIN


def _strings(node, path: str = "$"):
    """Yield ``(path, value)`` for every string anywhere in a parsed YAML document."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{path}[{i}]")


def expression_bearing_strings(doc) -> list[tuple[str, str]]:
    return [(path, s) for path, s in _strings(doc) if "${{" in s]


def check_no_expression_bearing_string_nears_the_cap(doc, *, source: str = "<doc>") -> None:
    over = [(path, len(s)) for path, s in expression_bearing_strings(doc) if len(s) > LIMIT]
    assert not over, (
        f"{source}: expression-bearing string(s) past {LIMIT} characters "
        f"(GitHub's cap is {EXPRESSION_CAP}; this guard keeps {EXPRESSION_CAP_MARGIN} in hand, #2625): "
        + ", ".join(f"{path}={n}" for path, n in over)
        + ". Trim prose (the reasoning lives in the PR, docs and the ledger) or move the "
        "`${{ }}` out of the string so it is no longer an expression (trinity#2626)."
    )


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow", sorted(_WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_no_expression_bearing_string_nears_the_cap(workflow: Path):
    check_no_expression_bearing_string_nears_the_cap(_load(workflow), source=workflow.name)


def test_the_scan_sees_the_block_that_bit():
    """Vacuity guard (learnings 2026-08-03): the deploy script must be found by the
    walk, and found to carry expressions — otherwise the parametrised test above
    passes because it looked at nothing."""
    paths = [path for path, _ in expression_bearing_strings(_load(_WORKFLOWS / "deploy-dev.yml"))]
    assert any(path.endswith(".with.script") for path in paths), paths


# ---------------------------------------------------------------------------
# Meta-tests: the guard goes red on the shape that broke dev, wherever the
# expression sits, and stays quiet on a long string that carries none.
# ---------------------------------------------------------------------------

def test_the_guard_rejects_the_deploy_script_padded_back_past_the_cap():
    text = (_WORKFLOWS / "deploy-dev.yml").read_text(encoding="utf-8")
    padded = text.replace(
        '            echo "=== Done ==="\n',
        '            echo "=== Done ==="\n' + "            # padding for the meta-test only\n" * 200,
    )
    assert padded != text, "the mutation did not change the file — it would prove nothing"
    with pytest.raises(AssertionError, match="with.script"):
        check_no_expression_bearing_string_nears_the_cap(yaml.safe_load(padded), source="padded")


@pytest.mark.parametrize(
    "step",
    [
        {"run": "${{ github.sha }}\n" + "x" * LIMIT},
        {"uses": "some/action@sha", "with": {"body": "${{ github.sha }} " + "x" * LIMIT}},
        {"run": "echo", "env": {"MESSAGE": "${{ github.sha }} " + "x" * LIMIT}},
    ],
    ids=["run", "with-input", "env-value"],
)
def test_an_expression_bearing_string_is_capped_wherever_it_sits(step):
    doc = {"name": "synthetic", "jobs": {"j": {"runs-on": "ubuntu-latest", "steps": [step]}}}
    with pytest.raises(AssertionError):
        check_no_expression_bearing_string_nears_the_cap(doc, source="synthetic")


def test_a_long_string_without_an_expression_is_not_capped():
    """The cap is GitHub's expression cap: a plain string, however long, is not one."""
    doc = {"name": "synthetic", "jobs": {"j": {"steps": [{"run": "x" * (EXPRESSION_CAP + 5_000)}]}}}
    check_no_expression_bearing_string_nears_the_cap(doc, source="synthetic")
