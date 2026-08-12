"""Session-tab runtime gate (#1187 Phase H).

The cached-UUID ``--resume`` turn is gated so a Codex agent runs a stateless
turn instead. The gate must:
  * recognize codex as a non-resume runtime,
  * leave Claude (and Gemini, in the MVP) resume-capable,
  * fail safe (assume resume-capable) on any Docker lookup hiccup.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path


# ent#358: the gate moved from routers/sessions.py into the shared
# services/session_turn_service.py, because Workspace chat runs the same engine.
# The load-in-isolation trick is unchanged and still needed: `from services
# import ...` would execute enough of the package that a sibling unit test's
# stubbed `services.agent_service` (installed in sys.modules at collection time)
# breaks this module's collection under `-p randomly` — the #1187 regression-diff
# failure. Exec'ing the leaf module directly sidesteps that; its absolute imports
# resolve via sys.path (conftest puts src/backend on it).
def _load_turn_service() -> types.ModuleType:
    for base in (
        Path(__file__).resolve().parents[2] / "src" / "backend",  # host / CI
        Path("/app"),  # trinity-backend container
    ):
        path = base / "services" / "session_turn_service.py"
        if path.exists():
            spec = importlib.util.spec_from_file_location(
                "session_turn_service_under_test", str(path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module
    raise RuntimeError("Cannot locate services/session_turn_service.py")


sessions = _load_turn_service()


def _status(runtime):
    return types.SimpleNamespace(runtime=runtime)


def test_codex_in_no_resume_constant():
    assert "codex" in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME


def test_supports_resume_false_for_codex(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("codex")
    )
    assert sessions.supports_session_resume("a") is False


def test_supports_resume_true_for_claude(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("claude-code")
    )
    assert sessions.supports_session_resume("a") is True


def test_supports_resume_true_for_gemini_in_mvp(monkeypatch):
    """Only codex is gated in the MVP — Gemini keeps its (existing) Session tab."""
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container", lambda c: _status("gemini-cli")
    )
    assert sessions.supports_session_resume("a") is True


def test_supports_resume_true_when_container_missing(monkeypatch):
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: None)
    assert sessions.supports_session_resume("a") is True


def test_supports_resume_defaults_true_on_lookup_failure(monkeypatch):
    def _boom(name):
        raise RuntimeError("docker socket down")

    monkeypatch.setattr(sessions, "get_agent_container", _boom)
    # Must not raise, and must fail safe to resume-capable.
    assert sessions.supports_session_resume("a") is True
