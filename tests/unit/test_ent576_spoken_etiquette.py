"""ent#576 — say it once: spoken etiquette for the whole tool cycle.

The voice model announced what it was about to do, called the tool, and — when
the tool returned — said the same thing again, often reciting content the
canvas was already showing. That was the *predictable* output of the prompt we
shipped: the only etiquette rule was a pre-call filler for `run_task`, and
nothing said what to do after a result, nothing about canvas tools, nothing
about a sequence of calls.

This pins the contract on the pattern of `test_1535_report_prompt_guidance.py`:
the ONE etiquette block is in the session's `system_instruction` for workspace
and non-workspace sessions alike, it is built from the manifest (ent#535 AC 6 —
never a word about a tool the session cannot call), the `run_task` description
agrees with it, and its size is capped because it rides every session.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

# The block rides EVERY voice session, so its size is a decision, not an
# accident. The fullest variant (run_task in the background + the canvas)
# landed at ~1.9 KB.
MAX_BLOCK_CHARS = 2200


def _gv():
    from services import gemini_voice
    return gemini_voice


def _session(gv, *, workspace: bool, manifest=None):
    """A Workspace call (bound to a thread → background dispatch) or a call
    with no thread (VoIP / legacy Agent Detail → the synchronous path)."""
    kw = dict(session_id="vs", agent_name="a", chat_session_id=None, user_id=1,
              user_email="u@example.com", system_prompt="You are the agent.",
              workspace_mode=workspace, tool_manifest=manifest)
    if workspace:
        kw.update(portal_session_id="p-1", client_email="u@example.com", is_platform=True)
    return gv.VoiceSession(**kw)


def _instruction(gv, session) -> str:
    cfg = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)._build_live_config(session)
    return cfg.system_instruction or ""


def _block(text: str) -> str:
    gv = _gv()
    assert text.count(gv.SPOKEN_ETIQUETTE_HEADING) == 1, "the etiquette block must appear exactly once"
    return text.split(gv.SPOKEN_ETIQUETTE_HEADING, 1)[1]


# ---------------------------------------------------------------------------
# One block, both front doors, both dispatch paths
# ---------------------------------------------------------------------------
class TestOneBlockEverywhere:
    def test_a_workspace_session_carries_the_block(self):
        gv = _gv()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        text = _instruction(gv, _session(gv, workspace=True, manifest=PLATFORM_VOICE_TOOLS))
        block = _block(text)
        for rule in ("Announce a wait, not an action", "Say it once", "One narration per sequence",
                     "Report a failure once, with its reason"):
            assert rule in block, rule
        # Background dispatch (ent#551) is what a Workspace call does.
        assert "runs in the background" in block
        assert "Background tasks" in block
        assert f"at most {gv.MAX_BACKGROUND_TASKS_PER_CALL} run at a time" in block

    def test_a_non_workspace_session_carries_the_same_rules_in_the_synchronous_wording(self):
        # VoIP and the legacy Agent Detail session: one thread-less call,
        # `run_task` only, blocking. Same rules, no background paragraph.
        gv = _gv()
        text = _instruction(gv, _session(gv, workspace=False, manifest=frozenset({gv.RUN_TASK})))
        block = _block(text)
        for rule in ("Announce a wait, not an action", "Say it once", "One narration per sequence",
                     "Report a failure once, with its reason"):
            assert rule in block, rule
        assert "cannot speak while it runs" in block
        assert "Background tasks" not in block
        assert "system notice" not in block

    def test_the_block_is_appended_by_the_config_builder_and_nowhere_else(self):
        # One place: `_build_live_config` runs for every connection leg of
        # every session, whichever front door started it.
        gv = _gv()
        src = inspect.getsource(gv)
        assert src.count("spoken_etiquette_instruction(") == 2       # the def + the one call
        assert "spoken_etiquette_instruction(" in inspect.getsource(gv.GeminiVoiceService._build_live_config)
        assert not hasattr(gv, "_TOOL_ETIQUETTE_INSTRUCTION"), "the filler-only appendix must be gone, not duplicated"

    def test_the_block_is_bounded(self):
        gv = _gv()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        assert len(gv.spoken_etiquette_instruction(PLATFORM_VOICE_TOOLS, background=True)) <= MAX_BLOCK_CHARS


# ---------------------------------------------------------------------------
# Built from the manifest (ent#535 AC 6 preserved)
# ---------------------------------------------------------------------------
class TestBuiltFromTheManifest:
    def test_no_tools_means_no_block(self):
        gv = _gv()
        assert gv.spoken_etiquette_instruction(frozenset()) == ""
        assert gv.SPOKEN_ETIQUETTE_HEADING not in _instruction(gv, _session(gv, workspace=True, manifest=frozenset()))

    def test_a_canvas_only_session_never_hears_of_run_task(self):
        gv = _gv()
        block = gv.spoken_etiquette_instruction(frozenset({"show_markdown", "show_diagram"}), background=True)
        assert "run_task" not in block
        assert "Background tasks" not in block
        assert "Do not announce a drawing" in block
        assert "Never read the canvas aloud" in block

    def test_a_run_task_only_session_never_hears_of_the_canvas(self):
        gv = _gv()
        block = gv.spoken_etiquette_instruction(frozenset({gv.RUN_TASK}), background=False)
        assert "canvas" not in block.lower()
        assert "run_task" in block

    def test_the_model_is_told_the_dispatch_shape_of_its_own_session(self):
        gv = _gv()
        sync = gv.spoken_etiquette_instruction(frozenset({gv.RUN_TASK}), background=False)
        background = gv.spoken_etiquette_instruction(frozenset({gv.RUN_TASK}), background=True)
        assert "cannot speak while it runs" in sync and "runs in the background" not in sync
        assert "runs in the background" in background and "cannot speak while it runs" not in background


# ---------------------------------------------------------------------------
# The rules themselves
# ---------------------------------------------------------------------------
class TestTheRules:
    def test_announce_a_wait_not_an_action(self):
        gv = _gv()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        block = gv.spoken_etiquette_instruction(PLATFORM_VOICE_TOOLS, background=True)
        # A filler for the thing that waits…
        assert "Never call it silently" in block
        # …and none for the thing that returns at once.
        assert "The canvas tools return at once" in block
        assert "the drawing appearing IS the acknowledgement" in block

    def test_say_it_once_and_do_not_read_the_canvas(self):
        gv = _gv()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        block = gv.spoken_etiquette_instruction(PLATFORM_VOICE_TOOLS, background=True)
        assert "add only what the person does not already have" in block
        assert "Never restate the intention you announced before the call" in block
        assert "point at it and interpret it" in block

    def test_a_sequence_is_one_narration(self):
        gv = _gv()
        block = gv.spoken_etiquette_instruction(frozenset({gv.RUN_TASK}))
        assert "one announcement at the start and one report at the end" in block
        assert "not a line per call" in block

    def test_failure_is_reported_once_never_dressed_as_success(self):
        gv = _gv()
        block = gv.spoken_etiquette_instruction(frozenset({gv.RUN_TASK}))
        assert "never dressed up as success, never re-announced" in block


# ---------------------------------------------------------------------------
# The other two prompt surfaces agree with the block
# ---------------------------------------------------------------------------
class TestTheOtherSurfacesAgree:
    def test_the_run_task_description_agrees(self):
        gv = _gv()
        [decl] = gv._RUN_TASK_TOOL.function_declarations
        desc = decl.description
        assert "never call it silently" in desc.lower()
        assert "do not repeat that line" in desc
        # The old duplicate filler rule is gone — one contract, not two copies.
        assert "ALWAYS say a brief out-loud filler" not in desc

    def test_the_panel_instructions_state_the_division_of_labour(self):
        gv = _gv()
        text = gv.WORKSPACE_PANEL_INSTRUCTIONS
        assert "Don't mirror every voice response on the canvas" in text
        assert "don't mirror the canvas in your voice" in text
        assert "the canvas is the artefact, the voice is what it means" in text
        assert "never read the blocks aloud" in text
