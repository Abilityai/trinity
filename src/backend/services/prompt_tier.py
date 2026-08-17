"""Model → platform-prompt tier — single source of truth (ent#243).

Answers one question: "how much explicit instruction does model X want in the
platform system prompt?" Anthropic's Claude 5-generation context-engineering
guidance (rules → judgment, examples → interface design) removed 80%+ of Claude
Code's own system prompt with no measured loss — but that guidance is scoped to
frontier *coding-post-trained* models and is ACTIVELY HARMFUL below that line.
Sonnet 4.5 and Haiku 4.5 need the structure it deletes.

THE AXIS IS THE MODEL, NOT THE RUNTIME
--------------------------------------
Deliberately NOT keyed on ``AGENT_RUNTIME``. MODEL-001 lets a schedule, loop, or
chat turn pin any model string — including free text — so a ``claude-code``
agent routinely runs Haiku 4.5. Gating on the runtime label would silently
degrade the majority of the selectable surface (most ``model_catalog`` entries
sit in the tier where the guidance does not apply). ``runtime`` already has an
orthogonal job over in ``platform_prompt_service``: it rewrites MCP tool naming
for Codex (#1187 F-MCP). Keep the two axes apart — a tier is about how much prose
a model wants, tool naming is about what the harness can parse.

Three vendors reached this conclusion independently (OpenAI on GPT-5-Codex
over-prompting, Google on Gemini 3 over-analyzing verbose prompt engineering), so
the fault line is model class — not vendor, and not harness.

DESIGN INVARIANT — unknown fails toward VERBOSE (ent#243)
---------------------------------------------------------
An unrecognized, empty, or ``None`` model resolves to ``VERBOSE``: exactly the
prompt shipped before this module existed. The asymmetry is the whole point.
Over-instructing a Claude 5 model costs tokens; under-instructing a 4.5 model
silently degrades it, with no error and no signal. Free-text model passthrough
makes an unknown id routine rather than exceptional, so this default is load
bearing rather than defensive. It mirrors ``model_context.py``'s "fail toward the
conservative value" rule, and it is the property that bounds this feature's blast
radius to *status quo*.

Do NOT "modernize" this by defaulting an unknown id to MINIMAL because "most new
models are frontier". The failure is silent in that direction, which is precisely
why it must not be the default.

SHIPS AS A PROVABLE NO-OP (ent#243 PR 1)
----------------------------------------
``_MINIMAL_PREFIXES`` is EMPTY on purpose. Every model resolves ``VERBOSE``, so
the rendered prompt is byte-identical to the previous release
(``tests/unit/test_ent243_prompt_tier.py`` proves it over the live preset list).
Enabling a family is a separate, evidence-gated change — a fleet-wide prompt
change does not belong in the same PR as the mechanism that makes it possible.

NOT VENDORED (contrast: Invariant #5)
-------------------------------------
Unlike ``model_context.py``, this module is NOT copied into the agent server. The
backend composes the system prompt and ships it in the turn payload; the agent
server never resolves a tier. If that ever changes, vendor it byte-identically
and add a parity test — do not import across the image boundary.
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class PromptTier(str, Enum):
    """How much explicit instruction the target model wants.

    ``str`` mixin so a tier is log- and JSON-friendly without a ``.value`` dance.
    """

    #: Frontier coding-post-trained models: judgment over rules, interface over
    #: examples. Tool-usage sections are dropped in favour of tool descriptions.
    MINIMAL = "minimal"

    #: Everything else, and the SAFE DEFAULT for anything unrecognized. This is
    #: the prompt Trinity shipped before ent#243.
    VERBOSE = "verbose"


#: Model-id prefixes that want the MINIMAL prompt (first match wins, most
#: specific first — the ``model_context.py`` shape).
#:
#: INTENTIONALLY EMPTY (ent#243 PR 1). Populating this tuple is the behaviour
#: change; it lands separately behind per-(runtime, model-tier) evidence, since a
#: spot-check cannot catch a regression that only manifests on Haiku-pinned
#: schedules. Candidates when that evidence exists: ``claude-fable-5``,
#: ``claude-sonnet-5``, ``gpt-5.1-codex``, ``gemini-3``.
_MINIMAL_PREFIXES: tuple[str, ...] = ()


def resolve_prompt_tier(model: str | None) -> PromptTier:
    """Best-effort prompt tier for a model string.

    Total function: any input returns a ``PromptTier`` and never raises.

    Resolution order:
      1. Empty / ``None`` → ``VERBOSE`` (the caller does not know the model; on
         the chat path the container picks one *after* composition, so this is a
         real and expected case, not an error — see requirements §41.2 FR-7).
      2. A known MINIMAL family prefix → ``MINIMAL``.
      3. Anything else → ``VERBOSE``.

    Deliberately silent. ``model_context.py`` warns on an unrecognized id because
    there the fallback is a *guess* at a numeric window; here the fallback is the
    exact prompt the platform already shipped, so an unknown id is a non-event.
    Warning would fire on every turn of every agent while ``_MINIMAL_PREFIXES``
    is empty.
    """
    if not model:
        return PromptTier.VERBOSE
    m = model.strip().lower()
    if not m:
        return PromptTier.VERBOSE
    for prefix in _MINIMAL_PREFIXES:
        if m.startswith(prefix):
            return PromptTier.MINIMAL
    return PromptTier.VERBOSE
