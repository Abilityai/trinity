"""ent#314 — author-controlled YAML is parsed behind size/alias/duplicate guards.

`template.yaml` was loaded with bare `yaml.safe_load`, which stops arbitrary
object construction and nothing else. Two live vectors, both reachable by any
`creator`-role user pointing agent creation at a public GitHub repo (ent#123
made that tokenless, and `_build_template` runs unfenced inside
`get_all_templates()`, so one hostile repo is on the `/api/templates` path for
everyone who lists templates):

1. **Alias amplification at SERIALIZATION time.** Measured on this tree with a
   10-way nested anchor bomb terminating in `skills:`:

       level 4 — 298 B -> 1.10 MB json.dumps (3,700x)
       level 5 — 357 B -> 11.02 MB          (30,882x)
       level 6 — 416 B -> 110.25 MB         (265,017x)

   `safe_load` itself takes 0.0011 s at level 6 — the parse is free, the graph
   is a small shared-reference DAG, and the cost lands entirely on whatever
   walks it. An input-size cap therefore cannot close this: the input is small.

2. **Duplicate-key silence.** `safe_load` keeps the LAST duplicate with no
   signal, so a template can show one `credentials:` block to a human reading
   the file and declare a different one to Trinity.

#1884 hardened system manifests against exactly this and ent#314 makes that one
shared loader (`utils/safe_yaml.py`) rather than a fourth copy — there were
already three partial implementations plus the unguarded catalog path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
# #1965: the agent server is a separate image that cannot import `src/backend`,
# so it carries a byte-identical vendored copy of the loader (Invariant #5). It
# parses the same author-controlled documents and was outside ent#314's sweep.
_AGENT_SERVER = (
    Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server"
)
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from utils.safe_yaml import (  # noqa: E402
    AliasPolicy,
    HardenedYamlError,
    load_hardened_yaml,
)


def alias_bomb(levels: int) -> str:
    """The measured fixture: 10-way nesting terminating in a real template key."""
    lines = ["name: evil", "a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, levels + 1):
        lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i-1}"] * 10) + "]")
    lines.append(f"skills: *a{levels}")
    return "\n".join(lines) + "\n"


DUPLICATE_CREDENTIALS = """name: real
credentials:
  env_file: [FIRST_KEY]
credentials:
  env_file: [SECOND_KEY]
"""


# ---------------------------------------------------------------------------
# The vector still exists in bare safe_load — the fixture is not theoretical
# ---------------------------------------------------------------------------

def test_the_bomb_really_amplifies_under_bare_safe_load():
    """Guards the FIXTURE, not the product: if a future PyYAML stopped resolving
    aliases this way, every test below would pass vacuously."""
    src = alias_bomb(4)
    serialized = json.dumps(yaml.safe_load(src))

    assert len(src.encode()) < 500
    assert len(serialized) > 1_000_000, "the fixture no longer amplifies"


def test_bare_safe_load_silently_keeps_the_last_duplicate():
    """The behaviour the guard exists to stop, pinned so the test file states
    the problem rather than assuming the reader knows it."""
    assert yaml.safe_load(DUPLICATE_CREDENTIALS)["credentials"] == {
        "env_file": ["SECOND_KEY"]
    }


# ---------------------------------------------------------------------------
# AC 1 — a level-6 bomb is rejected at parse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("levels", [4, 5, 6])
@pytest.mark.parametrize("policy", [AliasPolicy.BUDGET, AliasPolicy.REJECT])
def test_alias_bomb_is_rejected_at_parse(levels, policy):
    with pytest.raises(HardenedYamlError) as exc:
        load_hardened_yaml(alias_bomb(levels), kind="template", alias_policy=policy)
    assert exc.value.code in {
        "template_alias_budget_exceeded",
        "template_alias_not_permitted",
    }


def test_a_small_honest_anchor_still_parses_under_budget():
    """The #1932 lesson: a guard that rejects the LEGITIMATE document reads as
    'hardened' in an audit while being an outage. A real template may anchor a
    shared block, and the budget admits it."""
    doc = """name: fine
common: &common [git, docker]
skills: *common
"""
    assert load_hardened_yaml(doc, kind="template", alias_policy=AliasPolicy.BUDGET) == {
        "name": "fine",
        "common": ["git", "docker"],
        "skills": ["git", "docker"],
    }


def test_reject_policy_refuses_even_one_alias():
    doc = "name: fine\ncommon: &c [git]\nskills: *c\n"
    with pytest.raises(HardenedYamlError) as exc:
        load_hardened_yaml(doc, kind="frontmatter", alias_policy=AliasPolicy.REJECT)
    assert exc.value.code == "frontmatter_alias_not_permitted"


def test_reject_policy_also_gates_at_the_scanner():
    """`skill_packaging`'s copy had BOTH a `fetch_alias` and a `compose_node`
    gate. The consolidation must not silently drop the stricter one, so the
    merge-key form (which reaches the scanner) is pinned separately."""
    with pytest.raises(HardenedYamlError):
        load_hardened_yaml(
            "a: &x {k: 1}\n<<: *x\n", kind="frontmatter", alias_policy=AliasPolicy.REJECT
        )


# ---------------------------------------------------------------------------
# AC 2 — duplicate keys are a named error, at every depth
# ---------------------------------------------------------------------------

def test_duplicate_credentials_block_is_a_named_error():
    """The issue's exact case. This is the one that matters most: for a
    credential declaration, last-wins means the file does not mean what a human
    reviewer reads at the top of it."""
    with pytest.raises(HardenedYamlError) as exc:
        load_hardened_yaml(
            DUPLICATE_CREDENTIALS, kind="template", alias_policy=AliasPolicy.BUDGET
        )
    assert exc.value.code == "template_duplicate_key"
    assert "credentials" in str(exc.value)
    assert "line" in str(exc.value), "the error should point at the offending line"


def test_duplicate_key_is_caught_at_nested_depth_too():
    doc = """name: real
credentials:
  env_file: [A]
  env_file: [B]
"""
    with pytest.raises(HardenedYamlError) as exc:
        load_hardened_yaml(doc, kind="template", alias_policy=AliasPolicy.BUDGET)
    assert exc.value.code == "template_duplicate_key"


# ---------------------------------------------------------------------------
# AC 3 — the fetched document is size-capped
# ---------------------------------------------------------------------------

def test_oversize_document_is_refused_before_parsing():
    huge = "name: x\ndescription: " + ("a" * 300_000) + "\n"
    with pytest.raises(HardenedYamlError) as exc:
        load_hardened_yaml(huge, kind="template", alias_policy=AliasPolicy.BUDGET)
    assert exc.value.code == "template_too_large"


def test_template_service_applies_the_cap_to_fetched_yaml():
    """Assert the BEHAVIOUR through template_service, and the constant at its
    canonical home. An earlier draft read `ts.TEMPLATE_YAML_MAX_BYTES`, which
    only existed as a re-export the test itself had caused — a constant kept
    alive by its own assertion."""
    from services import template_service as ts
    from utils.safe_yaml import TEMPLATE_YAML_MAX_BYTES

    assert TEMPLATE_YAML_MAX_BYTES == 256 * 1024
    with pytest.raises(HardenedYamlError):
        ts.parse_template_yaml("name: x\nd: " + "a" * 300_000)


# ---------------------------------------------------------------------------
# Rejection, never truncation
# ---------------------------------------------------------------------------

def test_a_refused_document_raises_rather_than_returning_a_partial():
    """"Reject rather than truncate, so a hostile template fails loudly instead
    of being silently reinterpreted" — a truncating guard would be the
    duplicate-key bug again, one layer up."""
    with pytest.raises(HardenedYamlError):
        load_hardened_yaml(alias_bomb(6), kind="template", alias_policy=AliasPolicy.BUDGET)


# ---------------------------------------------------------------------------
# AC 4 — ONE loader; the manifest path migrated onto it, no fourth copy
# ---------------------------------------------------------------------------

def test_manifest_error_is_a_hardened_yaml_error_subclass():
    """So the shared loader can raise the manifest's own type and every existing
    `except ManifestError` — and the router's code-to-400 mapping — is unchanged."""
    from services.system_service import ManifestError

    assert issubclass(ManifestError, HardenedYamlError)


def test_manifest_keeps_its_published_error_codes():
    """The codes are consumed by `routers/systems.py` to answer a NAMED 400.
    A refactor that renamed them would turn a clear 400 into a generic one."""
    from services.system_service import ManifestError, _load_manifest_yaml

    with pytest.raises(ManifestError) as exc:
        _load_manifest_yaml(alias_bomb(6))
    assert exc.value.code == "manifest_alias_budget_exceeded"

    with pytest.raises(ManifestError) as exc:
        _load_manifest_yaml("name: a\nagents: {}\nagents: {}\n")
    assert exc.value.code == "manifest_duplicate_key"

    with pytest.raises(ManifestError) as exc:
        _load_manifest_yaml("x: " + "a" * 300_000)
    assert exc.value.code == "manifest_too_large"


def test_no_module_grows_a_fourth_hardened_loader():
    """The consolidation is the point of AC 4. Three near-copies had already
    accumulated (manifest + two `_NoAliasSafeLoader`s) while the catalog path had
    none — which is how the hole survived. A new local SafeLoader subclass is
    how that starts again."""
    offenders = []
    for path in (_BACKEND / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "yaml.SafeLoader)" in text and "class " in text:
            for line in text.splitlines():
                if line.strip().startswith("class ") and "yaml.SafeLoader" in line:
                    offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "define the policy in utils/safe_yaml.py and pass it as an argument "
        f"instead of subclassing SafeLoader again (ent#314): {offenders}"
    )


# Parsers of PLATFORM-authored YAML, where the document is not attacker
# controlled. Each needs a stated reason; the point of the allowlist is that
# adding to it is a visible decision, not a silent omission.
_BARE_SAFE_LOAD_ALLOWED: dict = {}


def test_no_service_parses_yaml_without_the_shared_loader():
    """Scans the WHOLE services tree, not a hand-listed set.

    The first version of this guard listed the four files it had just fixed, so
    it passed while `agent_service/crud.py` — `template.yaml` on the *creation*
    path, the exact file this issue is about — was still on bare `safe_load`,
    along with `system_agent_service`, `compatibility/static_checks`,
    `agent_service/lifecycle` and `git_service`. A guard whose scope is "the
    files I remembered" cannot find the one you forgot; that is how three
    partial loaders accumulated while the catalog path had none.

    **#1965: and the WHOLE agent server too.** The first version of this scan
    walked `_BACKEND.rglob` only, so `docker/base-image/agent_server/` stayed
    outside the sweep with six bare `safe_load` calls on documents the backend
    itself assigns REJECT — `template.yaml`, skill frontmatter, `dashboard.yaml`
    and `.trinity/persistent-state.yaml`. A guard scoped to one tree cannot see
    the sibling tree that parses the same documents, which is the same
    self-fulfilling-scope failure this test's own docstring describes, one
    directory up.
    """
    import ast

    offenders = []
    # The WHOLE backend, not just services/: an earlier draft scanned only the
    # directory it had just fixed, which is the same self-fulfilling scope that
    # let `agent_service/crud.py` sit unguarded. `utils/safe_yaml.py` is the one
    # module allowed to call PyYAML directly — it IS the guard.
    #
    # `_AGENT_SERVER` is a separate image that structurally cannot import
    # `src/backend`, so it carries a byte-identical vendored copy (Invariant #5)
    # — hence two exempt paths, one per tree.
    roots = [
        (_BACKEND, {"utils/safe_yaml.py"}),
        (_AGENT_SERVER, {"safe_yaml.py"}),
    ]
    for root, exempt in roots:
        if not root.exists():  # pragma: no cover - partial checkout
            continue
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root))
            if rel in exempt or "/venv/" in rel or rel.startswith("migrations/"):
                continue
            if rel in _BARE_SAFE_LOAD_ALLOWED:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # newer syntax than this interpreter — not our call
                continue
            for node in ast.walk(tree):
                # AST, not a line scan: `template_schedules.py`'s module docstring
                # QUOTES `yaml.safe_load(...)` while explaining the guard, and a
                # textual scan reported that prose as a vulnerability. The repo's
                # own learnings entry (2026-07-10) records this exact trap.
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "safe_load":
                        offenders.append(f"{root.name}/{rel}:{node.lineno}")

    assert not offenders, (
        "author-controlled YAML must go through the shared hardened loader "
        f"(ent#314 / #1965). Unguarded parses: {offenders}"
    )


def test_the_named_consumers_are_actually_on_the_shared_loader():
    """The scan above proves nothing bare remains; this proves the replacement
    is the shared loader rather than, say, a deleted call."""
    for rel in (
        "services/template_service.py",
        "services/system_service.py",
        "services/skill_packaging.py",
        "services/credential_requirements_service.py",
        "services/compatibility/static_checks.py",
        "services/git_service/trinity_files.py",
        "services/agent_service/lifecycle.py",
        "services/agent_service/crud.py",
        "services/system_agent_service.py",
    ):
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        # Either the raw loader or a policy wrapper from the same module — the
        # property is "parses through utils/safe_yaml", not one symbol name.
        assert "utils.safe_yaml" in text, f"{rel} is not on the shared loader"
