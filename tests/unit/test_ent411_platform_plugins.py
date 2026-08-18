"""ent#411 — the platform-provided plugin set, and how its absence is reported.

The feature exists to break a chicken-and-egg: `/trinity:onboard` is what makes a
deployed agent Trinity-compatible, but #1704 reads the plugin list from
`template.yaml`, and the agent that most needs onboarding is a bare GitHub repo
with no `template.yaml` at all. It declares nothing, so nothing is installed, so
the plugin that would write `template.yaml` is missing.

Two halves are pinned here:

  * **the reconciler** — the platform set is ensured whether or not anything is
    declared, is additive (nothing is ever uninstalled), pins its own marketplace
    source against the agent-writable manifest, and can be switched off wholesale;
  * **the report** — `.trinity/plugins-state.json` and the I-006 check that reads
    it, so an operator can tell a bootstrap *failure* from a plugin that was never
    wanted. A bare presence flag cannot distinguish those, which is the whole
    point of carrying the withheld reason.

The reconciler half loads the agent-server module by path (the
`test_1704_plugins_reinstall.py` idiom — the agent server ships in its own image
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
        spec = importlib.util.spec_from_file_location("_plugins_reinstall_411", _MODULE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(_AGENT_SERVER))


class _FakeClaude:
    def __init__(self, *, marketplaces=None, plugins=None, add_rc=0, install_rc=0):
        self.marketplaces = marketplaces or []
        self.plugins = plugins or []
        self.add_rc = add_rc
        self.install_rc = install_rc
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        tail = args[1:] if args and args[0] == "claude" else args
        if tail[:3] == ["plugin", "marketplace", "list"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"marketplaces": [{"name": n} for n in self.marketplaces]}),
                stderr="",
            )
        if tail[:2] == ["plugin", "list"]:
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps({"plugins": self.plugins}), stderr=""
            )
        if tail[:3] == ["plugin", "marketplace", "add"]:
            return types.SimpleNamespace(returncode=self.add_rc, stdout="", stderr="unreachable")
        if tail[:2] == ["plugin", "install"]:
            return types.SimpleNamespace(returncode=self.install_rc, stdout="", stderr="install-err")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="unknown")

    def installs(self):
        return [c for c in self.calls if c[1:3] == ["plugin", "install"]]

    def adds(self):
        return [c for c in self.calls if c[1:4] == ["plugin", "marketplace", "add"]]


@pytest.fixture()
def state_file(tmp_path, mod, monkeypatch):
    """Redirect the state write off /home/developer."""
    path = tmp_path / ".trinity" / "plugins-state.json"
    monkeypatch.setattr(mod, "STATE_FILE", path)
    return path


@pytest.fixture(autouse=True)
def _default_on(monkeypatch):
    monkeypatch.delenv("TRINITY_PLATFORM_PLUGINS", raising=False)


# --- the reconciler ---------------------------------------------------------

def test_bare_repo_gets_the_platform_plugin(mod, monkeypatch, state_file):
    """The whole point: nothing declared, plugin installed anyway."""
    fake = _FakeClaude()
    monkeypatch.setattr(mod.subprocess, "run", fake)

    res = mod.reinstall({})

    assert res["status"] == "platform_defaults_only"
    assert "trinity@abilityai" in res["installed"]
    assert ["claude", "plugin", "marketplace", "add", "abilityai/abilities"] in fake.adds()


def test_pre_installed_plugin_runs_zero_installs(mod, monkeypatch, state_file):
    """The base image pre-installs it, so the common boot must do no work."""
    fake = _FakeClaude(
        marketplaces=["abilityai"],
        plugins=[{"name": "trinity", "marketplace": "abilityai"}],
    )
    monkeypatch.setattr(mod.subprocess, "run", fake)

    res = mod.reinstall({})

    assert fake.installs() == []
    assert fake.adds() == []
    assert "plugin:trinity@abilityai" in res["skipped"]


def test_declaration_that_omits_the_platform_plugin_never_uninstalls_it(mod, monkeypatch, state_file):
    """Additive, never subtractive — reconcile means "install what is missing",
    not "make the installed set match the declaration"."""
    fake = _FakeClaude(
        marketplaces=["abilityai", "other"],
        plugins=[{"name": "trinity", "marketplace": "abilityai"}],
    )
    monkeypatch.setattr(mod.subprocess, "run", fake)

    mod.reinstall({
        "marketplaces": {"other": "someone/else"},
        "installed": ["thing@other"],
    })

    assert not any("uninstall" in " ".join(c) or "remove" in " ".join(c) for c in fake.calls)


def test_declared_plugins_survive_alongside_the_platform_set(mod, monkeypatch, state_file):
    fake = _FakeClaude()
    monkeypatch.setattr(mod.subprocess, "run", fake)

    res = mod.reinstall({
        "marketplaces": {"other": "someone/else"},
        "installed": ["thing@other"],
    })

    assert set(res["installed"]) == {"thing@other", "trinity@abilityai"}
    assert res["status"] == "ok", "a declared manifest is not 'defaults only'"


def test_manifest_cannot_repoint_the_platform_marketplace(mod, monkeypatch, state_file):
    """The manifest is on the agent-writable volume. Letting a declaration
    re-point `abilityai` at another repo would turn a self-healing boot step into
    an arbitrary-code-fetch primitive."""
    fake = _FakeClaude()
    monkeypatch.setattr(mod.subprocess, "run", fake)

    mod.reinstall({
        "marketplaces": {"abilityai": "attacker/evil"},
        "installed": ["trinity@abilityai"],
    })

    sources = [c[-1] for c in fake.adds()]
    assert "abilityai/abilities" in sources
    assert "attacker/evil" not in sources


def test_operator_can_switch_the_platform_set_off(mod, monkeypatch, state_file):
    monkeypatch.setenv("TRINITY_PLATFORM_PLUGINS", "0")
    fake = _FakeClaude()
    monkeypatch.setattr(mod.subprocess, "run", fake)

    res = mod.reinstall({})

    assert res["status"] == "no_manifest"
    assert fake.installs() == []


def test_unreachable_marketplace_withholds_with_a_reason(mod, monkeypatch, state_file):
    fake = _FakeClaude(add_rc=1)
    monkeypatch.setattr(mod.subprocess, "run", fake)

    res = mod.reinstall({})

    assert res["installed"] == []
    assert "marketplace:abilityai" in res["withheld"]
    assert "plugin:trinity@abilityai" in res["withheld"]


# --- the state file ---------------------------------------------------------

def test_state_file_records_the_reconcile(mod, monkeypatch, state_file):
    fake = _FakeClaude()
    monkeypatch.setattr(mod.subprocess, "run", fake)

    mod.reinstall({})

    state = json.loads(state_file.read_text())
    assert state["schema"] == 1
    assert state["platform_defaults_enabled"] is True
    assert "trinity@abilityai" in state["installed"]


def test_state_file_records_why_a_plugin_is_absent(mod, monkeypatch, state_file):
    monkeypatch.setattr(mod.subprocess, "run", _FakeClaude(add_rc=1))

    mod.reinstall({})

    state = json.loads(state_file.read_text())
    assert state["installed"] == []
    assert state["withheld"]["plugin:trinity@abilityai"]


def test_state_file_distinguishes_off_from_failed(mod, monkeypatch, state_file):
    monkeypatch.setenv("TRINITY_PLATFORM_PLUGINS", "0")
    monkeypatch.setattr(mod.subprocess, "run", _FakeClaude())

    mod.reinstall({})

    state = json.loads(state_file.read_text())
    assert state["platform_defaults_enabled"] is False
    assert state["withheld"] == {}


def test_an_unwritable_state_path_is_not_fatal(mod, monkeypatch, tmp_path):
    """The reconcile matters; the record of it is best-effort."""
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "nope" / "x" / "state.json")
    monkeypatch.setattr(mod.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    monkeypatch.setattr(mod.subprocess, "run", _FakeClaude())

    res = mod.reinstall({})   # must not raise

    assert "trinity@abilityai" in res["installed"]


# --- the I-006 report -------------------------------------------------------

def _snap(state):
    content = state if isinstance(state, str) else json.dumps(state)
    return {"files": {".trinity/plugins-state.json": {"exists": True, "content": content}}}


def test_i006_passes_when_the_plugin_is_installed():
    from services.compatibility.static_checks import c_i006
    status, msg, _ = c_i006(_snap({"installed": ["trinity@abilityai"], "withheld": {}}))
    assert status == "pass", msg


def test_i006_passes_when_the_plugin_was_already_present():
    """A pre-installed plugin is `skipped`, not `installed` — reading only
    `installed` would report the healthiest case as a failure."""
    from services.compatibility.static_checks import c_i006
    status, _msg, _ = c_i006(_snap({"skipped": ["plugin:trinity@abilityai"], "withheld": {}}))
    assert status == "pass"


def test_i006_reports_the_withheld_reason():
    from services.compatibility.static_checks import c_i006
    status, msg, detail = c_i006(_snap({
        "installed": [],
        "withheld": {"plugin:trinity@abilityai": "marketplace 'abilityai' unavailable"},
    }))
    assert status == "fail"
    assert "unavailable" in detail["withheld"]


def test_i006_skips_when_the_operator_turned_it_off():
    """Never wanted is not the same as failed — the operator sees a skip, not a finding."""
    from services.compatibility.static_checks import c_i006
    status, _msg, _ = c_i006(_snap({
        "installed": [], "withheld": {}, "platform_defaults_enabled": False,
    }))
    assert status == "skipped"


def test_i006_skips_when_no_state_was_reported():
    """An image or boot that predates the mechanism is not a failed install."""
    from services.compatibility.static_checks import c_i006
    status, _msg, _ = c_i006({"files": {}})
    assert status == "skipped"


def test_i006_rejects_a_forged_state_file():
    """The file is agent-writable. Presence is cross-checked against the recorded
    lists, so a free-text status claiming success proves nothing."""
    from services.compatibility.static_checks import c_i006
    status, _msg, _ = c_i006(_snap({"status": "ok", "installed": [], "withheld": {}}))
    assert status == "fail"


def test_i006_survives_a_malformed_state_file():
    from services.compatibility.static_checks import c_i006
    assert c_i006(_snap("{not json"))[0] == "fail"
    assert c_i006(_snap("[]"))[0] == "fail"


# --- the startup guard ------------------------------------------------------

def test_startup_runs_the_hook_for_an_agent_that_declares_nothing():
    """The regression this feature would die of, silently.

    #1704's guard ran the reconciler only when a manifest or a `template.yaml
    plugins:` block existed. Under that guard the bare repo — the one agent the
    platform plugin exists for — is the single agent that never runs the hook, so
    the pre-install would be the ONLY delivery path and any agent on an older
    volume would never self-heal. Pinned as text because the guard is shell in an
    image this suite cannot boot.
    """
    startup = (_ROOT / "docker" / "base-image" / "startup.sh").read_text()
    guard = startup[startup.index("_trinity_platform_plugins="):]
    guard = guard[: guard.index("\nfi\n")]

    assert "TRINITY_PLATFORM_PLUGINS" in guard, "the opt-out must gate the widened guard"
    assert '_trinity_platform_default_on" = "1"' in guard, (
        "the hook must run when platform defaults are on, independent of any declaration"
    )
    # …and the declaration paths must survive, so an opted-out agent that DOES
    # declare plugins still reconciles them.
    assert ".trinity/plugins.yaml" in guard
    assert "plugins:" in guard
