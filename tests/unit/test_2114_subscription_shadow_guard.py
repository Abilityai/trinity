"""#2114 — subscription auth must not be shadowed by `.env`-resident API keys.

The bug: #1999 made every spawn env re-read `.env`, which made the file a
second authoritative source for `ANTHROPIC_API_KEY` — a key Claude Code
prefers over `CLAUDE_CODE_OAUTH_TOKEN`. A stale key on a subscription-backed
agent's persistent volume therefore shadowed the subscription token at every
spawn, and SUB-003 mis-attributed the identical auth failure to each healthy
subscription in turn.

These tests pin the fix's three properties:

  1. `arm_subscription_auth_guard()` — boot-time force-unset of API-key-style
     Claude auth, armed ONLY when the container baseline carries a truthy
     `CLAUDE_CODE_OAUTH_TOKEN` on a Claude runtime (restart-durable: the
     rotated override-file token is exported by startup.sh BEFORE the server
     launches, so it is always baseline);
  2. every no-arm path is untouched — `.env`-only API-key auth, platform-key
     agents, non-Claude runtimes, operator-managed `.env` auth;
  3. the suppression is visible as data — `env_drift_report` marks force-unset
     keys `suppressed_for_spawn` (including keys absent from `.env`), and the
     per-key WARNING memo re-arms when a key is removed and re-added.

Module loaded standalone by path (the agent server ships in its own image and
cannot import `src/backend`), matching `test_1999_execution_env.py`.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = (
    _ROOT / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
)

pytestmark = pytest.mark.unit

# The keys these tests reason about — scoped rather than clearing the whole
# environ, so PATH/HOME stay real for anything else running in-process, and a
# dev shell exporting ANTHROPIC_API_KEY can't flake the matrix.
_CONTROLLED_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "AGENT_RUNTIME",
)


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Snapshot/restore the CONTENTS of os.environ (never the object) — the
    test_1999 idiom; see its docstring for why replacing the mapping is
    hostile to concurrent threads."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _load(monkeypatch, baseline: dict):
    """Fresh module copy under a controlled baseline (test_1999 idiom).
    Re-importing per test also resets the module-level override dict and the
    #2114 suppression-warn memo, so no state leaks between rows."""
    spec = importlib.util.spec_from_file_location(
        f"_execution_env_2114_{len(sys.modules)}", _MODULE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.INITIAL_ENV = dict(baseline)
    for key in _CONTROLLED_KEYS:
        if key in baseline:
            os.environ[key] = baseline[key]
        else:
            os.environ.pop(key, None)
    return mod


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / ".env"


# ---------------------------------------------------------------------------
# The arm: trigger conditions
# ---------------------------------------------------------------------------

class TestArmTrigger:

    def test_baseline_token_arms_and_suppresses_env_api_key(
        self, monkeypatch, env_file, caplog
    ):
        """The reported bug, fixed: subscription agent + stale .env key →
        the key never reaches the spawn env; the token does."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-good"})
        assert mod.arm_subscription_auth_guard() is True
        env_file.write_text('ANTHROPIC_API_KEY="sk-ant-api-stale"\n')

        with caplog.at_level(logging.WARNING):
            env = mod.build_execution_env(env_file=env_file)

        assert "ANTHROPIC_API_KEY" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-good"
        warned = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "ANTHROPIC_API_KEY" in r.getMessage()
        ]
        assert len(warned) == 1
        # Names only — the stale value must never reach a log line.
        assert "sk-ant-api-stale" not in caplog.text

    def test_auth_token_spelling_is_the_same_shadow_class(
        self, monkeypatch, env_file
    ):
        """Claude Code honors ANTHROPIC_AUTH_TOKEN with the same
        key-over-OAuth precedence — it rides along in the arm."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        mod.arm_subscription_auth_guard()
        env_file.write_text('ANTHROPIC_AUTH_TOKEN="sk-ant-auth-stale"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_arm_is_idempotent_and_diagnostics_visible(self, monkeypatch):
        """Unlike an in-function guard, the arm is inspectable state:
        runtime_overrides() shows the force-unset. Arming twice is a no-op."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        assert mod.arm_subscription_auth_guard() is True
        assert mod.arm_subscription_auth_guard() is True

        overrides = mod.runtime_overrides()
        assert overrides["ANTHROPIC_API_KEY"] is None
        assert overrides["ANTHROPIC_AUTH_TOKEN"] is None

    def test_empty_baseline_token_does_not_arm(self, monkeypatch, env_file):
        """Truthiness, not presence: the platform-key create path can bake an
        empty-string value. Arming on it would drop the .env key while
        providing no working token — a total auth outage."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": ""})
        assert mod.arm_subscription_auth_guard() is False
        env_file.write_text('ANTHROPIC_API_KEY="sk-ant-api-works"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert env["ANTHROPIC_API_KEY"] == "sk-ant-api-works"

    def test_non_claude_runtime_does_not_arm(self, monkeypatch, env_file):
        """A vestigial subscription token baked into a pre-#1187 Gemini/Codex
        container must not strip a .env key the agent's own scripts may use —
        on those runtimes the key never shadows anything."""
        mod = _load(monkeypatch, {
            "CLAUDE_CODE_OAUTH_TOKEN": "vestigial",
            "AGENT_RUNTIME": "gemini-cli",
        })
        assert mod.arm_subscription_auth_guard() is False
        env_file.write_text('ANTHROPIC_API_KEY="for-agent-scripts"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert env["ANTHROPIC_API_KEY"] == "for-agent-scripts"


# ---------------------------------------------------------------------------
# No-arm paths stay untouched
# ---------------------------------------------------------------------------

class TestNoArmPaths:

    def test_terminal_auth_agent_env_key_passes(self, monkeypatch, env_file):
        """No baseline auth at all (terminal-login agent): .env API-key auth
        keeps working exactly as before."""
        mod = _load(monkeypatch, {})
        assert mod.arm_subscription_auth_guard() is False
        env_file.write_text('ANTHROPIC_API_KEY="sk-ant-api-mine"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert env["ANTHROPIC_API_KEY"] == "sk-ant-api-mine"

    def test_platform_key_agent_file_value_still_wins(self, monkeypatch, env_file):
        """Platform-key agent (baseline API key, no OAuth): guard inert, and
        the .env value beats the baseline — file-authoritative merge, pinned
        so the test asserts WHICH value survives, not just presence."""
        mod = _load(monkeypatch, {"ANTHROPIC_API_KEY": "baked-platform-key"})
        assert mod.arm_subscription_auth_guard() is False
        env_file.write_text('ANTHROPIC_API_KEY="from-env-file"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert env["ANTHROPIC_API_KEY"] == "from-env-file"

    def test_operator_managed_env_auth_untouched(self, monkeypatch, env_file):
        """OAuth token + API key both supplied via .env with no baseline auth:
        operator-managed arrangement — .env-sourced OAuth never arms the
        guard, both values pass (pre-existing behavior)."""
        mod = _load(monkeypatch, {})
        assert mod.arm_subscription_auth_guard() is False
        env_file.write_text(
            'CLAUDE_CODE_OAUTH_TOKEN="file-oauth"\nANTHROPIC_API_KEY="file-key"\n'
        )

        env = mod.build_execution_env(env_file=env_file)

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "file-oauth"
        assert env["ANTHROPIC_API_KEY"] == "file-key"


# ---------------------------------------------------------------------------
# Override-layer semantics
# ---------------------------------------------------------------------------

class TestOverrideLayerSemantics:

    def test_hot_reload_window_without_baseline(self, monkeypatch, env_file):
        """The L1 path: no baseline token (fresh process pre-restart), the
        reload endpoint arms the same overrides — .env key suppressed, rotated
        token delivered."""
        mod = _load(monkeypatch, {})
        mod.set_runtime_override("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-rotated")
        mod.set_runtime_override("ANTHROPIC_API_KEY", None)
        env_file.write_text('ANTHROPIC_API_KEY="sk-ant-api-stale"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert "ANTHROPIC_API_KEY" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-rotated"

    def test_extra_beats_the_arm(self, monkeypatch, env_file):
        """`extra` is the last layer by contract (#407 EXECUTION_TAG_NAME must
        never be displaced) — the arm is merge-layer only and must not eat a
        caller-supplied key. Pins the codex_runtime post-build pattern too."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        mod.arm_subscription_auth_guard()

        env = mod.build_execution_env(
            extra={"ANTHROPIC_API_KEY": "from-extra"}, env_file=env_file
        )

        assert env["ANTHROPIC_API_KEY"] == "from-extra"

    def test_explicit_non_none_override_beats_the_arm(self, monkeypatch, env_file):
        """A later explicit override replaces the arm's force-unset — last
        write wins within the override dict. Pinned so the semantics are a
        decision, not an accident."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        mod.arm_subscription_auth_guard()
        mod.set_runtime_override("ANTHROPIC_API_KEY", "explicit-later")

        env = mod.build_execution_env(env_file=env_file)

        assert env["ANTHROPIC_API_KEY"] == "explicit-later"


# ---------------------------------------------------------------------------
# Observability: the suppression is data, not just a log line
# ---------------------------------------------------------------------------

class TestObservability:

    def test_memo_warns_once_then_rearms_on_readd(self, monkeypatch, env_file, caplog):
        """Per-key memo with active invalidation: repeated builds warn once,
        but a key removed from .env and later re-added warns AGAIN — a
        process-global bool would go silent forever."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        mod.arm_subscription_auth_guard()

        def warnings_for_key():
            return [
                r for r in caplog.records
                if r.levelno == logging.WARNING
                and "suppresses ANTHROPIC_API_KEY" in r.getMessage()
            ]

        with caplog.at_level(logging.WARNING):
            env_file.write_text('ANTHROPIC_API_KEY="stale"\n')
            mod.build_execution_env(env_file=env_file)
            mod.build_execution_env(env_file=env_file)
            assert len(warnings_for_key()) == 1

            env_file.write_text("")  # key removed → memo invalidated
            mod.build_execution_env(env_file=env_file)
            assert len(warnings_for_key()) == 1

            env_file.write_text('ANTHROPIC_API_KEY="re-added"\n')
            mod.build_execution_env(env_file=env_file)
            assert len(warnings_for_key()) == 2

    def test_mirror_stays_unguarded_and_drift_report_tells_the_truth(
        self, monkeypatch, env_file
    ):
        """The deliberate split: sync_process_env still mirrors the file
        truthfully (the error classifier and sanitizer read os.environ), while
        env_drift_report carries suppressed_for_spawn so the one surface built
        to expose file/spawn divergence cannot show all-green over an active
        suppression — the #1999 failure mode, inverted. A future 'cleanup'
        that guards the mirror or drops the marker fails here."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        mod.arm_subscription_auth_guard()
        env_file.write_text('ANTHROPIC_API_KEY="stale"\n')

        mod.sync_process_env(env_file=env_file)
        assert os.environ["ANTHROPIC_API_KEY"] == "stale"  # mirror = file truth

        report = {row["key"]: row for row in mod.env_drift_report(env_file=env_file)}
        api_row = report["ANTHROPIC_API_KEY"]
        assert api_row["in_file"] is True
        assert api_row["in_process_env"] is True
        assert api_row["suppressed_for_spawn"] is True

        # The widened iteration set: a force-unset key absent from .env and
        # never mirrored still appears — without it the report structurally
        # could not mention ANTHROPIC_AUTH_TOKEN at all.
        auth_row = report["ANTHROPIC_AUTH_TOKEN"]
        assert auth_row["in_file"] is False
        assert auth_row["suppressed_for_spawn"] is True

    def test_unsuppressed_keys_report_false(self, monkeypatch, env_file):
        """The marker is per-key: an ordinary .env credential reports
        suppressed_for_spawn=False."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        mod.arm_subscription_auth_guard()
        env_file.write_text('GOOGLE_API_KEY="unrelated"\n')

        report = {row["key"]: row for row in mod.env_drift_report(env_file=env_file)}
        assert report["GOOGLE_API_KEY"]["suppressed_for_spawn"] is False


# ---------------------------------------------------------------------------
# Known residual, pinned so it can't be undiscovered
# ---------------------------------------------------------------------------

class TestKnownResidual:

    def test_stale_env_oauth_still_beats_rotated_baseline_token(
        self, monkeypatch, env_file
    ):
        """DELIBERATELY UNFIXED (#2114 follow-up 1): a stale
        CLAUDE_CODE_OAUTH_TOKEN in .env still overrides the rotated baseline
        token after a restart — same shadow class, one key over, but the fix
        shape is a precedence carve-out (baseline must WIN), not a force-unset
        (which would remove Claude auth entirely). This test pins the current
        behavior as a known, named residual; when the follow-up lands, flip
        this assertion rather than being surprised by it."""
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "rotated-fresh"})
        mod.arm_subscription_auth_guard()
        env_file.write_text('CLAUDE_CODE_OAUTH_TOKEN="stale-from-file"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "stale-from-file"
