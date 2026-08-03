"""One hardened YAML loader for every author-controlled document (ent#314).

`yaml.safe_load` blocks arbitrary object construction. It does not block the two
things that actually bite here, and both are reachable today by any
`creator`-role user pointing agent creation at a public GitHub repo
(trinity-enterprise#123 made that tokenless):

1. **Alias/anchor amplification at SERIALIZATION time.** PyYAML resolves aliases
   into shared references, so the parse is free and the in-memory graph is
   small — measured 0.0011 s for a level-6 bomb. The blow-up happens when
   something *walks* the graph: `json.dumps` for `/api/templates`, a deep copy,
   a `str()` inside a warning. Measured on this tree:

   | anchor levels | source | `json.dumps` output | amplification |
   |--------------:|-------:|--------------------:|--------------:|
   | 4             |  298 B |             1.10 MB |        3,700x |
   | 5             |  357 B |            11.02 MB |       30,882x |
   | 6             |  416 B |           110.25 MB |      265,017x |

   Each added level is another 10x for ~60 more source bytes, so an input-size
   cap cannot close this: the input genuinely is small.

2. **Duplicate-key silence.** `safe_load` keeps the LAST duplicate and discards
   the rest with no signal. For a `credentials:` block that is worse than a
   footgun — a template can show one set of required credentials to a human
   reading the top of the file and declare a different set to Trinity.

Prior art this consolidates rather than joins: `system_service`'s
`_HardenedManifestLoader` (#1884, size + expansion budget + duplicate keys) and
two independent `_NoAliasSafeLoader` copies (`credential_requirements_service`,
`skill_packaging`) that reject aliases outright. `src/mcp-server/src/tools/
pipelines.ts` (#919) is the TypeScript sibling and set the house standard.

TWO ALIAS POLICIES, DELIBERATELY
--------------------------------
`AliasPolicy.BUDGET` bounds the expansion cost; `AliasPolicy.REJECT` refuses any
alias at all. Both exist because the callers genuinely differ, and unifying them
would mean loosening somebody's gate:

- A **manifest** or a **template catalog entry** may legitimately anchor a
  repeated block, and rejecting that outright would break real documents for no
  security gain — the measured budget already refuses level 4 and up while
  admitting the small, honest anchor (level 3 serializes to ~0.1 MB).
- A **live agent-writable** `template.yaml` read for credential advisories has
  its own walk-based amplifier and already refuses aliases; relaxing it to a
  budget would be a security regression shipped as a refactor.

So the policy is a required argument at every call site. There is no default:
picking one silently is how a caller ends up with the wrong gate.
"""

from __future__ import annotations

import enum
from typing import Any, Optional

import yaml

# Bounds the INPUT. Never sufficient alone (see the table above), but it stops
# the boring case where somebody posts a 40 MB document.
DEFAULT_MAX_BYTES = 256 * 1024

# Bounds the EXPANSION COST, not the alias count.
#
# A naive `maxAliasCount` is the wrong shape: the classic billion-laughs uses
# ~45 aliases across a few levels and sails under any count-based budget,
# because the blow-up is MULTIPLICATIVE per level and only linear in aliases.
# This budget is the number of logical nodes the document would have if every
# alias were written out — exactly the quantity that explodes.
DEFAULT_MAX_EXPANDED_NODES = 100_000


class AliasPolicy(enum.Enum):
    """How a document is allowed to use YAML anchors/aliases."""

    #: Any alias is refused at compose time. For documents where no legitimate
    #: author needs one and the consumer walks every field.
    REJECT = "reject"
    #: Aliases allowed while the resolved node count stays under the budget.
    BUDGET = "budget"


class HardenedYamlError(ValueError):
    """A document the platform refuses to parse.

    Carries a machine-readable `code` so a router can answer a NAMED 400 rather
    than letting a bomb surface as a request timeout or an unnamed 500 — the
    difference between "your file is malformed" and "Trinity is broken".
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _make_loader(
    *,
    kind: str,
    alias_policy: AliasPolicy,
    max_expanded_nodes: int,
    error_cls: type,
):
    """Build a one-shot SafeLoader subclass carrying this call's policy.

    A fresh class per parse keeps the budget counters per-document; PyYAML
    instantiates the loader itself, so there is nowhere else to put them.
    """

    class _HardenedLoader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self._expanded_nodes = 0
            self._anchor_cost: dict = {}

        def fetch_alias(self):
            # Second, earlier gate for REJECT: refuse at the SCANNER, before an
            # alias token is ever produced. Carried over from
            # `skill_packaging._NoAliasSafeLoader`, which had both hooks — the
            # consolidation must not drop the stricter of the two copies.
            if alias_policy is AliasPolicy.REJECT:
                raise error_cls(
                    f"{kind}_alias_not_permitted",
                    f"YAML aliases are not permitted in {kind}.",
                )
            return super().fetch_alias()

        def compose_node(self, parent, index):
            if self.check_event(yaml.events.AliasEvent):
                if alias_policy is AliasPolicy.REJECT:
                    raise error_cls(
                        f"{kind}_alias_not_permitted",
                        f"YAML aliases are not permitted in {kind}. Write the "
                        f"repeated block out instead of anchoring it.",
                    )
                # An alias costs the FULL logical size of what it points at, so
                # a pyramid of anchors costs what it would cost written out.
                anchor = self.peek_event().anchor
                self._expanded_nodes += self._anchor_cost.get(anchor, 1)
                if self._expanded_nodes > max_expanded_nodes:
                    raise error_cls(
                        f"{kind}_alias_budget_exceeded",
                        f"{kind} expands to more than {max_expanded_nodes:,} nodes "
                        "once its YAML aliases are resolved. This is the shape of "
                        "an expansion bomb; if the document is genuine, write the "
                        "repeated block out instead of anchoring it.",
                    )
                return super().compose_node(parent, index)

            before = self._expanded_nodes
            self._expanded_nodes += 1
            event = self.peek_event()
            anchor = getattr(event, "anchor", None)
            node = super().compose_node(parent, index)
            if anchor:
                # Cost of this subtree = everything composed while inside it.
                self._anchor_cost[anchor] = self._expanded_nodes - before
            return node

        def construct_mapping(self, node, deep=False):
            # Applied at EVERY mapping depth, not just the top level: a
            # duplicate inside one agent's config, or inside `credentials:`,
            # is exactly the case that misleads a human reader.
            seen = set()
            for key_node, _ in node.value:
                try:
                    key = self.construct_object(key_node, deep=deep)
                except yaml.constructor.ConstructorError:
                    continue
                try:
                    if key in seen:
                        raise error_cls(
                            f"{kind}_duplicate_key",
                            f"Duplicate key {key!r} at line "
                            f"{key_node.start_mark.line + 1}. YAML would silently "
                            "keep only the last one, so the file would not mean "
                            "what it appears to say.",
                        )
                    seen.add(key)
                except TypeError:
                    # Unhashable key — malformed, but not this guard's business.
                    continue
            return super().construct_mapping(node, deep=deep)

    return _HardenedLoader


def load_hardened_yaml(
    text: str,
    *,
    kind: str,
    alias_policy: AliasPolicy,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_expanded_nodes: int = DEFAULT_MAX_EXPANDED_NODES,
    error_cls: Optional[type] = None,
) -> Any:
    """Parse author-controlled YAML with size, alias and duplicate-key guards.

    Args:
        text: the raw document.
        kind: prefix for error codes and messages (``"manifest"`` →
            ``manifest_duplicate_key``). Callers that already publish codes MUST
            pass the prefix they published.
        alias_policy: see `AliasPolicy` — required, never defaulted.
        max_bytes / max_expanded_nodes: the two budgets.
        error_cls: raise this instead of `HardenedYamlError` (a subclass, so an
            existing consumer's `except` keeps working during migration).

    Raises:
        `error_cls` (default `HardenedYamlError`) with a `code` of
        ``{kind}_too_large`` / ``_alias_not_permitted`` / ``_alias_budget_exceeded``
        / ``_duplicate_key`` / ``_yaml_invalid``. Rejects rather than truncates,
        so a hostile document fails loudly instead of being silently reinterpreted.
    """
    err = error_cls or HardenedYamlError

    if len(text.encode("utf-8")) > max_bytes:
        raise err(
            f"{kind}_too_large",
            f"{kind} exceeds the {max_bytes}-byte parse cap.",
        )

    loader = _make_loader(
        kind=kind,
        alias_policy=alias_policy,
        max_expanded_nodes=max_expanded_nodes,
        error_cls=err,
    )
    try:
        return yaml.load(text, Loader=loader)
    except err:
        raise
    except yaml.YAMLError as e:
        raise err(f"{kind}_yaml_invalid", f"YAML parse error: {e}")


# --- Per-document policies -------------------------------------------------
#
# Defined HERE, not in the consuming service, for two reasons: the policy is one
# decision that several callers must share, and `utils.*` is import-safe from
# anywhere — `services.*` modules are stubbed wholesale by several test
# harnesses, so a policy helper living there silently becomes a MagicMock at the
# call site (which then fails the caller's own `isinstance(..., dict)` check and
# looks like a malformed template).

#: `template.yaml` — BUDGET, because a real template may anchor a repeated
#: block and the measured budget already refuses the bomb (level 4+).
TEMPLATE_YAML_MAX_BYTES = 256 * 1024
TEMPLATE_YAML_MAX_EXPANDED_NODES = 100_000


def load_template_yaml(content: str):
    """Parse an author-controlled `template.yaml` with the ent#314 guards."""
    return load_hardened_yaml(
        content,
        kind="template",
        alias_policy=AliasPolicy.BUDGET,
        max_bytes=TEMPLATE_YAML_MAX_BYTES,
        max_expanded_nodes=TEMPLATE_YAML_MAX_EXPANDED_NODES,
    )
