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

_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\d+(\.\d+)*$")


def _requirements() -> list[str]:
    lines = [l.split("#", 1)[0].strip() for l in _REQS.read_text().splitlines()]
    return [l for l in lines if l]


def test_every_agent_server_dependency_is_exact_pinned():
    reqs = _requirements()
    names = {r.split("==")[0].lower() for r in reqs}
    assert {"fastapi", "starlette", "uvicorn", "httpx", "pydantic",
            "python-multipart", "pyyaml"} <= names
    for r in reqs:
        assert _PIN.match(r), f"{r!r} is not an exact pin"


def test_fastapi_stays_below_the_auto_telemetry_release():
    """(a) already makes 0.142+ safe; this keeps the pin on the line the
    agent server was validated against until someone bumps it on purpose."""
    fastapi = next(r for r in _requirements() if r.lower().startswith("fastapi=="))
    major, minor = (int(p) for p in fastapi.split("==")[1].split(".")[:2])
    assert (major, minor) < (0, 142)


def test_the_image_installs_from_the_pin_file():
    text = (_BASE / "Dockerfile").read_text()
    assert "agent-server-requirements.txt" in text
    assert re.search(r"pip install --user[^\n]*-r /opt/trinity/agent-server-requirements\.txt", text)


def _pip_install_commands(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)
    return [l.strip() for l in joined.splitlines() if "pip install" in l and not l.strip().startswith("#")]


def test_the_boot_repair_is_pinned_and_never_upgrades_unpinned():
    cmds = _pip_install_commands((_BASE / "startup.sh").read_text())
    assert cmds, "startup.sh should still repair the agent-server dependencies"
    for cmd in cmds:
        assert "--upgrade" not in cmd, f"unpinned upgrade at boot: {cmd}"
        assert "-r /opt/trinity/agent-server-requirements.txt" in cmd, cmd


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
