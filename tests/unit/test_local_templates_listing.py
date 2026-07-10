"""
Unit tests for the local-templates listing (#843).

The list endpoint at `routers/templates.py` defers to
`services/template_service.py::get_local_templates()` and
`get_local_template()`. Tests load template_service standalone (no
backend deps) and exercise the local-template scan against a
temporary directory shaped like `config/agent-templates/`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_template_service(monkeypatch, fake_templates_dir: Path):
    """Load template_service.py and redirect `_local_templates_dir`
    to point at our fixture instead of `/agent-configs/templates`."""
    # Backend pulls `config` at import-time. Stub via monkeypatch.setitem
    # (not bare `sys.modules[...]=` — would trip tests/lint_sys_modules.py
    # baseline check). monkeypatch undoes the insertion on test teardown.
    if "config" not in sys.modules:
        import types
        config_mod = types.ModuleType("config")
        config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
        config_mod.GITHUB_PAT_CREDENTIAL_ID = "test-pat"
        monkeypatch.setitem(sys.modules, "config", config_mod)

    spec = importlib.util.spec_from_file_location(
        "ts_under_test", _BACKEND / "services" / "template_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_local_templates_dir", lambda: fake_templates_dir)
    return module


def _seed_template(parent: Path, name: str, body: str | None = None) -> Path:
    """Create a fake local-template directory with template.yaml."""
    tdir = parent / name
    tdir.mkdir(parents=True)
    if body is not None:
        (tdir / "template.yaml").write_text(body)
    return tdir


# -----------------------------------------------------------------------------
# get_local_templates
# -----------------------------------------------------------------------------

def test_lists_templates_with_template_yaml(tmp_path, monkeypatch):
    """A directory with a parseable template.yaml is listed."""
    _seed_template(tmp_path, "dd-compliance", body="""
name: dd-compliance
display_name: DD Compliance Agent
description: Regulatory and compliance analysis
capabilities:
  - regulatory-research
  - compliance-assessment
use_cases:
  - Assess regulation
""")
    ts = _load_template_service(monkeypatch, tmp_path)

    templates = ts.get_local_templates()
    assert len(templates) == 1
    t = templates[0]
    assert t["id"] == "local:dd-compliance"
    assert t["display_name"] == "DD Compliance Agent"
    assert t["description"] == "Regulatory and compliance analysis"
    assert t["source"] == "local"
    assert t["capabilities"] == ["regulatory-research", "compliance-assessment"]
    assert t["use_cases"] == ["Assess regulation"]


def test_skips_directories_without_template_yaml(tmp_path, monkeypatch):
    """Directories under templates/ that lack template.yaml are silently
    skipped — they're not Trinity templates."""
    _seed_template(tmp_path, "no-yaml")  # no template.yaml
    _seed_template(tmp_path, "real-one", body="name: real-one\ndisplay_name: Real")
    ts = _load_template_service(monkeypatch, tmp_path)

    templates = ts.get_local_templates()
    ids = {t["id"] for t in templates}
    assert ids == {"local:real-one"}


def test_skips_unparseable_yaml(tmp_path, monkeypatch):
    """Templates with broken YAML are logged and skipped, not surfaced as
    half-formed entries — better to omit than confuse the UI."""
    _seed_template(tmp_path, "broken", body="name: [unclosed bracket")
    _seed_template(tmp_path, "ok", body="name: ok\ndisplay_name: OK")
    ts = _load_template_service(monkeypatch, tmp_path)

    templates = ts.get_local_templates()
    ids = {t["id"] for t in templates}
    assert ids == {"local:ok"}


def test_returns_empty_when_dir_missing(tmp_path, monkeypatch):
    """Pointing at a nonexistent directory returns [] rather than
    raising — Trinity ships without the dir on some installs."""
    ts = _load_template_service(monkeypatch, tmp_path / "does-not-exist")
    assert ts.get_local_templates() == []


def test_skips_files_only_dirs(tmp_path, monkeypatch):
    """A plain file at the root (not a directory) shouldn't be treated
    as a template."""
    (tmp_path / "readme.md").write_text("not a template")
    _seed_template(tmp_path, "real", body="name: real")
    ts = _load_template_service(monkeypatch, tmp_path)
    ids = {t["id"] for t in ts.get_local_templates()}
    assert ids == {"local:real"}


def test_results_sorted_by_directory_name(tmp_path, monkeypatch):
    """Stable order is alphabetical by directory name — the list endpoint
    re-sorts by display_name, but the underlying scan should be
    deterministic."""
    _seed_template(tmp_path, "zebra", body="name: zebra")
    _seed_template(tmp_path, "alpha", body="name: alpha")
    _seed_template(tmp_path, "middle", body="name: middle")
    ts = _load_template_service(monkeypatch, tmp_path)
    ids = [t["id"] for t in ts.get_local_templates()]
    assert ids == ["local:alpha", "local:middle", "local:zebra"]


# -----------------------------------------------------------------------------
# get_local_template (single by id)
# -----------------------------------------------------------------------------

def test_get_single_local_template(tmp_path, monkeypatch):
    _seed_template(tmp_path, "x", body="name: x\ndisplay_name: X Agent")
    ts = _load_template_service(monkeypatch, tmp_path)

    t = ts.get_local_template("local:x")
    assert t is not None
    assert t["id"] == "local:x"
    assert t["display_name"] == "X Agent"


def test_get_local_template_returns_none_for_unknown(tmp_path, monkeypatch):
    ts = _load_template_service(monkeypatch, tmp_path)
    assert ts.get_local_template("local:nonexistent") is None


def test_get_local_template_rejects_wrong_prefix(tmp_path, monkeypatch):
    """A `github:` id passed to the local helper must return None
    rather than scanning the local dir for a same-named directory."""
    _seed_template(tmp_path, "x", body="name: x")
    ts = _load_template_service(monkeypatch, tmp_path)
    assert ts.get_local_template("github:org/x") is None
    assert ts.get_local_template("x") is None  # unprefixed


# -----------------------------------------------------------------------------
# Display fallbacks
# -----------------------------------------------------------------------------

def test_display_name_falls_back_to_name_then_dirname(tmp_path, monkeypatch):
    # No display_name, but has `name`
    _seed_template(tmp_path, "fallback-a", body="name: yaml-name-only")
    # Neither — pure dir name
    _seed_template(tmp_path, "fallback-b", body="description: just a desc")
    ts = _load_template_service(monkeypatch, tmp_path)
    by_id = {t["id"]: t for t in ts.get_local_templates()}
    assert by_id["local:fallback-a"]["display_name"] == "yaml-name-only"
    assert by_id["local:fallback-b"]["display_name"] == "fallback-b"


def test_description_falls_back_to_tagline(tmp_path, monkeypatch):
    _seed_template(tmp_path, "x", body="name: x\ntagline: punchy line")
    ts = _load_template_service(monkeypatch, tmp_path)
    t = ts.get_local_templates()[0]
    assert t["description"] == "punchy line"


# -----------------------------------------------------------------------------
# hidden: true — internal fixtures excluded from the catalog (#1513)
# -----------------------------------------------------------------------------

def test_hidden_template_excluded_from_list(tmp_path, monkeypatch):
    """A directory whose template.yaml sets `hidden: true` is a test/canary/demo
    fixture — it must NOT appear in the user-facing catalog list."""
    _seed_template(tmp_path, "test-echo", body="name: test-echo\nhidden: true")
    _seed_template(tmp_path, "scout", body="name: scout\ndisplay_name: Scout")
    ts = _load_template_service(monkeypatch, tmp_path)

    ids = {t["id"] for t in ts.get_local_templates()}
    assert ids == {"local:scout"}  # test-echo hidden, scout surfaced


def test_hidden_template_still_resolvable_by_id(tmp_path, monkeypatch):
    """Hiding only affects the LIST — a hidden fixture stays resolvable by id so
    creation-by-id keeps working for the canary/test harness (#1513)."""
    _seed_template(tmp_path, "test-echo", body="name: test-echo\nhidden: true")
    ts = _load_template_service(monkeypatch, tmp_path)

    t = ts.get_local_template("local:test-echo")
    assert t is not None
    assert t["id"] == "local:test-echo"
    assert t["hidden"] is True


def test_hidden_false_and_absent_are_visible(tmp_path, monkeypatch):
    """`hidden: false` and an omitted `hidden` key are both catalog-visible."""
    _seed_template(tmp_path, "explicit-false", body="name: explicit-false\nhidden: false")
    _seed_template(tmp_path, "omitted", body="name: omitted")
    ts = _load_template_service(monkeypatch, tmp_path)

    by_id = {t["id"]: t for t in ts.get_local_templates()}
    assert set(by_id) == {"local:explicit-false", "local:omitted"}
    assert by_id["local:explicit-false"]["hidden"] is False
    assert by_id["local:omitted"]["hidden"] is False


# -----------------------------------------------------------------------------
# priority surfacing + coercion — guards the None-in-sort-key 500 (#1513)
# -----------------------------------------------------------------------------

def test_priority_surfaced_and_coerced_to_int(tmp_path, monkeypatch):
    """Every surfaced template carries an int `priority`. A present-but-null,
    string, or bool value must coerce to the default — otherwise the router's
    `(priority, display_name)` sort raises TypeError and 500s the endpoint."""
    _seed_template(tmp_path, "has-int", body="name: has-int\npriority: 20")
    _seed_template(tmp_path, "no-key", body="name: no-key")
    _seed_template(tmp_path, "null-val", body="name: null-val\npriority: null")
    _seed_template(tmp_path, "str-val", body="name: str-val\npriority: high")
    _seed_template(tmp_path, "bool-val", body="name: bool-val\npriority: true")
    ts = _load_template_service(monkeypatch, tmp_path)

    by_id = {t["id"]: t for t in ts.get_local_templates()}
    assert by_id["local:has-int"]["priority"] == 20
    for id_ in ("local:no-key", "local:null-val", "local:str-val", "local:bool-val"):
        p = by_id[id_]["priority"]
        assert isinstance(p, int) and not isinstance(p, bool), f"{id_}: {p!r}"
        assert p == 100


def test_coerce_priority_helper(tmp_path, monkeypatch):
    """Direct unit coverage of the coercion helper's edge cases."""
    ts = _load_template_service(monkeypatch, tmp_path)
    assert ts._coerce_priority(20) == 20
    assert ts._coerce_priority(0) == 0            # a legit 'highest' — NOT coerced away
    assert ts._coerce_priority(-5) == -5
    assert ts._coerce_priority(None) == 100
    assert ts._coerce_priority("high") == 100
    assert ts._coerce_priority(True) == 100        # bool is not a valid priority
    assert ts._coerce_priority(1.5) == 100


def test_router_sort_key_never_raises_over_mixed_priority(tmp_path, monkeypatch):
    """Replicates routers/templates.py's exact sort over a mixed catalog. With
    coercion in place the key is always `(int, str)`, so the sort orders
    priority-ascending and never raises — the regression guard for the crash a
    raw `data.get('priority')` would have shipped (#1513)."""
    _seed_template(tmp_path, "starter", body="name: starter\ndisplay_name: Starter\npriority: 20")
    _seed_template(tmp_path, "plain-b", body="name: plain-b\ndisplay_name: Bravo")
    _seed_template(tmp_path, "plain-a", body="name: plain-a\ndisplay_name: Alpha")
    _seed_template(tmp_path, "nulled", body="name: nulled\ndisplay_name: Nulled\npriority: null")
    ts = _load_template_service(monkeypatch, tmp_path)

    templates = ts.get_local_templates()
    # exact key from routers/templates.py::list_templates
    templates.sort(key=lambda t: (t.get("priority", 100), t.get("display_name", "")))
    order = [t["id"] for t in templates]

    # priority 20 leads; the rest (default 100) fall back to display_name order
    assert order == ["local:starter", "local:plain-a", "local:plain-b", "local:nulled"]


# -----------------------------------------------------------------------------
# Real-catalog convention guard — a future unflagged fixture fails CI (#1513)
# -----------------------------------------------------------------------------

def _load_ts_real_catalog(monkeypatch):
    """Load template_service pointed at the REAL config/agent-templates/ dir."""
    real_dir = _BACKEND.parent.parent / "config" / "agent-templates"
    return _load_template_service(monkeypatch, real_dir), real_dir


def test_real_catalog_hides_all_known_fixtures(monkeypatch):
    """The shipped catalog must never surface an internal fixture or the
    auto-deployed system agent — even if a future author forgets `hidden: true`,
    this convention check fails CI before it leaks to users."""
    ts, real_dir = _load_ts_real_catalog(monkeypatch)
    if not real_dir.exists():
        pytest.skip("config/agent-templates not present in this checkout")

    ids = {t["id"].split(":", 1)[1] for t in ts.get_local_templates()}

    # No fixture (by explicit name or naming convention) may appear.
    forbidden = {
        "test-echo", "test-counter", "test-delegator", "test-codex",
        "test-gemini", "sleep-echo", "test-leak-hook",
        "demo-researcher", "demo-analyst", "trinity-system",
    }
    leaked = forbidden & ids
    assert not leaked, f"internal fixtures leaked into the catalog: {sorted(leaked)}"

    convention_leaks = {n for n in ids if n.startswith(("test-", "demo-"))}
    assert not convention_leaks, f"test-/demo- named dirs in catalog: {sorted(convention_leaks)}"


def test_real_catalog_surfaces_starters_ahead_of_suite(monkeypatch):
    """scout/sage/scribe are present and rank ahead of the dd-* suite after the
    router sort — the priority-surfacing fix, verified end-to-end (#1513).
    Also guards that scribe's template.yaml parses (a broken YAML would silently
    drop it from the catalog)."""
    ts, real_dir = _load_ts_real_catalog(monkeypatch)
    if not real_dir.exists():
        pytest.skip("config/agent-templates not present in this checkout")

    templates = ts.get_local_templates()
    templates.sort(key=lambda t: (t.get("priority", 100), t.get("display_name", "")))
    order = [t["id"].split(":", 1)[1] for t in templates]

    for starter in ("scout", "sage", "scribe"):
        assert starter in order, f"starter {starter} missing from catalog"

    dd_positions = [i for i, n in enumerate(order) if n.startswith("dd-")]
    if dd_positions:
        last_starter = max(order.index(s) for s in ("scout", "sage", "scribe"))
        assert last_starter < min(dd_positions), (
            "real starters must sort ahead of the dd-* suite"
        )
