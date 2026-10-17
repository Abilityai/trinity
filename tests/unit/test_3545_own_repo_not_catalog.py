"""A `github:` repo that resolves is not thereby a catalog template (#3545).

`get_github_template()` builds a template for EVERY `github:` id — the dynamic
fall-through is what lets `github:owner/repo` work unlisted. The create path
read that truthiness as "this is a shared catalog template" and made every
agent pull-only, including one created from the creator's own repository with
their own token. `is_catalog_github_template()` answers the membership question
on the same ladder, and these tests run the REAL module for both.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load(monkeypatch, *, db_entries=None, registry=(), defaults=()):
    """Load template_service standalone with each rung of the ladder chosen."""
    if "config" not in sys.modules:
        config_mod = types.ModuleType("config")
        config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
        config_mod.GITHUB_PAT_CREDENTIAL_ID = "test-pat"
        monkeypatch.setitem(sys.modules, "config", config_mod)

    spec = importlib.util.spec_from_file_location(
        "ts_under_test_3545", _BACKEND / "services" / "template_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DEFAULT_GITHUB_TEMPLATE_REPOS", list(defaults))
    monkeypatch.setattr(module, "_get_cached_metadata_result", lambda repo: ({}, None))
    if registry is not None:  # None keeps the real fence
        monkeypatch.setattr(module, "_registry_template_overrides", lambda: list(registry))

    settings_stub = types.ModuleType("services.settings_service")
    settings_stub.get_github_templates = lambda: db_entries
    monkeypatch.setitem(sys.modules, "services.settings_service", settings_stub)
    return module


def test_an_unlisted_repo_resolves_but_is_not_a_catalog_template(monkeypatch):
    """The bug: the real function returns a template for a repo in no list."""
    ts = _load(monkeypatch, registry=[{"github_repo": "acme/one"}])

    assert ts.get_github_template("github:alice/my-agent")["github_repo"] == "alice/my-agent"
    assert ts.is_catalog_github_template("alice/my-agent") is False


@pytest.mark.parametrize("kwargs", [
    {"db_entries": [{"github_repo": "acme/one"}]},
    {"registry": [{"github_repo": "acme/one"}]},
    {"defaults": ["acme/one"]},
    # the defaults rung is consulted even under an admin list, as it is by
    # get_github_template()
    {"db_entries": [], "defaults": ["acme/one"]},
])
def test_each_rung_of_the_ladder_is_a_catalog_template(monkeypatch, kwargs):
    ts = _load(monkeypatch, **kwargs)

    assert ts.is_catalog_github_template("acme/one") is True
    assert ts.is_catalog_github_template("alice/my-agent") is False


def test_an_admin_list_takes_the_registry_out_of_the_catalog(monkeypatch):
    """Same precedence as get_all_templates(): a curated list, even an empty
    one, means the registry is never consulted."""
    ts = _load(monkeypatch, db_entries=[], registry=[{"github_repo": "acme/one"}])

    assert ts.is_catalog_github_template("acme/one") is False


@pytest.mark.parametrize("spelling", ["Acme/One", "ACME/ONE", "acme/one.git", "Acme/One.GIT"])
def test_another_spelling_of_a_catalog_repo_is_still_the_catalog_repo(monkeypatch, spelling):
    """GitHub names are case-insensitive and clone with or without `.git`; the
    ownership check downstream lowercases, so an exact match here would let a
    respelled catalog repo through to it."""
    ts = _load(monkeypatch, registry=[{"github_repo": "acme/one"}])

    assert ts.is_catalog_github_template(spelling) is True


def test_the_predicate_and_the_resolver_read_one_ladder(monkeypatch):
    """A registry entry is a catalog template to the predicate exactly when the
    resolver applies its override."""
    ts = _load(monkeypatch, registry=[{"github_repo": "acme/one", "display_name": "One"}])

    assert ts.is_catalog_github_template("acme/one") is True
    assert ts.get_github_template("github:acme/one")["display_name"] == "One"
    assert ts.get_github_template("github:alice/my-agent")["display_name"] == "my-agent"


def test_an_exploding_registry_reads_as_no_catalog_and_never_raises(monkeypatch):
    """Fail-open by design: the create path still requires the token's own
    account to own the repo and a passing push probe."""
    registry_stub = types.ModuleType("services.template_registry_service")

    def _boom():
        raise RuntimeError("registry down")

    registry_stub.get_registry_overrides = _boom
    monkeypatch.setitem(sys.modules, "services.template_registry_service", registry_stub)
    ts = _load(monkeypatch, registry=None)

    assert ts.is_catalog_github_template("acme/one") is False
