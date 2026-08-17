"""The single source of truth for the selectable Claude-model catalog (#2086).

Before #2086 the same catalog was hand-maintained in three independent places
with no shared registry, so the copies drifted silently (the #1660 / #1662 class:
a shipping model missing from one list). The worst drift was asymmetric —
``PUBLIC_CHANNEL_MODELS`` is a *validation* set, so a model absent there is
rejected **422** on ``PUT /api/agents/{name}/public-channel-model`` (an owner
cannot select it even via the API), while the two frontend lists degraded quietly
(free-text still saved).

This module is now the ONE place the catalog is defined. Every consumer derives
its view by filtering:

  * ``PUBLIC_CHANNEL_MODELS`` (below) — the #894 server-validated allow-list for
    the per-agent public-channel override. ``settings_service`` re-exports it.
  * ``src/frontend/src/constants/modelCatalog.js`` — a GENERATED, checked-in JS
    mirror (do NOT edit it by hand; run ``python scripts/gen_model_catalog.py``).
    The ModelSelector picker derives ``PRESET_MODELS`` from it; the admin
    default-model dropdown filters it on ``adminDefaultSelectable``.

A ``tests/unit/`` guard (``test_2086_model_catalog_parity.py``) byte-matches the
committed JS against ``render_js()`` **and** structurally validates the parsed
records against this source, so a consumer edited without re-running codegen —
or a wrong-but-fresh ``render_js()`` — fails CI on every PR.

WHAT THE GUARD DOES NOT CATCH — the human control (#2086)
--------------------------------------------------------
The guard catches consumer-vs-source divergence. It does NOT catch
source-vs-reality staleness: when Anthropic ships a new model and nobody edits
this file, every list stays consistent and green while the new model is
unselectable everywhere (the exact bug class that created #2086). The control for
that is human, and it is a single-file edit:

    >>> When Anthropic ships a selectable Claude model, add one ModelEntry to
    >>> MODEL_CATALOG below and re-run ``python scripts/gen_model_catalog.py``.

That is the whole maintenance surface — one file, one script.

DELIBERATELY OUT OF SCOPE
-------------------------
* ``settings_service.PLATFORM_DEFAULT_MODEL_VALUE`` (#831) — the *actual* fleet
  default is a cost/latency POLICY choice, not catalog data. The ``recommended``
  flag below is pinned to it (the guard asserts they agree), but this module
  never decides the default.
* ``services/model_context.py`` (#1521) — the context-window map is prefix-based
  and vendored byte-identically into the agent image under Invariant #5. It is a
  separate registry; folding it in here would break the vendoring contract.

Stdlib-only leaf (Eng #5/#7): imports nothing from ``settings_service`` or
``database`` (both trigger ``init_database()`` at import), so the codegen script
and the parity test load it with zero DB dependency. Dependency direction is
one-way — ``settings_service`` imports this, never the reverse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelEntry:
    """One selectable model and its policy dimensions.

    * ``public_channel`` — selectable as the #894 per-agent public-channel
      override (the ``PUBLIC_CHANNEL_MODELS`` validation set).
    * ``admin_default_selectable`` — offered in the admin fleet-default dropdown.
      Named to avoid colliding with ``PLATFORM_DEFAULT_MODEL_VALUE`` (which means
      the *actual* default). Haiku is deliberately public but NOT default-selectable
      (#1080: an admin must not be able to default the whole fleet to the cheap tier).
    * ``recommended`` — drives the admin dropdown's "(recommended)" marker. Exactly
      one entry carries it, and it is pinned to ``PLATFORM_DEFAULT_MODEL_VALUE``.
    """

    id: str
    label: str
    note: str
    public_channel: bool
    admin_default_selectable: bool
    recommended: bool


# The ordered catalog. Order is preserved into the picker and the admin dropdown.
# Model ids verified against the ``claude-api`` skill (do not state ids from
# memory): ``claude-opus-5`` is the current Opus tier; the ``-5`` family and
# ``claude-sonnet-4-6`` are current; ``opus-4-8/4-7/4-6`` are the prior Opus
# generation (legacy). The date-suffixed ids are kept verbatim.
MODEL_CATALOG: tuple[ModelEntry, ...] = (
    # Current generation.
    ModelEntry(
        "claude-opus-5",
        "Claude Opus 5",
        "Most capable Opus (latest)",
        True,
        True,
        False,
    ),
    ModelEntry(
        "claude-fable-5",
        "Claude Fable 5",
        "Most capable \u2014 longest tasks (latest)",
        True,
        True,
        False,
    ),
    ModelEntry(
        "claude-sonnet-5",
        "Claude Sonnet 5",
        "Fast + smart, 1M context (latest)",
        True,
        True,
        False,
    ),
    # Prior Opus generation — still selectable, relabelled Legacy (#2086).
    ModelEntry(
        "claude-opus-4-8", "Claude Opus 4.8", "Legacy (prior Opus)", True, True, False
    ),
    ModelEntry("claude-opus-4-7", "Claude Opus 4.7", "Legacy", True, True, False),
    ModelEntry("claude-opus-4-6", "Claude Opus 4.6", "Legacy", True, True, False),
    # The recommended fleet default (PLATFORM_DEFAULT_MODEL_VALUE, #831).
    ModelEntry(
        "claude-sonnet-4-6", "Claude Sonnet 4.6", "Fast + smart", True, True, True
    ),
    # Public-channel-selectable but NOT an admin default (#1080).
    ModelEntry(
        "claude-haiku-4-5-20251001",
        "Claude Haiku 4.5",
        "Fastest, cheapest",
        True,
        False,
        False,
    ),
    # Legacy, picker-only (neither public-channel nor admin-default).
    ModelEntry(
        "claude-opus-4-5-20251101", "Claude Opus 4.5", "Legacy", False, False, False
    ),
    ModelEntry(
        "claude-sonnet-4-5-20250929", "Claude Sonnet 4.5", "Legacy", False, False, False
    ),
)


# Import-time invariants — asserted only where genuinely load-bearing (Strategy
# #6): flags are otherwise independent (a "default-able but not public-channel"
# model is a coherent future entry a subset lattice would wrongly block). What is
# NOT independent: there must be exactly one recommendation, and you cannot
# recommend a model the admin cannot pick.
_recommended = [m for m in MODEL_CATALOG if m.recommended]
assert (
    len(_recommended) == 1
), f"exactly one MODEL_CATALOG entry must be recommended, found {len(_recommended)}"
assert _recommended[0].admin_default_selectable, (
    "the recommended model must be admin_default_selectable "
    "(you cannot recommend a model the admin cannot pick)"
)


# The #894 server-validated allow-list. Consumers keep importing it from
# ``settings_service`` (which re-exports); this is the definition.
PUBLIC_CHANNEL_MODELS = frozenset(m.id for m in MODEL_CATALOG if m.public_channel)


# snake_case source field -> camelCase JS key. Applied when building the emitted
# records so the JS is a deliberate mirror, not a trivial ``asdict()`` dump.
_JS_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("id", "id"),
    ("label", "label"),
    ("note", "note"),
    ("public_channel", "publicChannel"),
    ("admin_default_selectable", "adminDefaultSelectable"),
    ("recommended", "recommended"),
)

_GENERATED_HEADER = (
    "/* eslint-disable */\n"
    "// prettier-ignore\n"
    "// GENERATED by scripts/gen_model_catalog.py — DO NOT EDIT.\n"
    "// Edit src/backend/services/model_catalog.py and re-run the script.\n"
    "//\n"
    "// The `[1m]` extended-context suffix (e.g. 'claude-sonnet-4-6[1m]') and any\n"
    "// other free-text model id are accepted by ModelSelector.vue via its onInput\n"
    "// passthrough — a preset here is only a picker suggestion, never a hard gate.\n"
)


def render_js() -> str:
    """Emit the frontend ``modelCatalog.js`` as a byte-deterministic string.

    Determinism contract (#2086, mandatory — the parity test byte-matches this):
      * The array is serialized via ``json.dumps(..., ensure_ascii=False,
        indent=2)`` — deterministic quoting/escaping, and ``ensure_ascii=False``
        keeps the raw UTF-8 em-dash in labels/notes (matching the ``voices.js``
        idiom). JSON is a subset of JS object-literal syntax, so
        ``export const MODEL_CATALOG = <json>`` is valid JS.
      * U+2028 / U+2029 are escaped to their ``\\u`` forms — valid JSON, but
        illegal raw in pre-ES2019 JS string literals (defensive; no current
        label contains them).
      * Exactly one trailing newline; LF line endings (paired with a
        ``.gitattributes`` ``eol=lf`` entry so an autocrlf contributor's regen
        still byte-matches CI).

    ``render_js`` lives here (not in ``scripts/``) so the CLI script and the
    parity test share one importable renderer with no ``sys.path`` hack.
    """
    records = [
        {js_key: getattr(entry, field) for field, js_key in _JS_KEY_MAP}
        for entry in MODEL_CATALOG
    ]
    body = json.dumps(records, ensure_ascii=False, indent=2)
    # U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR are legal in JSON but
    # break pre-ES2019 JS string literals if left raw.
    body = body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"{_GENERATED_HEADER}\nexport const MODEL_CATALOG = {body};\n"
