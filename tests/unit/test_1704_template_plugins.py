"""Unit tests for #1704 — the `plugins:` template reader (`services/template_plugins.py`).

The reader is a **total** leaf (never raises), mirroring `template_schedules.py`:
it degrades a malformed block to named errors and a safe empty declaration, so a
bad `plugins:` block can never empty the catalog (bare list comprehensions in
`get_all_templates()`) or enter creation's rollback fence.

Security-critical assertions here (autoplan security phase):
  * a marketplace `source` embedding `user:token@` userinfo is REJECTED — a
    committed, world-readable manifest must never carry a credential;
  * a non-`owner/repo` / non-`https://` source, and any shell-metacharacter /
    traversal / leading-`-` name, is dropped with a named error;
  * `enabledPlugins: false` entries are dropped;
  * an empty declaration is a full no-op (`{}`);
  * a stable input yields a byte-identical serialization (determinism —
    churn-safety for the 15-min auto-sync loop).

The module is loaded by FILE PATH under a standalone name so the test stays a
fast pure-unit test — importing `services.template_plugins` triggers
`services/__init__` (docker/pydantic), which this reader does not need. Only
`utils.credential_sanitizer` (stdlib-only) is imported for real.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


def _load():
    path = _REPO / "src" / "backend" / "services" / "template_plugins.py"
    spec = importlib.util.spec_from_file_location("template_plugins_1704", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tp = _load()
normalize = tp.normalize_declared_plugins
errors_of = tp.plugin_shape_errors


# ---------------------------------------------------------------------------
# Happy path + shape acceptance
# ---------------------------------------------------------------------------


def test_basic_declaration_normalizes():
    block = {
        "marketplaces": [{"name": "abilityai", "source": "abilityai/abilities"}],
        "installed": ["trinity@abilityai"],
    }
    assert normalize(block) == {
        "marketplaces": [{"name": "abilityai", "source": "abilityai/abilities"}],
        "installed": ["trinity@abilityai"],
    }
    assert errors_of(block) == []


def test_https_source_accepted():
    block = {
        "marketplaces": [{"name": "ev", "source": "https://github.com/owner/repo"}],
        "installed": ["p@ev"],
    }
    out = normalize(block)
    assert out["marketplaces"][0]["source"] == "https://github.com/owner/repo"
    assert out["installed"] == ["p@ev"]


def test_enabled_plugins_mapping_form_true_only():
    block = {
        "marketplaces": [{"name": "ab", "source": "abilityai/abilities"}],
        "enabledPlugins": {"trinity@ab": True, "beta@ab": False},
    }
    out = normalize(block)
    assert out["installed"] == ["trinity@ab"]  # false entry dropped


def test_installed_list_and_enabled_map_union_and_dedupe():
    block = {
        "marketplaces": [{"name": "ab", "source": "a/b"}],
        "installed": ["one@ab", "one@ab"],
        "enabledPlugins": {"two@ab": True},
    }
    out = normalize(block)
    assert out["installed"] == ["one@ab", "two@ab"]  # sorted + de-duplicated


# ---------------------------------------------------------------------------
# Determinism (churn-safety)
# ---------------------------------------------------------------------------


def test_byte_stable_serialization_regardless_of_input_order():
    a = {
        "marketplaces": [
            {"name": "zeta", "source": "z/z"},
            {"name": "alpha", "source": "a/a"},
        ],
        "installed": ["z@zeta", "a@alpha"],
    }
    b = copy.deepcopy(a)
    b["marketplaces"].reverse()
    b["installed"].reverse()
    ser_a = yaml.safe_dump({"plugins": normalize(a)}, sort_keys=True)
    ser_b = yaml.safe_dump({"plugins": normalize(b)}, sort_keys=True)
    assert ser_a == ser_b
    # sorted output, not input order
    assert normalize(a)["marketplaces"][0]["name"] == "alpha"
    assert normalize(a)["installed"] == ["a@alpha", "z@zeta"]


# ---------------------------------------------------------------------------
# Opt-in no-op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block", [None, {}, [], {"marketplaces": [], "installed": []}])
def test_empty_is_a_full_no_op(block):
    assert normalize(block) == {}


def test_non_mapping_block_is_a_named_error_not_a_raise():
    assert normalize("scalar") == {}
    assert errors_of("scalar")  # one named error, never a raise


# ---------------------------------------------------------------------------
# Security — the marketplace `source` is the dangerous argument
# ---------------------------------------------------------------------------


def test_userinfo_url_source_is_refused():
    block = {
        "marketplaces": [{"name": "ev", "source": "https://user:tok@github.com/o/r"}],
        "installed": [],
    }
    assert normalize(block) == {}
    assert any("credentials" in e for e in errors_of(block))


def test_non_owner_repo_source_is_refused():
    for bad in [
        "not a repo; rm -rf",
        "http://github.com/o/r",
        "git@github.com:o/r",
        "../../etc",
        "/abs/path",
    ]:
        block = {"marketplaces": [{"name": "ev", "source": bad}]}
        assert normalize(block) == {}, f"{bad!r} should be refused"
        assert errors_of(block)


def test_metacharacter_names_are_dropped():
    block = {
        "marketplaces": [{"name": "ab", "source": "a/b"}],
        "installed": ["p$(x)@ab", "ok@ab"],
    }
    out = normalize(block)
    assert out["installed"] == ["ok@ab"]
    assert any("invalid" in e for e in errors_of(block))


def test_traversal_marketplace_name_dropped():
    block = {"marketplaces": [{"name": "../evil", "source": "a/b"}]}
    assert normalize(block) == {}
    assert errors_of(block)


def test_install_referencing_undeclared_marketplace_dropped():
    block = {
        "marketplaces": [{"name": "ab", "source": "a/b"}],
        "installed": ["p@zzz"],
    }
    out = normalize(block)
    assert out["installed"] == []  # marketplace not declared → cannot be added
    assert any("not declared" in e for e in errors_of(block))


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_marketplace_cap_enforced():
    markets = [
        {"name": f"m{i}", "source": f"o/r{i}"} for i in range(tp.MAX_MARKETPLACES + 5)
    ]
    out = normalize({"marketplaces": markets})
    assert len(out["marketplaces"]) == tp.MAX_MARKETPLACES


def test_plugin_cap_enforced():
    block = {
        "marketplaces": [{"name": "ab", "source": "a/b"}],
        "installed": [f"p{i}@ab" for i in range(tp.MAX_PLUGINS + 10)],
    }
    out = normalize(block)
    assert len(out["installed"]) == tp.MAX_PLUGINS
