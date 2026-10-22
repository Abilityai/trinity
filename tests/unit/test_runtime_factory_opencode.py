"""Runtime factory tests for the OpenCode runtime (#2981).

The sibling of ``test_runtime_factory_codex.py``. ``get_runtime()`` must:
  * return an OpenCodeRuntime when AGENT_RUNTIME=opencode, in any casing
    (the #1187 casing trap — ``OpenCode`` must not fall through to Claude),
  * still RAISE on a near-miss value instead of silently selecting Claude,
  * hand back the process-wide singleton, like the other three runtimes.
"""

from __future__ import annotations

import pytest

from agent_server.services.runtime_adapter import (  # noqa: E402
    KNOWN_RUNTIMES,
    get_runtime,
)
from agent_server.services.opencode_runtime import (  # noqa: E402
    OpenCodeRuntime,
    get_opencode_runtime,
)


@pytest.mark.parametrize("value", ["opencode", "OpenCode", "  opencode  "])
def test_runtime_factory_opencode(monkeypatch, value):
    monkeypatch.setenv("AGENT_RUNTIME", value)
    assert isinstance(get_runtime(), OpenCodeRuntime)


def test_runtime_factory_near_miss_raises(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "opencodex")
    with pytest.raises(ValueError) as exc_info:
        get_runtime()
    assert "opencodex" in str(exc_info.value)


def test_opencode_is_a_known_runtime():
    assert "opencode" in KNOWN_RUNTIMES


def test_factory_returns_the_singleton(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")
    assert get_runtime() is get_opencode_runtime() is get_opencode_runtime()
