"""#1971 — Codex subscription auth must survive Trinity's dispatch.

`_execute_codex` set **both** `OPENAI_API_KEY` and `CODEX_API_KEY` from the same
resolved value, described in-source as harmless defence ("some Codex builds also
read CODEX_API_KEY"). It is not harmless: the mere **presence** of
`CODEX_API_KEY` flips the Codex CLI into API-key auth mode and makes it discard a
valid subscription `auth.json`. Every subscription agent therefore 401'd against
api.openai.com, retried 5x and failed the turn — while the same `auth.json`
worked when the CLI was invoked directly. Through `/task` that was the *only*
reachable behaviour, so those agents could not complete a single dispatch.

The reporter's own repro exposes a **second** defect they did not file. Their
working case is described as "only a placeholder `OPENAI_API_KEY` set" — a
placeholder, because `_execute_codex` raised 503 before invoking the CLI at all
whenever no API key was found, and a ChatGPT-plan container has no API key by
design. A credential that exists purely to satisfy a check is the check being
wrong, so the gate now accepts a subscription `auth.json` as what it is.

Both halves are needed for the issue's actual goal. Fixing only the reported one
leaves subscription agents working *provided they know to set a fake key*.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))

try:
    from agent_server.services import codex_runtime
except ImportError:  # pragma: no cover - agent-server deps required
    codex_runtime = None

pytestmark = pytest.mark.skipif(
    codex_runtime is None, reason="agent-server deps unavailable"
)


def _clear_key_env(monkeypatch):
    for var in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Source tracking — which name the key actually came from.
# ---------------------------------------------------------------------------


def test_source_is_openai_when_that_is_what_was_set(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
    assert codex_runtime._load_api_key_with_source() == ("sk-1", "OPENAI_API_KEY")


def test_source_is_codex_when_that_is_what_was_set(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("CODEX_API_KEY", "ck-1")
    assert codex_runtime._load_api_key_with_source() == ("ck-1", "CODEX_API_KEY")


def test_source_is_reported_for_a_key_parsed_out_of_dotenv(monkeypatch, tmp_path):
    """The cold-start path: `.env` is on disk but not exported into the process."""
    _clear_key_env(monkeypatch)
    monkeypatch.setattr(codex_runtime, "_AGENT_HOME", str(tmp_path))
    (tmp_path / ".env").write_text('CODEX_API_KEY="ck-from-file"\n')
    assert codex_runtime._load_api_key_with_source() == (
        "ck-from-file", "CODEX_API_KEY",
    )


def test_no_key_anywhere_reports_no_source(monkeypatch, tmp_path):
    _clear_key_env(monkeypatch)
    monkeypatch.setattr(codex_runtime, "_AGENT_HOME", str(tmp_path))
    assert codex_runtime._load_api_key_with_source() == (None, None)


def test_the_value_wrapper_is_unchanged(monkeypatch):
    """`_load_openai_api_key` is the seam existing tests monkeypatch, and
    `_execute_codex` still calls it. Splitting the source lookup must not move
    that seam — otherwise every existing patch silently stops applying, which is
    exactly what my first attempt did (127 Codex tests went red)."""
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-2")
    assert codex_runtime._load_openai_api_key() == "sk-2"


# ---------------------------------------------------------------------------
# Subscription detection.
# ---------------------------------------------------------------------------


def test_subscription_auth_detected(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"id": "x"}}))
    assert codex_runtime._has_subscription_auth(str(tmp_path)) is True


def test_missing_auth_json_is_not_subscription(tmp_path):
    assert codex_runtime._has_subscription_auth(str(tmp_path)) is False


def test_empty_auth_json_is_not_subscription(tmp_path):
    """A zero-byte file is a half-written mount, not a credential. Treating it
    as one would swap a clear 503 for an opaque CLI failure."""
    (tmp_path / "auth.json").write_text("")
    assert codex_runtime._has_subscription_auth(str(tmp_path)) is False


def test_a_directory_named_auth_json_is_not_subscription(tmp_path):
    (tmp_path / "auth.json").mkdir()
    assert codex_runtime._has_subscription_auth(str(tmp_path)) is False


def test_detection_does_not_validate_the_token(tmp_path):
    """Deliberately existence-only. Validating is the CLI's job, and a stale
    token should surface as the CLI's own auth error — not as a Trinity 503
    claiming no credentials are configured, which would be a false statement
    about a subscription agent."""
    (tmp_path / "auth.json").write_text("this is not even json")
    assert codex_runtime._has_subscription_auth(str(tmp_path)) is True


def test_unreadable_codex_home_is_not_subscription(tmp_path):
    assert codex_runtime._has_subscription_auth(str(tmp_path / "nope")) is False


# ---------------------------------------------------------------------------
# The env the child process actually receives — the reported defect.
# ---------------------------------------------------------------------------


def _captured_env(monkeypatch, tmp_path, *, key, source_env):
    """Run `_execute_codex` far enough to capture the env it builds.

    Stops at Popen — the parse/drain machinery is covered by
    `test_codex_runtime.py`; what matters here is purely which credential
    variables cross into the child.
    """
    captured = {}

    _clear_key_env(monkeypatch)
    for name, value in source_env.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: key)
    monkeypatch.setattr(codex_runtime, "_ensure_codex_home", lambda: str(tmp_path))
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: False)
    monkeypatch.setattr(codex_runtime, "_load_guardrails", lambda: {})
    # #2208: auth materialisation spawns `codex login` BEFORE `codex exec`, so
    # without this the first Popen captured here is the login, not the turn —
    # this helper would then assert credential forwarding against the wrong
    # process. What crosses into the CHILD is what #1971 is about.
    monkeypatch.setattr(codex_runtime, "_login_with_api_key", lambda h, k: (True, ""))

    class _Stop(RuntimeError):
        pass

    def _fake_popen(cmd, **kwargs):
        captured.update(kwargs.get("env") or {})
        raise _Stop()

    monkeypatch.setattr(codex_runtime.subprocess, "Popen", _fake_popen)

    runtime = codex_runtime.CodexRuntime()
    import asyncio

    with pytest.raises(Exception):
        asyncio.run(
            runtime._execute_codex(
                prompt="hi", model=None, system_prompt=None,
                resume_thread_id=None, timeout_seconds=5,
                allowed_tools=None, execution_id="e1",
            )
        )
    return captured


def test_codex_api_key_is_not_synthesized_from_an_openai_key(monkeypatch, tmp_path):
    """THE bug. The key was supplied as OPENAI_API_KEY; Trinity invented a
    CODEX_API_KEY from it, and that invention is what discarded auth.json."""
    env = _captured_env(
        monkeypatch, tmp_path, key="sk-real", source_env={"OPENAI_API_KEY": "sk-real"}
    )
    assert env.get("OPENAI_API_KEY") == "sk-real"
    assert "CODEX_API_KEY" not in env, (
        "CODEX_API_KEY is still synthesized — its presence flips the Codex CLI "
        "into API-key auth and discards a subscription auth.json (#1971)"
    )


def test_codex_api_key_is_forwarded_when_the_operator_set_it(monkeypatch, tmp_path):
    """The only case the original 'defensive' rationale actually covers: the
    operator supplied the key UNDER THAT NAME. Dropping it here would be a
    different regression."""
    env = _captured_env(
        monkeypatch, tmp_path, key="ck-real", source_env={"CODEX_API_KEY": "ck-real"}
    )
    assert env.get("CODEX_API_KEY") == "ck-real"
    assert env.get("OPENAI_API_KEY") == "ck-real"


def test_no_key_variables_are_invented_for_a_subscription_agent(monkeypatch, tmp_path):
    """A subscription container has no API key. Neither variable may be
    fabricated — an empty or placeholder value under either name is what breaks
    auth.json in the first place."""
    (tmp_path / "auth.json").write_text('{"tokens": {}}')
    env = _captured_env(monkeypatch, tmp_path, key=None, source_env={})
    assert "CODEX_API_KEY" not in env
    assert not env.get("OPENAI_API_KEY")


def test_codex_home_is_still_exported(monkeypatch, tmp_path):
    """Unchanged behaviour — the relocation (#1098) must survive the refactor."""
    env = _captured_env(
        monkeypatch, tmp_path, key="sk-x", source_env={"OPENAI_API_KEY": "sk-x"}
    )
    assert env.get("CODEX_HOME") == str(tmp_path)


# ---------------------------------------------------------------------------
# The gate — the second defect, visible in the reporter's own repro.
# ---------------------------------------------------------------------------


def _run_execute(monkeypatch, tmp_path, *, key):
    import asyncio

    _clear_key_env(monkeypatch)
    monkeypatch.setattr(codex_runtime, "_load_openai_api_key", lambda: key)
    monkeypatch.setattr(codex_runtime, "_ensure_codex_home", lambda: str(tmp_path))
    monkeypatch.setattr(codex_runtime, "_is_read_only", lambda: False)
    monkeypatch.setattr(codex_runtime, "_load_guardrails", lambda: {})

    def _fake_popen(cmd, **kwargs):
        raise RuntimeError("reached the CLI")

    monkeypatch.setattr(codex_runtime.subprocess, "Popen", _fake_popen)

    runtime = codex_runtime.CodexRuntime()
    return asyncio.run(
        runtime._execute_codex(
            prompt="hi", model=None, system_prompt=None, resume_thread_id=None,
            timeout_seconds=5, allowed_tools=None, execution_id="e1",
        )
    )


def test_subscription_auth_alone_is_enough_to_dispatch(monkeypatch, tmp_path):
    """The second defect: a ChatGPT-plan agent has no API key BY DESIGN, and the
    old gate 503'd it before the CLI ran. The reporter's working setup needed a
    placeholder key purely to get past this check."""
    from fastapi import HTTPException

    (tmp_path / "auth.json").write_text('{"tokens": {"id": "x"}}')

    with pytest.raises(Exception) as exc:
        _run_execute(monkeypatch, tmp_path, key=None)

    assert not isinstance(exc.value, HTTPException), (
        "a subscription agent still 503s before the CLI is invoked (#1971)"
    )
    assert "reached the CLI" in str(exc.value)


def test_no_credentials_at_all_still_fails_fast(monkeypatch, tmp_path):
    """The gate must not become a no-op. With neither an API key nor an
    auth.json there is genuinely nothing to authenticate with, and failing at
    dispatch is better than a confusing CLI error."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _run_execute(monkeypatch, tmp_path, key=None)

    assert exc.value.status_code == 503


def test_the_503_names_both_ways_to_fix_it(monkeypatch, tmp_path):
    """The old message said only "inject OPENAI_API_KEY", which sent a
    subscription user toward the placeholder workaround that caused this
    issue."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _run_execute(monkeypatch, tmp_path, key=None)

    detail = str(exc.value.detail)
    assert "OPENAI_API_KEY" in detail
    assert "auth.json" in detail
    assert str(tmp_path) in detail, "the message should name the CODEX_HOME searched"


def test_an_api_key_alone_still_dispatches(monkeypatch, tmp_path):
    """No-regression: the API-key path is untouched."""
    with pytest.raises(RuntimeError, match="reached the CLI"):
        _run_execute(monkeypatch, tmp_path, key="sk-real")
