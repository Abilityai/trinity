"""Unit tests for the model → context-window catalog (#1521).

Two concerns:
  1. ``resolve_context_window`` maps model strings to the right fallback window
     (decision A: bare Claude → 200K safe floor, ``[1m]`` → 1M, non-Claude natives).
  2. Regression guard for the CRITICAL wiring finding: the catalog value actually
     reaches Claude's ``metadata.context_window``. ``get_context_window`` is never
     called for the Claude runtime, so the real fallback is the catalog seed set at
     metadata creation, and the stream parser must NOT clobber it back to a flat
     200K when the runtime doesn't report a ``contextWindow`` — while the
     runtime-reported value (primary) must still override it when present.
"""
from __future__ import annotations

import json

import pytest

# conftest wires docker/base-image/agent_server as the `agent_server` package.
from agent_server.model_context import (  # noqa: E402
    CODEX_CONTEXT_WINDOW,
    DEFAULT_CONTEXT_WINDOW,
    EXTENDED_CONTEXT_WINDOW,
    pick_context_window,
    resolve_context_window,
)
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.stream_parser import process_stream_line  # noqa: E402


class TestResolveContextWindow:
    @pytest.mark.parametrize(
        "model,expected",
        [
            # [1m] opt-in → 1M (alias or full id, case-insensitive, whitespace-tolerant)
            ("opus[1m]", EXTENDED_CONTEXT_WINDOW),
            ("sonnet[1m]", EXTENDED_CONTEXT_WINDOW),
            ("claude-opus-4-8[1m]", EXTENDED_CONTEXT_WINDOW),
            ("SONNET[1M]", EXTENDED_CONTEXT_WINDOW),
            ("  opus[1m]  ", EXTENDED_CONTEXT_WINDOW),
            # bare Claude → 200K safe floor (decision A), incl. natively-1M ids and
            # dated ids — the runtime value delivers real 1M in practice.
            ("claude-opus-4-8", DEFAULT_CONTEXT_WINDOW),
            ("claude-opus-4-7", DEFAULT_CONTEXT_WINDOW),
            ("claude-sonnet-5", DEFAULT_CONTEXT_WINDOW),
            ("claude-fable-5", DEFAULT_CONTEXT_WINDOW),
            ("claude-sonnet-4-6", DEFAULT_CONTEXT_WINDOW),
            ("claude-haiku-4-5-20251001", DEFAULT_CONTEXT_WINDOW),
            ("claude-sonnet-4-5-20250929", DEFAULT_CONTEXT_WINDOW),
            ("opus", DEFAULT_CONTEXT_WINDOW),
            ("sonnet", DEFAULT_CONTEXT_WINDOW),
            ("haiku", DEFAULT_CONTEXT_WINDOW),
            ("fable", DEFAULT_CONTEXT_WINDOW),
            # non-Claude native runtimes
            ("gemini-3-flash", EXTENDED_CONTEXT_WINDOW),
            ("gemini-3-pro", EXTENDED_CONTEXT_WINDOW),
            ("gpt-5.1-codex", CODEX_CONTEXT_WINDOW),
            ("codex", CODEX_CONTEXT_WINDOW),
            # unknown / empty / None → safe floor
            ("some-future-model-x", DEFAULT_CONTEXT_WINDOW),
            ("", DEFAULT_CONTEXT_WINDOW),
            ("   ", DEFAULT_CONTEXT_WINDOW),
            (None, DEFAULT_CONTEXT_WINDOW),
        ],
    )
    def test_resolution_table(self, model, expected):
        assert resolve_context_window(model) == expected

    def test_constants(self):
        assert DEFAULT_CONTEXT_WINDOW == 200_000
        assert EXTENDED_CONTEXT_WINDOW == 1_000_000
        assert CODEX_CONTEXT_WINDOW == 272_000

    def test_never_raises_on_weird_input(self):
        # Total function: any input returns a bounded int, never raises.
        for m in [None, "", "🙂", "claude-", "[1m]", "x" * 5000]:
            assert isinstance(resolve_context_window(m), int)

    def test_unknown_model_logs_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert resolve_context_window("totally-made-up-model") == DEFAULT_CONTEXT_WINDOW
        assert any("unrecognized model" in r.message for r in caplog.records)

    def test_known_family_does_not_warn(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_context_window("claude-opus-4-8")
            resolve_context_window("gemini-3-flash")
        assert not any("unrecognized model" in r.message for r in caplog.records)


class TestCatalogReachesClaudeMetadata:
    """Regression guard for the dead-code finding: the catalog seed is the real
    Claude fallback and must survive the stream parser."""

    def _result_line(self, model_usage: dict) -> str:
        return json.dumps({"type": "result", "result": "done", "modelUsage": model_usage})

    def test_seed_persists_when_runtime_omits_context_window(self):
        # Mirrors the claude_code.py seed: metadata.context_window set from the
        # catalog at creation, BEFORE the stream runs.
        metadata = ExecutionMetadata()
        metadata.context_window = resolve_context_window("opus[1m]")
        assert metadata.context_window == EXTENDED_CONTEXT_WINDOW

        # A result event WITHOUT contextWindow (the fallback path the bug lives on).
        line = self._result_line({"claude-opus-4-8": {"inputTokens": 1234}})
        process_stream_line(line, [], metadata, {}, [])

        # The catalog seed must NOT be clobbered back to a flat 200K.
        assert metadata.context_window == EXTENDED_CONTEXT_WINDOW

    def test_runtime_value_overrides_seed(self):
        # Primary wins: a runtime-reported contextWindow overrides the seed.
        metadata = ExecutionMetadata()
        metadata.context_window = resolve_context_window("claude-opus-4-8")  # 200K seed
        assert metadata.context_window == DEFAULT_CONTEXT_WINDOW

        line = self._result_line({"claude-opus-4-8": {"contextWindow": 1_000_000}})
        process_stream_line(line, [], metadata, {}, [])

        assert metadata.context_window == 1_000_000


class TestAgentStateInitWindow:
    """Guards the state.py init-ordering fix: current_model is set BEFORE
    session_context_window so an [1m] agent's window is 1M at init, not 200K."""

    def _fresh_state(self, monkeypatch, *, runtime, model):
        from agent_server import state as state_module

        monkeypatch.setenv("AGENT_RUNTIME", runtime)
        if model is None:
            monkeypatch.delenv("AGENT_RUNTIME_MODEL", raising=False)
            monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        else:
            monkeypatch.setenv("AGENT_RUNTIME_MODEL", model)
        return state_module.AgentState()

    def test_1m_claude_agent_has_1m_window_at_init(self, monkeypatch):
        # The init-ordering bug: before #1521 this returned 200K because
        # current_model was unset when _get_default_context_window ran.
        st = self._fresh_state(monkeypatch, runtime="claude-code", model="opus[1m]")
        assert st.session_context_window == EXTENDED_CONTEXT_WINDOW

    def test_bare_claude_agent_has_200k_floor_at_init(self, monkeypatch):
        st = self._fresh_state(monkeypatch, runtime="claude-code", model="claude-opus-4-8")
        assert st.session_context_window == DEFAULT_CONTEXT_WINDOW

    def test_gemini_agent_has_1m_window_at_init_without_model(self, monkeypatch):
        # None model → falls back to the runtime name (gemini-cli) → 1M.
        st = self._fresh_state(monkeypatch, runtime="gemini-cli", model=None)
        assert st.session_context_window == EXTENDED_CONTEXT_WINDOW


class TestMultiModelUsageSelection:
    """#1840: ``modelUsage`` carries one entry PER MODEL the turn touched.

    Claude Code bills side work (tool-permission classification, title
    generation) to a cheap Haiku, so a Sonnet-5 turn reports two entries. The
    old parser took the FIRST one — the side model whenever one ran — so the
    same agent flipped between 1M and 200K run to run. Observed live on
    v0.8.6: 4 of 5 identical Sonnet-5 turns recorded 200000.
    """

    # The payload verbatim from a live Sonnet-5 turn (#1840).
    REAL_WORLD = {
        "claude-haiku-4-5-20251001": {"contextWindow": 200_000, "inputTokens": 522},
        "claude-sonnet-5": {"contextWindow": 1_000_000, "inputTokens": 2},
    }

    def _run(self, model_usage: dict, model_name: str | None, seed: str = "claude-sonnet-5"):
        metadata = ExecutionMetadata()
        metadata.context_window = resolve_context_window(seed)
        if model_name:
            assistant = json.dumps({
                "type": "assistant",
                "message": {"model": model_name, "usage": {"input_tokens": 1}, "content": []},
            })
            process_stream_line(assistant, [], metadata, {}, [])
        result = json.dumps({"type": "result", "result": "done", "modelUsage": model_usage})
        process_stream_line(result, [], metadata, {}, [])
        return metadata.context_window

    def test_side_model_does_not_win(self):
        """The exact regression: Haiku listed first must not set the window."""
        assert self._run(self.REAL_WORLD, "claude-sonnet-5") == 1_000_000

    def test_selection_is_order_independent(self):
        reordered = dict(reversed(list(self.REAL_WORLD.items())))
        assert self._run(reordered, "claude-sonnet-5") == 1_000_000

    def test_larger_side_window_never_inflates_a_smaller_main_model(self):
        """The dangerous direction: a 1M side model must not hide a 200K wall."""
        usage = {
            "claude-sonnet-5": {"contextWindow": 1_000_000},
            "claude-opus-4-8": {"contextWindow": 200_000},
        }
        assert self._run(usage, "claude-opus-4-8", seed="claude-opus-4-8") == 200_000

    def test_unidentifiable_model_keeps_the_seed(self):
        """No match + ambiguous map ⇒ keep the catalog fallback, never guess."""
        assert self._run(self.REAL_WORLD, "claude-some-unlisted-model") == DEFAULT_CONTEXT_WINDOW

    def test_single_entry_still_wins_without_a_model_name(self):
        """Unambiguous turns keep working even if the model id is unknown."""
        assert self._run({"claude-sonnet-5": {"contextWindow": 1_000_000}}, None) == 1_000_000

    def test_entry_without_context_window_does_not_abort_the_scan(self):
        """The old `break` sat outside the `if`, so a first entry lacking
        contextWindow ended the search before reaching a usable one."""
        usage = {
            "claude-haiku-4-5-20251001": {"inputTokens": 10},   # no contextWindow
            "claude-sonnet-5": {"contextWindow": 1_000_000},
        }
        assert self._run(usage, "claude-sonnet-5") == 1_000_000


class TestPickContextWindow:
    """Direct unit coverage of the selection rule (#1840)."""

    def test_matches_by_canonical_model_when_the_key_is_an_alias(self):
        usage = {"sonnet": {"contextWindow": 1_000_000, "canonicalModel": "claude-sonnet-5"}}
        assert pick_context_window(usage, "claude-sonnet-5") == 1_000_000

    @pytest.mark.parametrize("bad", [None, {}, [], "nope", 42])
    def test_non_map_input_returns_none(self, bad):
        assert pick_context_window(bad, "claude-sonnet-5") is None

    @pytest.mark.parametrize("value", [0, -1, "1000000", None, {}])
    def test_non_positive_int_windows_are_ignored(self, value):
        assert pick_context_window({"claude-sonnet-5": {"contextWindow": value}}, "claude-sonnet-5") is None

    def test_ambiguous_map_without_a_match_returns_none(self):
        usage = {"a": {"contextWindow": 200_000}, "b": {"contextWindow": 1_000_000}}
        assert pick_context_window(usage, None) is None
