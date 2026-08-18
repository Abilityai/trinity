"""#1704 — the agent-side boot re-install module (`plugins_reinstall.py`).

The module reads the committed, untrusted `~/.trinity/plugins.yaml`, compares
the declared plugin set against what `claude plugin [marketplace] list --json`
reports, and re-installs only what is missing — so a git-based reconstitution
onto a fresh volume self-heals, while a volume-persisting restart runs ZERO
subprocesses.

Pinned properties (autoplan security + engineering phases):
  1. an already-present set runs no install subprocesses;
  2. a fresh volume adds the marketplace then installs the plugin with `--yes`,
     as an ARG LIST (never a shell string);
  3. the untrusted manifest is hardened-parsed and re-charset-validated — a
     `user:token@` source, a traversal/flag/metachar name, and an undeclared
     marketplace are all dropped BEFORE any subprocess;
  4. every subprocess is `timeout`-bounded (`stdin=DEVNULL`) and non-fatal — a
     timeout / missing `claude` withholds with a reason, never raises;
  5. `main()` always returns 0 (startup continues regardless).

Loaded standalone by path with the agent-server dir on `sys.path`, so the
module's `from safe_yaml import ...` fallback resolves — the
`test_2007_mcp_template_render.py` idiom (the agent server ships in its own image
and cannot import `src/backend`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"
_MODULE = _AGENT_SERVER / "plugins_reinstall.py"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(_AGENT_SERVER))
    try:
        spec = importlib.util.spec_from_file_location(
            "_plugins_reinstall_1704", _MODULE
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(_AGENT_SERVER))


class _FakeClaude:
    """Dispatch `claude plugin ...` invocations. Records every arg list."""

    def __init__(
        self,
        *,
        marketplaces=None,
        plugins=None,
        add_rc=0,
        install_rc=0,
        list_rc=0,
        raise_exc=None,
        timeout=False,
    ):
        self.marketplaces = marketplaces if marketplaces is not None else []
        self.plugins = plugins if plugins is not None else []
        self.add_rc = add_rc
        self.install_rc = install_rc
        self.list_rc = list_rc
        self.raise_exc = raise_exc
        self.timeout = timeout
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        if self.timeout:
            import subprocess

            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
        if self.raise_exc:
            raise self.raise_exc
        tail = args[1:] if args and args[0] == "claude" else args
        if tail[:3] == ["plugin", "marketplace", "list"]:
            return types.SimpleNamespace(
                returncode=self.list_rc,
                stdout=json.dumps(
                    {"marketplaces": [{"name": n} for n in self.marketplaces]}
                ),
                stderr="",
            )
        if tail[:2] == ["plugin", "list"]:
            return types.SimpleNamespace(
                returncode=self.list_rc,
                stdout=json.dumps({"plugins": self.plugins}),
                stderr="",
            )
        if tail[:3] == ["plugin", "marketplace", "add"]:
            return types.SimpleNamespace(
                returncode=self.add_rc, stdout="", stderr="add-err"
            )
        if tail[:2] == ["plugin", "install"]:
            return types.SimpleNamespace(
                returncode=self.install_rc, stdout="", stderr="install-err"
            )
        return types.SimpleNamespace(returncode=1, stdout="", stderr="unknown")

    def install_calls(self):
        return [c for c in self.calls if c[1:3] == ["plugin", "install"]]

    def add_calls(self):
        return [c for c in self.calls if c[1:4] == ["plugin", "marketplace", "add"]]


def _patch_claude(monkeypatch, mod, fake):
    monkeypatch.setattr(mod.subprocess, "run", fake)


_MANIFEST = {
    "marketplaces": {"abilityai": "abilityai/abilities"},
    "installed": ["trinity@abilityai"],
}


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def test_all_present_runs_zero_installs(mod, monkeypatch):
    fake = _FakeClaude(
        marketplaces=["abilityai"],
        plugins=[{"name": "trinity", "marketplace": "abilityai"}],
    )
    _patch_claude(monkeypatch, mod, fake)
    res = mod.reinstall(dict(_MANIFEST))
    assert res["installed"] == []
    assert res["added_marketplaces"] == []
    assert fake.install_calls() == []
    assert fake.add_calls() == []
    assert "marketplace:abilityai" in res["skipped"]


def test_fresh_volume_adds_marketplace_then_installs_with_yes(mod, monkeypatch):
    fake = _FakeClaude(marketplaces=[], plugins=[])
    _patch_claude(monkeypatch, mod, fake)
    res = mod.reinstall(dict(_MANIFEST))
    assert res["added_marketplaces"] == ["abilityai"]
    assert res["installed"] == ["trinity@abilityai"]
    # marketplace add before install (order matters — install needs the source)
    assert fake.calls[-2][1:4] == ["plugin", "marketplace", "add"]
    install = fake.install_calls()[0]
    assert install == ["claude", "plugin", "install", "trinity@abilityai", "--yes"]
    # arg list, never a shell string
    assert isinstance(install, list)


def test_subprocess_is_timeout_bounded_with_devnull_stdin(mod, monkeypatch):
    fake = _FakeClaude(marketplaces=[], plugins=[])
    _patch_claude(monkeypatch, mod, fake)
    mod.reinstall(dict(_MANIFEST))
    for kw in fake.kwargs:
        assert kw.get("timeout") and kw["timeout"] > 0
        assert kw.get("stdin") is not None  # DEVNULL
        assert kw.get("shell") is not True


def test_missing_plugin_installed_when_marketplace_present(mod, monkeypatch):
    fake = _FakeClaude(marketplaces=["abilityai"], plugins=[])
    _patch_claude(monkeypatch, mod, fake)
    res = mod.reinstall(dict(_MANIFEST))
    assert res["added_marketplaces"] == []  # marketplace already present
    assert res["installed"] == ["trinity@abilityai"]


# ---------------------------------------------------------------------------
# Non-fatal failure handling
# ---------------------------------------------------------------------------


def test_install_failure_is_withheld_not_fatal(mod, monkeypatch):
    fake = _FakeClaude(marketplaces=["abilityai"], plugins=[], install_rc=1)
    _patch_claude(monkeypatch, mod, fake)
    res = mod.reinstall(dict(_MANIFEST))
    assert res["installed"] == []
    assert "plugin:trinity@abilityai" in res["withheld"]


def test_timeout_is_withheld_not_fatal(mod, monkeypatch):
    fake = _FakeClaude(timeout=True)
    _patch_claude(monkeypatch, mod, fake)
    # read failures → treated as not-present; add/install then time out → withheld
    res = mod.reinstall(dict(_MANIFEST))
    assert res["installed"] == []
    assert res["withheld"]  # something withheld, nothing raised


def test_missing_claude_cli_is_non_fatal(mod, monkeypatch):
    def _raise(args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(mod.subprocess, "run", _raise)
    res = mod.reinstall(dict(_MANIFEST))
    assert res["installed"] == []
    # nothing raised; declared marketplace could not be added → plugin withheld
    assert res["status"] == "ok"


def test_no_manifest_still_installs_the_platform_set(mod, monkeypatch):
    """ent#411: an agent that declares nothing is exactly the one that needs the
    Trinity plugin — a bare repo has no template.yaml to declare it in."""
    _patch_claude(monkeypatch, mod, _FakeClaude())
    res = mod.reinstall({})
    assert res["status"] == "platform_defaults_only"
    assert "trinity@abilityai" in res["installed"]


def test_no_manifest_status_when_platform_set_is_off(mod, monkeypatch):
    monkeypatch.setenv("TRINITY_PLATFORM_PLUGINS", "0")
    _patch_claude(monkeypatch, mod, _FakeClaude())
    assert mod.reinstall({})["status"] == "no_manifest"


def test_main_always_returns_zero(mod, monkeypatch):
    monkeypatch.setattr(mod, "load_manifest", lambda *a, **k: {})
    assert mod.main() == 0


# ---------------------------------------------------------------------------
# load_manifest — untrusted parse + re-validation
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: str) -> Path:
    d = tmp_path / ".trinity"
    d.mkdir()
    p = d / "plugins.yaml"
    p.write_text(body)
    return p


def test_load_manifest_normalizes_and_drops_invalid(mod, tmp_path):
    p = _write_manifest(
        tmp_path,
        (
            "plugins:\n"
            "  marketplaces:\n"
            "  - name: abilityai\n"
            "    source: abilityai/abilities\n"
            "  installed:\n"
            "  - trinity@abilityai\n"
            "  - evil@undeclared\n"  # marketplace not declared → dropped
            "  - '-flag@abilityai'\n"  # leading '-' name → dropped
        ),
    )
    out = mod.load_manifest(p)
    assert out["marketplaces"] == {"abilityai": "abilityai/abilities"}
    assert out["installed"] == ["trinity@abilityai"]


def test_load_manifest_rejects_userinfo_source(mod, tmp_path):
    p = _write_manifest(
        tmp_path,
        (
            "plugins:\n"
            "  marketplaces:\n"
            "  - name: ev\n"
            "    source: https://user:tok@github.com/o/r\n"
            "  installed: []\n"
        ),
    )
    out = mod.load_manifest(p)
    assert out == {}  # the only marketplace was refused → nothing installable


def test_load_manifest_missing_file_is_empty(mod, tmp_path):
    # Explicit non-existent template_path so the fallback can't reach the real
    # /home/developer/template.yaml on the test host.
    assert (
        mod.load_manifest(
            tmp_path / ".trinity" / "plugins.yaml", tmp_path / "template.yaml"
        )
        == {}
    )


def test_load_manifest_falls_back_to_template_yaml(mod, tmp_path):
    """Cornelius / source-mode survival: no committed manifest, but the
    re-cloned template.yaml carries the `plugins:` block (same nested shape)."""
    tpl = tmp_path / "template.yaml"
    tpl.write_text(
        "name: cornelius\n"
        "resources: {cpu: '2', memory: '4g'}\n"
        "plugins:\n"
        "  marketplaces:\n"
        "  - name: abilityai\n"
        "    source: abilityai/abilities\n"
        "  installed:\n"
        "  - trinity@abilityai\n"
    )
    out = mod.load_manifest(tmp_path / ".trinity" / "plugins.yaml", tpl)
    assert out["marketplaces"] == {"abilityai": "abilityai/abilities"}
    assert out["installed"] == ["trinity@abilityai"]


def test_manifest_wins_over_template_yaml(mod, tmp_path):
    """When both exist, the committed manifest is authoritative (an operator or
    the materializer decided it), not template.yaml."""
    p = _write_manifest(
        tmp_path,
        "plugins:\n  marketplaces:\n  - name: m\n    source: o/r\n  installed:\n  - p@m\n",
    )
    tpl = tmp_path / "template.yaml"
    tpl.write_text(
        "plugins:\n  marketplaces:\n  - name: other\n    source: x/y\n  installed:\n  - q@other\n"
    )
    out = mod.load_manifest(p, tpl)
    assert out["marketplaces"] == {"m": "o/r"}
    assert out["installed"] == ["p@m"]


def test_template_yaml_fallback_tolerates_anchors(mod, tmp_path):
    """template.yaml is a full author document and may legitimately use a YAML
    anchor elsewhere — the fallback uses BUDGET, not REJECT, so it is not dropped."""
    tpl = tmp_path / "template.yaml"
    tpl.write_text(
        "defaults: &d {cpu: '2'}\n"
        "resources: *d\n"
        "plugins:\n"
        "  marketplaces:\n"
        "  - name: ab\n"
        "    source: a/b\n"
        "  installed:\n"
        "  - p@ab\n"
    )
    out = mod.load_manifest(tmp_path / ".trinity" / "plugins.yaml", tpl)
    assert out["marketplaces"] == {"ab": "a/b"}
    assert out["installed"] == ["p@ab"]


def test_load_manifest_size_cap(mod, tmp_path):
    p = _write_manifest(tmp_path, "plugins:\n  installed:\n" + ("  - x@ab\n" * 100000))
    # Over the 256 KiB cap → empty, never a huge in-memory parse.
    assert mod.load_manifest(p) == {}


def test_load_manifest_rejects_yaml_alias_bomb(mod, tmp_path):
    # AliasPolicy.REJECT — any alias refuses the whole document.
    p = _write_manifest(
        tmp_path,
        (
            "plugins:\n"
            "  marketplaces: &a\n"
            "  - name: ab\n"
            "    source: a/b\n"
            "  installed: *a\n"
        ),
    )
    assert mod.load_manifest(p) == {}


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,ok",
    [
        ("abilityai/abilities", True),
        ("https://github.com/o/r", True),
        ("https://user:tok@github.com/o/r", False),  # userinfo
        ("--evil", False),  # flag injection
        ("../../etc", False),  # traversal
        ("http://github.com/o/r", False),  # not https
        ("ftp://evil.com/x", False),  # non-https scheme (backend parity, #1704)
        ("file:///etc/passwd", False),  # non-https scheme
        ("ssh://host/path", False),  # non-https scheme
        ("git@github.com:o/r", False),  # ssh/userinfo
        ("a/b/c", False),  # not owner/repo
    ],
)
def test_is_source(mod, src, ok):
    assert mod._is_source(src) is ok


@pytest.mark.parametrize(
    "name,ok",
    [
        ("trinity", True),
        ("p.v_1-x", True),
        ("p$(x)", False),
        ("-x", False),
        ("a/b", False),
        ("..", False),
        ("", False),
    ],
)
def test_is_name(mod, name, ok):
    assert mod._is_name(name) is ok
