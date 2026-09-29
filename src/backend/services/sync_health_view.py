"""One call, one answer: an agent's sync-health view (trinity-enterprise#706).

Every backend surface that shows sync health goes through :func:`sync_view`,
which feeds the persisted `agent_sync_state` row and the `agent_git_config`
flags to `sync_freeze_policy.classify`. The policy module is a stdlib leaf
(the scheduler vendors it), so the two backend-only inputs are resolved here:

- `push_denied` — `git_service.is_push_denied(last_error_summary)` (#2107),
  which turns a refused push into the `credential is read-only` recommendation;
- the git-config model — read into the plain dict `classify` takes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services import git_service
from services.sync_freeze_policy import classify

# The computed fields every surface adds next to the row it already returns.
VIEW_FIELDS = (
    "state",
    "reason",
    "recommendation",
    "binding",
    "work_agent",
    "divergence_age_s",
    "dirty_age_s",
    "sync_failing",
    "freeze",
    "freeze_cause",
    "freeze_reason",
    "stale_observation",
)

_CONFIG_FIELDS = (
    "source_mode",
    "auto_sync_enabled",
    "freeze_schedules_if_sync_failing",
    "created_at",
)


def _config_dict(config: Any) -> Dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return {key: config.get(key) for key in _CONFIG_FIELDS}
    return {key: getattr(config, key, None) for key in _CONFIG_FIELDS}


def sync_view(
    row: Optional[Dict[str, Any]],
    config: Any,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The policy verdict for one agent, restricted to `VIEW_FIELDS`.

    `row` is its `agent_sync_state` row or None; `config` its
    `AgentGitConfig` (or a dict with the same fields) or None when the agent
    has no git binding.
    """
    error = (row or {}).get("last_error_summary") or ""
    verdict = classify(
        row,
        _config_dict(config),
        now or datetime.now(timezone.utc),
        push_denied=git_service.is_push_denied(error),
    )
    return {key: verdict[key] for key in VIEW_FIELDS}
