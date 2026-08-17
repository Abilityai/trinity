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
them — a future edit must not promote the bare-Claude floor to 1M. Reading that
runtime value correctly is ``pick_context_window``'s job (#1840): the runtime
reports a window PER MODEL the turn touched, so the entry has to be matched to
the model that answered, not taken arbitrarily.

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
    https://developers.openai.com/api/docs/pricing   (GPT-5.x windows)
Last synced: 2026-08-15 (#2207 — GPT-5.6 family windows)
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

# OpenAI Codex — legacy families (gpt-5, gpt-5.1, bare "codex"). Their real
# window IS 272K, so this is exact for them, not a floor.
#
# NOTE (#2207): 272K is ALSO OpenAI's long-context PRICE break for the newer
# families, which is why it looked like a universal ceiling. It is not — see
# CODEX_EXTENDED_CONTEXT_WINDOW. The pricing-side constant lives in
# codex_runtime.CODEX_PRICING/LONG_CONTEXT_THRESHOLD_TOKENS; do not collapse the
# two, they answer different questions and will diverge on the next release.
CODEX_CONTEXT_WINDOW = 272_000

# OpenAI GPT-5.6 family (sol / terra / luna) — verified 1,050,000 context window
# for all three (922K max input + 128K max output). Verified against the live
# model pages 2026-08-15, so this is a KNOWN value, not a capability guess: the
# DESIGN INVARIANT above forbids optimistic guessing, not documented facts.
# gpt-5.5 / gpt-5.4 deliberately stay on the 272K floor — their windows were not
# verified, and under-reporting capacity is the safe direction.
CODEX_EXTENDED_CONTEXT_WINDOW = 1_050_000

# Marker Claude Code uses to request the 1M extended-context beta. Case-folded
# before the check so ``[1M]`` matches too.
_EXTENDED_MARKER = "[1m]"

# Family-prefix rules for the FALLBACK window (first match wins — order matters,
# most specific first). These encode the guaranteed-safe floor per family, NOT
# the capability ceiling for Claude. A match SUPPRESSES the unknown-model warning
# (the id is recognized, just resolved to its floor); a miss warns.
#
# DELIBERATE ASYMMETRY with codex_runtime._resolve_pricing (#2207) — do not
# "unify" them. That function had to make its prefix match boundary-aware,
# because there falling through to an older, broader entry picks the CHEAPEST
# rate and silently under-bills. Here the same fall-through picks the SMALLEST
# window, which over-reports "% context used" — the safe direction this module's
# DESIGN INVARIANT asks for. Identical mechanism, opposite risk: a future
# ``gpt-5.7-x`` landing on ``gpt-5`` → 272K is correct behaviour here and a bug
# there.
_FAMILY_PREFIX_WINDOWS: tuple[tuple[str, int], ...] = (
    # Non-Claude runtimes — plan-independent native windows.
    ("gemini", EXTENDED_CONTEXT_WINDOW),
    # gpt-5.6 BEFORE gpt-5 — first match wins, so the specific family must lead.
    ("gpt-5.6", CODEX_EXTENDED_CONTEXT_WINDOW),  # sol / terra / luna (#2207)
    ("gpt-5", CODEX_CONTEXT_WINDOW),   # OpenAI Codex legacy (e.g. gpt-5.1-codex)
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


def pick_context_window(model_usage: object, model_name: str | None) -> int | None:
    """Select the TURN's context window from a Claude Code ``modelUsage`` map.

    ``modelUsage`` maps EVERY model the turn touched, not just the one that
    answered: Claude Code bills side work (tool-permission classification, title
    generation) to a cheap Haiku, so a Sonnet-5 turn routinely reports

        {"claude-haiku-4-5-20251001": {"contextWindow":   200000, ...},
         "claude-sonnet-5":           {"contextWindow": 1000000, ...}}

    Taking an arbitrary entry — dict order, i.e. whichever model was recorded
    first, which is the side model whenever one ran — reports the wrong window
    for the turn and makes the same agent flip between 1M and 200K run to run
    (#1840). Match the entry to ``model_name`` (the id from the latest assistant
    message, which is the model that actually answered).

    Returns ``None`` when the turn's model can't be identified in the map, so the
    caller keeps its already-seeded fallback rather than guessing. Guessing here
    would mean picking the largest window, which is the DANGEROUS direction — a
    1M side model alongside a 200K main model would hide an imminent compaction
    wall (see the DESIGN INVARIANT above).
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return None

    def _window(entry: object) -> int | None:
        if isinstance(entry, dict):
            value = entry.get("contextWindow")
            if isinstance(value, int) and value > 0:
                return value
        return None

    if isinstance(model_name, str) and model_name:
        # Exact key, then the entry's own canonicalModel (the key is not always
        # the canonical id — an alias like "sonnet" can key a canonical entry).
        window = _window(model_usage.get(model_name))
        if window is not None:
            return window
        for entry in model_usage.values():
            if isinstance(entry, dict) and entry.get("canonicalModel") == model_name:
                window = _window(entry)
                if window is not None:
                    return window

    # Unambiguous single-model turn: no side model ran, so the only entry IS the
    # turn's model — the common case, and the one that was already correct.
    windows = [w for w in (_window(e) for e in model_usage.values()) if w is not None]
    if len(windows) == 1:
        return windows[0]
    return None


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
