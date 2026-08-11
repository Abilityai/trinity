"""Schedule and execution management database operations.

Handles schedule CRUD, execution tracking, and Git configuration.

`ScheduleOperations` is composed here from ten concern-scoped mixins — the
sanctioned `db/agent_settings/` shape (Invariant #2). The facade import path
`from db.schedules import ScheduleOperations` is preserved by this package
re-export, so `database.py` and `db/__init__.py` are unchanged.

MRO contract (why there are NO import edges between the mixin *files*):
every cross-slice reference is a runtime ``self.<method>()`` call that resolves
via this composed class's MRO — never a module import. The complete cross-slice
`self.` edge map:
  - crud.create_schedule            -> self._generate_id (common), self._calculate_next_run_at (crud)
  - webhooks.get_schedule_by_webhook_token -> self._row_to_schedule (crud)
  - analytics.get_schedule_analytics -> self.get_schedule (crud)
  - cleanup.get_executions_pending_validation / get_validation_execution -> self._row_to_schedule_execution (executions)
  - cleanup.create_validation_execution / git_config.create_git_config /
    executions.create_task_execution / create_schedule_execution -> self._generate_id (common)
  - git_config.get_git_config / list_git_enabled_agents -> self._row_to_git_config (git_config)
The only intra-package import is ``from ._common import _norm_ts`` (the sole
cross-slice module-global, a bare function that can NOT resolve via MRO), pulled
by executions/analytics/stats. ``_generate_id`` is an MRO-shared static on
``ScheduleCommonMixin``. The mixin-file dependency graph is therefore acyclic.

``__init__`` is defined on the composed class (NOT a mixin) so ``self._user_ops``
/ ``self._agent_ops`` are set once and reachable by every mixin.
"""

from ._common import ScheduleCommonMixin, _norm_ts
from .crud import ScheduleCrudMixin
from .webhooks import ScheduleWebhooksMixin
from .executions import ScheduleExecutionsMixin
from .queue import ScheduleQueueMixin
from .cleanup import ScheduleCleanupMixin
from .analytics import (
    ScheduleAnalyticsMixin,
    _TRIGGER_BUCKETS,
    _BUCKET_ORDER,
    _OTHER_BUCKET,
    _bucket_for_trigger,
)
from .stats import ScheduleStatsMixin
from .git_config import ScheduleGitConfigMixin
from .retention import ScheduleRetentionMixin


class ScheduleOperations(
    ScheduleCommonMixin,
    ScheduleCrudMixin,
    ScheduleWebhooksMixin,
    ScheduleExecutionsMixin,
    ScheduleQueueMixin,
    ScheduleCleanupMixin,
    ScheduleAnalyticsMixin,
    ScheduleStatsMixin,
    ScheduleGitConfigMixin,
    ScheduleRetentionMixin,
):
    """Schedule and execution database operations."""

    def __init__(self, user_ops, agent_ops):
        """Initialize with references to user and agent operations."""
        self._user_ops = user_ops
        self._agent_ops = agent_ops


__all__ = [
    "ScheduleOperations",
    "_norm_ts",
    "_TRIGGER_BUCKETS",
    "_BUCKET_ORDER",
    "_OTHER_BUCKET",
    "_bucket_for_trigger",
]
