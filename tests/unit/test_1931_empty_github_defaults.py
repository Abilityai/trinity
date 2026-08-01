"""An empty `DEFAULT_GITHUB_TEMPLATE_REPOS` removes a BROWSE surface, never a
CREATE capability (#1931).

#1931 empties the shipped default repo list. The plan's analysis concluded that
every consumer tolerates `[]`, and — critically — that the one path which could
have been a real regression is not one: an **existing** agent created from
`github:abilityai/agent-ruby` recreates through
`crud._resolve_github_repo_and_pat` -> `get_github_template`, whose two branches

    if repo in DEFAULT_GITHUB_TEMPLATE_REPOS:   # membership now always False
        metadata = _get_cached_metadata(repo)
        return _build_template(repo, metadata)
    # Dynamic: repo not in any configured list but still a valid github: ID
    metadata = _get_cached_metadata(repo)
    return _build_template(repo, metadata)

are byte-identical two-line bodies. That was verified by *reading*. These tests
verify it by *running* — the sibling-path rule: a guard proven on one codepath
has to be proven on every path that reaches the same behaviour.

They also pin the intended side-effect: with no default repos, `get_all_templates`
issues **zero** outbound GitHub calls on a cold metadata cache, where it
previously submitted one `_fetch_template_yaml` per default repo to a 5-worker
pool with a 10s per-request timeout. On a firewalled or offline host that was a
multi-second first paint on the Library's default view.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load_template_service(monkeypatch, default_repos):
    """Load template_service standalone with a chosen default repo list.

    `monkeypatch.setitem` (not a bare `sys.modules[...] =`) keeps
    `tests/lint_sys_modules.py` green and undoes the insertion on teardown.
    """
    if "config" not in sys.modules:
        config_mod = types.ModuleType("config")
        config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
        config_mod.GITHUB_PAT_CREDENTIAL_ID = "test-pat"
        monkeypatch.setitem(sys.modules, "config", config_mod)

    spec = importlib.util.spec_from_file_location(
        "ts_under_test_1931", _BACKEND / "services" / "template_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The module binds the constant at import (`from config import ...`), so
    # override the module-level name rather than the config stub.
    monkeypatch.setattr(module, "DEFAULT_GITHUB_TEMPLATE_REPOS", list(default_repos))
    return module


def _stub_network(monkeypatch, module, calls, db_entries=None):
    """Replace every hop that leaves the process; record what it would fetch.

    `_get_cached_metadata` and `_fetch_template_yaml` are the two GitHub reads;
    `_get_github_pat` reads settings/DB and is called before the pool is even
    built. `services.settings_service.get_github_templates` is imported lazily
    inside both `get_github_template` and `get_all_templates` — `None` is the
    "no admin override row" case, i.e. fall back to the defaults.
    """
    def _fake(repo, *args, **kwargs):
        calls.append(repo)
        return {}

    monkeypatch.setattr(module, "_get_cached_metadata", _fake)
    monkeypatch.setattr(module, "_fetch_template_yaml", _fake)
    monkeypatch.setattr(module, "_get_github_pat", lambda *a, **k: "test-pat")
    # `_metadata_cache` is a module-level dict, so each `exec_module` above
    # already gets a fresh one — this is belt-and-braces against a future load
    # helper that reuses a module object, not a live requirement.
    module._metadata_cache.clear()

    settings_stub = types.ModuleType("services.settings_service")
    settings_stub.get_github_templates = lambda: db_entries
    monkeypatch.setitem(sys.modules, "services.settings_service", settings_stub)


UNCONFIGURED_REPO = "abilityai/agent-ruby"
UNCONFIGURED_ID = f"github:{UNCONFIGURED_REPO}"


def test_unconfigured_github_repo_still_resolves(monkeypatch):
    """The regression that would have broken every existing github:-templated
    agent's recreate. With the default list empty the membership test is always
    False and the call falls through to the dynamic branch — which returns the
    same thing."""
    ts = _load_template_service(monkeypatch, [])
    _stub_network(monkeypatch, ts, [])

    result = ts.get_github_template(UNCONFIGURED_ID)

    assert result, "an unconfigured github: repo must still resolve to a template"
    assert result["id"] == UNCONFIGURED_ID
    assert result["source"] == "github"


def test_empty_and_configured_defaults_resolve_identically(monkeypatch):
    """Prove the two branches are the same behaviour, not merely the same
    source text — this is the assertion that makes emptying the constant a
    provably zero-delta change for the create/recreate path."""
    configured = _load_template_service(monkeypatch, [UNCONFIGURED_REPO])
    _stub_network(monkeypatch, configured, [])
    via_defaults = configured.get_github_template(UNCONFIGURED_ID)

    emptied = _load_template_service(monkeypatch, [])
    _stub_network(monkeypatch, emptied, [])
    via_dynamic = emptied.get_github_template(UNCONFIGURED_ID)

    assert via_defaults, "sanity: the pre-#1931 configured path resolved"
    assert via_dynamic == via_defaults


def test_empty_defaults_make_no_outbound_github_calls(monkeypatch, tmp_path):
    """Intended side-effect (#1931): `GET /api/templates` stops depending on
    GitHub on the default path. `_fetch_all_metadata([])` leaves `to_fetch`
    empty, so the `if to_fetch:` block — and with it the ThreadPoolExecutor,
    the HTTP requests and the PAT read — is skipped entirely."""
    ts = _load_template_service(monkeypatch, [])
    calls: list[str] = []
    _stub_network(monkeypatch, ts, calls)
    monkeypatch.setattr(ts, "_local_templates_dir", lambda: tmp_path)

    assert ts._fetch_all_metadata([]) == {}
    assert ts.get_all_templates() == []
    assert calls == [], f"expected zero outbound metadata fetches, got {calls}"


def test_a_configured_repo_would_still_be_fetched(monkeypatch, tmp_path):
    """Counter-test, so the assertion above is not vacuously green: the same
    code path DOES fetch when an admin has curated a repo. Without this, a
    future refactor that broke fetching entirely would keep the test above
    passing."""
    ts = _load_template_service(monkeypatch, [UNCONFIGURED_REPO])
    calls: list[str] = []
    _stub_network(monkeypatch, ts, calls)
    monkeypatch.setattr(ts, "_local_templates_dir", lambda: tmp_path)

    templates = ts.get_all_templates()

    assert calls == [UNCONFIGURED_REPO]
    assert [t["id"] for t in templates] == [UNCONFIGURED_ID]


def test_shipped_default_is_empty():
    """The constant itself. Kept (not deleted) because TMPL-001's None-vs-[]
    fallback, routers/settings.py and the Settings 'defaults' badge all
    reference it, and trinity-enterprise#14 will repoint this seam.

    Do NOT 'helpfully' refill it: the previous list was a pre-2026 repo set no
    install had ever overridden, so every operator browsed the same dead
    catalog. Curation is an operator act (Settings -> GitHub Templates)."""
    # No `sys.path` mutation here on purpose: `tests/unit/conftest.py` already
    # puts `src/backend` on the path, and a bare `insert` from a test body is a
    # permanent, un-undone global side-effect (one duplicate entry per session).
    try:
        import config
    except Exception:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")

    assert config.DEFAULT_GITHUB_TEMPLATE_REPOS == []
