"""ent#243 — model-conditional prompt tiers: no-op proof + mechanism contract.

PR 1 ships the tier *mechanism* with an EMPTY ``_MINIMAL_PREFIXES``, so the
rendered prompt must be byte-identical to the pre-ent#243 release for every model
anyone can actually select. "Provable no-op" is only a claim until a test proves
it; that is what ``TestNoOp`` is for, and it reads the live picker list off
``ModelSelector.vue`` so a newly-added preset is covered without anyone
remembering to update this file.

The remaining classes test the mechanism directly (by constructing a MINIMAL
render) so that PR 2 — which populates the prefix tuple — inherits a suite that
already pins what MINIMAL is allowed to drop.
"""

from __future__ import annotations

import pytest

from services.model_catalog import MODEL_CATALOG
from services.platform_prompt_service import (
    _ALWAYS_SECTIONS,
    _KNOWN_SECTION_HEADINGS,
    _MINIMAL_DROP_SECTIONS,
    _SECTION_DELIMITER,
    _iter_sections,
    PLATFORM_INSTRUCTIONS,
    render_platform_instructions,
)
from services.prompt_tier import (
    _MINIMAL_PREFIXES,
    PromptTier,
    resolve_prompt_tier,
)


def _preset_models() -> list[str]:
    """Every model id the picker offers, from the single source of truth.

    Repointed from a brittle ModelSelector.vue regex to the Python catalog
    (#2086): the picker now derives from ``services/model_catalog.py``, so this
    list covers any preset added there without anyone touching this file — and it
    no longer breaks when the Vue array stops being a literal.
    """
    return [m.id for m in MODEL_CATALOG]


class TestNoOp:
    """PR 1 changes no agent's prompt. Byte-identity, not equivalence."""

    def test_preset_list_parsed(self):
        # Guard the guard: a refactor of ModelSelector.vue that breaks the parse
        # would otherwise make every test below vacuously pass.
        presets = _preset_models()
        assert len(presets) >= 5
        assert "claude-fable-5" in presets
        assert "claude-haiku-4-5-20251001" in presets

    @pytest.mark.parametrize("model", _preset_models())
    def test_every_preset_renders_the_unchanged_prompt(self, model):
        assert render_platform_instructions(resolve_prompt_tier(model)) == PLATFORM_INSTRUCTIONS

    @pytest.mark.parametrize(
        "model",
        [None, "", "   ", "claude-fable-5[1m]", "gpt-5.1-codex", "gemini-3-flash",
         "some-unreleased-model", "Claude-Sonnet-5", "🙂"],
    )
    def test_edge_and_freetext_models_render_the_unchanged_prompt(self, model):
        assert render_platform_instructions(resolve_prompt_tier(model)) == PLATFORM_INSTRUCTIONS

    def test_minimal_prefix_set_is_empty(self):
        # The no-op contract itself. PR 2 populating this MUST consciously edit
        # this test — that is the point, not an inconvenience.
        assert _MINIMAL_PREFIXES == ()


class TestTierResolution:
    def test_unknown_fails_toward_verbose(self):
        for model in (None, "", "  ", "not-a-real-model", "gpt-4", "llama-3"):
            assert resolve_prompt_tier(model) is PromptTier.VERBOSE

    def test_total_function_never_raises(self):
        for model in (None, "", "x" * 10_000, "\n\t", "💥", "claude-" * 500):
            assert isinstance(resolve_prompt_tier(model), PromptTier)

    def test_tier_is_not_keyed_on_runtime(self):
        # The signature is the contract: runtime decides MCP tool *naming*
        # (#1187), model decides prose volume. Merging them is the regression
        # this asserts against (requirements §41.1).
        import inspect

        params = inspect.signature(resolve_prompt_tier).parameters
        assert list(params) == ["model"]


class TestSectionSplit:
    def test_split_round_trips_byte_exactly(self):
        rebuilt = _SECTION_DELIMITER.join(chunk for _, chunk in _iter_sections(PLATFORM_INSTRUCTIONS))
        assert rebuilt == PLATFORM_INSTRUCTIONS

    def test_heading_set_is_pinned(self):
        found = {heading for heading, _ in _iter_sections(PLATFORM_INSTRUCTIONS) if heading}
        assert found == _KNOWN_SECTION_HEADINGS, (
            "A '###' section was added or renamed. An unmapped section renders at "
            "every tier (safe), but a renamed one silently orphans its drop-list "
            "entry — update _KNOWN_SECTION_HEADINGS and _MINIMAL_DROP_SECTIONS."
        )

    def test_drop_set_is_a_subset_of_known_headings(self):
        # A typo'd drop entry is dead code that looks like configuration.
        assert _MINIMAL_DROP_SECTIONS <= _KNOWN_SECTION_HEADINGS

    def test_subsections_stay_with_their_parent(self):
        # Operator Communication owns four '####' subsections; the delimiter must
        # not split on them or the contract section would fragment.
        sections = dict((h, c) for h, c in _iter_sections(PLATFORM_INSTRUCTIONS))
        operator = sections["Operator Communication"]
        assert "#### The contract: fire-and-park, never block-and-wait" in operator
        assert "#### Ask before irreversible actions" in operator

    def test_preamble_is_first_and_headingless(self):
        first_heading, first_chunk = _iter_sections(PLATFORM_INSTRUCTIONS)[0]
        assert first_heading == ""
        assert first_chunk.startswith("# Trinity Platform Instructions")


class TestMinimalRender:
    """Exercises the mechanism PR 2 will switch on. Inert in production today."""

    @pytest.fixture
    def minimal(self) -> str:
        return render_platform_instructions(PromptTier.MINIMAL)

    def test_minimal_is_shorter(self, minimal):
        assert len(minimal) < len(PLATFORM_INSTRUCTIONS)

    def test_minimal_drops_exactly_the_drop_set(self, minimal):
        headings = {h for h, _ in _iter_sections(minimal) if h}
        assert headings == _KNOWN_SECTION_HEADINGS - _MINIMAL_DROP_SECTIONS

    def test_minimal_keeps_the_operator_contract_verbatim(self, minimal):
        # #1402's sentinels are load-bearing and sentinel-locked elsewhere; a tier
        # must never be able to remove the async human-gate contract.
        assert "fire-and-park, never block-and-wait" in minimal
        assert "#### Ask before irreversible actions" in minimal
        assert "~/.trinity/operator-queue.json" in minimal

    def test_minimal_keeps_the_user_memory_leak_warning(self, minimal):
        # The shared-memory-directory warning is a privacy guard, not tool docs.
        assert "~/.claude/projects/memory/" in minimal
        assert "shared across all users" in minimal

    def test_minimal_keeps_package_persistence(self, minimal):
        assert "~/.trinity/setup.sh" in minimal

    def test_always_sections_all_present(self, minimal):
        for heading in _ALWAYS_SECTIONS:
            assert f"### {heading}" in minimal

    def test_minimal_still_starts_with_the_preamble(self, minimal):
        assert minimal.startswith("# Trinity Platform Instructions")

    def test_minimal_has_no_dangling_delimiters(self, minimal):
        assert "\n\n\n\n" not in minimal
        assert not minimal.endswith(_SECTION_DELIMITER)
