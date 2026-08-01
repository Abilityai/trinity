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
import os
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
# #1900 — path-traversal containment on the READ path
#
# `GET /api/templates/{template_id:path}` hands `local:<name>` straight to
# `get_local_template`, which joined `<name>` onto the templates root with no
# validation. `{template_id:path}` captures `/`, so `local:../x`,
# `local:/abs/x` and a root-escaping symlink each read `<escaped>/template.yaml`
# and echoed its contents to any authenticated caller.
#
# ⚠️ READ THIS BEFORE ADDING A TEST HERE. On UNPATCHED code `get_local_template`
# returns `None` for any id whose target directory simply has no `template.yaml`
# — so "assert None" is green before *and* after the fix unless the test PLANTS
# a real `template.yaml` at the exact escaped location. Every test below is
# therefore labelled:
#   REPRO   — plants a target; returns a dict on unpatched code (verified red).
#   HYGIENE — cannot be made red; encodes the intended contract only.
# A rejection test with nothing planted proves the fixture is empty, not that
# the guard exists.
# -----------------------------------------------------------------------------

def _seed_root(tmp_path):
    """`(base, root)` — the templates root is a CHILD of base, so `..` escapes
    into a directory the test controls."""
    root = tmp_path / "templates"
    root.mkdir()
    return tmp_path, root


def test_1900_rejects_parent_traversal(tmp_path, monkeypatch):
    """REPRO — `local:../<x>` and `local:..` must not escape the root.

    Both targets are planted, so unpatched code returns a dict for each.
    """
    base, root = _seed_root(tmp_path)
    _seed_template(base, "outside", body="name: outside\ndisplay_name: SECRET")
    # The parent of the root is itself a template dir, so `local:..` is a real
    # repro rather than a vacuous None.
    (base / "template.yaml").write_text("name: parent\ndisplay_name: PARENT SECRET")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template("local:../outside") is None
    assert ts.get_local_template("local:..") is None


def test_1900_rejects_multi_level_traversal(tmp_path, monkeypatch):
    """REPRO — two levels up is planted and must still be refused."""
    root = tmp_path / "lvl1" / "lvl2" / "templates"
    root.mkdir(parents=True)
    _seed_template(tmp_path / "lvl1", "deep", body="name: deep\ndisplay_name: DEEP SECRET")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template("local:../../deep") is None


def test_1900_rejects_absolute_path_id(tmp_path, monkeypatch):
    """REPRO — the shape the issue never tested.

    `Path("/root") / "/abs/x"` is `/abs/x`: an absolute right-hand side wins the
    join outright, so no `..` is needed. It also needs no percent-encoding to
    survive a conforming HTTP client (RFC 3986 dot-segment removal only drops a
    segment that is exactly `..`).
    """
    base, root = _seed_root(tmp_path)
    target = _seed_template(base, "abs-target", body="name: abs\ndisplay_name: ABS SECRET")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template(f"local:{target}") is None


def test_1900_rejects_sibling_escape(tmp_path, monkeypatch):
    """REPRO — `<root>-evil` is the escape a string prefix check MISSES.

    `str("/x/templates-evil").startswith("/x/templates")` is True, so a
    `startswith` containment check passes this. Only `is_relative_to` on the
    resolved paths refuses it.
    """
    base, root = _seed_root(tmp_path)
    _seed_template(base, "templates-evil", body="name: evil\ndisplay_name: SIBLING SECRET")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template("local:../templates-evil") is None


def test_1900_rejects_symlink_escape(tmp_path, monkeypatch):
    """REPRO — a symlink INSIDE the root pointing OUT of it.

    The name allowlist cannot see this (`linked` is a perfectly legal name);
    only resolving both sides catches it.
    """
    base, root = _seed_root(tmp_path)
    outside = _seed_template(base, "elsewhere", body="name: e\ndisplay_name: LINK SECRET")
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable on this platform")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template("local:linked") is None


def test_1900_accepts_symlink_that_stays_inside_the_root(tmp_path, monkeypatch):
    """Anti-over-blocking — only ESCAPING symlinks are refused, not all of them.

    Note the emitted id is the link TARGET's (`local:sage`), because the helper
    returns the resolved path and `_build_local_template` derives the id from
    `Path.name`. That is deliberate: "use the value you checked" is the barrier
    shape CodeQL recognises, and the shipped catalog contains no symlinks.
    """
    base, root = _seed_root(tmp_path)
    _seed_template(root, "sage", body="name: sage\ndisplay_name: Sage")
    try:
        os.symlink(root / "sage", root / "inner", target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable on this platform")
    ts = _load_template_service(monkeypatch, root)

    got = ts.get_local_template("local:inner")
    assert got is not None
    assert got["id"] == "local:sage"


def test_1900_rejects_malformed_names_with_a_planted_target(tmp_path, monkeypatch):
    """REPRO — every one of these is a genuine, plantable POSIX directory name.

    Each target is created with a real `template.yaml`, so unpatched code
    returns a dict for each and the test is differential.
    """
    base, root = _seed_root(tmp_path)
    hostile = ["a/b", "-lead", "_lead", ".hidden", "a..b", "sp ace", "a\\b"]
    for name in hostile:
        _seed_template(root, name, body=f"name: x\ndisplay_name: LEAK {name!r}")
    ts = _load_template_service(monkeypatch, root)

    for name in hostile:
        assert ts.get_local_template(f"local:{name}") is None, name


def test_1900_trailing_dot_name_is_contained_and_allowed(tmp_path, monkeypatch):
    """Anti-over-blocking — `sage.` is a DIFFERENT directory that is still inside
    the root, so `is_relative_to` correctly admits it (POSIX keeps the trailing
    dot; the backend is Linux-only by construction). Not a bypass."""
    base, root = _seed_root(tmp_path)
    _seed_template(root, "sage.", body="name: sage\ndisplay_name: Trailing Dot")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template("local:sage.") is not None


def test_1900_trailing_newline_name_is_stopped_by_the_existence_check(
    tmp_path, monkeypatch
):
    """The one deliberately-permissive edge of the mirrored regex, pinned.

    Python's `$` matches before a trailing newline, so `"sage\\n"` passes the
    allowlist — exactly as it does in `crud._safe_local_template_path`, whose
    pattern this one is byte-identical to (pinned by
    `test_1759_template_root_parity.py`). Per the 2026-07-28 learning the
    question is *what else* stops the case the mirrored predicate allows: here
    it is the caller's own existence check, because `<root>/"sage\\n"` names a
    DIFFERENT, non-existent directory — not a traversal. Both sinks and crud
    carry that check, so nothing is inherited on faith.

    Deliberately NOT planted: planting it would create the directory and make
    the id legitimately resolvable, testing the opposite of the contract.
    """
    base, root = _seed_root(tmp_path)
    _seed_template(root, "sage", body="name: sage\ndisplay_name: Sage")
    ts = _load_template_service(monkeypatch, root)

    assert ts.get_local_template("local:sage\n") is None
    assert ts.get_local_template("local:sage") is not None


def test_1900_rejects_unplantable_malformed_names(tmp_path, monkeypatch):
    """HYGIENE — green on unpatched code by construction.

    These encode the intended contract; they do NOT reproduce the bug (no
    directory can be planted at any of them). Listed so the contract is
    explicit, labelled so nobody counts them as coverage.
    """
    base, root = _seed_root(tmp_path)
    _seed_template(base, "outside", body="name: outside")
    ts = _load_template_service(monkeypatch, root)

    for name in [
        "", ".", "..", "...", "a\x00b", "%2e%2e", "..%2f", "....//",
        "．．/outside",          # fullwidth lookalike
        "‮outside",        # RTL override
        "   ",
    ]:
        assert ts.get_local_template(f"local:{name}") is None, repr(name)


def test_1900_legit_ids_still_resolve(tmp_path, monkeypatch):
    """Anti-over-blocking — the whole legitimate name space keeps working."""
    base, root = _seed_root(tmp_path)
    for name in ["x", "dd-compliance", "a.b", "a_b", "9lives", "trinity-system"]:
        _seed_template(root, name, body=f"name: {name}\ndisplay_name: {name}")
    ts = _load_template_service(monkeypatch, root)

    for name in ["x", "dd-compliance", "a.b", "a_b", "9lives", "trinity-system"]:
        got = ts.get_local_template(f"local:{name}")
        assert got is not None, name
        assert got["id"] == f"local:{name}"


def test_1900_hidden_template_still_resolves_by_id(tmp_path, monkeypatch):
    """The #1513 contract is preserved — the barrier reads the NAME, never the
    `hidden` flag, so hidden fixtures stay resolvable by id."""
    base, root = _seed_root(tmp_path)
    _seed_template(root, "canary", body="name: canary\ndisplay_name: C\nhidden: true")
    ts = _load_template_service(monkeypatch, root)

    got = ts.get_local_template("local:canary")
    assert got is not None
    assert got["hidden"] is True
    # ...and it is still excluded from the user-facing listing.
    assert ts.get_local_templates() == []


def test_1900_every_listed_id_resolves(tmp_path, monkeypatch):
    """Round-trip — anything the listing surface emits must resolve on detail.

    Hermetic guard against the barrier over-blocking a name the catalog scan
    happily lists.
    """
    base, root = _seed_root(tmp_path)
    for name in ["alpha", "beta-two", "gamma.three", "delta_four"]:
        _seed_template(root, name, body=f"name: {name}\ndisplay_name: {name}")
    ts = _load_template_service(monkeypatch, root)

    listed = ts.get_local_templates()
    assert len(listed) == 4
    for entry in listed:
        assert ts.get_local_template(entry["id"]) is not None, entry["id"]


def test_1900_real_catalog_names_match_the_id_allowlist(monkeypatch):
    """Convention guard on the SHIPPED catalog.

    A future template directory named `my template` would list fine and then
    404 on detail. Cheap to catch here rather than in review.
    """
    real_dir = _BACKEND.parent.parent / "config" / "agent-templates"
    if not real_dir.is_dir():
        pytest.skip("shipped catalog not present in this checkout")
    ts = _load_template_service(monkeypatch, real_dir)

    offenders = [
        child.name
        for child in sorted(real_dir.iterdir())
        if child.is_dir() and ts.contained_template_dir(child.name, real_dir) is None
    ]
    assert offenders == []


def test_1900_containment_survives_a_symlinked_root(tmp_path, monkeypatch):
    """LANDMINE GUARD — resolve BOTH sides, not just the candidate.

    Its job is to fail the *fix*, not the bug: it is green before the change and
    red on a half-resolved implementation that resolves only the candidate. When
    the live root sits under a symlinked prefix (a container bind, an operator's
    symlinked deploy dir), resolving one side and not the other rejects EVERY
    legitimate template. All 130 pre-existing tests pass against that broken
    variant — this is the only test that catches it, so do not weaken it.
    """
    real_base = tmp_path / "real"
    root = real_base / "templates"
    root.mkdir(parents=True)
    _seed_template(root, "sage", body="name: sage\ndisplay_name: Sage")
    try:
        os.symlink(real_base, tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable on this platform")

    # Deliberately UNRESOLVED — this is what `_local_templates_dir()` returns.
    ts = _load_template_service(monkeypatch, tmp_path / "link" / "templates")

    got = ts.get_local_template("local:sage")
    assert got is not None
    assert got["id"] == "local:sage"


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
        # #1759: `default` matches NEITHER the test-/demo- prefix convention
        # checked below NOR any other guard, so an author dropping its
        # `hidden: true` would leak a bare "Default" entry into every user's
        # Create-Agent picker — duplicating the UI's own "Blank Agent" option
        # — with no CI signal at all. Listed explicitly for exactly that reason.
        "default",
    }
    leaked = forbidden & ids
    assert not leaked, f"internal fixtures leaked into the catalog: {sorted(leaked)}"

    # #1931: `dd-` joins the prefix convention. The 11-agent VC due-diligence
    # fleet is a demo we still run, not a starter — it ships `hidden: true` and
    # is reached deliberately via config/manifests/vc-due-diligence.yaml. This
    # is the cheapest possible regression guard: un-hiding any dd-* turns CI
    # red. It runs over VISIBLE ids only, so the hidden fleet is unaffected.
    convention_leaks = {n for n in ids if n.startswith(("test-", "demo-", "dd-"))}
    assert not convention_leaks, (
        f"test-/demo-/dd- named dirs in catalog: {sorted(convention_leaks)}"
    )


def test_real_catalog_surfaces_the_three_starters(monkeypatch):
    """scout/sage/scribe are present, each still declares `priority: 20`, and
    they sort ahead of anything else visible (#1513, reworked by #1931).

    Renamed from `..._ahead_of_suite`: since #1931 the dd-* suite is
    `hidden: true`, so the old `if dd_positions:` clause could only ever go
    **vacuous, not red** — a green run would have proved nothing. What is
    load-bearing now:

    - all three present (this also proves each `template.yaml` parses — a
      broken YAML silently drops a template from the catalog, the #1513
      `scribe` bug);
    - each still declares `priority: 20` — the *mechanism* the ordering
      depends on, and a genuinely non-vacuous assertion today;
    - the ordering clause, generalised off `dd-` to "anything else visible".
      **It is inert today** (the visible catalog is exactly these three), kept
      because it is what a fourth starter would rely on. Do not read a green
      run here as proof that ordering is guarded.
    """
    ts, real_dir = _load_ts_real_catalog(monkeypatch)
    if not real_dir.exists():
        pytest.skip("config/agent-templates not present in this checkout")

    starters = ("scout", "sage", "scribe")

    templates = ts.get_local_templates()
    templates.sort(key=lambda t: (t.get("priority", 100), t.get("display_name", "")))
    order = [t["id"].split(":", 1)[1] for t in templates]

    by_name = {t["id"].split(":", 1)[1]: t for t in templates}
    for starter in starters:
        assert starter in order, f"starter {starter} missing from catalog"
        assert by_name[starter].get("priority") == 20, (
            f"starter {starter} no longer declares priority: 20 — the router "
            f"sort would drop it behind every default-100 template"
        )

    others = [i for i, n in enumerate(order) if n not in starters]
    if others:  # inert while the visible catalog is exactly the three starters
        last_starter = max(order.index(s) for s in starters)
        assert last_starter < min(others), (
            "real starters must sort ahead of every other visible template"
        )
