"""System-manifest YAML hardening — size, expansion, duplicate keys (#1884).

`yaml.safe_load` blocks arbitrary object construction but not two other things,
and the MCP pipeline reader (#919) already set the house standard for this class:
size cap + duplicate-key guard + alias guard. PyYAML has no equivalent of the JS
`yaml` library's `uniqueKeys` / `maxAliasCount`, so both are implemented in
`system_service` and pinned here.

**A measured correction to the issue's premise, recorded so the next reader does
not re-derive it.** The issue describes a few-hundred-byte manifest expanding to
gigabytes and pinning a worker. That does not reproduce on PyYAML: it memoises
constructed objects per node, so a billion-laughs document yields a
shared-reference DAG (~20 KB peak, milliseconds), not an exponential tree. The
expansion guard is kept anyway — the DAG re-expands the instant anything walks or
serialises it (`json.dumps`, a deep copy, echoing the manifest back) — but the
duplicate-key half is the one with immediate, user-visible impact.

That correction is also why the budget counts **expansion cost, not aliases**: a
classic bomb uses only ~45 aliases and would sail under any count-based limit,
because the blow-up is multiplicative per level.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _svc():
    try:
        from services import system_service
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return system_service


def _bomb(levels: int, fan: int = 9) -> str:
    """A classic billion-laughs pyramid: each level references the one below
    `fan` times, so the logical size is fan**levels."""
    lines = ["name: bomb", 'a0: &a0 ["x","x","x","x","x","x","x","x","x"]']
    for i in range(1, levels):
        refs = ",".join([f"*a{i - 1}"] * fan)
        lines.append(f"a{i}: &a{i} [{refs}]")
    lines.append(f"top: [{','.join([f'*a{levels - 1}'] * fan)}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Expansion budget
# ---------------------------------------------------------------------------

def test_billion_laughs_is_refused_by_name():
    """AC: a named 400-able error, not a timeout and not an unnamed 500."""
    svc = _svc()
    doc = _bomb(levels=8)
    assert len(doc) < 1000, "the point of a bomb is that the source is tiny"

    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest(doc)
    assert exc.value.code == "manifest_alias_budget_exceeded"


def test_the_budget_counts_expansion_not_alias_count():
    """The reason a `maxAliasCount`-style limit is the wrong shape here.

    This bomb uses far FEWER than 100 alias references, so a count-based budget
    would pass it — while its expanded size is in the millions.
    """
    svc = _svc()
    doc = _bomb(levels=8)
    assert doc.count("*a") < 100, "otherwise this test proves nothing"

    with pytest.raises(svc.ManifestError):
        svc.parse_manifest(doc)


def test_legitimate_anchor_reuse_still_parses():
    """The guard must not break the feature. Merge keys are the normal, useful
    way to share defaults across agents in a manifest — an over-tight budget
    would make anchors unusable and get the guard removed."""
    svc = _svc()
    manifest = svc.parse_manifest(
        "name: sys\n"
        "defaults: &d\n"
        "  template: 'local:default'\n"
        "agents:\n"
        "  a: {<<: *d}\n"
        "  b: {<<: *d}\n"
        "  c: {<<: *d}\n"
    )
    assert sorted(manifest.agents) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Duplicate keys — the half with immediate user-visible impact
# ---------------------------------------------------------------------------

def test_duplicate_agents_block_is_refused():
    """`safe_load` silently keeps the LAST duplicate, so this manifest would
    have deployed only `b` with no signal — the real footgun now that the
    manifest is hand-editable in a textarea."""
    svc = _svc()
    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest(
            "name: sys\n"
            "agents:\n"
            "  a: {template: 'local:x'}\n"
            "agents:\n"
            "  b: {template: 'local:y'}\n"
        )
    assert exc.value.code == "manifest_duplicate_key"
    assert "agents" in str(exc.value)


def test_duplicate_key_inside_one_agent_is_also_refused():
    """Applied at every mapping depth, not only the two levels the issue names —
    a duplicated `template:` is the same silent-last-wins bug one level down."""
    svc = _svc()
    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest(
            "name: sys\n"
            "agents:\n"
            "  a:\n"
            "    template: 'local:x'\n"
            "    template: 'local:y'\n"
        )
    assert exc.value.code == "manifest_duplicate_key"


def test_the_error_names_the_line():
    """A refusal an operator can act on. 'Duplicate key' without a location in a
    200-line manifest is a worse experience than the silent bug."""
    svc = _svc()
    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest(
            "name: sys\n"
            "agents:\n"
            "  a: {template: 'local:x'}\n"
            "agents:\n"
            "  b: {template: 'local:y'}\n"
        )
    assert "line 4" in str(exc.value)


# ---------------------------------------------------------------------------
# Size cap, and the contract the callers rely on
# ---------------------------------------------------------------------------

def test_oversized_manifest_is_refused():
    svc = _svc()
    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest("name: x\n" + "#" * svc.MANIFEST_MAX_BYTES)
    assert exc.value.code == "manifest_too_large"


def test_manifest_error_is_a_valueerror():
    """Both call sites (`deploy_manifest`, `system_seed_service`) catch
    ValueError. Subclassing keeps them working unchanged — the named code is
    additive, not a breaking change."""
    svc = _svc()
    assert issubclass(svc.ManifestError, ValueError)


def test_a_normal_manifest_is_unaffected():
    svc = _svc()
    manifest = svc.parse_manifest(
        "name: sys\n"
        "description: a normal system\n"
        "agents:\n"
        "  worker: {template: 'local:default'}\n"
    )
    assert manifest.name == "sys"
    assert list(manifest.agents) == ["worker"]


def test_invalid_yaml_still_reports_as_a_parse_error():
    """Malformed YAML must not be mislabelled as a bomb or a duplicate key."""
    svc = _svc()
    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest("name: [unclosed\n")
    assert exc.value.code == "manifest_yaml_invalid"


# ---------------------------------------------------------------------------
# Gaps found reviewing this change, not writing it
# ---------------------------------------------------------------------------

def test_a_wide_shallow_bomb_is_also_refused():
    """The tests above are all DEEP pyramids. A bomb can equally be wide and
    shallow — two levels with a huge fan — which a level-based intuition misses
    entirely. The budget is shape-agnostic because it counts expanded nodes, and
    this pins that rather than leaving it to luck."""
    svc = _svc()
    fan = 400
    doc = (
        "name: b\n"
        'a: &a ["' + '","'.join(["x"] * fan) + '"]\n'
        f"b: [{','.join(['*a'] * fan)}]\n"
    )
    assert len(doc) < 4000
    with pytest.raises(svc.ManifestError) as exc:
        svc.parse_manifest(doc)
    assert exc.value.code == "manifest_alias_budget_exceeded"


def test_every_shipped_manifest_still_parses():
    """The guard must not brick first-run seeding.

    `config/manifests/default-system.yaml` is deployed automatically on a fresh
    install (`system_seed_service`), so a false positive here would not be a
    rejected request — it would be a broken installation. Parse everything the
    repo ships.
    """
    import glob

    svc = _svc()
    root = Path(__file__).resolve().parents[2]
    manifests = sorted(glob.glob(str(root / "config" / "manifests" / "*.yaml")))
    assert manifests, "expected bundled manifests to exist"

    for path in manifests:
        with open(path) as fh:
            manifest = svc.parse_manifest(fh.read())
        assert manifest.agents, f"{path} parsed to zero agents"
