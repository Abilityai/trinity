"""#2625 — no expression-bearing string in any workflow may approach GitHub's 21,000 cap.

GitHub evaluates a workflow string that contains ANY ``${{ … }}`` as ONE expression
and refuses the whole file at parse time when that string exceeds 21,000:

    Invalid workflow file: (Line: 50, Col: 19): Exceeded max expression length 21000

That is how the first push to ``dev`` after #2622 died. ``deploy-dev.yml``'s ssh-action
``script:`` embeds three expressions, so its ~400-line script is one expression; the
#2578 comments took it from 17,837 to 21,923, the run came up named after the file
with ZERO jobs, and — because there were no jobs — the ``notify-failure`` job that
files the #2204 incident never ran either. PyYAML and ``bash -n`` both accept such a
file, and a workflow that triggers only on push to ``dev`` is never parsed before
merge, so the red run was the first signal.

Scope (review on #2627): every ``.yml`` AND ``.yaml`` under ``.github/workflows/``,
plus every composite-action manifest under ``.github/actions/**/action.y*ml``, whose
inputs carry the same cap — an extension filter is a two-item hand-written list
(learnings 2026-07-30: a guard scoped by a hand-written list validates the fix, not
the codebase), and a file added under the other spelling would otherwise sit silently
outside a guard whose failure signature is "CI green, deploy run with zero jobs".

Measured quantity: the PARSED, de-indented scalar — the raw block text carries a
12-space indent per line and would read ~4,300 too high — in UTF-8 BYTES: GitHub's
message does not say whether it counts characters or bytes, ``dev`` was over on either
reading, and bytes is the reading that cannot be wrong in the unsafe direction (the
deploy script's em dashes cost 78). A long string WITHOUT an expression is not capped
and is deliberately left alone.

The structural fix for the one block that lived near the cap — passing the values
through step ``env:`` + ssh-action ``envs:`` so the script carries no expression at
all — landed with trinity#2626 (#2629). The script is now ~22,000 bytes and OVER the
cap, which is safe only for as long as it stays expression-free: one ``${{ }}`` put
back anywhere in it, even in a comment, turns the whole scalar back into one
expression and the file stops parsing. ``test_the_deploy_script_carries_no_expression``
pins that directly, and the padded meta-test proves the guard would fire the moment
one returns.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_GITHUB = _REPO / ".github"

EXPRESSION_CAP = 21_000
EXPRESSION_CAP_MARGIN = 1_000
LIMIT = EXPRESSION_CAP - EXPRESSION_CAP_MARGIN


def workflow_files(github_dir: Path = _GITHUB) -> list[Path]:
    """Every file GitHub parses for expressions: workflows in both spellings, and
    composite-action manifests (their inputs carry the same cap)."""
    workflows = github_dir / "workflows"
    actions = github_dir / "actions"
    files = [
        *workflows.glob("*.yml"),
        *workflows.glob("*.yaml"),
        *actions.glob("**/action.yml"),
        *actions.glob("**/action.yaml"),
    ]
    return sorted(set(files))


def _size(s: str) -> int:
    return len(s.encode("utf-8"))


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
    over = [(path, _size(s)) for path, s in expression_bearing_strings(doc) if _size(s) > LIMIT]
    assert not over, (
        f"{source}: expression-bearing string(s) past {LIMIT} bytes "
        f"(GitHub's cap is {EXPRESSION_CAP}; this guard keeps {EXPRESSION_CAP_MARGIN} in hand, #2625): "
        + ", ".join(f"{path}={n}" for path, n in over)
        + ". Trim prose (the reasoning lives in the PR, docs and the ledger) or move the "
        "`${{ }}` out of the string so it is no longer an expression (trinity#2626)."
    )


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: str(p.relative_to(_GITHUB)))
def test_no_expression_bearing_string_nears_the_cap(path: Path):
    check_no_expression_bearing_string_nears_the_cap(_load(path), source=str(path.relative_to(_REPO)))


# ---------------------------------------------------------------------------
# Vacuity guards (learnings 2026-08-03): the walk must see the file and the
# block that bit, and the file set must not be an accidental two-item list.
# ---------------------------------------------------------------------------

def test_the_file_set_includes_the_file_that_bit():
    assert _GITHUB / "workflows" / "deploy-dev.yml" in workflow_files()


def test_the_file_set_covers_both_spellings_and_composite_actions(tmp_path: Path):
    gh = tmp_path / ".github"
    (gh / "workflows").mkdir(parents=True)
    (gh / "actions" / "x").mkdir(parents=True)
    for rel in ("workflows/a.yml", "workflows/b.yaml", "actions/x/action.yml"):
        (gh / rel).write_text("name: t\n", encoding="utf-8")
    (gh / "workflows" / "notes.md").write_text("not a workflow\n", encoding="utf-8")
    found = {str(p.relative_to(gh)) for p in workflow_files(gh)}
    assert found == {"workflows/a.yml", "workflows/b.yaml", "actions/x/action.yml"}


_DEPLOY = _GITHUB / "workflows" / "deploy-dev.yml"
_SCRIPT_HEAD = "            set -e\n"


def _deploy_script(doc) -> str:
    return next(s for path, s in _strings(doc) if path.endswith(".with.script"))


def _with_one_expression(text: str) -> str:
    """The live script carries no expression (trinity#2626); the meta-tests put ONE
    back so the block is an expression again, the shape that broke ``dev``."""
    mutated = text.replace(_SCRIPT_HEAD, "            set -e  # ${{ github.sha }}\n", 1)
    assert mutated != text, "the mutation did not change the file — it would prove nothing"
    return mutated


def test_the_deploy_script_carries_no_expression():
    """trinity#2626: every workflow-context value the ssh script needs is resolved in
    the step ``env:`` and passed through ``envs:``. The script is over the cap in
    bytes, so it is safe ONLY while it contains no ``${{`` — a comment included."""
    doc = _load(_DEPLOY)
    script = _deploy_script(doc)
    assert "${{" not in script, "a workflow expression is back in the deploy script — see trinity#2626"
    step = next(s for s in doc["jobs"]["deploy"]["steps"] if "script" in (s.get("with") or {}))
    passed = set(step["with"]["envs"].split(","))
    assert passed == set(step["env"]), (passed, set(step["env"]))


def test_the_scan_sees_the_block_that_bit():
    """The walk must reach ``.with.script`` — proven by putting one expression back
    into the live file and watching the path appear."""
    text = _DEPLOY.read_text(encoding="utf-8")
    paths = [path for path, _ in expression_bearing_strings(yaml.safe_load(_with_one_expression(text)))]
    assert any(path.endswith(".with.script") for path in paths), paths


def test_the_guard_measures_the_parsed_scalar_in_bytes():
    """Bytes ≥ characters (the em dashes), and the parsed scalar is what is measured —
    never the raw, indented block text, which reads thousands higher."""
    script = _deploy_script(_load(_DEPLOY))
    assert _size(script) >= len(script)
    raw_block = "\n".join("            " + line for line in script.splitlines())
    assert _size(raw_block) > _size(script)


# ---------------------------------------------------------------------------
# Meta-tests: the guard goes red on the shape that broke dev, wherever the
# expression sits, and stays quiet on a long string that carries none.
# ---------------------------------------------------------------------------

def test_the_guard_rejects_the_deploy_script_the_moment_an_expression_returns():
    """The live script is already past the cap in bytes; a single expression put back
    is enough for the guard to fire on ``.with.script`` — no padding needed."""
    text = _DEPLOY.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="with.script"):
        check_no_expression_bearing_string_nears_the_cap(yaml.safe_load(_with_one_expression(text)), source="one-expr")


def test_the_guard_rejects_the_deploy_script_padded_back_past_the_cap():
    text = _with_one_expression(_DEPLOY.read_text(encoding="utf-8"))
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
