"""#3870: execution-owned images survive unconfirmed termination."""
from unittest.mock import Mock
import pytest
from agent_server.services.process_registry import ProcessRegistry
pytestmark = pytest.mark.unit

@pytest.mark.parametrize("release", ["unregister", "cleanup_finished", "list_running"])
def test_cleanup_waits_for_observed_exit_and_runs_once_outside_lock(release):
    registry = ProcessRegistry()
    process = Mock()
    process.poll.return_value = None
    observed = []
    def cleanup():
        acquired = registry._lock.acquire(blocking=False)
        assert acquired, "resource cleanup must not hold the registry lock"
        registry._lock.release()
        observed.append(process.poll())
    registry.register("image-turn", process, on_exit=cleanup)
    registry.unregister("image-turn")
    registry.cleanup_finished()
    assert observed == []
    process.poll.return_value = 0
    if release == "unregister":
        registry.unregister("image-turn")
    elif release == "cleanup_finished":
        assert registry.cleanup_finished() == 1
    else:
        registry.list_running()
    registry.unregister("image-turn")
    registry.cleanup_finished()
    assert observed == [0]

def test_failing_cleanup_does_not_keep_completed_process_registered():
    registry = ProcessRegistry()
    process = Mock()
    process.poll.return_value = 0
    cleanup = Mock(side_effect=OSError("disk unavailable"))
    registry.register("image-turn", process, on_exit=cleanup)
    registry.unregister("image-turn")
    assert not registry.is_execution_running("image-turn")
    cleanup.assert_called_once_with()

def test_reused_execution_id_does_not_release_an_older_live_child_resources():
    registry = ProcessRegistry()
    first, second = Mock(), Mock()
    first.poll.return_value = None
    second.poll.return_value = 0
    first_cleanup, second_cleanup = Mock(), Mock()
    registry.register("same-id", first, on_exit=first_cleanup)
    registry.register("same-id", second, on_exit=second_cleanup)
    registry.unregister("same-id")
    second_cleanup.assert_called_once_with()
    first_cleanup.assert_not_called()
    first.poll.return_value = 0
    registry.list_running()
    first_cleanup.assert_called_once_with()


def test_terminate_already_finished_releases_resources():
    registry = ProcessRegistry()
    process = Mock()
    process.poll.return_value = 0
    cleanup = Mock()
    registry.register("image-turn", process, on_exit=cleanup)
    assert registry.terminate("image-turn")["reason"] == "already_finished"
    cleanup.assert_called_once_with()
