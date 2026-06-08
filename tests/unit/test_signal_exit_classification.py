"""Regression tests for Issue #516: SIGKILL/timeout terminations of the
claude subprocess must not be misclassified as authentication failures.

When the agent-server's headless task path sees a non-zero return code,
two heuristics in the auth-fallback block (string-match on the captured
stderr/transcript, and "zero tokens processed") used to fire on every
external signal kill — turning a schedule-timeout SIGKILL or operator
cancel into a misleading "Subscription token may be expired" error.

The fix introduces ``_classify_signal_exit`` which is consulted *before*
the auth heuristics. These tests pin its contract so the misclassification
cannot regress.

Module under test:
    docker/base-image/agent_server/services/error_classifier.py::_classify_signal_exit
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import the module under test without booting the full agent_server package.
# agent_server/__init__.py loads FastAPI app; we only need a small helper.
# Pre-populate sys.modules with a namespace-package shim so Python finds the
# real submodules via __path__ but skips the heavy __init__.py side effects.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

if "agent_server" not in sys.modules:
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [str(_AGENT_SERVER_DIR)]
    sys.modules["agent_server"] = _stub

from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.error_classifier import _classify_signal_exit  # noqa: E402


# ---------------------------------------------------------------------------
# Signal exits — must classify as 504 with a clear external-kill message.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "return_code,expected_signal_name",
    [
        # Python's subprocess returns negative N for signal N kills.
        (-2, "SIGINT"),
        (-9, "SIGKILL"),
        (-15, "SIGTERM"),
        # Shell-encoded variants (128 + signum) — surfaced when an
        # intermediate shell wraps the exit.
        (130, "SIGINT"),
        (137, "SIGKILL"),
        (143, "SIGTERM"),
    ],
)
def test_known_signal_exits_return_504(return_code, expected_signal_name):
    """SIGINT/SIGKILL/SIGTERM in either Python-native or shell-encoded form
    must produce a 504 with the signal named in the message."""
    metadata = ExecutionMetadata(tool_count=3, num_turns=5)

    result = _classify_signal_exit(return_code, metadata)

    assert result is not None, (
        f"return_code={return_code} should be classified as a signal exit"
    )
    status_code, detail = result
    assert status_code == 504
    assert expected_signal_name in detail
    # Diagnostic context from metadata is included so operators can see
    # how far the run got before being killed.
    assert "3 tool calls" in detail
    assert "5 turns" in detail
    # The actionable hint about what to do next must be present.
    assert "timeout" in detail.lower()


def test_unknown_negative_signal_uses_signal_n_label():
    """Negative return codes for signals we don't have a name for fall
    back to a generic ``signal N`` label rather than misclassifying."""
    metadata = ExecutionMetadata()

    # SIGUSR1 = 10 → return_code = -10
    result = _classify_signal_exit(-10, metadata)

    assert result is not None
    status_code, detail = result
    assert status_code == 504
    assert "signal 10" in detail


# ---------------------------------------------------------------------------
# Non-signal exits — must NOT be classified, so the caller continues with
# the auth-failure / generic-error code paths unchanged.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "return_code",
    [
        0,    # success — won't be reached in production but contract should hold
        1,    # generic error
        2,    # misuse of command / invalid args
        126,  # command found but not executable
        127,  # command not found
    ],
)
def test_non_signal_exits_return_none(return_code):
    """Clean (non-signal) non-zero exits must return None so the existing
    auth-failure detection and generic error handling can take over."""
    metadata = ExecutionMetadata()

    assert _classify_signal_exit(return_code, metadata) is None


def test_classifier_handles_missing_metadata():
    """Defensive: helper must not crash if metadata is None."""
    result = _classify_signal_exit(-9, None)

    assert result is not None
    status_code, detail = result
    assert status_code == 504
    assert "SIGKILL" in detail
    assert "0 tool calls" in detail
    assert "0 turns" in detail


def test_classifier_handles_metadata_with_no_turns():
    """num_turns is Optional[int]; a None value must render as 0, not crash."""
    metadata = ExecutionMetadata(tool_count=2)  # num_turns left as None

    result = _classify_signal_exit(-15, metadata)

    assert result is not None
    _, detail = result
    assert "2 tool calls" in detail
    assert "0 turns" in detail


# ---------------------------------------------------------------------------
# Boundary case — ensures we don't over-broaden the shell-encoded set.
# Adding 139 (SIGSEGV) here would silently change semantics; pin the current
# scope (130, 137, 143) so any expansion is a deliberate, reviewed change.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("return_code", [128, 129, 131, 138, 139, 144, 200])
def test_other_high_exit_codes_are_not_classified_as_signals(return_code):
    """Only the documented shell-encoded signals (130/137/143) are classified.
    Other ≥128 exit codes pass through to normal error handling — apps
    sometimes use ≥128 as application-defined error codes."""
    assert _classify_signal_exit(return_code, ExecutionMetadata()) is None


# ---------------------------------------------------------------------------
# Issue #906: the chat path in claude_code.py was missing the
# _classify_signal_exit call that was added to headless_executor.py for
# Issue #516. Both call sites must consult the signal classifier BEFORE
# falling through to _diagnose_exit_failure — otherwise a SIGKILL at
# 0 turns is misclassified as "Subscription token may be expired".
#
# This is a structural / source-level invariant test. If the call ordering
# is restructured in either file, this test fires and forces a deliberate
# re-evaluation of the auth-fallback ordering guarantee.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename",
    [
        "claude_code.py",       # chat path /api/chat — added by Issue #906
        "headless_executor.py", # headless path /api/task — added by Issue #516
    ],
)
def test_signal_exit_classified_before_diagnose_failure(filename):
    """In both error-path call sites, _classify_signal_exit(...) must appear
    before _diagnose_exit_failure(...) so signal kills aren't mis-routed
    through the auth-fallback heuristic. (Issues #516 and #906.)

    The check is purely structural — it looks for the function-call
    patterns (``name(``) which only match call sites, not imports
    (imports use trailing commas in the parenthesised block).
    """
    path = _AGENT_SERVER_DIR / "services" / filename
    src = path.read_text()

    signal_call = src.find("_classify_signal_exit(")
    diagnose_call = src.find("_diagnose_exit_failure(")

    assert signal_call != -1, (
        f"{filename}: _classify_signal_exit is not called — "
        f"signal-exit classification is missing (Issue #516 / #906)."
    )
    assert diagnose_call != -1, (
        f"{filename}: _diagnose_exit_failure is not called — "
        f"unexpected; the error path should still fall through to it "
        f"for non-signal exits."
    )
    assert signal_call < diagnose_call, (
        f"{filename}: _classify_signal_exit must be called before "
        f"_diagnose_exit_failure. Reversed ordering would let the "
        f"auth-fallback heuristic misclassify SIGKILL/SIGTERM/SIGINT "
        f"as expired-token errors (Issue #906)."
    )
