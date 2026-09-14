"""#2433 — the backend agent-call limiter knobs are forwarded everywhere the
backend runs, and the module documents the default it actually uses.

``BACKEND_AGENT_CALL_LIMIT`` / ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S`` lived only
in ``docker-compose.yml``; ``docker-compose.prod.yml`` launches standalone (no
base merge, no env_file), so on production the operator could not raise the
cap at all — the #1039 packaging-gap class. The hosted compose must carry the
same block (the #2280 wholesale env parity guard).

Also pins the module docstring's stated default to the code's (it said 30; the
code has been 3600 since the limiter shipped).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_VARS = ("BACKEND_AGENT_CALL_LIMIT", "BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S")


def _backend_env(compose: str) -> dict:
    doc = yaml.safe_load((_ROOT / compose).read_text())
    env = doc["services"]["backend"]["environment"]
    out = {}
    for item in env:
        if isinstance(item, str) and "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
    return out


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml"])
def test_limiter_vars_forwarded(compose):
    env = _backend_env(compose)
    for var in _VARS:
        assert var in env, f"{compose}: backend must forward {var}"


def test_prod_and_dev_defaults_agree():
    dev = _backend_env("docker-compose.yml")
    prod = _backend_env("docker-compose.prod.yml")
    hosted = _backend_env("docker-compose.hosted.yml")
    for var in _VARS:
        assert dev[var] == prod[var] == hosted[var], var


def test_env_example_documents_both():
    text = (_ROOT / ".env.example").read_text()
    for var in _VARS:
        assert re.search(rf"^{var}=", text, re.M), f".env.example must document {var}"


def test_docstring_default_matches_code_default():
    src = (_ROOT / "src" / "backend" / "services" / "agent_call_limiter.py").read_text()
    docstring = src.split('"""')[1]
    assert "default 3600" in docstring
    assert not re.search(r"BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S.*default 30\b", docstring)
    assert 'os.getenv("BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S", "3600")' in src
