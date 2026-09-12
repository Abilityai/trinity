"""#2726 — the ops cost/token dashboard must not mangle a point-release model id.

`_format_model_name` strips an **8-digit** date suffix and then prefix-matches a
small mapping, falling back to title-case-with-hyphens-as-spaces. Every catalog
addition before this one degraded *cleanly* through that fallback
(``claude-opus-5`` -> "Claude Opus 5"). ``claude-fable-5-1`` is the first that
degrades **wrongly**: ``-1`` is not an 8-digit suffix and no mapping prefix hits,
so the fallback rendered the literal **"Claude Fable 5 1"** on the per-model cost
and token breakdowns (`routers/ops.py:1105`).

The surface was already declared a known-deferred follow-up by #2086 FR-7, and
the id was reachable as free text before this change — but making the model
selectable in the picker turned a theoretical mangling into a likely one, so the
exact mapping ships with it.

Two things this file pins that a single equality would not:

* the new entry must not swallow its neighbour. The mapping is scanned with
  ``startswith`` and first-match-wins, so an entry for ``claude-fable-5`` would
  match ``claude-fable-5-1`` first and silently relabel the newer model. Fable 5
  is therefore deliberately unmapped (its fallback is already right), and that is
  asserted, not assumed.
* the rule, not just the instance. ``test_no_catalog_id_renders_a_split_version``
  is derived from ``MODEL_CATALOG``, so the *next* point-release id is covered
  without editing this file — which is the half #2726 would have needed to catch
  itself.

**The JS twin is deliberately NOT changed.** ``stores/observability.js``'s
``formatModelName`` returns a bare family name ("Claude Opus") with no version
and keeps no ``model_id`` alongside it in ``costBreakdown()``. A one-line
``includes('fable')`` branch there would render ``claude-fable-5`` and
``claude-fable-5-1`` as the same "Claude Fable" label on a per-model breakdown —
two indistinguishable rows where today both show their distinct raw ids. Making
the two prettifiers actually agree needs version-aware structure on the JS side,
which is out of scope for this fix (#2086 FR-7 still owns it).
"""
import re

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def fmt():
    from routers.ops import _format_model_name

    return _format_model_name


@pytest.fixture(scope="module")
def catalog_ids():
    from services.model_catalog import MODEL_CATALOG

    return [m.id for m in MODEL_CATALOG]


@pytest.mark.parametrize(
    "model_id,expected",
    [
        # The fix.
        ("claude-fable-5-1", "Claude Fable 5.1"),
        # A dated snapshot of the same point release: the 8-digit strip runs
        # first, then the exact entry matches.
        ("claude-fable-5-1-20260101", "Claude Fable 5.1"),
        # Unchanged neighbours — proof the new entry swallowed nothing.
        ("claude-fable-5", "Claude Fable 5"),
        ("claude-opus-5", "Claude Opus 5"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        # The pre-existing date-strip + prefix-mapping path.
        ("claude-haiku-4-5-20251001", "Claude Haiku 4"),
        ("claude-opus-4-8", "Claude Opus 4"),
        # The empty-input guard.
        ("", "Unknown"),
    ],
)
def test_format_model_name(fmt, model_id, expected):
    assert fmt(model_id) == expected


def test_no_catalog_id_renders_a_split_version(fmt, catalog_ids):
    """Derived from the catalog, so the NEXT point release is covered too.

    "Claude Fable 5 1" is the observable signature of this bug class: a version
    fragment split across a space because a non-date numeric suffix reached the
    hyphens-to-spaces fallback. No selectable model may render that way.
    """
    split_version = re.compile(r"\d \d")
    mangled = {
        model_id: fmt(model_id)
        for model_id in catalog_ids
        if split_version.search(fmt(model_id))
    }
    assert not mangled, (
        f"these selectable models render with a split version number: {mangled} "
        "— add an exact entry to `_format_model_name`'s `mappings` (#2726)"
    )
