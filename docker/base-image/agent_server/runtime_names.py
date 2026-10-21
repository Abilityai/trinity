"""The names that identify an agent runtime — a zero-dependency leaf.

Split out of ``services/runtime_adapter`` (#2981) for one
reason:
``runtime_adapter`` imports ``..models`` and ``..model_context``, so it can
only be imported as part of the ``agent_server`` package. ``plugins_reinstall``
runs as a standalone ``python3 -m`` boot hook and is loaded by path in tests,
where a relative import reaches beyond the top-level package and fails. A
predicate that silently falls back to "yes, Claude" whenever its import fails
is not a gate, so the predicate lives here, importable both ways.

This module imports nothing but ``os`` — keep it that way.
"""
from __future__ import annotations

import os
from typing import Optional

# Accepted AGENT_RUNTIME values, lowercased. An unknown value must fail loudly
# rather than silently selecting Claude (#1187 Phase D) — see
# ``runtime_adapter.get_runtime``.
CLAUDE_RUNTIMES = frozenset({"claude-code", "claude"})
GEMINI_RUNTIMES = frozenset({"gemini-cli", "gemini"})
CODEX_RUNTIMES = frozenset({"codex"})
OPENCODE_RUNTIMES = frozenset({"opencode"})
KNOWN_RUNTIMES = (
    CLAUDE_RUNTIMES | GEMINI_RUNTIMES | CODEX_RUNTIMES | OPENCODE_RUNTIMES
)


def normalize_runtime(runtime: Optional[str] = None) -> str:
    """The comparable form of a runtime name: stripped, lowercased, defaulted.

    ``None``/empty means "unset", which has always meant claude-code. Reading
    the env here (rather than at import) is deliberate — a container whose env
    is refreshed between spawns must be honoured.

    Lowercasing is the #1187 casing trap: every dispatch on a runtime name goes
    through this function so that ``AGENT_RUNTIME=Codex`` cannot take a
    different branch from ``codex``.
    """
    value = runtime if runtime is not None else os.getenv("AGENT_RUNTIME")
    return (value or "claude-code").strip().lower()


def is_claude_runtime(runtime: Optional[str] = None) -> bool:
    """True for the Claude Code runtime, INCLUDING the default (unset/empty).

    The agent-side twin of
    ``src/backend/services/agent_service/helpers.py::is_claude_runtime``.
    """
    return normalize_runtime(runtime) in CLAUDE_RUNTIMES
