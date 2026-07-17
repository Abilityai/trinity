"""Unit tests for agent Docker-volume reclamation (#1581).

`agent-{name}-{workspace|public|shared}` volumes were leaked forever — nothing
removed them at soft-delete, cascade, or the retention hard-purge. These tests
pin the fix:

- the destructive removal is DOUBLE-GUARDED (name AND label) and fail-closed,
- NotFound / in-use (APIError) are tolerated (no raise; retry next sweep),
- the retention purge sweep removes the purged agent's volumes,
- the orphan sweep reclaims volumes with no ownership row, respects the
  recovery window (soft-deleted rows still reserved) and a creation grace.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import docker
import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend = str(_project_root / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)


def _load_docker_utils():
    """Fresh docker_utils with a mocked docker_client (no daemon)."""
    mock_client = Mock()
    with patch.dict(
        "sys.modules",
        {"services.docker_service": Mock(docker_client=mock_client)},
    ):
        # Loaded via spec into a fresh module object (not `import`), so there's
        # no `services.docker_utils` sys.modules entry to clear — and the #762
        # lint bans bare sys.modules mutation anyway.
        spec = importlib.util.spec_from_file_location(
            "docker_utils", f"{_backend}/services/docker_utils.py"
        )
        mod = importlib.util.module_from_spec(spec)
        mod.docker_client = mock_client
        spec.loader.exec_module(mod)
    return mod, mock_client


def _volume(name, agent_name="ag1", platform="agent-workspace", created="2020-01-01T00:00:00Z"):
    v = Mock()
    v.name = name
    labels = {}
    if agent_name is not None:
        labels["trinity.agent-name"] = agent_name
    if platform is not None:
        labels["trinity.platform"] = platform
    v.attrs = {"Labels": labels, "CreatedAt": created}
    v.remove = Mock()
    return v


# --------------------------------------------------------------------------- #
# Guard (double-guard: name AND label). Fail-closed.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestGuard:
    def setup_method(self):
        self.du, _ = _load_docker_utils()

    def test_valid_workspace_volume_is_reclaimable(self):
        v = _volume("agent-ag1-workspace", "ag1", "agent-workspace")
        assert self.du.is_reclaimable_agent_volume(v, "ag1") is True

    def test_public_and_shared_platforms_accepted(self):
        assert self.du.is_reclaimable_agent_volume(
            _volume("agent-ag1-public", "ag1", "agent-public"), "ag1") is True
        assert self.du.is_reclaimable_agent_volume(
            _volume("agent-ag1-shared", "ag1", "agent-shared"), "ag1") is True

    def test_name_mismatch_refused(self):
        # right label, wrong name (not one of the 3 suffixes)
        v = _volume("agent-ag1-scratch", "ag1", "agent-workspace")
        assert self.du.is_reclaimable_agent_volume(v, "ag1") is False

    def test_label_agent_name_mismatch_refused(self):
        # name says ag1 but the label belongs to ag2 → refuse (bug shield)
        v = _volume("agent-ag1-workspace", "ag2", "agent-workspace")
        assert self.du.is_reclaimable_agent_volume(v, "ag1") is False

    def test_wrong_platform_label_refused(self):
        v = _volume("agent-ag1-workspace", "ag1", "some-other-platform")
        assert self.du.is_reclaimable_agent_volume(v, "ag1") is False

    def test_missing_labels_refused(self):
        v = _volume("agent-ag1-workspace", agent_name=None, platform=None)
        assert self.du.is_reclaimable_agent_volume(v, "ag1") is False

    def test_empty_agent_name_refused(self):
        v = _volume("agent-ag1-workspace", "ag1", "agent-workspace")
        assert self.du.is_reclaimable_agent_volume(v, "") is False


# --------------------------------------------------------------------------- #
# remove_agent_volumes — happy path + tolerance
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRemoveAgentVolumes:
    def setup_method(self):
        self.du, self.client = _load_docker_utils()

    async def _run(self, get_side_effect):
        self.client.volumes.get.side_effect = get_side_effect
        return await self.du.remove_agent_volumes("ag1")

    @pytest.mark.asyncio
    async def test_removes_all_three_volumes(self):
        vols = {
            "agent-ag1-workspace": _volume("agent-ag1-workspace", "ag1", "agent-workspace"),
            "agent-ag1-public": _volume("agent-ag1-public", "ag1", "agent-public"),
            "agent-ag1-shared": _volume("agent-ag1-shared", "ag1", "agent-shared"),
        }
        removed = await self._run(lambda n: vols[n])
        assert removed == 3
        for v in vols.values():
            v.remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_notfound_tolerated(self):
        removed = await self._run(docker.errors.NotFound("gone"))
        assert removed == 0  # no volume, no raise

    @pytest.mark.asyncio
    async def test_in_use_apierror_tolerated_and_skipped(self):
        v = _volume("agent-ag1-workspace", "ag1", "agent-workspace")
        v.remove.side_effect = docker.errors.APIError("volume is in use")
        only_ws = {"agent-ag1-workspace": v}

        def get(n):
            if n in only_ws:
                return only_ws[n]
            raise docker.errors.NotFound("gone")

        removed = await self._run(get)
        assert removed == 0          # in-use volume not counted
        v.remove.assert_called_once()  # attempted, error swallowed

    @pytest.mark.asyncio
    async def test_guard_refuses_mislabeled_volume(self):
        # A volume at the expected NAME but whose label points at another agent
        # must never be removed (durable-agent data shield).
        v = _volume("agent-ag1-workspace", "victim-agent", "agent-workspace")
        only = {"agent-ag1-workspace": v}

        def get(n):
            if n in only:
                return only[n]
            raise docker.errors.NotFound("gone")

        removed = await self._run(get)
        assert removed == 0
        v.remove.assert_not_called()


# --------------------------------------------------------------------------- #
# Cleanup-service orchestration: purge sweep + orphan sweep
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCleanupOrchestration:
    def _service(self):
        from services.cleanup_service import CleanupService

        svc = CleanupService(poll_interval=300)
        svc._reconcile_orphaned_executions = AsyncMock(return_value=(0, 0, set()))
        svc._process_stale_slot_reclaims = AsyncMock(return_value=None)
        return svc

    @pytest.mark.asyncio
    async def test_purge_sweep_removes_volumes_for_purged_agents(self):
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = self._service()
        report = CleanupReport()

        db = MagicMock()
        db.get_setting_value.side_effect = lambda k, d=None: (
            "180" if k == "agent_soft_delete_retention_days" else d
        )
        db.find_soft_deleted_agents_past_retention.return_value = ["ag1", "ag2"]
        db.purge_agent_ownership.return_value = True
        # #1664: un-renamed agents — volume base == agent name (one call each).
        db.get_volume_base_name.side_effect = lambda n: n

        removed = AsyncMock(return_value=2)  # 2 volumes per agent
        with patch.object(cs, "db", db), \
             patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), \
             patch("services.docker_utils.remove_agent_volumes", removed):
            await svc._sweep_soft_deleted_agents(report)

        assert report.soft_deleted_agents_purged == 2
        assert report.agent_volumes_removed == 4  # 2 agents × 2 volumes
        assert removed.await_count == 2

    @pytest.mark.asyncio
    async def test_purge_volume_failure_does_not_block_purge(self):
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = self._service()
        report = CleanupReport()
        db = MagicMock()
        db.get_setting_value.side_effect = lambda k, d=None: (
            "180" if k == "agent_soft_delete_retention_days" else d
        )
        db.find_soft_deleted_agents_past_retention.return_value = ["ag1"]
        db.purge_agent_ownership.return_value = True
        db.get_volume_base_name.side_effect = lambda n: n

        with patch.object(cs, "db", db), \
             patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), \
             patch("services.docker_utils.remove_agent_volumes",
                   AsyncMock(side_effect=RuntimeError("docker down"))):
            await svc._sweep_soft_deleted_agents(report)

        # Purge still counted even though volume removal raised.
        assert report.soft_deleted_agents_purged == 1
        assert report.agent_volumes_removed == 0

    @pytest.mark.asyncio
    async def test_orphan_sweep_reclaims_only_rowless_past_grace(self):
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = self._service()
        report = CleanupReport()

        # 3 candidate volumes:
        #  - orphan-old: no row, created long ago  -> reclaim
        #  - live-agent: has row                    -> skip (recoverable)
        #  - orphan-new: no row but just created    -> skip (grace)
        vols = [
            _volume("agent-orphan-old-workspace", "orphan-old", "agent-workspace",
                    created="2020-01-01T00:00:00Z"),
            _volume("agent-live-agent-workspace", "live-agent", "agent-workspace",
                    created="2020-01-01T00:00:00Z"),
            _volume("agent-orphan-new-workspace", "orphan-new", "agent-workspace",
                    created="2999-01-01T00:00:00Z"),
        ]

        db = MagicMock()
        # #1664: ownership is resolved by volume base, not by agent name — a
        # renamed agent owns volumes named after its former self.
        db.is_volume_base_reserved.side_effect = lambda base: base == "live-agent"

        async def fake_remove(name):
            return 1

        with patch.object(cs, "db", db), \
             patch("services.docker_utils.list_agent_data_volumes",
                   AsyncMock(return_value=vols)), \
             patch("services.docker_utils.list_attached_volume_names",
                   AsyncMock(return_value=set())), \
             patch("services.docker_utils.remove_agent_volumes",
                   AsyncMock(side_effect=fake_remove)) as rm:
            # #1664: reclaim needs a streak of unattached cycles, so the
            # recreate window (a live volume momentarily unmounted) can't be
            # mistaken for an orphan.
            for _ in range(cs.ORPHAN_VOLUME_UNATTACHED_STRIKES):
                await svc._sweep_orphan_agent_volumes(report)

        rm.assert_awaited_once_with("orphan-old")
        assert report.orphan_agent_volumes_reclaimed == 1

    @pytest.mark.asyncio
    async def test_orphan_sweep_empty_is_noop(self):
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = self._service()
        report = CleanupReport()
        with patch("services.docker_utils.list_agent_data_volumes",
                   AsyncMock(return_value=[])):
            await svc._sweep_orphan_agent_volumes(report)
        assert report.orphan_agent_volumes_reclaimed == 0
