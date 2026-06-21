"""
Tests for Issue #904 (SIGKILL / OOM / timeout must not be misclassified as an
auth failure that triggers SUB-003 subscription auto-switch) and Issue #1088
(unify the auth-class classifier into one shared module + drift guard).

Four regression surfaces:

1. `failure_classifier.is_auth_failure` — the canonical classifier at
   `src/backend/services/failure_classifier.py` (#1088). Short-circuits to
   False when the message contains an unambiguous signal-kill / OOM / timeout
   marker, even if an AUTH_INDICATOR substring also happens to match (#904).
   `subscription_auto_switch` re-exports it for its SUB-003 consumers.
2. The scheduler copy at `src/scheduler/failure_classifier.py` is a
   byte-identical vendored mirror of the canonical backend file — the
   scheduler runs in a separate container and cannot import from
   backend.services, and it uses the classifier for log-labelling only.
   `TestBackendSchedulerParity` enforces byte-identity so the two can never
   drift; `TestBackendReExportGuard` pins the backend re-export (#1088).
3. `error_classifier._is_auth_failure_message` — the AGENT-runtime classifier
   — is a genuinely DIFFERENT classifier (token-state markers, and crucially
   NO kill-marker short-circuit). It is kept kill-safe one level up by
   `_classify_signal_exit` running BEFORE the auth check (precedence).
   `TestAgentClassifierKillSafetyContract` documents that contract honestly;
   merging it is out of scope (#1088 / #945).
4. `error_classifier._diagnose_exit_failure` no longer returns the
   "Subscription token may be expired" string for the OAuth-only-config
   branch, so its output cannot loop back through `_is_auth_failure_message`
   and surface as a 503 auth failure on `headless_executor.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_CLASSIFIER = _REPO_ROOT / "src" / "backend" / "services" / "failure_classifier.py"
_SCHEDULER_CLASSIFIER = _REPO_ROOT / "src" / "scheduler" / "failure_classifier.py"
_SUBSCRIPTION_AUTO_SWITCH = _REPO_ROOT / "src" / "backend" / "services" / "subscription_auto_switch.py"


def _load_module_from_path(path: Path, mod_name: str):
    """Exec a single source file in isolation and return the module.

    Used for the two `failure_classifier.py` copies, which are pure stdlib
    (#1088) — no heavy backend / apscheduler imports, no `sys.modules` stubs,
    and nothing registered in `sys.modules` (no cross-test pollution)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        pytest.skip(f"source not available: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -----------------------------------------------------------------------------
# failure_classifier.is_auth_failure  — canonical backend copy
# -----------------------------------------------------------------------------


def _load_backend_is_auth_failure():
    """Load the canonical `is_auth_failure` from
    `src/backend/services/failure_classifier.py` by path (#1088)."""
    return _load_module_from_path(_BACKEND_CLASSIFIER, "_iaf_backend").is_auth_failure


class TestBackendIsAuthFailureNonAuthMarkers:
    """`is_auth_failure` must return False for signal-kill / OOM / timeout
    messages even when an AUTH_INDICATOR substring is present."""

    @pytest.fixture
    def is_auth_failure(self):
        return _load_backend_is_auth_failure()

    def test_sigkill_marker_overrides_auth_indicator(self, is_auth_failure):
        # The headless_executor pre-#904 503 string ("possible authentication
        # issue") contained "authentication" but the real cause was SIGKILL.
        # New wording drops "authentication" entirely — but if a future
        # regression re-adds it, the SIGKILL marker must still win.
        msg = "Execution terminated by SIGKILL after 0 tool calls / 0 turns (exit code -9). authentication issue"
        assert is_auth_failure(msg) is False

    def test_exit_137_marker_overrides_auth_indicator(self, is_auth_failure):
        msg = "claude failed (exit code 137): unauthorized"
        assert is_auth_failure(msg) is False

    def test_oom_marker_overrides_auth_indicator(self, is_auth_failure):
        msg = "memory cgroup out of memory: killed process (git). credentials expired"
        assert is_auth_failure(msg) is False

    def test_real_auth_still_triggers(self, is_auth_failure):
        # Regression: legitimate auth failures still classify True.
        assert is_auth_failure("HTTP 401 unauthorized") is True
        assert is_auth_failure("credit balance is too low") is True
        assert is_auth_failure("Token expired, re-authenticate") is True

    def test_empty_message_returns_false(self, is_auth_failure):
        assert is_auth_failure("") is False
        assert is_auth_failure(None) is False  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# scheduler failure_classifier  — byte-identical mirror must stay in sync
# -----------------------------------------------------------------------------


def _load_scheduler_is_auth_failure():
    """Load the scheduler's vendored-mirror `is_auth_failure` from
    `src/scheduler/failure_classifier.py` by path (#1088). Pure stdlib mirror —
    no apscheduler stubs or service.py text-slicing needed."""
    return _load_module_from_path(_SCHEDULER_CLASSIFIER, "_iaf_scheduler").is_auth_failure


class TestSchedulerIsAuthFailureNonAuthMarkers:
    """Same contract as the backend side. Drift between the two would
    re-introduce the bug on the scheduler-driven path."""

    @pytest.fixture
    def is_auth_failure(self):
        return _load_scheduler_is_auth_failure()

    def test_sigkill_marker_overrides_auth_indicator(self, is_auth_failure):
        msg = "Execution terminated by SIGKILL after 0 tool calls / 0 turns (exit code -9). authentication issue"
        assert is_auth_failure(msg) is False

    def test_exit_137_marker_overrides_auth_indicator(self, is_auth_failure):
        assert is_auth_failure("claude failed (exit code 137): unauthorized") is False

    def test_oom_marker_overrides_auth_indicator(self, is_auth_failure):
        assert is_auth_failure("OOM killed: credentials expired") is False

    def test_real_auth_still_triggers(self, is_auth_failure):
        assert is_auth_failure("HTTP 401 unauthorized") is True
        assert is_auth_failure("credit balance is too low") is True


# -----------------------------------------------------------------------------
# #1088 — single-source-of-truth guards: byte-identity + backend re-export
# -----------------------------------------------------------------------------


class TestBackendSchedulerParity:
    """#1088: the scheduler copy is a byte-identical vendored mirror of the
    canonical backend classifier. Any drift re-introduces the #904 bug class
    on the scheduler-driven path and breaks the single source of truth."""

    def test_classifier_files_are_byte_identical(self):
        backend_bytes = _BACKEND_CLASSIFIER.read_bytes()
        scheduler_bytes = _SCHEDULER_CLASSIFIER.read_bytes()
        assert backend_bytes == scheduler_bytes, (
            "src/scheduler/failure_classifier.py has drifted from the canonical "
            "src/backend/services/failure_classifier.py. Re-sync the scheduler "
            "mirror from the backend copy:\n"
            "    cp src/backend/services/failure_classifier.py "
            "src/scheduler/failure_classifier.py"
        )


class TestBackendReExportGuard:
    """#1088: subscription_auto_switch must re-export `is_auth_failure` from
    the shared module and must NOT redefine it (or the duplicated tables), so
    the single source of truth holds and consumers' patch targets stay valid."""

    @pytest.fixture
    def source(self):
        return _SUBSCRIPTION_AUTO_SWITCH.read_text(encoding="utf-8")

    def test_reexports_from_failure_classifier(self, source):
        assert "from services.failure_classifier import is_auth_failure" in source, (
            "subscription_auto_switch.py must re-export is_auth_failure from "
            "services.failure_classifier (#1088)"
        )

    def test_does_not_redefine_classifier(self, source):
        assert "def is_auth_failure" not in source, (
            "subscription_auto_switch.py must NOT redefine is_auth_failure — it "
            "now lives in services.failure_classifier (#1088)"
        )
        assert "AUTH_INDICATORS = [" not in source, (
            "subscription_auto_switch.py must not redefine AUTH_INDICATORS (#1088)"
        )
        assert "NON_AUTH_KILL_MARKERS = [" not in source, (
            "subscription_auto_switch.py must not redefine NON_AUTH_KILL_MARKERS (#1088)"
        )


# -----------------------------------------------------------------------------
# error_classifier (AGENT runtime) — separate classifier; kill-safety is by
# PRECEDENCE, not a kill-marker short-circuit.
# -----------------------------------------------------------------------------


def _load_classifier():
    import importlib.util

    # error_classifier imports `..utils.credential_sanitizer`. Skip if the
    # agent-server tree isn't on the path (local dev w/o image build).
    base_image = _REPO_ROOT / "docker" / "base-image"
    if not (base_image / "agent_server" / "services" / "error_classifier.py").exists():
        pytest.skip("agent_server tree not present")
    if str(base_image) not in sys.path:
        sys.path.insert(0, str(base_image))
    try:
        return importlib.import_module("agent_server.services.error_classifier")
    except ImportError as e:
        pytest.skip(f"error_classifier import failed: {e}")


class TestAgentClassifierKillSafetyContract:
    """D4 (#1088): the AGENT-runtime classifier
    (`error_classifier._is_auth_failure_message`) is intentionally NOT the
    shared backend/scheduler classifier — it uses token-state markers and,
    crucially, has NO kill-marker short-circuit. This documents that
    divergence honestly: a kill+auth message classifies True here BY DESIGN.

    The agent is kept kill-safe NOT by this function but by precedence —
    `_classify_signal_exit` runs BEFORE the auth check on the live exit path
    (pinned by `test_signal_classification_takes_precedence` and
    `TestChatPathSignalExitWireUp`). Merging it into the shared table is out of
    scope: separate Docker build context + semantic divergence (#1088 / #945)."""

    @pytest.fixture
    def classifier(self):
        return _load_classifier()

    def test_agent_classifier_has_no_kill_short_circuit_by_design(self, classifier):
        # A message carrying BOTH an OOM-kill marker AND an auth marker
        # ("credentials expired") classifies True here — the agent function
        # has no NON_AUTH_KILL_MARKERS short-circuit, unlike the shared
        # backend/scheduler classifier. This asymmetry is intentional; the
        # agent path stays kill-safe via _classify_signal_exit precedence.
        assert classifier._is_auth_failure_message("OOM killed: credentials expired") is True, (
            "agent _is_auth_failure_message is expected to be kill-blind by "
            "design; it is kept kill-safe by _classify_signal_exit precedence, "
            "NOT by a kill-marker short-circuit (#1088 D4)"
        )

    def test_shared_classifier_short_circuits_the_same_message(self):
        # Contrast: the SHARED backend/scheduler classifier DOES short-circuit
        # the very same message to False (the kill-marker wins). This is the
        # behavioural divergence that the agent's precedence contract compensates
        # for — the two classifiers must not be naively merged.
        is_auth_failure = _load_backend_is_auth_failure()
        assert is_auth_failure("OOM killed: credentials expired") is False


# -----------------------------------------------------------------------------
# error_classifier._diagnose_exit_failure — OAuth-only branch must NOT
# return a string that `_is_auth_failure_message` matches.
# -----------------------------------------------------------------------------


class TestDiagnoseExitFailureOauthOnlyBranch:
    """Issue #904: when the agent has OAuth but no API key and the
    subprocess exits non-zero with empty stderr, the diagnostic string
    must not be classifiable as an auth failure."""

    @pytest.fixture
    def classifier(self):
        return _load_classifier()

    def test_oauth_only_diagnosis_not_auth(self, classifier, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        msg = classifier._diagnose_exit_failure(return_code=1, metadata=None)

        # New wording (positive): the message tells the operator the
        # likely causes without falsely declaring token expiry.
        assert "OOM kill" in msg or "OOM" in msg
        assert "timeout" in msg.lower() or "timeout_seconds" in msg

        # Negative: must NOT contain any phrase that
        # `_is_auth_failure_message` matches.
        assert not classifier._is_auth_failure_message(msg), (
            f"Diagnose output still trips auth detection: {msg!r}"
        )

    def test_signal_classification_takes_precedence(self, classifier):
        """`_classify_signal_exit` must return a 504 for exit code -9
        (SIGKILL) without consulting the auth heuristics. Regression
        for the path that produced the false-positive on cgroup OOM."""
        result = classifier._classify_signal_exit(return_code=-9, metadata=None)
        assert result is not None
        status_code, detail = result
        assert status_code == 504
        assert "SIGKILL" in detail
        # And the detail string itself must carry the marker so the
        # auto-switch matcher correctly identifies it as a non-auth event.
        assert any(
            marker in detail.lower()
            for marker in ("sigkill", "terminated by", "exit code -9")
        )

    def test_signal_classification_handles_shell_encoded_137(self, classifier):
        result = classifier._classify_signal_exit(return_code=137, metadata=None)
        assert result is not None
        status_code, detail = result
        assert status_code == 504
        assert "SIGKILL" in detail


# -----------------------------------------------------------------------------
# Static wire-up regression: chat path (`claude_code.py`) must call
# `_classify_signal_exit` BEFORE `_diagnose_exit_failure`. Symmetric with
# the headless path; without this, OOM kills on /api/chat produced the
# false "Subscription token may be expired" diagnostic.
# -----------------------------------------------------------------------------


class TestChatPathSignalExitWireUp:
    """The headless executor was already correct (#516). The chat path
    was not — this is the #904 fix surface."""

    def test_chat_path_imports_classify_signal_exit(self):
        src = (
            _REPO_ROOT / "docker" / "base-image" / "agent_server"
            / "services" / "claude_code.py"
        ).read_text(encoding="utf-8")
        assert "_classify_signal_exit" in src, (
            "claude_code.py must import _classify_signal_exit (#904)"
        )

    def test_chat_path_calls_signal_classifier_before_diagnose(self):
        src = (
            _REPO_ROOT / "docker" / "base-image" / "agent_server"
            / "services" / "claude_code.py"
        ).read_text(encoding="utf-8")
        # Find the `if return_code != 0:` block and prove the signal
        # classifier call appears in it BEFORE any fallback to
        # `_diagnose_exit_failure`.
        idx_block = src.find("if return_code != 0:")
        # Match the call site (`name(`), not the surrounding comments —
        # the docstring above the block also mentions `_diagnose_exit_failure`
        # by name and would otherwise be picked up first.
        idx_classify = src.find("_classify_signal_exit(", idx_block)
        idx_diagnose = src.find("_diagnose_exit_failure(", idx_block)
        assert idx_block != -1, "chat error block not found"
        assert idx_classify != -1, (
            "`_classify_signal_exit` must be called in chat error block "
            "(#904 — without this, SIGKILL produces a false 'token expired')"
        )
        assert idx_diagnose != -1, "diagnose fallback should still exist"
        assert idx_classify < idx_diagnose, (
            "signal classifier must run BEFORE the diagnose fallback — "
            "otherwise the fallback's 'Subscription token...' string "
            "fires on every OOM"
        )
