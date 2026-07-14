"""#1081 B1 — pull-mode env-key set + de-pilot clear (unit).

The recreate merge in ``services/agent_service/lifecycle.py`` must POP the
managed pull keys (``PULL_MODE_ENV_KEYS``) BEFORE re-applying
``pull_mode_env_vars`` — otherwise a bare ``.update()`` for a de-piloted agent
(whose ``pull_mode_env_vars`` returns ``{}``) leaves a stale
``TRINITY_PULL_MODE=true`` baked in and the agent-side worker keeps pulling after
de-pilot. These are pure-logic proofs of that pop+re-apply idiom over the exported
``PULL_MODE_ENV_KEYS`` / ``pull_mode_env_vars`` seam; the CLAIM-seam consumer
backstop is proven at the endpoint layer in test_1081_pull_endpoints.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# src/backend on the path (conftest also does this; keep the file self-sufficient).
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


class TestPullModeEnvKeyPop:
    def test_env_keys_are_exactly_the_two_pull_knobs(self):
        from services.agent_service.pull_mode import PULL_MODE_ENV_KEYS

        # The set lifecycle.py pops is exactly the two non-secret pull knobs —
        # never the master internal secret (least-privilege, #307/#1159).
        assert PULL_MODE_ENV_KEYS == (
            "TRINITY_PULL_MODE",
            "TRINITY_MAX_PARALLEL_TASKS",
        )

    def test_depilot_merge_clears_baked_pull_knobs(self, monkeypatch):
        """No pilots → pull_mode_env_vars('foo') == {} → the pop is what actually
        clears the previously-baked pull knobs; unrelated env is untouched."""
        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        from services.agent_service.pull_mode import (
            PULL_MODE_ENV_KEYS,
            pull_mode_env_vars,
        )

        env = {
            "TRINITY_PULL_MODE": "true",
            "TRINITY_MAX_PARALLEL_TASKS": "5",
            "OTHER": "x",
        }
        # The lifecycle.py recreate merge: pop the managed keys, THEN re-apply.
        for k in PULL_MODE_ENV_KEYS:
            env.pop(k, None)
        env.update(pull_mode_env_vars("foo"))

        assert "TRINITY_PULL_MODE" not in env
        assert "TRINITY_MAX_PARALLEL_TASKS" not in env
        assert env["OTHER"] == "x"                       # untouched

    def test_repilot_merge_restores_pull_knobs(self, monkeypatch):
        """Piloted → the same pop+re-apply merge leaves both pull knobs PRESENT
        (TRINITY_PULL_MODE=='true'); unrelated env still untouched."""
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "foo")
        from services.agent_service.pull_mode import (
            PULL_MODE_ENV_KEYS,
            pull_mode_env_vars,
        )

        env = {
            "TRINITY_PULL_MODE": "true",
            "TRINITY_MAX_PARALLEL_TASKS": "5",
            "OTHER": "x",
        }
        for k in PULL_MODE_ENV_KEYS:
            env.pop(k, None)
        with patch(
            "services.settings_service.get_effective_max_parallel_tasks",
            return_value=7,
        ):
            env.update(pull_mode_env_vars("foo"))

        assert env["TRINITY_PULL_MODE"] == "true"
        assert env["TRINITY_MAX_PARALLEL_TASKS"] == "7"
        assert env["OTHER"] == "x"
