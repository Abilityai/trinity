"""
Unit tests for #1169 — `data_paths` surfaced through template_service.

`_build_template` (GitHub) and `_build_local_template` (local dir) must each
surface a `data_paths` list from template metadata, defaulting to `[]` when the
template omits the key. This is the metadata surface for the template-listing
API; the actual materialization into `.trinity/data-paths.yaml` happens in
crud.py at agent creation (exercised by integration, not here).

Loads template_service standalone, mirroring test_local_templates_listing.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_template_service(monkeypatch, fake_templates_dir: Path):
    if "config" not in sys.modules:
        config_mod = types.ModuleType("config")
        config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
        config_mod.GITHUB_PAT_CREDENTIAL_ID = "test-pat"
        monkeypatch.setitem(sys.modules, "config", config_mod)

    spec = importlib.util.spec_from_file_location(
        "ts_under_test_data_paths", _BACKEND / "services" / "template_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_local_templates_dir", lambda: fake_templates_dir)
    return module


def _seed_template(parent: Path, name: str, body: str) -> Path:
    tdir = parent / name
    tdir.mkdir(parents=True)
    (tdir / "template.yaml").write_text(body)
    return tdir


# ---------------------------------------------------------------------------
# Local templates
# ---------------------------------------------------------------------------


def test_local_template_surfaces_data_paths(tmp_path, monkeypatch):
    _seed_template(tmp_path, "kb", body="""
name: kb
display_name: KB Agent
data_paths:
  - data/**
  - data/index.sqlite
""")
    ts = _load_template_service(monkeypatch, tmp_path)

    t = ts.get_local_template("local:kb")
    assert t is not None
    assert t["data_paths"] == ["data/**", "data/index.sqlite"]


def test_local_template_data_paths_defaults_empty(tmp_path, monkeypatch):
    _seed_template(tmp_path, "plain", body="name: plain\ndisplay_name: Plain")
    ts = _load_template_service(monkeypatch, tmp_path)

    t = ts.get_local_template("local:plain")
    assert t is not None
    assert t["data_paths"] == []


# ---------------------------------------------------------------------------
# GitHub templates
# ---------------------------------------------------------------------------


def test_build_template_surfaces_data_paths(tmp_path, monkeypatch):
    ts = _load_template_service(monkeypatch, tmp_path)

    result = ts._build_template("org/repo", {"data_paths": ["data/cache/**"]})
    assert result["data_paths"] == ["data/cache/**"]


def test_build_template_data_paths_defaults_empty(tmp_path, monkeypatch):
    ts = _load_template_service(monkeypatch, tmp_path)

    result = ts._build_template("org/repo", {})
    assert result["data_paths"] == []
