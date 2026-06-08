"""
Tests for Issue #904: SIGKILL / OOM / timeout must not be misclassified as
an auth failure that triggers SUB-003 subscription auto-switch.

Three regression surfaces:

1. `subscription_auto_switch.is_auth_failure` short-circuits to False when
   the message contains an unambiguous signal-kill / OOM / timeout marker,
   even if an AUTH_INDICATOR substring also happens to match.
2. `src/scheduler/service.py:_is_auth_failure` mirrors the same exclusion
   list (the scheduler runs in a separate container and can't import
   from backend.services — see the comment on `_NON_AUTH_KILL_MARKERS`).
3. `error_classifier._diagnose_exit_failure` no longer returns the
   "Subscription token may be expired" string for the OAuth-only-config
   branch, so its output cannot loop back through
   `_is_auth_failure_message` and surface as a 503 auth failure on
   `headless_executor.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO_ROOT / "src" / "backend"
_SCHEDULER = _REPO_ROOT / "src" / "scheduler"


# `_load_backend_is_auth_failure` and the scheduler slice both stub
# heavy backend imports (`database`, `db_models`, apscheduler.*) via
# `sys.modules[name] = stub` so the pure-function code under test
# exec's without pulling in the real `DatabaseManager()` / APScheduler
# objects. The sanctioned snapshot/restore pattern (precedent:
# `tests/unit/test_agent_cleanup_parity.py`) tells
# `tests/lint_sys_modules.py:_has_stubbed_module_names_helper` to
# exempt this file from the no-bare-`sys.modules`-mutation lint.
_STUBBED_MODULE_NAMES = [
    "database",
    "db_models",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
    "apscheduler.executors",
    "apscheduler.executors.asyncio",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# -----------------------------------------------------------------------------
# subscription_auto_switch.is_auth_failure  — backend side
# -----------------------------------------------------------------------------


def _load_backend_is_auth_failure():
    """Load `is_auth_failure` directly from the source file by path.

    Avoids importing the whole `services.subscription_auto_switch` module,
    which pulls in `database` (real `db = DatabaseManager()` init) — heavy
    and not needed for the pure-function test.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_iaf_backend",
        str(_BACKEND / "services" / "subscription_auto_switch.py"),
    )
    if spec is None or spec.loader is None:
        pytest.skip("backend source not available")
    # The module's top-level `from database import db` would trigger heavy
    # backend init. Stub `database` to a no-op so the file's pure
    # functions are exec'd without side effects.
    if "database" not in sys.modules:
        stub_db = type(sys)("database")
        stub_db.db = type("_Db", (), {})()
        sys.modules["database"] = stub_db
    if "db_models" not in sys.modules:
        stub_models = type(sys)("db_models")

        class _NotificationCreate:  # pragma: no cover — just a placeholder
            pass

        stub_models.NotificationCreate = _NotificationCreate
        sys.modules["db_models"] = stub_models

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.is_auth_failure


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
# scheduler._is_auth_failure  — scheduler-side copy must stay in sync
# -----------------------------------------------------------------------------


def _load_scheduler_is_auth_failure():
    import importlib.util

    # Stub the scheduler's heavy imports so we can exec just the module.
    # The function is at module scope, so spec exec works even with the
    # SchedulerService class body present below it.
    for stub_name in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio",
                      "apscheduler.triggers", "apscheduler.triggers.cron",
                      "apscheduler.executors", "apscheduler.executors.asyncio"):
        if stub_name not in sys.modules:
            sys.modules[stub_name] = type(sys)(stub_name)

    # Grab the function via a pure ast-eval approach: read the file, exec
    # just the top-level constants + function definitions we need.
    src = (_SCHEDULER / "service.py").read_text(encoding="utf-8")
    # Slice from the `_AUTH_INDICATORS = [` line through the end of
    # `_is_auth_failure` — keep the test independent of other top-level
    # symbols that pull in apscheduler etc.
    start_marker = "_AUTH_INDICATORS = ["
    end_marker = "class SchedulerService:"
    if start_marker not in src or end_marker not in src:
        pytest.skip("scheduler service.py layout changed")
    snippet = src[src.index(start_marker): src.index(end_marker)]

    ns: dict = {}
    exec(compile(snippet, "<scheduler-slice>", "exec"), ns)  # noqa: S102
    return ns["_is_auth_failure"]


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
# error_classifier._diagnose_exit_failure — OAuth-only branch must NOT
# return a string that `_is_auth_failure_message` matches.
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
