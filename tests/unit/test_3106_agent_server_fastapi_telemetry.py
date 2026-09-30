"""#3106 — the agent server must boot whatever FastAPI the container holds.

FastAPI 0.142 auto-configures OpenTelemetry from the standard `OTEL_*` env at
ASGI lifespan startup and REFUSES the `grpc` protocol Trinity injects for
Claude Code — so every OTEL-enabled agent's server exited on its next boot,
because `startup.sh` upgraded FastAPI unpinned on every container start.

Three layers, each pinned here:
  (a) the agent server opts out of FastAPI's auto-telemetry (the `OTEL_*` env
      belongs to Claude Code, not the server), without breaking a FastAPI that
      predates the `telemetry` parameter;
  (b) the agent-server dependencies are exact-pinned in ONE file, used by the
      image build and by the boot-time repair — no unpinned `--upgrade` left;
  (c) the backend sets `OTEL_SDK_DISABLED=true` wherever it injects the OTEL
      env, which rescues agents still on an older base image on recreate.

The test venv runs FastAPI 0.115 (tests/requirements-test.txt), so a real
0.142 boot cannot run here; (a) is tested against both constructor shapes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BASE = _ROOT / "docker" / "base-image"
_REQS = _BASE / "agent-server-requirements.txt"


# --------------------------------------------------------------------- (a)

def test_opt_out_is_passed_when_fastapi_supports_it():
    from agent_server.fastapi_options import fastapi_app_options

    class NewFastAPI:
        def __init__(self, *, title="", telemetry=None):  # noqa: ARG002
            pass

    assert fastapi_app_options(NewFastAPI) == {"telemetry": {"auto_configure": False}}


def test_nothing_is_passed_to_an_older_fastapi():
    from agent_server.fastapi_options import fastapi_app_options

    class OldFastAPI:
        def __init__(self, *, title=""):  # noqa: ARG002
            pass

    assert fastapi_app_options(OldFastAPI) == {}


def test_the_installed_fastapi_accepts_the_options():
    from fastapi import FastAPI

    from agent_server.fastapi_options import fastapi_app_options

    FastAPI(title="probe", **fastapi_app_options(FastAPI))


def test_main_builds_the_app_with_the_options():
    tree = ast.parse((_BASE / "agent_server" / "main.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "FastAPI"]
    assert len(calls) == 1, "one FastAPI() construction expected in agent_server/main.py"
    splats = [k.value for k in calls[0].keywords if k.arg is None]
    assert any(isinstance(s, ast.Call) and getattr(s.func, "id", None) == "fastapi_app_options"
               for s in splats), "FastAPI(...) must receive **fastapi_app_options(FastAPI)"


# --------------------------------------------------------------------- (b)
#
# Two files, two jobs. The IMAGE build installs exact pins (reproducible
# rebuilds, the #3012 lesson). The BOOT repair installs floors for the server's
# own packages only: it installs what is missing or too old and never
# downgrades a newer version a template brought for its own code — the
# agent server and template code share ~/.local.

_IMAGE = _BASE / "agent-image-requirements.txt"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\d+(?:\.\d+)*)$")
_FLOOR = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)>=(\d+(?:\.\d+)*)$")
_SERVER_PACKAGES = {"fastapi", "uvicorn", "httpx", "pydantic", "python-multipart", "pyyaml"}


def _lines(path: Path) -> list[str]:
    lines = [l.split("#", 1)[0].strip() for l in path.read_text().splitlines()]
    return [l for l in lines if l]


def _pins() -> dict[str, str]:
    out = {}
    for line in _lines(_IMAGE):
        m = _PIN.match(line)
        assert m, f"{line!r} in {_IMAGE.name} is not an exact pin"
        out[m.group(1).lower()] = m.group(2)
    return out


def _floors() -> dict[str, str]:
    out = {}
    for line in _lines(_REQS):
        m = _FLOOR.match(line)
        assert m, f"{line!r} in {_REQS.name} must be a plain floor (name>=x.y.z)"
        out[m.group(1).lower()] = m.group(2)
    return out


def _v(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def test_the_image_pins_every_package_exactly():
    assert _SERVER_PACKAGES | {"starlette", "rich", "cryptography"} <= set(_pins())


def test_the_boot_repair_covers_exactly_the_server_packages_as_floors():
    assert set(_floors()) == _SERVER_PACKAGES


def test_every_floor_is_satisfied_by_the_image_pin():
    """A fresh container must boot with the boot repair as a no-op."""
    pins = _pins()
    for name, floor in _floors().items():
        assert _v(pins[name]) >= _v(floor), f"{name}: image pins {pins[name]} below floor {floor}"


def test_fastapi_stays_below_the_auto_telemetry_release():
    """(a) already makes 0.142+ safe; this keeps the image on the line the
    agent server was validated against until someone bumps it on purpose."""
    assert _v(_pins()["fastapi"])[:2] < (0, 142)


def test_the_image_installs_from_the_exact_pins():
    text = (_BASE / "Dockerfile").read_text()
    assert re.search(r"pip install --user[^\n]*-r /opt/trinity/agent-image-requirements\.txt", text)


def _pip_install_commands(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)
    return [l.strip() for l in joined.splitlines() if "pip install" in l and not l.strip().startswith("#")]


def test_the_boot_repair_uses_floors_and_never_upgrades():
    cmds = _pip_install_commands((_BASE / "startup.sh").read_text())
    assert cmds, "startup.sh should still repair the agent-server dependencies"
    for cmd in cmds:
        assert "--upgrade" not in cmd, f"unpinned upgrade at boot: {cmd}"
        assert "-r /opt/trinity/agent-server-requirements.txt" in cmd, cmd
        assert "agent-image-requirements" not in cmd, f"exact pins at boot would downgrade: {cmd}"


# --------------------------------------------------------------------- (c)

def _otel_blocks():
    """Every function in src/backend that assigns OTEL_EXPORTER_OTLP_PROTOCOL."""
    for path in (_ROOT / "src" / "backend").rglob("*.py"):
        if "enterprise" in path.parts:
            continue
        src = path.read_text()
        if "OTEL_EXPORTER_OTLP_PROTOCOL" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = ast.get_source_segment(src, node) or ""
                if re.search(r"\[['\"]OTEL_EXPORTER_OTLP_PROTOCOL['\"]\]\s*=", body):
                    yield path.relative_to(_ROOT), node.name, body


def test_every_otel_injection_also_disables_the_sdk_auto_configuration():
    found = list(_otel_blocks())
    assert len(found) >= 3, found  # create, recovery, system agent
    for path, fn, body in found:
        assert re.search(r"\[['\"]OTEL_SDK_DISABLED['\"]\]\s*=\s*['\"]true['\"]", body), (
            f"{path}::{fn} injects the OTEL env without OTEL_SDK_DISABLED=true (#3106)")


# ------------------------------------------------- (c) on the recreate path
#
# A recreate replays the OLD container's Config.Env, so an agent created before
# this fix never gained the flag on restart / drift self-heal / rebuild pass —
# the window where the backend is updated but the base image is not yet
# rebuilt. Driven through the real lifecycle seam (the #1854 harness shape).

def _recreate_env(monkeypatch, old_env):
    import asyncio
    from types import SimpleNamespace

    from services.agent_service import lifecycle

    captured = {}

    async def _fake_provision(agent_name, **kw):
        captured.update(kw)
        return SimpleNamespace(name=f"agent-{agent_name}")

    old = SimpleNamespace(
        attrs={
            "Config": {"Env": list(old_env), "Image": "trinity-agent-base:latest",
                       "Labels": {"trinity.ssh-port": "2222"}},
            "HostConfig": {"RestartPolicy": {}},
            "Mounts": [],
        },
        status="running",
    )
    monkeypatch.setattr(lifecycle, "_provision_folders_and_run_agent_container", _fake_provision)
    monkeypatch.setattr(lifecycle, "validate_base_image", lambda image: None)
    monkeypatch.setattr(lifecycle, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(lifecycle, "get_agent_default_resources", lambda: {"cpu": "2", "memory": "4g"})

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(lifecycle, "container_stop", _noop)
    monkeypatch.setattr(lifecycle, "container_remove", _noop)

    async def _img(_i):
        return SimpleNamespace(labels={})
    monkeypatch.setattr(lifecycle, "image_get", _img)
    for name, val in (
        ("get_agent_subscription_id", None), ("get_resource_limits", None),
        ("get_guardrails_config", None), ("get_agent_github_pat", None),
        ("get_git_config", None), ("get_public_mount_path", "/home/developer/public"),
    ):
        monkeypatch.setattr(lifecycle.db, name, (lambda v: (lambda *a, **k: v))(val))

    asyncio.run(lifecycle.recreate_container_with_updated_config("scout", old, "system"))
    return captured["env_vars"]


def test_a_recreated_otel_agent_gains_the_flag(monkeypatch):
    env = _recreate_env(monkeypatch, ["AGENT_NAME=scout", "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
                                      "OTEL_METRICS_EXPORTER=otlp"])
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"   # Claude Code's metrics untouched


def test_a_recreated_agent_without_otel_is_left_alone(monkeypatch):
    env = _recreate_env(monkeypatch, ["AGENT_NAME=scout"])
    assert "OTEL_SDK_DISABLED" not in env


def test_an_explicit_operator_value_is_kept(monkeypatch):
    env = _recreate_env(monkeypatch, ["AGENT_NAME=scout", "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
                                      "OTEL_SDK_DISABLED=false"])
    assert env["OTEL_SDK_DISABLED"] == "false"
