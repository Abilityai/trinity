"""Model → context-window catalog — single source of truth (#1521).

Answers one question: "what max-token window (the denominator for a `% context
used` bar) should we assume for model X?" It replaces ~scattered ``200000``
literals and the several disagreeing per-runtime resolvers that guessed the
window independently.

FALLBACK, NOT AUTHORITY
-----------------------
The authoritative per-turn window is the value the runtime reports for that turn
(Claude Code's ``modelUsage.contextWindow``, Gemini's equivalent, etc.). Only the
runtime knows the *effective* window, which depends on auth mode, plan tier,
extended-context credits, and ``CLAUDE_CODE_DISABLE_1M_CONTEXT``. Callers MUST
prefer the runtime-reported value and consult this catalog only when it is absent.

DESIGN INVARIANT — do NOT "modernize" (#1521 decision A)
--------------------------------------------------------
The fallback for a *bare* Claude model id is the GUARANTEED-SAFE FLOOR of 200K —
NOT the model's 1M *capability ceiling*. A bare ``opus``/``sonnet`` turn can be
200K in practice (e.g. a Pro plan without extended-context credits), and on the
fallback path we have no way to know the tier. Over-reporting usage (a too-full
bar) is the safe failure; under-reporting (showing a 1M denominator, hiding an
imminent 200K compaction wall) is dangerous. The explicit ``[1m]`` suffix is the
operator's opt-in signal that a 1M window was requested → it resolves to 1M.
Sonnet 5 / Fable 5 are unconditionally 1M (no 200K variant exists), but we still
keep the uniform safe floor here and rely on the runtime value to show 1M for
them — a future edit must not promote the bare-Claude floor to 1M.

VENDORING (Invariant #5)
------------------------
The agent server is a separate image and cannot import ``src/backend``, so this
module is vendored BYTE-IDENTICALLY into
``docker/base-image/agent_server/model_context.py``. A parity test enforces
byte-identity. Keep this module pure-stdlib and free of repo-specific imports so
the copy is literally identical.

Canonical model windows (keep this comment as the bump-anchor):
    https://platform.claude.com/docs/en/about-claude/models/overview
    https://code.claude.com/docs/en/model-config  (extended-context / [1m])
Last synced: 2026-07-08 (#1521)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Guaranteed-safe fallback for an unknown / free-text model id, and the safe
# floor for every bare Claude model (see DESIGN INVARIANT above).
DEFAULT_CONTEXT_WINDOW = 200_000

# Explicit 1M window: the operator's ``[1m]`` opt-in, and native-1M non-Claude
# runtimes (Gemini).
EXTENDED_CONTEXT_WINDOW = 1_000_000

# OpenAI Codex single window.
CODEX_CONTEXT_WINDOW = 272_000

# Marker Claude Code uses to request the 1M extended-context beta. Case-folded
# before the check so ``[1M]`` matches too.
_EXTENDED_MARKER = "[1m]"

# Family-prefix rules for the FALLBACK window (first match wins — order matters,
# most specific first). These encode the guaranteed-safe floor per family, NOT
# the capability ceiling for Claude. A match SUPPRESSES the unknown-model warning
# (the id is recognized, just resolved to its floor); a miss warns.
_FAMILY_PREFIX_WINDOWS: tuple[tuple[str, int], ...] = (
    # Non-Claude runtimes — plan-independent native windows.
    ("gemini", EXTENDED_CONTEXT_WINDOW),
    ("gpt-5", CODEX_CONTEXT_WINDOW),   # OpenAI Codex (e.g. gpt-5.1-codex)
    ("codex", CODEX_CONTEXT_WINDOW),
    # Claude — safe 200K floor for every bare id (opus / sonnet / haiku). The
    # real 1M window comes from the runtime value (primary) or the [1m] suffix.
    ("claude", DEFAULT_CONTEXT_WINDOW),
    # Bare Claude Code aliases (no provider prefix): opus / sonnet / haiku / fable.
    ("opus", DEFAULT_CONTEXT_WINDOW),
    ("sonnet", DEFAULT_CONTEXT_WINDOW),
    ("haiku", DEFAULT_CONTEXT_WINDOW),
    ("fable", DEFAULT_CONTEXT_WINDOW),
)


def resolve_context_window(model: str | None) -> int:
    """Best-effort context window (the max-token denominator) for a model string.

    FALLBACK ONLY — callers must prefer the runtime-reported window when present.
    Total function: any input returns a bounded int in
    ``{DEFAULT_CONTEXT_WINDOW, CODEX_CONTEXT_WINDOW, EXTENDED_CONTEXT_WINDOW}``
    and never raises.

    Resolution order:
      1. An explicit ``[1m]`` suffix anywhere in the string (e.g. ``"opus[1m]"``,
         ``"claude-opus-4-8[1m]"``) → ``EXTENDED_CONTEXT_WINDOW`` (1M).
      2. A known family prefix → that family's safe-floor window (warning-free).
      3. Unknown / free-text / empty / ``None`` → ``DEFAULT_CONTEXT_WINDOW`` with
         a logged warning, so a newly-introduced model id is visible rather than
         silently mis-counted.
    """
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    m = model.strip().lower()
    if not m:
        return DEFAULT_CONTEXT_WINDOW
    if _EXTENDED_MARKER in m:
        return EXTENDED_CONTEXT_WINDOW
    for prefix, window in _FAMILY_PREFIX_WINDOWS:
        if m.startswith(prefix):
            return window
    logger.warning(
        "resolve_context_window: unrecognized model id %r; using default %d "
        "(bump the catalog in model_context.py if this is a real model)",
        model,
        DEFAULT_CONTEXT_WINDOW,
    )
    return DEFAULT_CONTEXT_WINDOW
