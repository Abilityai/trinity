"""Drift guard for the hand-synced model lists (#1660).

`ModelSelector.vue` is the de-facto canonical picker (#1080). Two other lists are
kept in sync with it **by hand** — there is no shared Python/Vue model registry:

  1. `services/settings_service.PUBLIC_CHANNEL_MODELS` — server-validated whitelist
     for the per-agent public-channel override (#894); current-gen only (no legacy).
  2. The platform default-model `<option>` list in `views/Settings.vue` — the admin
     fleet-wide default; Opus/Sonnet tiers only (#1080 AC), so Haiku is deliberately
     absent and is NOT asserted here.

The #1660 bug: ModelSelector gained the Claude 5 presets and neither list followed,
so an admin couldn't pick them as the platform default and the public-channel PUT
422'd them. These tests assert the current lineup reaches all three lists.

Deliberately narrow: this pins the *current-generation* models by name rather than
diffing whole lists, because the three lists are legitimately different sizes
(legacy is picker-only; Haiku is public-channel-only). Adding the next generation
means adding it here too — that failure is the point.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _PROJECT_ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_MODEL_SELECTOR = _PROJECT_ROOT / "src" / "frontend" / "src" / "components" / "ModelSelector.vue"
_SETTINGS_VUE = _PROJECT_ROOT / "src" / "frontend" / "src" / "views" / "Settings.vue"

# The Claude 5 family — the generation #1660 found missing. Every list below must
# offer these; they are current-gen and Opus/Sonnet-tier-equivalent, so they belong
# in all three (unlike Haiku, which is public-channel-only by #1080).
CLAUDE_5 = ("claude-fable-5", "claude-sonnet-5")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing source file: {path}"
    return path.read_text()


def _model_selector_presets() -> set[str]:
    """Every `value:` in ModelSelector.vue's PRESET_MODELS array."""
    src = _read(_MODEL_SELECTOR)
    block = re.search(r"const PRESET_MODELS = \[(.*?)\n\]", src, re.S)
    assert block, "PRESET_MODELS array not found in ModelSelector.vue — did it move?"
    values = set(re.findall(r"value:\s*'([^']+)'", block.group(1)))
    assert values, "PRESET_MODELS parsed but yielded no values — parse is broken"
    return values


def _settings_default_model_options() -> set[str]:
    """Every `<option value="...">` in the platform default-model <select>."""
    src = _read(_SETTINGS_VUE)
    block = re.search(
        r'<select\s+v-model="platformDefaultModelValue".*?</select>', src, re.S
    )
    assert block, "platform default-model <select> not found in Settings.vue — did it move?"
    values = set(re.findall(r'<option value="([^"]+)"', block.group(0)))
    assert values, "default-model <select> parsed but yielded no options — parse is broken"
    return values


@pytest.mark.parametrize("model", CLAUDE_5)
def test_claude_5_is_offered_in_model_selector(model):
    assert model in _model_selector_presets(), (
        f"{model} missing from ModelSelector.vue PRESET_MODELS"
    )


@pytest.mark.parametrize("model", CLAUDE_5)
def test_claude_5_is_valid_for_public_channels(model):
    """#1660: the public-channel override PUT 422'd the Claude 5 models."""
    from services.settings_service import is_valid_public_channel_model

    assert is_valid_public_channel_model(model), (
        f"{model} missing from PUBLIC_CHANNEL_MODELS — "
        f"PUT /api/agents/{{name}}/public-channel-model would 422 it"
    )


@pytest.mark.parametrize("model", CLAUDE_5)
def test_claude_5_is_selectable_as_platform_default(model):
    """#1660: an admin could not pick Claude 5 as the fleet default."""
    assert model in _settings_default_model_options(), (
        f"{model} missing from the platform default-model dropdown in Settings.vue"
    )


def test_public_channel_whitelist_is_a_subset_of_the_picker():
    """Every server-accepted model must be offered by the canonical picker.

    A model the backend accepts but the picker never shows is unreachable via the
    UI — that direction of drift is a bug even though it fails 'open'.
    """
    from services.settings_service import PUBLIC_CHANNEL_MODELS

    orphaned = set(PUBLIC_CHANNEL_MODELS) - _model_selector_presets()
    assert not orphaned, (
        f"PUBLIC_CHANNEL_MODELS accepts models absent from ModelSelector.vue: "
        f"{sorted(orphaned)}"
    )


def test_platform_default_dropdown_is_a_subset_of_the_picker():
    """The admin dropdown must not offer a model the canonical picker dropped."""
    orphaned = _settings_default_model_options() - _model_selector_presets()
    assert not orphaned, (
        f"Settings.vue default-model dropdown offers models absent from "
        f"ModelSelector.vue: {sorted(orphaned)}"
    )


def test_platform_default_value_is_selectable():
    """The hardcoded fallback must itself be offered in the dropdown.

    Otherwise a fresh install renders a blank <select> (Vue drops a v-model value
    with no matching <option>).
    """
    from services.settings_service import PLATFORM_DEFAULT_MODEL_VALUE

    assert PLATFORM_DEFAULT_MODEL_VALUE in _settings_default_model_options(), (
        f"PLATFORM_DEFAULT_MODEL_VALUE ({PLATFORM_DEFAULT_MODEL_VALUE!r}) is not an "
        f"option in the platform default-model dropdown"
    )
