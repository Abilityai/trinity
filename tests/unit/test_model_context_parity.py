"""Parity guard for the model → context-window catalog (#1521, Invariant #5).

The agent server is a separate image and cannot import ``src/backend``, so the
catalog is vendored into the base image. Both copies MUST stay byte-identical —
otherwise the backend and the agent-server runtimes could assume different
context-window denominators for the same model.

Edit the canonical file (``src/backend/services/model_context.py``) and copy it
over the agent-server vendored file to keep this green.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CANON = _ROOT / "src/backend/services/model_context.py"
_VENDORED = _ROOT / "docker/base-image/agent_server/model_context.py"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"mc_{path.parent.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_catalog_copies_are_byte_identical():
    assert _CANON.exists() and _VENDORED.exists()
    assert _CANON.read_bytes() == _VENDORED.read_bytes(), (
        "model_context.py drifted between backend and agent-server — "
        "re-copy the canonical file over the vendored one:\n"
        "  cp src/backend/services/model_context.py "
        "docker/base-image/agent_server/model_context.py"
    )


def test_canonical_catalog_behaves():
    mc = _load(_CANON)
    r = mc.resolve_context_window
    # bare Claude → safe 200K floor (decision A); [1m] → 1M; non-Claude natives.
    assert r("claude-opus-4-8") == mc.DEFAULT_CONTEXT_WINDOW == 200_000
    assert r("opus[1m]") == r("claude-opus-4-8[1m]") == 1_000_000
    assert r("claude-haiku-4-5-20251001") == 200_000
    assert r("gemini-3-flash") == 1_000_000
    assert r("gpt-5.1-codex") == mc.CODEX_CONTEXT_WINDOW == 272_000
    # unknown / empty / None never raise, always a safe floor.
    assert r("who-knows") == r("") == r(None) == 200_000
