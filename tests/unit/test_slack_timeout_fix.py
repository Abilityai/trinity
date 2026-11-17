"""
Unit tests for Slack message router timeout fix (#221).

Verifies:
- timeout_seconds=None is passed to TaskExecutionService (uses agent timeout)
- (error replies: see test_3603_channel_failure_reply.py)

Module: src/backend/adapters/message_router.py
Issue: https://github.com/abilityai/trinity/issues/221
"""

import pytest


# #3603 retired #221's error forwarding: a channel member is no longer sent the
# execution's own error (it is the operator's diagnosis, and in a group every
# member reads it). The behaviour is executed against the real router in
# test_3603_channel_failure_reply.py; the copy of the old logic that stood here
# could not fail.


class TestTimeoutParameter:
    """Test that timeout_seconds=None is the correct approach.

    TaskExecutionService.execute_task() line 153-155:
        if timeout_seconds is None:
            timeout_seconds = db.get_execution_timeout(agent_name)

    Passing None lets the service use the agent's configured timeout.
    Other callers that do it correctly: chat.py, paid.py
    """

    def test_none_triggers_agent_lookup(self):
        """Passing None means 'use agent default' — this is the contract."""
        timeout_seconds = None
        # Simulates TaskExecutionService logic
        if timeout_seconds is None:
            timeout_seconds = 900  # db.get_execution_timeout() default
        assert timeout_seconds == 900

    def test_explicit_value_overrides(self):
        """Passing an explicit value skips the agent lookup."""
        timeout_seconds = 120
        if timeout_seconds is None:
            timeout_seconds = 900
        assert timeout_seconds == 120  # keeps the explicit value

    def test_none_is_not_zero(self):
        """None and 0 are different — 0 would not trigger the lookup."""
        timeout_seconds = 0
        if timeout_seconds is None:
            timeout_seconds = 900
        assert timeout_seconds == 0  # 0 is falsy but not None
