"""#1965 — the agent server was outside ent#314's YAML sweep.

`utils/safe_yaml.py` (ent#314, PR #1961) put every author-controlled YAML reader
in the backend behind one hardened loader, and its AST guard walks the whole
backend with an empty allowlist so a new bare `yaml.safe_load` there fails the
build. That guard walked `_BACKEND.rglob("*.py")` **only**, and
`docker/base-image/agent_server/` kept parsing the same documents bare:

| Site | Document | Backend policy for the same document |
|---|---|---|
| `routers/info.py` ×2 | `template.yaml` | REJECT (`credential_requirements_service`) |
| `routers/skills.py` | skill frontmatter | REJECT (`skill_packaging`) |
| `routers/dashboard.py` | `dashboard.yaml` | REJECT (`compatibility/static_checks`) |
| `routers/files.py` | `.trinity/persistent-state.yaml` | — (agent-authored, S4 #383) |

The vector ent#314 measured is amplification at **serialization**, not parse: a
416 B level-6 anchor bomb resolves in ~0.001 s and blows up to ~110 MB when
something walks the graph. The backend proxies `/info` and `/dashboard`, so that
walk happens inside the container first and then again across the wire.

Lower privilege than the catalog path ent#314 closed — this needs an already
created agent whose workspace you can write — which is why #1961 shipped without
it. But an in-container `template.yaml` is precisely the "live agent-writable"
document ent#314 assigns the strictest REJECT policy on the backend side.

The AST-guard extension itself lives in `test_ent314_hardened_yaml.py` (widened
to both trees, empty allowlist), because splitting a guard across two files is
how the second copy stops being run.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CANON = _ROOT / "src" / "backend" / "utils" / "safe_yaml.py"
_VENDORED = _ROOT / "docker" / "base-image" / "agent_server" / "safe_yaml.py"
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"


# ---------------------------------------------------------------------------
# Vendored-mirror parity (the `credential_paths.py` shape).
# ---------------------------------------------------------------------------


def test_loader_copies_are_byte_identical():
    """AC #1. The agent server ships in its own image and structurally cannot
    import `src/backend`, so the loader is vendored. Both copies MUST stay
    byte-identical, or the two trees enforce different alias budgets on the
    same document — the divergence Invariant #5 exists to prevent.

    Edit the canonical file and copy it over the vendored one.
    """
    assert _CANON.exists(), "the canonical loader is gone"
    assert _VENDORED.exists(), (
        "safe_yaml.py is not vendored into the agent server (#1965)"
    )
    assert _CANON.read_bytes() == _VENDORED.read_bytes(), (
        "safe_yaml.py drifted between backend and agent-server — re-copy the "
        "canonical file over the vendored one."
    )


def _load_vendored():
    """Import the vendored copy standalone, exactly as a byte-parity mirror
    should be importable (it has no intra-package imports)."""
    spec = importlib.util.spec_from_file_location("_vendored_safe_yaml_1965", _VENDORED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def vendored():
    try:
        return _load_vendored()
    except ImportError:  # pragma: no cover - PyYAML required
        pytest.skip("PyYAML required")


def _alias_bomb(levels: int) -> str:
    """The ent#314 shape: each level references the one below it ten times."""
    lines = ["a0: &a0 'x'"]
    for i in range(1, levels + 1):
        prev = f"*a{i - 1}"
        lines.append(f"a{i}: &a{i} [{', '.join([prev] * 10)}]")
    lines.append(f"root: *a{levels}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The vendored copy actually works (parity of BYTES is not parity of BEHAVIOUR
# if the file never imports).
# ---------------------------------------------------------------------------


def test_vendored_loader_refuses_a_level_six_bomb(vendored):
    with pytest.raises(vendored.HardenedYamlError) as exc:
        vendored.load_hardened_yaml(
            _alias_bomb(6), kind="template", alias_policy=vendored.AliasPolicy.BUDGET
        )
    assert exc.value.code == "template_alias_budget_exceeded"


def test_vendored_loader_refuses_any_alias_under_reject(vendored):
    with pytest.raises(vendored.HardenedYamlError) as exc:
        vendored.load_hardened_yaml(
            "a: &x 1\nb: *x\n",
            kind="template",
            alias_policy=vendored.AliasPolicy.REJECT,
        )
    assert exc.value.code == "template_alias_not_permitted"


def test_vendored_loader_rejects_duplicate_keys(vendored):
    """The `credentials:` case: a file can show one thing to a human reading the
    top and declare another to Trinity."""
    with pytest.raises(vendored.HardenedYamlError) as exc:
        vendored.load_hardened_yaml(
            "name: a\nname: b\n",
            kind="template",
            alias_policy=vendored.AliasPolicy.REJECT,
        )
    assert exc.value.code == "template_duplicate_key"


def test_vendored_loader_still_parses_an_honest_document(vendored):
    """A guard that refuses real templates is not a fix."""
    data = vendored.load_hardened_yaml(
        "name: my-agent\ncapabilities:\n  - brain-orb\n",
        kind="template",
        alias_policy=vendored.AliasPolicy.REJECT,
    )
    assert data == {"name": "my-agent", "capabilities": ["brain-orb"]}


# ---------------------------------------------------------------------------
# AC #2 — every site on the shared loader, with the policy its backend
# counterpart uses for the same document.
# ---------------------------------------------------------------------------


def _src(rel: str) -> str:
    return (_AGENT_SERVER / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("rel", "kind", "policy"),
    [
        pytest.param("routers/info.py", "template", "REJECT", id="template.yaml"),
        pytest.param("routers/skills.py", "frontmatter", "REJECT", id="frontmatter"),
        pytest.param(
            "routers/dashboard.py", "workspace_yaml", "REJECT", id="dashboard.yaml"
        ),
        pytest.param(
            "routers/files.py", "persistent_state", "REJECT", id="persistent-state"
        ),
    ],
)
def test_each_site_uses_its_backend_counterpart_policy(rel, kind, policy):
    src = _src(rel)
    assert "load_hardened_yaml(" in src, f"{rel} is not on the shared loader"
    assert f'kind="{kind}"' in src, f"{rel} does not use kind={kind!r}"
    assert f"AliasPolicy.{policy}" in src, f"{rel} does not use {policy}"


def test_the_platform_written_config_is_budget_not_reject():
    """`/config/agent-config.yaml` is the deliberate exception, and stating it
    is the point: the platform writes it and bind-mounts it `mode: 'ro'`, so the
    agent cannot author it. REJECT there would risk refusing a legitimate
    document, since `yaml.dump` emits an anchor for any shared object
    reference — a self-inflicted outage in exchange for no security."""
    src = _src("routers/info.py")
    assert 'kind="agent_config"' in src
    assert "AliasPolicy.BUDGET" in src


def test_no_bare_safe_load_remains_in_the_agent_server():
    """Belt to the ent#314 guard's braces. That guard is now widened to this
    tree; this asserts the outcome directly so a regression is legible from
    this file too."""
    import ast

    offenders = []
    for path in _AGENT_SERVER.rglob("*.py"):
        rel = str(path.relative_to(_AGENT_SERVER))
        if rel == "safe_yaml.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "safe_load":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"bare yaml.safe_load in the agent server: {offenders}"


def test_standalone_loadable_routers_keep_their_imports_function_local():
    """#1795 invariant, re-broken by this issue's first attempt.

    `files.py` must load via `spec_from_file_location` with NO package context —
    two tests do exactly that to check its protected-path logic. A module-level
    `from ..safe_yaml import …` raises `attempted relative import with no known
    parent package`, which is how the first version of this fix went red in CI
    against a rule that predates it by 170 issues.

    Asserted by actually loading the file standalone, not by grepping for
    `from ..` — the sibling routers legitimately use module-level relative
    imports, so the rule is "this file loads bare", not "nobody uses relative
    imports".
    """
    spec = importlib.util.spec_from_file_location(
        "_test1965_standalone_files", _AGENT_SERVER / "routers" / "files.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not raise
    assert hasattr(mod, "_read_persistent_state")


def test_hardened_error_is_caught_where_yamlerror_was():
    """`HardenedYamlError` is a **ValueError**, not a `yaml.YAMLError`.

    Every site that already had an `except yaml.YAMLError` needed it named
    explicitly, or a refused bomb escapes the handler that promised to turn a
    bad document into a clean answer — which is how a REJECT turns into the
    unnamed 500 / timeout the AC rules out. `static_checks._parse_yaml` records
    the same trap on the backend side.
    """
    dashboard = _src("routers/dashboard.py")
    assert "except HardenedYamlError" in dashboard, (
        "dashboard.py catches YAMLError but not HardenedYamlError — a refused "
        "bomb would fall through to the generic handler (#1965)"
    )
    files = _src("routers/files.py")
    assert "HardenedYamlError" in files


# ---------------------------------------------------------------------------
# AC #4 — a bomb in a container's template.yaml is REFUSED, by name.
# ---------------------------------------------------------------------------


@pytest.fixture
def info_router(monkeypatch, tmp_path):
    """Import the real agent-server info router with its template path
    redirected at a temp file."""
    agent_root = _ROOT / "docker" / "base-image"
    if str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))
    try:
        from agent_server.routers import info as mod
    except ImportError as exc:  # pragma: no cover - fastapi required
        pytest.skip(f"agent-server deps unavailable: {exc}")

    template = tmp_path / "template.yaml"
    monkeypatch.setattr(mod, "get_template_path", lambda: template, raising=False)
    return mod, template


def test_a_bomb_in_template_yaml_is_refused_not_served(info_router):
    """AC #4. The endpoint must decline the document — not hang walking it, and
    not serve the expanded graph back through the proxying backend."""
    import asyncio

    mod, template = info_router
    template.write_text(_alias_bomb(6))

    result = asyncio.run(mod.get_template_info())

    # The handler treats an unreadable template as "no template", which is the
    # pre-existing contract; what matters is that the bomb never becomes data.
    assert result.get("has_template") is False
    assert "a6" not in str(result), "the expanded bomb reached the response"


def test_an_honest_template_still_serves(info_router):
    """The other half: the guard must not refuse real agents."""
    import asyncio

    mod, template = info_router
    template.write_text("name: my-agent\ndescription: does things\n")

    result = asyncio.run(mod.get_template_info())

    assert result.get("has_template") is True


def test_the_metrics_endpoint_refuses_the_same_bomb(info_router):
    """Second `template.yaml` reader in the same file. Fixing one and not the
    other leaves the identical amplifier one endpoint over — the shape of this
    whole issue.

    Asserts the NAMED refusal, not just `has_metrics is False`: a bomb parses
    perfectly well under bare `safe_load` and yields no `metrics:` key, so the
    unfixed code also answers `has_metrics: False`. Checking only that flag
    would pass against the very tree this issue reports.
    """
    import asyncio

    mod, template = info_router
    template.write_text(_alias_bomb(6))

    result = asyncio.run(mod.get_metrics())

    assert result.get("has_metrics") is False
    assert "a6" not in str(result), "the expanded bomb reached the response"
    assert "not permitted" in result.get("message", ""), (
        "the document was parsed rather than refused — this assertion is the "
        "only thing separating the fixed tree from the unfixed one here"
    )
