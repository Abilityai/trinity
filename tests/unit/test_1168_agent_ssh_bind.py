"""Agent SSH listener posture (trinity-enterprise#1168).

Five requirements (docs/memory/requirements/roadmap.md §16.2, SSH-BIND-001..005),
one test file, because they are one change: where the agent container's
``22/tcp`` is published, what the grant endpoint advertises for it, whether a
listener runs at all before a grant, and what the image's sshd accepts.

* **SSH-BIND-001** — the host interface comes from ``AGENT_SSH_BIND_ADDRESS``,
  loopback by default, validated as an IP literal, and every container create
  site builds its port mapping through ONE helper
  (``services/agent_ssh_binding.py::agent_ssh_port_binding``). The AST guard
  below is the fourth property to prove *"the create path is never one call
  site"* (learnings 2026-07-10; #1871 log_config, #2541 restart policy,
  #1098 tmpfs before it) and mirrors ``test_2541_restart_policy_parity.py``'s
  scanner — a standalone copy, not a shared helper, for the reasons that file
  states.
* **SSH-BIND-002** — the grant response agrees with what Docker actually
  published (``HostConfig.PortBindings``, Invariant #11), never with the
  configured value alone, and says how to connect.
* **SSH-BIND-003** — no listener without a grant: create sites pass
  ``ENABLE_SSH=false``; the grant path starts sshd on demand; the boot sweep
  warns about containers still on every interface and stops idle listeners.
* **SSH-BIND-004** — the base image ships a locked ``developer`` account and a
  drop-in sshd policy whose EFFECTIVE values are asserted at image build
  (``sshd -T``). The only place ``sshd -T`` can run in CI is the image build,
  so this file pins the Dockerfile text and the drop-in; the build pins the
  behaviour.
* **SSH-BIND-005** — the four-file wiring rule for the new knob.

Lives in tests/unit/ — no backend connection, no Docker daemon. Async tests
carry an explicit ``@pytest.mark.asyncio`` (strict mode in this island).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

ENV_KEY = "AGENT_SSH_BIND_ADDRESS"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexampleexampleexampleexample test@example.com"


def _binding():
    from services import agent_ssh_binding
    return agent_ssh_binding


# ---------------------------------------------------------------------------
# SSH-BIND-001 — resolver + port mapping
# ---------------------------------------------------------------------------


class TestResolver:
    def test_unset_is_loopback(self, monkeypatch, caplog):
        monkeypatch.delenv(ENV_KEY, raising=False)
        with caplog.at_level(logging.WARNING):
            assert _binding().resolve_agent_ssh_bind_address() == "127.0.0.1"
        assert not caplog.records, "the default must be silent"

    def test_blank_and_whitespace_are_loopback(self, monkeypatch):
        for raw in ("", "   ", "\t"):
            monkeypatch.setenv(ENV_KEY, raw)
            assert _binding().resolve_agent_ssh_bind_address() == "127.0.0.1"

    def test_all_interfaces_is_an_explicit_opt_in(self, monkeypatch):
        for raw in ("0.0.0.0", "::"):
            monkeypatch.setenv(ENV_KEY, raw)
            assert _binding().resolve_agent_ssh_bind_address() == raw

    def test_value_is_stripped(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "  10.1.2.3 ")
        assert _binding().resolve_agent_ssh_bind_address() == "10.1.2.3"

    def test_ipv6_literal_is_bracket_free(self, monkeypatch):
        """docker-py's HostIp takes the bare literal, never ``[::1]``."""
        monkeypatch.setenv(ENV_KEY, "fd00::1")
        assert _binding().resolve_agent_ssh_bind_address() == "fd00::1"

    @pytest.mark.parametrize("bad", ["localhost", "1.2.3", "0.0.0.0:22", "[::1]", "tailscale0", "127.0.0.1/8"])
    def test_invalid_falls_back_to_loopback_never_all_interfaces(self, monkeypatch, caplog, bad):
        b = _binding()
        b._reset_logged_values_for_tests()
        monkeypatch.setenv(ENV_KEY, bad)
        with caplog.at_level(logging.ERROR, logger=b.__name__):
            assert b.resolve_agent_ssh_bind_address() == "127.0.0.1"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "exactly one named ERROR per bad value"
        assert ENV_KEY in errors[0].getMessage() and bad in errors[0].getMessage()

    def test_invalid_value_is_logged_once_not_per_create(self, monkeypatch, caplog):
        b = _binding()
        b._reset_logged_values_for_tests()
        monkeypatch.setenv(ENV_KEY, "not-an-ip")
        with caplog.at_level(logging.ERROR, logger=b.__name__):
            b.resolve_agent_ssh_bind_address()
            b.resolve_agent_ssh_bind_address()
            b.resolve_agent_ssh_bind_address()
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    def test_specific_non_loopback_ip_warns_about_interface_ordering(self, monkeypatch, caplog):
        """E5: a VPN interface IP fails every ``unless-stopped`` restart until that
        interface is up after a reboot — say so once, at WARNING."""
        b = _binding()
        b._reset_logged_values_for_tests()
        monkeypatch.setenv(ENV_KEY, "100.64.0.5")
        with caplog.at_level(logging.WARNING, logger=b.__name__):
            assert b.resolve_agent_ssh_bind_address() == "100.64.0.5"
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1 and "100.64.0.5" in warns[0].getMessage()

    def test_detail_names_the_source(self, monkeypatch):
        b = _binding()
        b._reset_logged_values_for_tests()
        monkeypatch.delenv(ENV_KEY, raising=False)
        assert b.resolve_agent_ssh_bind_address_detail() == ("127.0.0.1", "default")
        monkeypatch.setenv(ENV_KEY, "0.0.0.0")
        assert b.resolve_agent_ssh_bind_address_detail() == ("0.0.0.0", "env")
        monkeypatch.setenv(ENV_KEY, "garbage")
        assert b.resolve_agent_ssh_bind_address_detail() == ("127.0.0.1", "invalid")


class TestPortBinding:
    def test_tuple_form_publishes_on_the_resolved_ip(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "10.9.8.7")
        assert _binding().agent_ssh_port_binding(2231) == {"22/tcp": ("10.9.8.7", 2231)}

    def test_default_is_loopback_tuple_not_bare_int(self, monkeypatch):
        """The bug: a bare int publishes on ``0.0.0.0``."""
        monkeypatch.delenv(ENV_KEY, raising=False)
        value = _binding().agent_ssh_port_binding(2222)["22/tcp"]
        assert isinstance(value, tuple) and value == ("127.0.0.1", 2222)

    def test_port_is_coerced_to_int(self, monkeypatch):
        monkeypatch.delenv(ENV_KEY, raising=False)
        assert _binding().agent_ssh_port_binding("2222")["22/tcp"][1] == 2222


class TestPublishedHostIp:
    def test_empty_host_ip_means_all_interfaces(self):
        attrs = {"HostConfig": {"PortBindings": {"22/tcp": [{"HostIp": "", "HostPort": "2237"}]}}}
        assert _binding().published_ssh_host_ip(attrs) == "0.0.0.0"

    def test_explicit_ip_is_returned(self):
        attrs = {"HostConfig": {"PortBindings": {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2237"}]}}}
        assert _binding().published_ssh_host_ip(attrs) == "127.0.0.1"

    def test_absent_mapping_is_none(self):
        assert _binding().published_ssh_host_ip({"HostConfig": {"PortBindings": {}}}) is None
        assert _binding().published_ssh_host_ip({"HostConfig": {"PortBindings": None}}) is None
        assert _binding().published_ssh_host_ip({}) is None
        assert _binding().published_ssh_host_ip(None) is None


class TestBindFailureClassifier:
    def test_cannot_assign_requested_address_names_the_knob(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "100.64.0.5")
        exc = RuntimeError("500 Server Error: driver failed programming external connectivity: "
                           "Error starting userland proxy: listen tcp4 100.64.0.5:2222: bind: "
                           "cannot assign requested address")
        msg = _binding().describe_ssh_bind_failure(exc)
        assert msg and ENV_KEY in msg and "100.64.0.5" in msg

    def test_walks_the_cause_chain(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "100.64.0.5")
        inner = OSError("bind: cannot assign requested address")
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert _binding().describe_ssh_bind_failure(outer)

    def test_unrelated_error_is_none(self):
        assert _binding().describe_ssh_bind_failure(RuntimeError("port is already allocated")) is None
        assert _binding().describe_ssh_bind_failure(RuntimeError("Conflict. The container name is already in use")) is None

    def test_raise_for_ssh_bind_failure_raises_the_named_error(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "100.64.0.5")
        b = _binding()
        exc = RuntimeError("cannot assign requested address")
        with pytest.raises(b.AgentSshBindAddressError) as ei:
            b.raise_for_ssh_bind_failure(exc)
        assert ei.value.__cause__ is exc
        # unrelated: returns, so the caller's bare `raise` re-raises the original
        assert b.raise_for_ssh_bind_failure(RuntimeError("other")) is None


def test_leaf_module_imports_only_the_stdlib():
    """The helper sits on the create path and in the unit island; it must never
    grow a docker / redis / fastapi import (Decision 5′)."""
    tree = ast.parse((_BACKEND / "services/agent_ssh_binding.py").read_text(encoding="utf-8"))
    allowed = {"os", "ipaddress", "logging", "dataclasses", "typing", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert names <= allowed, f"non-stdlib import in the leaf: {names - allowed}"


# ---------------------------------------------------------------------------
# SSH-BIND-001 — the create-site guard (mirrors test_2541's scanner)
# ---------------------------------------------------------------------------

_HELPER_NAME = "agent_ssh_port_binding"
_CALL_NAMES = ("containers_run", "containers.run", "containers.create")
_PREFILTER_TOKEN = "containers"
_HELPER_LAYER = {"services/docker_utils.py"}
_LEAF = "services/agent_ssh_binding.py"
_EXCLUDE_DIRS = ("scheduler/",)
# docker-py spellings that publish ports without going through `ports=`.
_FORBIDDEN_KWARGS = ("publish_all_ports", "host_config", "port_bindings")


def _is_container_create(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "containers_run"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "containers_run":
        return True
    if func.attr not in ("run", "create"):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "containers"
    if isinstance(receiver, ast.Name):
        return receiver.id == "containers"
    return False


def _is_helper_call(value: ast.AST) -> bool:
    if not isinstance(value, ast.Call):
        return False
    f = value.func
    return (isinstance(f, ast.Name) and f.id == _HELPER_NAME) or (
        isinstance(f, ast.Attribute) and f.attr == _HELPER_NAME
    )


def _offending_calls(rel_path: str, text: str) -> list[str]:
    """Create sites that publish ports any way other than the shared helper, and
    (S7, scoped to services/**) any bare ``'22/tcp'`` outside the leaf."""
    if rel_path in _HELPER_LAYER or rel_path == _LEAF:
        return []
    if _PREFILTER_TOKEN not in text and "22/tcp" not in text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel_path}: unparseable ({exc.__class__.__name__})"]

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "22/tcp" and rel_path.startswith("services/"):
            offenders.append(f"{rel_path}:{node.lineno} (bare '22/tcp' outside {_LEAF})")
            continue
        if not isinstance(node, ast.Call) or not _is_container_create(node):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        for forbidden in _FORBIDDEN_KWARGS:
            if forbidden in kwargs:
                offenders.append(f"{rel_path}:{node.lineno} ({forbidden}= bypasses the SSH bind helper)")
        if "ports" in kwargs and not _is_helper_call(kwargs["ports"]):
            offenders.append(f"{rel_path}:{node.lineno} (ports= is not {_HELPER_NAME}(...))")
    return offenders


def _scan_backend() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            continue
        offenders.extend(_offending_calls(rel, path.read_text(encoding="utf-8")))
    return offenders


class TestCreateSiteGuard:
    def test_every_published_port_goes_through_the_helper(self):
        offenders = _scan_backend()
        assert not offenders, (
            "These call sites publish a container port without the shared SSH bind "
            "helper (trinity-enterprise#1168). Pass ports=agent_ssh_port_binding(<port>) "
            "from services.agent_ssh_binding — a bare int publishes on 0.0.0.0:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_three_known_agent_create_sites_use_the_helper(self):
        expected = {
            "services/agent_service/crud.py",
            "services/agent_service/lifecycle.py",
            "services/system_agent_service.py",
        }
        for rel in expected:
            text = (_BACKEND / rel).read_text(encoding="utf-8")
            assert f"ports={_HELPER_NAME}(" in text, (
                f"{rel} no longer passes ports={_HELPER_NAME}(...) — agents born there "
                "publish SSH on every interface (trinity-enterprise#1168)."
            )

    # --- meta-tests: the guard must actually catch things ---

    def test_guard_detects_bare_int_port(self):
        bad = "await containers_run(image, detach=True, ports={'22/tcp': port})\n"
        assert _offending_calls("services/planted_bad.py", bad) != []

    def test_guard_detects_literal_tuple_even_when_loopback(self):
        bad = "await containers_run(image, detach=True, ports={'22/tcp': ('127.0.0.1', port)})\n"
        assert _offending_calls("services/planted_literal.py", bad) != []

    def test_guard_detects_a_name_that_is_not_a_call(self):
        bad = "ports = build()\nawait containers_run(image, detach=True, ports=ports)\n"
        assert _offending_calls("services/planted_name.py", bad) != []

    def test_guard_passes_the_helper_in_both_spellings(self):
        good = (
            "await containers_run(image, detach=True, ports=agent_ssh_port_binding(port))\n"
            "await containers_run(image, detach=True, ports=binding.agent_ssh_port_binding(port))\n"
        )
        assert _offending_calls("services/planted_good.py", good) == []

    def test_guard_passes_a_create_with_no_ports(self):
        helper = "await containers_run('alpine', command='chown 1000:1000 /x', remove=True)\n"
        assert _offending_calls("services/planted_helper.py", helper) == []

    @pytest.mark.parametrize("kw", _FORBIDDEN_KWARGS)
    def test_guard_rejects_bypass_kwargs(self, kw):
        bad = f"await containers_run(image, detach=True, {kw}=x)\n"
        assert _offending_calls("services/planted_bypass.py", bad) != []

    def test_guard_detects_the_third_creation_form(self):
        bad = "c = client.containers.run(image, detach=True, ports={'22/tcp': 2222})\n"
        assert _offending_calls("services/planted_raw.py", bad) != []

    def test_guard_detects_containers_create(self):
        bad = "c = client.containers.create(image, ports={'22/tcp': 2222})\n"
        assert _offending_calls("services/planted_create.py", bad) != []

    def test_guard_flags_a_bare_22_tcp_in_services_outside_the_leaf(self):
        bad = "mapping = container.attrs['NetworkSettings']['Ports']['22/tcp']\n"
        assert _offending_calls("services/planted_reader.py", bad) != []

    def test_guard_allows_22_tcp_outside_services(self):
        """S7: the constant ban is scoped to services/** so a future router or
        test reading NetworkSettings.Ports is not swept in."""
        ok = "mapping = attrs['NetworkSettings']['Ports']['22/tcp']\n"
        assert _offending_calls("routers/planted_reader.py", ok) == []

    def test_guard_ignores_unrelated_run_and_create(self):
        unrelated = (
            'v = client.volumes.create(name="x")\n'
            "s = client.services.create(image)\n"
            'r = subprocess.run(["ls"])\n'
        )
        assert _offending_calls("services/planted_unrelated.py", unrelated) == []

    def test_guard_fails_closed_on_unparseable_file_that_mentions_the_call(self):
        broken = "async def f(:\n    await containers_run(image, ports=x)\n"
        assert _offending_calls("services/planted_broken.py", broken) != []

    def test_guard_skips_unparseable_file_that_cannot_contain_a_call(self):
        newer = "try:\n    pass\nexcept* ValueError:\n    pass\n"
        assert _offending_calls("services/planted_newer.py", newer) == []

    def test_prefilter_token_covers_every_call_form(self):
        for form in _CALL_NAMES:
            assert _PREFILTER_TOKEN in form

    def test_exempt_files_still_exist(self):
        for rel in _HELPER_LAYER | {_LEAF}:
            assert (_BACKEND / rel).is_file(), rel


# ---------------------------------------------------------------------------
# SSH-BIND-003 — no listener without a grant (create-time env)
# ---------------------------------------------------------------------------


class TestNoListenerByDefault:
    _SITES = (
        "services/agent_service/crud.py",
        "services/agent_service/lifecycle.py",
        "services/system_agent_service.py",
    )

    def test_no_create_site_bakes_enable_ssh_true(self):
        pattern = re.compile(r"""ENABLE_SSH['"]\s*[:=]\s*['"]true['"]""")
        for path in (_BACKEND / "services").rglob("*.py"):
            hits = pattern.findall(path.read_text(encoding="utf-8"))
            assert not hits, f"{path.relative_to(_BACKEND)} still bakes ENABLE_SSH=true"

    def test_every_create_site_sets_enable_ssh_false(self):
        pattern = re.compile(r"""ENABLE_SSH['"]\]?\s*[:=]\s*['"]false['"]""")
        for rel in self._SITES:
            assert pattern.search((_BACKEND / rel).read_text(encoding="utf-8")), (
                f"{rel} does not set ENABLE_SSH=false — the recreate path seeds env from the "
                "OLD container, so an unset key would carry a baked `true` forward."
            )

    def test_startup_sh_still_honours_the_switch(self):
        """Old images and the new one agree: sshd starts only on ENABLE_SSH=true,
        which is what makes the on-demand start image-independent."""
        text = (_ROOT / "docker/base-image/startup.sh").read_text(encoding="utf-8")
        assert re.search(r'if \[ "\$\{ENABLE_SSH\}" = "true" \]', text)


# ---------------------------------------------------------------------------
# SSH-BIND-002 — the decision table, pure
# ---------------------------------------------------------------------------


def _attrs(host_ip):
    if host_ip is None:
        return {"HostConfig": {"PortBindings": {}}}
    return {"HostConfig": {"PortBindings": {"22/tcp": [{"HostIp": host_ip, "HostPort": "2222"}]}}}


class TestDecisionTable:
    def test_all_interfaces_container_keeps_detected_host_and_warns_recreate(self, monkeypatch):
        monkeypatch.delenv(ENV_KEY, raising=False)
        d = _binding().decide_ssh_connection(_attrs(""), 2222, "ssh.example.com", "ssh_host")
        assert d.host == "ssh.example.com" and d.bind_address == "0.0.0.0"
        assert d.command == "ssh -p 2222 developer@ssh.example.com"
        assert len(d.warnings) == 1
        assert "recreate" in d.warnings[0].lower() and ENV_KEY in d.warnings[0]

    def test_all_interfaces_container_with_all_interfaces_configured_is_silent(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "0.0.0.0")
        d = _binding().decide_ssh_connection(_attrs(""), 2222, "ssh.example.com", "ssh_host")
        assert d.warnings == [] and d.host == "ssh.example.com"

    def test_loopback_container_advertises_loopback_with_jump_hint(self):
        d = _binding().decide_ssh_connection(_attrs("127.0.0.1"), 2222, "ssh.example.com", "ssh_host")
        assert d.host == "127.0.0.1" and d.bind_address == "127.0.0.1"
        assert d.command == "ssh -p 2222 developer@127.0.0.1"
        assert len(d.warnings) == 1
        assert "-J" in d.warnings[0] and "ssh.example.com" in d.warnings[0]
        assert ENV_KEY in d.warnings[0]

    @pytest.mark.parametrize("source", ["tailscale", "host_docker_internal", "gateway", "localhost"])
    def test_loopback_jump_hint_only_when_the_host_is_a_real_name(self, source):
        """E10: `-J <you>@host.docker.internal` is nonsense from a Mac."""
        d = _binding().decide_ssh_connection(_attrs("127.0.0.1"), 2222, "192.168.65.2", source)
        assert d.host == "127.0.0.1"
        assert len(d.warnings) == 1 and "-J" not in d.warnings[0]

    def test_loopback_jump_hint_for_frontend_url_too(self):
        d = _binding().decide_ssh_connection(_attrs("127.0.0.1"), 2222, "trinity.example.com", "frontend_url")
        assert "-J" in d.warnings[0]

    def test_specific_ip_is_advertised_and_disagreeing_ssh_host_is_named(self):
        d = _binding().decide_ssh_connection(_attrs("100.64.0.5"), 2222, "ssh.example.com", "ssh_host")
        assert d.host == "100.64.0.5" and d.bind_address == "100.64.0.5"
        assert d.command == "ssh -p 2222 developer@100.64.0.5"
        assert len(d.warnings) == 1 and "SSH_HOST" in d.warnings[0] and "ssh.example.com" in d.warnings[0]

    def test_specific_ip_matching_detected_host_is_silent(self):
        d = _binding().decide_ssh_connection(_attrs("100.64.0.5"), 2222, "100.64.0.5", "tailscale")
        assert d.host == "100.64.0.5" and d.warnings == []

    def test_specific_ip_with_autodetected_host_does_not_blame_ssh_host(self):
        d = _binding().decide_ssh_connection(_attrs("100.64.0.5"), 2222, "192.168.1.9", "gateway")
        assert d.host == "100.64.0.5" and d.warnings == []

    def test_unknown_binding_keeps_detected_host(self):
        d = _binding().decide_ssh_connection(_attrs(None), 2222, "ssh.example.com", "ssh_host")
        assert d.host == "ssh.example.com" and d.bind_address is None and d.warnings == []

    def test_warnings_is_always_a_fresh_list(self):
        a = _binding().decide_ssh_connection(_attrs(None), 2222, "h", "ssh_host")
        b = _binding().decide_ssh_connection(_attrs(None), 2222, "h", "ssh_host")
        assert isinstance(a.warnings, list) and a.warnings is not b.warnings


# ---------------------------------------------------------------------------
# SSH-BIND-002/003 — the router, with Docker / Redis / settings stubbed
# ---------------------------------------------------------------------------


@pytest.fixture()
def ssh(monkeypatch):
    """The real handler (test_1615's fixture shape, extended with PortBindings,
    the host-source detail and the on-demand sshd start)."""
    from routers import agent_ssh

    state = {
        "host_ip": "",
        "host": "ssh.example.com",
        "source": "ssh_host",
        "sshd_ok": True,
        "calls": [],
    }
    container = types.SimpleNamespace(status="running", attrs={})

    def _container(name):
        state["calls"].append(("get_agent_container", name))
        container.attrs = {
            "Config": {"Labels": {"trinity.ssh-port": "2222"}},
            **_attrs(state["host_ip"]),
        }
        return container

    monkeypatch.setattr(agent_ssh, "get_agent_container", _container)

    async def _reload(c):
        return None

    monkeypatch.setattr(agent_ssh, "container_reload", _reload)

    class _Svc:
        async def ensure_sshd_running(self, agent_name):
            state["calls"].append(("ensure_sshd_running", agent_name))
            return state["sshd_ok"]

        async def inject_ssh_key(self, agent_name, key):
            state["calls"].append(("inject_ssh_key", agent_name))
            state["injected"] = key
            return True

        def store_key_metadata(self, *a, **kw):
            state["calls"].append(("store_key_metadata", kw.get("agent_name")))

    monkeypatch.setitem(sys.modules, "services.ssh_service", types.SimpleNamespace(
        get_ssh_service=lambda: _Svc(),
        get_ssh_host=lambda: state["host"],
        get_ssh_host_detail=lambda: (state["host"], state["source"]),
        SSH_ACCESS_MAX_TTL_HOURS=24,
    ))
    monkeypatch.setitem(sys.modules, "services.settings_service", types.SimpleNamespace(
        get_ops_setting=lambda *a, **kw: True,
    ))
    monkeypatch.delenv(ENV_KEY, raising=False)

    admin = types.SimpleNamespace(id=1, username="admin", email="admin@example.com", role="admin")

    async def call(agent_name="bot", **body_kwargs):
        from models import SshAccessRequest
        body_kwargs.setdefault("public_key", PUBKEY)
        return await agent_ssh.create_ssh_access(
            agent_name=agent_name, body=SshAccessRequest(**body_kwargs), current_user=admin,
        )

    state["call"] = call
    return types.SimpleNamespace(**state, state=state)


class TestRouter:
    @pytest.mark.asyncio
    async def test_response_always_carries_warnings_list_and_bind_address(self, ssh):
        ssh.state["host_ip"] = None
        resp = await ssh.call()
        assert resp["warnings"] == [] and isinstance(resp["warnings"], list)
        assert "bind_address" in resp["connection"] and resp["connection"]["bind_address"] is None
        assert resp["connection"]["host"] == "ssh.example.com"

    @pytest.mark.asyncio
    async def test_legacy_all_interfaces_container_is_named_with_the_recreate_step(self, ssh):
        ssh.state["host_ip"] = ""
        resp = await ssh.call()
        assert resp["connection"]["host"] == "ssh.example.com"
        assert resp["connection"]["bind_address"] == "0.0.0.0"
        assert len(resp["warnings"]) == 1 and ENV_KEY in resp["warnings"][0]
        # instructions repeat the warnings so a client that only prints them sees it
        assert resp["warnings"][0] in resp["instructions"]

    @pytest.mark.asyncio
    async def test_loopback_container_advertises_loopback_and_a_jump(self, ssh):
        ssh.state["host_ip"] = "127.0.0.1"
        resp = await ssh.call()
        assert resp["connection"]["host"] == "127.0.0.1"
        assert resp["connection"]["command"] == "ssh -p 2222 developer@127.0.0.1"
        assert "-J" in resp["warnings"][0] and "ssh.example.com" in resp["warnings"][0]
        assert f"Connect: {resp['connection']['command']}" in resp["instructions"]

    @pytest.mark.asyncio
    async def test_specific_ip_container_advertises_that_ip(self, ssh):
        ssh.state["host_ip"] = "100.64.0.5"
        resp = await ssh.call()
        assert resp["connection"]["host"] == "100.64.0.5"
        assert "SSH_HOST" in resp["warnings"][0]

    @pytest.mark.asyncio
    async def test_sshd_is_started_on_demand_before_the_key_is_injected(self, ssh):
        ssh.state["host_ip"] = "127.0.0.1"
        await ssh.call()
        names = [c[0] for c in ssh.state["calls"]]
        assert "ensure_sshd_running" in names and "inject_ssh_key" in names
        assert names.index("ensure_sshd_running") < names.index("inject_ssh_key")
        assert "trinity-ephemeral-bot-" in ssh.state["injected"]

    @pytest.mark.asyncio
    async def test_sshd_start_failure_is_a_named_500_and_no_key_is_injected(self, ssh):
        from fastapi import HTTPException
        ssh.state["sshd_ok"] = False
        with pytest.raises(HTTPException) as ei:
            await ssh.call()
        assert ei.value.status_code == 500 and "SSH server" in ei.value.detail
        assert "inject_ssh_key" not in [c[0] for c in ssh.state["calls"]]

    @pytest.mark.asyncio
    async def test_system_agent_resolves_through_the_same_lookup(self, ssh):
        """G4: removing ENABLE_SSH=true from the system-agent create means the
        on-demand start must reach `agent-trinity-system` too — same lookup,
        same name, nothing special-cased."""
        ssh.state["host_ip"] = "127.0.0.1"
        await ssh.call(agent_name="trinity-system")
        assert ("get_agent_container", "trinity-system") in ssh.state["calls"]
        assert ("ensure_sshd_running", "trinity-system") in ssh.state["calls"]


# ---------------------------------------------------------------------------
# SSH-BIND-002/003 — ssh_service: host detail, on-demand start, boot sweep
# ---------------------------------------------------------------------------


def _load_ssh_service(monkeypatch, docker_client=None):
    """Load services/ssh_service.py with redis + docker stubbed, as
    test_ssh_service.py does, and return the module."""
    import importlib.util
    from unittest.mock import AsyncMock, Mock

    mock_redis = Mock()
    mock_redis.from_url = Mock(return_value=Mock())
    exec_run = AsyncMock()
    docker_service = types.SimpleNamespace(docker_client=docker_client, get_agent_container=Mock())
    docker_utils = types.SimpleNamespace(container_exec_run=exec_run)
    monkeypatch.setitem(sys.modules, "redis", mock_redis)
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_service)
    monkeypatch.setitem(sys.modules, "services.docker_utils", docker_utils)
    monkeypatch.delitem(sys.modules, "services.ssh_service", raising=False)
    spec = importlib.util.spec_from_file_location("ssh_service_1168", _BACKEND / "services/ssh_service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, exec_run


class TestHostDetail:
    def test_ssh_host_env_is_source_ssh_host(self, monkeypatch):
        mod, _ = _load_ssh_service(monkeypatch)
        monkeypatch.setenv("SSH_HOST", "ssh.example.com")
        assert mod.get_ssh_host_detail() == ("ssh.example.com", "ssh_host")
        assert mod.get_ssh_host() == "ssh.example.com"

    def test_frontend_url_is_source_frontend_url(self, monkeypatch):
        mod, _ = _load_ssh_service(monkeypatch)
        monkeypatch.delenv("SSH_HOST", raising=False)
        monkeypatch.setenv("FRONTEND_URL", "https://trinity.example.com")
        assert mod.get_ssh_host_detail() == ("trinity.example.com", "frontend_url")


class TestEnsureSshdRunning:
    @pytest.mark.asyncio
    async def test_runs_as_root_creates_the_privsep_dir_and_is_idempotent(self, monkeypatch):
        """G1: startup.sh creates /var/run/sshd only under ENABLE_SSH=true, and
        sshd refuses to start without it; the exec must run as root."""
        mod, exec_run = _load_ssh_service(monkeypatch)
        exec_run.return_value = types.SimpleNamespace(exit_code=0, output=b"")
        container = object()
        mod.get_agent_container = lambda name: container
        svc = mod.SshService(redis_url="redis://x")
        assert await svc.ensure_sshd_running("bot") is True
        args, kwargs = exec_run.call_args
        assert args[0] is container and kwargs.get("user") == "root"
        script = " ".join(args[1]) if isinstance(args[1], (list, tuple)) else args[1]
        assert "mkdir -p /var/run/sshd" in script
        assert "pgrep -x" in script and "/usr/sbin/sshd" in script
        # The boot sweep's `pkill` leaves a `[sshd] <defunct>` zombie that PID 1
        # never reaps (verified live); a bare `pgrep -x sshd` matches it and
        # would skip the start. `--runstates RSD` excludes zombies.
        assert "--runstates RSD sshd" in script

    @pytest.mark.asyncio
    async def test_nonzero_exit_or_exception_is_false(self, monkeypatch):
        mod, exec_run = _load_ssh_service(monkeypatch)
        mod.get_agent_container = lambda name: object()
        svc = mod.SshService(redis_url="redis://x")
        exec_run.return_value = types.SimpleNamespace(exit_code=1, output=b"Missing privilege separation directory")
        assert await svc.ensure_sshd_running("bot") is False
        exec_run.side_effect = RuntimeError("docker down")
        assert await svc.ensure_sshd_running("bot") is False

    @pytest.mark.asyncio
    async def test_missing_container_is_false(self, monkeypatch):
        mod, _ = _load_ssh_service(monkeypatch)
        mod.get_agent_container = lambda name: None
        svc = mod.SshService(redis_url="redis://x")
        assert await svc.ensure_sshd_running("ghost") is False


class _FakeContainer:
    def __init__(self, name, host_ip, status="running", fail_exec=False):
        self.name = f"agent-{name}"
        self.status = status
        self.attrs = {"Config": {"Labels": {"trinity.agent-name": name}}, **_attrs(host_ip)}
        self.execs = []
        self._fail_exec = fail_exec
        self.reloaded = 0

    def reload(self):
        self.reloaded += 1

    def exec_run(self, cmd, **kwargs):
        if self._fail_exec:
            raise RuntimeError("exec failed")
        self.execs.append((cmd, kwargs))
        return types.SimpleNamespace(exit_code=0, output=b"")


class _FakeDocker:
    def __init__(self, containers):
        self._containers = containers
        self.containers = types.SimpleNamespace(list=self._list)
        self.list_filters = None

    def _list(self, all=False, filters=None):
        self.list_filters = (all, filters)
        return list(self._containers)


class TestPostureSweep:
    def _settings(self, monkeypatch, enabled):
        monkeypatch.setitem(sys.modules, "services.settings_service", types.SimpleNamespace(
            get_ops_setting=lambda key, as_type=str: enabled,
        ))

    @pytest.mark.asyncio
    async def test_names_every_all_interfaces_container_in_one_warning(self, monkeypatch, caplog):
        monkeypatch.delenv(ENV_KEY, raising=False)
        self._settings(monkeypatch, enabled=True)
        docker = _FakeDocker([
            _FakeContainer("legacy-a", ""),
            _FakeContainer("legacy-b", "", status="exited"),
            _FakeContainer("fresh", "127.0.0.1"),
            _FakeContainer("noports", None),
        ])
        mod, _ = _load_ssh_service(monkeypatch, docker_client=docker)
        with caplog.at_level(logging.INFO, logger=mod.__name__):
            report = await mod.sweep_agent_ssh_posture()
        assert docker.list_filters == (True, {"label": "trinity.platform=agent"})
        assert sorted(report["all_interfaces"]) == ["legacy-a", "legacy-b"]
        assert report["checked"] == 4
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1 and "legacy-a" in warns[0] and "legacy-b" in warns[0] and "fresh" not in warns[0]
        assert "recreate" in warns[0].lower()
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any(ENV_KEY in m and "127.0.0.1" in m and "default" in m for m in infos), (
            "the boot log must name the resolved bind address and its source")

    @pytest.mark.asyncio
    async def test_all_interfaces_configured_warns_about_nothing(self, monkeypatch, caplog):
        monkeypatch.setenv(ENV_KEY, "0.0.0.0")
        self._settings(monkeypatch, enabled=True)
        docker = _FakeDocker([_FakeContainer("a", "")])
        mod, _ = _load_ssh_service(monkeypatch, docker_client=docker)
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            report = await mod.sweep_agent_ssh_posture()
        assert report["all_interfaces"] == []
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_idle_listeners_are_stopped_as_root_only_when_ssh_access_is_disabled(self, monkeypatch):
        monkeypatch.delenv(ENV_KEY, raising=False)
        running, stopped = _FakeContainer("r", ""), _FakeContainer("s", "", status="exited")
        docker = _FakeDocker([running, stopped])

        self._settings(monkeypatch, enabled=False)
        mod, _ = _load_ssh_service(monkeypatch, docker_client=docker)
        report = await mod.sweep_agent_ssh_posture()
        assert report["stopped"] == ["r"]
        assert len(running.execs) == 1 and running.execs[0][1].get("user") == "root"
        cmd = running.execs[0][0]
        assert "pkill -x sshd" in (" ".join(cmd) if isinstance(cmd, (list, tuple)) else cmd)
        assert stopped.execs == [], "a stopped container has no process to stop"

    @pytest.mark.asyncio
    async def test_enabled_setting_leaves_every_listener_alone(self, monkeypatch):
        monkeypatch.delenv(ENV_KEY, raising=False)
        running = _FakeContainer("r", "")
        self._settings(monkeypatch, enabled=True)
        mod, _ = _load_ssh_service(monkeypatch, docker_client=_FakeDocker([running]))
        report = await mod.sweep_agent_ssh_posture()
        assert report["stopped"] == [] and running.execs == []

    @pytest.mark.asyncio
    async def test_sweep_is_fail_open(self, monkeypatch, caplog):
        monkeypatch.delenv(ENV_KEY, raising=False)
        self._settings(monkeypatch, enabled=False)
        bad = _FakeContainer("bad", "", fail_exec=True)
        good = _FakeContainer("good", "")
        mod, _ = _load_ssh_service(monkeypatch, docker_client=_FakeDocker([bad, good]))
        report = await mod.sweep_agent_ssh_posture()
        assert report["stopped"] == ["good"], "one failing exec must not abort the sweep"

        class _Broken:
            containers = types.SimpleNamespace(list=lambda **kw: (_ for _ in ()).throw(RuntimeError("daemon")))

        mod, _ = _load_ssh_service(monkeypatch, docker_client=_Broken())
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            report = await mod.sweep_agent_ssh_posture()
        assert report["checked"] == 0

    @pytest.mark.asyncio
    async def test_no_docker_is_a_noop(self, monkeypatch):
        mod, _ = _load_ssh_service(monkeypatch, docker_client=None)
        assert (await mod.sweep_agent_ssh_posture())["checked"] == 0


def test_lifespan_calls_the_sweep_inside_the_docker_phase():
    """E8: `test_1028` pins STARTUP_PHASES by awaited name — the sweep lives
    INSIDE `_init_docker_and_system_agent`, never as a new phase."""
    tree = ast.parse((_BACKEND / "main.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_init_docker_and_system_agent")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "sweep_agent_ssh_posture" in attrs | names


# ---------------------------------------------------------------------------
# SSH-BIND-005 — wiring (the four-file rule)
# ---------------------------------------------------------------------------

_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml")


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class TestWiring:
    def test_every_compose_file_forwards_the_knob_with_a_default(self):
        for name in _COMPOSE_FILES:
            text = (_ROOT / name).read_text(encoding="utf-8")
            uses = re.findall(rf"\$\{{{ENV_KEY}([^}}]*)\}}", text)
            assert uses, f"{name} does not forward {ENV_KEY} to the backend (the #1039 inert-knob class)"
            assert all(u.startswith(":-") for u in uses), f"{name}: {ENV_KEY} without a default: {uses}"

    def test_env_example_documents_the_knob_beside_ssh_host(self):
        text = (_ROOT / ".env.example").read_text(encoding="utf-8")
        assert re.search(rf"^{ENV_KEY}=", text, re.M), f".env.example does not document {ENV_KEY}"
        assert text.index("SSH_HOST=") < text.index(f"{ENV_KEY}=") < text.index("MCP_BIND_ADDRESS=")

    def test_env_example_names_the_asymmetry_with_mcp_bind_address(self):
        """S4: three bind knobs, two semantics — say so where the operator reads."""
        text = (_ROOT / ".env.example").read_text(encoding="utf-8")
        start = text.index("SSH ACCESS CONFIGURATION")
        block = text[start:text.index("# ====", start + 60)]
        assert "MCP_BIND_ADDRESS" in block and "127.0.0.1" in block

    def test_provision_writes_loopback(self):
        text = (_ROOT / "scripts/deploy/start.sh").read_text(encoding="utf-8")
        assert re.search(rf"set_env_key {ENV_KEY} 127\.0\.0\.1", text)

    @pytest.mark.skipif(not _compose_available(), reason="docker compose CLI not available")
    def test_compose_resolves_the_knob_into_the_backend_env(self):
        """Resolution, not grep (learnings 2026-07-30)."""
        import secrets
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
        env.update({
            "ADMIN_PASSWORD": secrets.token_urlsafe(18) + "Aa1!",
            "REDIS_PASSWORD": secrets.token_hex(24),
            "REDIS_BACKEND_PASSWORD": secrets.token_hex(24),
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "TUNNEL_TOKEN": "placeholder",
            ENV_KEY: "10.0.0.5",
        })
        proc = subprocess.run(
            ["docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.prod.yml",
             "--profile", "tunnel", "config", "--format", "json"],
            cwd=_ROOT, capture_output=True, text=True, timeout=120, env=env)
        assert proc.returncode == 0, proc.stderr
        cfg = json.loads(proc.stdout)
        benv = cfg["services"]["backend"]["environment"]
        if isinstance(benv, list):
            benv = dict(item.split("=", 1) for item in benv)
        assert benv.get(ENV_KEY) == "10.0.0.5"


# ---------------------------------------------------------------------------
# SSH-BIND-004 — the base image
# ---------------------------------------------------------------------------

_DOCKERFILE = _ROOT / "docker/base-image/Dockerfile"
_DROPIN = _ROOT / "docker/base-image/sshd_trinity.conf"
_REQUIRED_DIRECTIVES = {
    "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no",
    "challengeresponseauthentication": "no",
    "permitrootlogin": "no",
    "pubkeyauthentication": "yes",
    "permitemptypasswords": "no",
    "usepam": "yes",
}
# What the build asserts with `sshd -T` (its output is lowercase). UsePAM is in
# the set because a `passwd -l` locked account logs in by key only under PAM.
_ASSERTED_EFFECTIVE = (
    "passwordauthentication no",
    "kbdinteractiveauthentication no",
    "permitrootlogin no",
    "pubkeyauthentication yes",
    "usepam yes",
)


class TestBaseImage:
    def test_developer_has_no_password(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "chpasswd" not in text, "developer:developer must not be set"
        assert "passwd -l developer" in text

    def test_sshd_policy_is_a_drop_in_not_a_sed(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "sshd_config.d/10-trinity.conf" in text
        assert "sed -i 's/#PasswordAuthentication" not in text
        assert _DROPIN.is_file()

    def test_drop_in_carries_every_required_directive(self):
        seen = {}
        for line in _DROPIN.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(" ")
            seen[key.lower()] = value.strip().lower()
        for key, value in _REQUIRED_DIRECTIVES.items():
            assert seen.get(key) == value, f"drop-in: {key} {value} missing or wrong (got {seen.get(key)})"

    def test_build_asserts_the_effective_sshd_config(self):
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "sshd -t" in text, "syntax check at build"
        assert "sshd -T" in text, "effective-config dump at build"
        assert re.search(r"grep -q .*Include /etc/ssh/sshd_config\.d", text), (
            "the drop-in only applies through Debian's Include line — assert it is still there")
        for effective in _ASSERTED_EFFECTIVE:
            assert effective in text, f"build does not assert `{effective}`"

    def test_lock_and_drop_in_precede_the_user_switch(self):
        """E7: both need root; everything after `USER developer` runs unprivileged."""
        text = _DOCKERFILE.read_text(encoding="utf-8")
        user_switch = text.index("\nUSER developer")
        assert text.index("passwd -l developer") < user_switch
        assert text.index("sshd_config.d/10-trinity.conf") < user_switch
        assert text.index("sshd -T") < user_switch


# ---------------------------------------------------------------------------
# MCP types stay in sync (passthrough only)
# ---------------------------------------------------------------------------


def test_mcp_types_declare_the_new_response_fields():
    text = (_ROOT / "src/mcp-server/src/types.ts").read_text(encoding="utf-8")
    conn = text[text.index("export interface SshConnectionInfo"):text.index("export interface SshAccessResponse")]
    assert "bind_address" in conn
    resp = text[text.index("export interface SshAccessResponse"):]
    resp = resp[:resp.index("}")]
    assert "warnings" in resp
