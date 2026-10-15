"""Proxy-trust wiring around the client address resolver
(abilityai/trinity-enterprise#873).

* No shipped launcher trusts forwarded headers from every peer at the uvicorn
  layer — `--forwarded-allow-ips=*` / `FORWARDED_ALLOW_IPS=*` would hand the
  resolver a caller-chosen peer (#181). The image CMD keeps `--proxy-headers`
  (uvicorn's default trust is loopback only).
* The resolver's three knobs reach the backend container in every compose file.
  Compose launches the backend without `env_file`, so a variable the
  `environment:` block does not forward is inert (the #1039 class).
* A wildcard uvicorn trust set by an operator is reported at startup.

The first two are pins on files whose only consumer is the deploy itself
(compose, Dockerfile); the third executes the startup check.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from utils import client_ip as cip

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml")
KNOBS = ("TRUSTED_PROXIES", "TRUSTED_PROXY_HOSTS", "TRUSTED_PROXY_HOPS")
WILDCARD = re.compile(r"forwarded[-_]allow[-_]ips\s*[=:]?\s*[\"']?\s*(\*|0\.0\.0\.0/0)", re.I)


def _backend_block(text: str) -> str:
    start = text.index("\n  backend:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1:])
    return text[start: start + 1 + nxt.start()] if nxt else text[start:]


@pytest.mark.parametrize("name", COMPOSE_FILES + ("docker/backend/Dockerfile", ".env.example"))
def test_no_wildcard_uvicorn_proxy_trust(name):
    assert not WILDCARD.search((_ROOT / name).read_text()), name


def test_image_cmd_keeps_proxy_headers():
    cmd = [ln for ln in (_ROOT / "docker/backend/Dockerfile").read_text().splitlines()
           if ln.startswith("CMD ")]
    assert len(cmd) == 1 and '"--proxy-headers"' in cmd[0]


@pytest.mark.parametrize("name", COMPOSE_FILES)
@pytest.mark.parametrize("knob", KNOBS)
def test_knob_forwarded_to_backend(name, knob):
    block = _backend_block((_ROOT / name).read_text())
    assert f"- {knob}=${{{knob}" in block, f"{name}: backend does not forward {knob}"


@pytest.mark.parametrize("knob", KNOBS)
def test_knob_documented(knob):
    assert re.search(rf"^#?\s*{knob}=", (_ROOT / ".env.example").read_text(), re.M)


class TestStartupGuard:
    @pytest.mark.parametrize("value", ["*", " * ", "0.0.0.0/0", "10.0.0.1,*"])
    def test_wildcard_is_reported(self, monkeypatch, caplog, value):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        with caplog.at_level(logging.ERROR, logger=cip.logger.name):
            assert cip.check_uvicorn_proxy_trust() is False
        assert "FORWARDED_ALLOW_IPS" in caplog.text

    @pytest.mark.parametrize("value", [None, "", "127.0.0.1", "10.0.0.5,192.168.1.0/24"])
    def test_scoped_or_unset_is_quiet(self, monkeypatch, caplog, value):
        if value is None:
            monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
        else:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        with caplog.at_level(logging.ERROR, logger=cip.logger.name):
            assert cip.check_uvicorn_proxy_trust() is True
        assert caplog.text == ""

    def test_main_runs_the_guard_at_import(self):
        # main.py is too heavy to import in the unit island (import side
        # effects); pin the module-level call by AST.
        import ast

        tree = ast.parse((_ROOT / "src/backend/main.py").read_text())
        calls = [
            n for n in tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "check_uvicorn_proxy_trust"
        ]
        assert len(calls) == 1
