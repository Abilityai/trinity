"""One run-time setting stops every request to a vendor-operated host (#4499).

`.env.example` offered `DO_NOT_TRACK=1` as the opt-out "for air-gapped /
privacy-strict installs", but it covered two channels out of five. The help
widget's Ask tab and the `ask_trinity` MCP tool had no switch at all, and the
report switch was a frontend BUILD argument, so it did nothing on the published
images (and nothing under `docker-compose.yml`, which never passed it).

Contract:
  - `BUG_REPORTING_ENABLED` and `ASK_TRINITY_ENABLED` are read at run time and
    are both off under `DO_NOT_TRACK` (same truthiness as #1486);
  - `GET /api/version` carries them as `help`, which is what the widget reads;
  - every compose file hands the switches to each container that acts on them;
  - the served Content-Security-Policy loses the host of a channel that is off;
  - a vendor host written into the source without a switch fails the census.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG = _ROOT / "src" / "backend" / "config.py"
_MAIN = _ROOT / "src" / "backend" / "main.py"
_RENDER = _ROOT / "src" / "frontend" / "render-nginx.sh"
_HEADERS = _ROOT / "src" / "frontend" / "security-headers.conf"

DOCS_QA_HOST = "us-central1-mcp-server-project-455215.cloudfunctions.net"
INTAKE_HOST = "intake.abilityai.dev"


def _config(monkeypatch, env: dict):
    monkeypatch.setenv("REDIS_URL", "redis://backend:devpassword@localhost:6379/0")
    for key in ("DO_NOT_TRACK", "BUG_REPORTING_ENABLED", "ASK_TRINITY_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        f"_config_4499_{abs(hash(frozenset(env.items())))}", str(_CONFIG))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheSwitches:
    def test_both_help_channels_are_on_by_default(self, monkeypatch):
        cfg = _config(monkeypatch, {})
        assert cfg.BUG_REPORTING_ENABLED is True
        assert cfg.ASK_TRINITY_ENABLED is True

    @pytest.mark.parametrize("dnt", ["1", "true", "TRUE", "yes", "on", "2", " 1 "])
    def test_do_not_track_turns_off_all_four_backend_read_channels(self, monkeypatch, dnt):
        cfg = _config(monkeypatch, {"DO_NOT_TRACK": dnt})
        assert cfg.BUG_REPORTING_ENABLED is False
        assert cfg.ASK_TRINITY_ENABLED is False
        assert cfg.OPERATOR_INTAKE_ENABLED is False
        assert cfg.TELEMETRY_SHARING_ENABLED is False

    @pytest.mark.parametrize("dnt", ["0", "", "false", "FALSE", " 0 "])
    def test_a_do_not_track_that_allows_leaves_them_on(self, monkeypatch, dnt):
        cfg = _config(monkeypatch, {"DO_NOT_TRACK": dnt})
        assert cfg.BUG_REPORTING_ENABLED is True
        assert cfg.ASK_TRINITY_ENABLED is True

    def test_each_channel_has_its_own_switch(self, monkeypatch):
        cfg = _config(monkeypatch, {"BUG_REPORTING_ENABLED": "false"})
        assert (cfg.BUG_REPORTING_ENABLED, cfg.ASK_TRINITY_ENABLED) == (False, True)
        cfg = _config(monkeypatch, {"ASK_TRINITY_ENABLED": "False"})
        assert (cfg.BUG_REPORTING_ENABLED, cfg.ASK_TRINITY_ENABLED) == (True, False)


class TestTheVersionAnswer:
    def test_the_handler_sends_both_switches_as_help(self):
        """The widget shows a tab only for a literal `true` under `help`, so the
        handler must send exactly these two keys from the two config names."""
        tree = ast.parse(_MAIN.read_text())
        handler = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_version")
        assigned = [n for n in ast.walk(handler) if isinstance(n, ast.Assign)
                    and isinstance(n.targets[0], ast.Subscript)
                    and getattr(n.targets[0].slice, "value", None) == "help"]
        assert len(assigned) == 1, "get_version sets payload['help'] exactly once"
        value = assigned[0].value
        assert isinstance(value, ast.Dict)
        sent = {k.value: v.id for k, v in zip(value.keys, value.values)}
        assert sent == {"docs_qa": "ASK_TRINITY_ENABLED", "bug_reporting": "BUG_REPORTING_ENABLED"}


def _env_of(compose: str, service: str) -> str:
    """The text of one service's block in a compose file."""
    text = (_ROOT / compose).read_text()
    start = re.search(rf"^  {re.escape(service)}:\n", text, re.M)
    assert start, f"{compose}: no service {service}"
    nxt = re.search(r"^  [a-z][a-z0-9-]*:\n", text[start.end():], re.M)
    return text[start.end(): start.end() + nxt.start()] if nxt else text[start.end():]


class TestComposeHandsTheSwitchesOver:
    """A switch that reaches only the backend is the reported bug: recreating
    the stack with `DO_NOT_TRACK=1` left the MCP server and the frontend as
    they were, because nothing passed it to them."""

    @pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml",
                                         "docker-compose.hosted.yml"])
    def test_backend_and_mcp_server(self, compose):
        backend = _env_of(compose, "backend")
        for name in ("DO_NOT_TRACK", "BUG_REPORTING_ENABLED", "ASK_TRINITY_ENABLED"):
            assert f"- {name}=${{{name}:-" in backend, f"{compose} backend lacks {name}"
        mcp = _env_of(compose, "mcp-server")
        for name in ("DO_NOT_TRACK", "ASK_TRINITY_ENABLED"):
            assert f"- {name}=${{{name}:-" in mcp, f"{compose} mcp-server lacks {name}"

    @pytest.mark.parametrize("compose", ["docker-compose.prod.yml", "docker-compose.hosted.yml"])
    def test_the_nginx_frontend(self, compose):
        """Only the nginx frontends serve the policy; the dev one is Vite."""
        frontend = _env_of(compose, "frontend")
        for name in ("DO_NOT_TRACK", "BUG_REPORTING_ENABLED", "ASK_TRINITY_ENABLED"):
            assert f"- {name}=${{{name}:-" in frontend, f"{compose} frontend lacks {name}"


def _render(tmp_path, env: dict) -> str:
    template = tmp_path / "nginx.conf.template"
    template.write_text("server { set_real_ip_from ${TRINITY_PROXY_GATEWAY}; }\n")
    out = tmp_path / "headers.conf"
    full_env = {
        "PATH": "/usr/bin:/bin", "TRINITY_PROXY_GATEWAY": "172.30.0.1",
        "TRINITY_NGINX_TEMPLATE": str(template), "TRINITY_NGINX_OUTPUT": str(tmp_path / "default.conf"),
        "TRINITY_HEADERS_SOURCE": str(_HEADERS), "TRINITY_HEADERS_OUTPUT": str(out), **env,
    }
    done = subprocess.run(["sh", str(_RENDER), "true"], env=full_env, capture_output=True, text=True,
                          timeout=20)
    assert done.returncode == 0, done.stderr
    policy = [line for line in out.read_text().splitlines() if "Content-Security-Policy" in line
              and not line.lstrip().startswith("#")]
    assert len(policy) == 1
    return re.search(r"connect-src[^;]*", policy[0]).group(0)


class TestTheServedPolicy:
    def test_with_nothing_set_both_hosts_are_allowed_as_before(self, tmp_path):
        connect = _render(tmp_path, {})
        assert DOCS_QA_HOST in connect and INTAKE_HOST in connect

    @pytest.mark.parametrize("dnt", ["1", "true", "YES"])
    def test_do_not_track_removes_both_and_nothing_else(self, tmp_path, dnt):
        before = _render(tmp_path, {})
        connect = _render(tmp_path, {"DO_NOT_TRACK": dnt})
        assert DOCS_QA_HOST not in connect and INTAKE_HOST not in connect
        kept = [t for t in before.split() if DOCS_QA_HOST not in t and INTAKE_HOST not in t]
        assert connect.split() == kept

    def test_each_switch_removes_its_own_host(self, tmp_path):
        connect = _render(tmp_path, {"ASK_TRINITY_ENABLED": "false"})
        assert DOCS_QA_HOST not in connect and INTAKE_HOST in connect
        connect = _render(tmp_path, {"BUG_REPORTING_ENABLED": "FALSE"})
        assert INTAKE_HOST not in connect and DOCS_QA_HOST in connect

    def test_a_setting_value_is_never_written_into_the_configuration(self, tmp_path):
        """Only two fixed strings are removed; a hostile value changes nothing."""
        hostile = "false; add_header X-Injected 1"
        _render(tmp_path, {"BUG_REPORTING_ENABLED": hostile, "ASK_TRINITY_ENABLED": hostile})
        assert (tmp_path / "headers.conf").read_text() == _HEADERS.read_text()

    def test_nginx_serves_the_rendered_copy(self):
        body = (_ROOT / "src" / "frontend" / "nginx-server.conf").read_text()
        assert "include /etc/nginx/security-headers.conf;" not in body
        assert body.count("include /etc/nginx/generated/security-headers.conf;") == 4


# --- the census ------------------------------------------------------------------
# Every place the shipped source names a vendor-operated host, and the switch
# that turns that request off. A new line here is a new way for data to leave an
# instance: wire it to a switch that `DO_NOT_TRACK` also turns off, then list it.

_VENDOR_HOST = re.compile(r"(?:[a-z0-9-]+\.)*(?:abilityai\.dev|cloudfunctions\.net)", re.I)
_SCANNED = ("src/backend", "src/frontend/src", "src/mcp-server/src", "src/scheduler",
            "docker/base-image/agent_server")
_SKIP_PARTS = {"node_modules", "enterprise", "tests", "__pycache__", "dist"}
_SUFFIXES = {".py", ".ts", ".js", ".vue", ".mjs"}

SWITCHED = {
    ("src/backend/config.py", INTAKE_HOST): "OPERATOR_INTAKE_ENABLED / TELEMETRY_SHARING_ENABLED",
    ("src/backend/services/telemetry_sharing_service.py", INTAKE_HOST): "TELEMETRY_SHARING_ENABLED",
    ("src/frontend/src/components/HelpChatWidget.vue", INTAKE_HOST): "BUG_REPORTING_ENABLED",
    ("src/frontend/src/components/HelpChatWidget.vue", DOCS_QA_HOST): "ASK_TRINITY_ENABLED",
    ("src/mcp-server/src/tools/ask_trinity_client.ts", DOCS_QA_HOST): "ASK_TRINITY_ENABLED",
}
# A host named in prose or as an example of a managed instance's own address:
# nothing is sent to it.
NOT_A_REQUEST = {
    ("src/backend/services/instance_identity.py", "eu2.abilityai.dev"),
    ("src/backend/services/ssh_service.py", "trinity.abilityai.dev"),
}


def _census() -> set:
    found = set()
    for base in _SCANNED:
        for path in (_ROOT / base).rglob("*"):
            if path.suffix not in _SUFFIXES or _SKIP_PARTS & set(path.parts):
                continue
            if path.name.endswith((".test.ts", ".spec.js", ".spec.ts")):
                continue
            rel = path.relative_to(_ROOT).as_posix()
            for host in _VENDOR_HOST.findall(path.read_text(errors="ignore")):
                found.add((rel, host.lower()))
    return found


class TestNoVendorHostWithoutASwitch:
    def test_every_vendor_host_in_the_source_is_listed_with_its_switch(self):
        unknown = _census() - set(SWITCHED) - NOT_A_REQUEST
        assert not unknown, (
            "A vendor-operated host appeared in the source with no switch on record: "
            f"{sorted(unknown)}. Gate the request on a run-time setting that DO_NOT_TRACK "
            "also turns off, pass it through every compose file, then add it to SWITCHED (#4499).")

    def test_the_list_names_nothing_that_is_gone(self):
        stale = (set(SWITCHED) | NOT_A_REQUEST) - _census()
        assert not stale, f"listed but no longer in the source: {sorted(stale)}"

    def test_every_listed_switch_is_off_under_do_not_track(self, monkeypatch):
        cfg = _config(monkeypatch, {"DO_NOT_TRACK": "1"})
        names = {n.strip() for switch in SWITCHED.values() for n in switch.split("/")}
        for name in names:
            assert getattr(cfg, name) is False, f"{name} stays on under DO_NOT_TRACK"
