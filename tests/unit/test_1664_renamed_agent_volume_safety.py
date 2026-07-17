"""Unit tests for renamed-agent volume safety in the #1581 orphan sweep (#1664).

Rename keeps the agent's Docker data volumes: the volume name AND its immutable
`trinity.agent-name` label both stay at the PRE-rename name. The #1581 orphan
sweep read that stale identity as ownership truth, found no agent by that name,
and force-removed what is actually the LIVE agent's `/home/developer` — a silent,
unrecoverable loss of #1169 `data_paths` data the moment a container recreate
left the volume briefly unattached.

These tests pin the fix, layer by layer:
- ownership is resolved from the DB (`is_volume_base_reserved`), not from the
  volume's self-description,
- rename pins `volume_base_name` atomically, and freezes it across re-renames,
- the sweep refuses to touch a mounted volume, and skips the whole cycle when
  Docker can't say what is mounted (fail-closed),
- an unattached sighting only counts as an orphan after N consecutive cycles
  (the recreate race),
- agents renamed before the column existed are healed from Docker's mounts.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend = str(_project_root / "src" / "backend")
while _backend in sys.path:
    sys.path.remove(_backend)
sys.path.insert(0, _backend)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401


def _load_docker_utils():
    """Fresh docker_utils with a mocked docker_client (no daemon)."""
    mock_client = Mock()
    with patch.dict(
        "sys.modules",
        {"services.docker_service": Mock(docker_client=mock_client)},
    ):
        spec = importlib.util.spec_from_file_location(
            "docker_utils", f"{_backend}/services/docker_utils.py"
        )
        mod = importlib.util.module_from_spec(spec)
        mod.docker_client = mock_client
        spec.loader.exec_module(mod)
    return mod, mock_client


def _volume(name, agent_name, created="2020-01-01T00:00:00Z"):
    v = Mock()
    v.name = name
    v.attrs = {
        "Labels": {
            "trinity.agent-name": agent_name,
            "trinity.platform": "agent-workspace",
        },
        "CreatedAt": created,
    }
    return v


def _container(name, mounts):
    c = Mock()
    c.name = name
    c.attrs = {"Mounts": mounts}
    return c


def _vol_mount(volume_name, dest="/home/developer"):
    return {"Type": "volume", "Name": volume_name, "Destination": dest}


class _Svc:
    """CleanupService with the unrelated sweeps stubbed out."""

    @staticmethod
    def build():
        from services.cleanup_service import CleanupService

        svc = CleanupService(poll_interval=300)
        svc._reconcile_orphaned_executions = AsyncMock(return_value=(0, 0, set()))
        svc._process_stale_slot_reclaims = AsyncMock(return_value=None)
        return svc


class TestDockerMountHelpers:
    @pytest.mark.asyncio
    async def test_attached_volume_names_spans_all_containers(self):
        mod, client = _load_docker_utils()
        client.containers.list.return_value = [
            _container("agent-a", [_vol_mount("agent-a-workspace")]),
            # A non-agent container's mount counts too: in-use is in-use.
            _container("some-other", [_vol_mount("agent-old-name-workspace")]),
            _container("no-mounts", []),
        ]

        names = await mod.list_attached_volume_names()

        assert names == {"agent-a-workspace", "agent-old-name-workspace"}
        # all=True — a stopped container still owns its volume.
        assert client.containers.list.call_args.kwargs["all"] is True

    @pytest.mark.asyncio
    async def test_bind_mounts_are_not_volume_names(self):
        mod, client = _load_docker_utils()
        client.containers.list.return_value = [
            _container("agent-a", [
                {"Type": "bind", "Source": "/data", "Destination": "/data"},
                _vol_mount("agent-a-workspace"),
            ])
        ]
        assert await mod.list_attached_volume_names() == {"agent-a-workspace"}

    @pytest.mark.asyncio
    async def test_attached_volume_names_fails_closed_on_docker_error(self):
        mod, client = _load_docker_utils()
        client.containers.list.side_effect = RuntimeError("docker down")

        # None = "unknown", never an empty set (which would read as
        # "nothing is mounted" and green-light every removal).
        assert await mod.list_attached_volume_names() is None

    @pytest.mark.asyncio
    async def test_workspace_volume_map_reads_renamed_container(self):
        mod, client = _load_docker_utils()
        client.containers.list.return_value = [
            # The rename fingerprint: container is `agent-new-name`, mount is
            # still the pre-rename volume.
            _container("agent-new-name", [_vol_mount("agent-old-name-workspace")]),
            _container("agent-plain", [_vol_mount("agent-plain-workspace")]),
        ]

        assert await mod.get_agent_workspace_volume_map() == {
            "new-name": "agent-old-name-workspace",
            "plain": "agent-plain-workspace",
        }

    @pytest.mark.asyncio
    async def test_workspace_volume_map_ignores_non_home_mounts(self):
        mod, client = _load_docker_utils()
        client.containers.list.return_value = [
            _container("agent-a", [
                _vol_mount("agent-a-public", dest="/home/developer/public"),
                _vol_mount("agent-a-workspace"),
            ])
        ]
        assert await mod.get_agent_workspace_volume_map() == {
            "a": "agent-a-workspace"
        }

    def test_volume_base_parsing(self):
        mod, _ = _load_docker_utils()
        assert mod.volume_base_from_workspace_volume("agent-foo-workspace") == "foo"
        assert mod.volume_base_from_workspace_volume("agent-a-b-workspace") == "a-b"
        assert mod.volume_base_from_workspace_volume("agent-foo-public") is None
        assert mod.volume_base_from_workspace_volume("redis-data") is None
        assert mod.volume_base_from_workspace_volume("agent--workspace") is None


class TestOrphanSweepRenameSafety:
    async def _run_cycles(self, svc, report, *, db, vols, attached, n=1):
        import services.cleanup_service as cs

        rm = AsyncMock(return_value=1)
        with patch.object(cs, "db", db), \
             patch("services.docker_utils.list_agent_data_volumes",
                   AsyncMock(return_value=vols)), \
             patch("services.docker_utils.list_attached_volume_names",
                   AsyncMock(return_value=attached)), \
             patch("services.docker_utils.remove_agent_volumes", rm):
            for _ in range(n):
                await svc._sweep_orphan_agent_volumes(report)
        return rm

    @pytest.mark.asyncio
    async def test_renamed_agents_volume_is_never_reclaimed(self):
        """THE bug: pinned base ⇒ the stale-named volume has an owner."""
        from services.cleanup_service import CleanupReport

        svc = _Svc.build()
        vols = [_volume("agent-old-name-workspace", "old-name")]

        db = MagicMock()
        # No agent is *named* old-name any more...
        db.is_agent_name_reserved.side_effect = lambda n: n == "new-name"
        # ...but new-name owns the volumes based on old-name.
        db.is_volume_base_reserved.side_effect = lambda base: base == "old-name"

        # Even with the container gone (mid-recreate) and many cycles.
        rm = await self._run_cycles(
            svc, CleanupReport(), db=db, vols=vols, attached=set(), n=5
        )

        rm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mounted_volume_is_never_reclaimed_even_if_unowned(self):
        """Docker-as-truth backstop: in use ⇒ someone's live data."""
        from services.cleanup_service import CleanupReport

        svc = _Svc.build()
        vols = [_volume("agent-old-name-workspace", "old-name")]
        db = MagicMock()
        # Worst case: the DB pin is missing (heal never ran).
        db.is_volume_base_reserved.return_value = False

        rm = await self._run_cycles(
            svc,
            CleanupReport(),
            db=db,
            vols=vols,
            attached={"agent-old-name-workspace"},
            n=5,
        )

        rm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_skipped_when_mounts_unknown(self):
        """Fail-closed: can't prove unused ⇒ don't delete."""
        from services.cleanup_service import CleanupReport

        svc = _Svc.build()
        db = MagicMock()
        db.is_volume_base_reserved.return_value = False

        rm = await self._run_cycles(
            svc,
            CleanupReport(),
            db=db,
            vols=[_volume("agent-gone-workspace", "gone")],
            attached=None,
            n=5,
        )

        rm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_orphan_needs_consecutive_unattached_cycles(self):
        """The recreate race: one unattached sighting is not an orphan."""
        from services.cleanup_service import CleanupReport
        from services.cleanup_service import ORPHAN_VOLUME_UNATTACHED_STRIKES as K

        svc = _Svc.build()
        report = CleanupReport()
        vols = [_volume("agent-gone-workspace", "gone")]
        db = MagicMock()
        db.is_volume_base_reserved.return_value = False

        rm = await self._run_cycles(
            svc, report, db=db, vols=vols, attached=set(), n=K - 1
        )
        rm.assert_not_awaited()

        rm = await self._run_cycles(svc, report, db=db, vols=vols, attached=set())
        rm.assert_awaited_once_with("gone")
        assert report.orphan_agent_volumes_reclaimed == 1

    @pytest.mark.asyncio
    async def test_attached_sighting_resets_the_streak(self):
        """A recreate gap on cycle 3 of 3 must not accumulate into a delete."""
        from services.cleanup_service import CleanupReport
        from services.cleanup_service import ORPHAN_VOLUME_UNATTACHED_STRIKES as K

        svc = _Svc.build()
        report = CleanupReport()
        vols = [_volume("agent-flappy-workspace", "flappy")]
        db = MagicMock()
        db.is_volume_base_reserved.return_value = False

        for _ in range(4):
            # Recreate window (unattached) ... K-1 times, never reaching K ...
            await self._run_cycles(
                svc, report, db=db, vols=vols, attached=set(), n=K - 1
            )
            # ... then the new container comes up and the streak dies.
            rm = await self._run_cycles(
                svc, report, db=db, vols=vols,
                attached={"agent-flappy-workspace"},
            )
            rm.assert_not_awaited()

        assert report.orphan_agent_volumes_reclaimed == 0

    @pytest.mark.asyncio
    async def test_strike_state_does_not_leak_for_vanished_volumes(self):
        from services.cleanup_service import CleanupReport

        svc = _Svc.build()
        db = MagicMock()
        db.is_volume_base_reserved.return_value = False

        await self._run_cycles(
            svc,
            CleanupReport(),
            db=db,
            vols=[_volume("agent-gone-workspace", "gone")],
            attached=set(),
        )
        assert "agent-gone-workspace" in svc._unattached_volume_strikes

        # Volume disappears (removed elsewhere) → its strike record goes too.
        await self._run_cycles(
            svc,
            CleanupReport(),
            db=db,
            vols=[_volume("agent-other-workspace", "other")],
            attached=set(),
        )
        assert "agent-gone-workspace" not in svc._unattached_volume_strikes


class TestPurgeUsesTheVolumeBase:
    @pytest.mark.asyncio
    async def test_purge_removes_the_renamed_agents_real_volumes(self):
        """The pin must be read BEFORE the purge deletes the row holding it."""
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = _Svc.build()
        report = CleanupReport()

        db = MagicMock()
        db.get_setting_value.side_effect = lambda k, d=None: (
            "180" if k == "agent_soft_delete_retention_days" else d
        )
        db.find_soft_deleted_agents_past_retention.return_value = ["new-name"]
        db.get_volume_base_name.return_value = "old-name"
        db.purge_agent_ownership.return_value = True
        db.is_volume_base_reserved.return_value = False  # no other row claims them

        rm = AsyncMock(return_value=3)
        with patch.object(cs, "db", db), \
             patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), \
             patch("services.docker_utils.remove_agent_volumes", rm):
            await svc._sweep_soft_deleted_agents(report)

        # Purging `new-name` must drop BOTH identities: the workspace kept the
        # pre-rename base, while public/shared (which name off the live agent
        # name) are under the current one. Dropping only one leaks the other.
        assert [c.args[0] for c in rm.await_args_list] == ["old-name", "new-name"]
        assert report.agent_volumes_removed == 6

    @pytest.mark.asyncio
    async def test_purge_skips_a_base_another_agent_still_claims(self):
        """`volume_base_name` has no unique constraint, and installs predating
        the create-time gate can hold a collision: agent `new` pinned to base
        `old` PLUS a live agent named `old`, sharing the volumes. Purging the
        first must not delete the second's live data."""
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = _Svc.build()
        report = CleanupReport()

        db = MagicMock()
        db.get_setting_value.side_effect = lambda k, d=None: (
            "180" if k == "agent_soft_delete_retention_days" else d
        )
        db.find_soft_deleted_agents_past_retention.return_value = ["new-name"]
        db.get_volume_base_name.return_value = "old-name"
        db.purge_agent_ownership.return_value = True
        # This row is already purged, so True = a DIFFERENT row claims `old-name`
        # (the live agent that reused the freed name); `new-name` is unclaimed.
        db.is_volume_base_reserved.side_effect = lambda base: base == "old-name"

        rm = AsyncMock(return_value=1)
        with patch.object(cs, "db", db), \
             patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), \
             patch("services.docker_utils.remove_agent_volumes", rm):
            await svc._sweep_soft_deleted_agents(report)

        # `old-name` skipped (someone's live data); `new-name` still reclaimed.
        assert [c.args[0] for c in rm.await_args_list] == ["new-name"]
        assert report.soft_deleted_agents_purged == 1

    @pytest.mark.asyncio
    async def test_purge_falls_back_to_agent_name(self):
        from services.cleanup_service import CleanupReport
        import services.cleanup_service as cs

        svc = _Svc.build()
        db = MagicMock()
        db.get_setting_value.side_effect = lambda k, d=None: (
            "180" if k == "agent_soft_delete_retention_days" else d
        )
        db.find_soft_deleted_agents_past_retention.return_value = ["plain"]
        db.purge_agent_ownership.return_value = True
        # A DB hiccup reading the pin must not strand the volumes.
        db.get_volume_base_name.side_effect = RuntimeError("db down")
        db.is_volume_base_reserved.return_value = False

        rm = AsyncMock(return_value=1)
        with patch.object(cs, "db", db), \
             patch("services.agent_runtime_state.clear_agent_runtime_state", AsyncMock()), \
             patch("services.docker_utils.remove_agent_volumes", rm):
            await svc._sweep_soft_deleted_agents(CleanupReport())

        # Un-renamed: both identities are the same name, so it stays ONE call.
        rm.assert_awaited_once_with("plain")


class TestVolumeIdentityDb:
    """The predicate + the rename pin, on the real schema (both backends)."""

    @pytest.fixture
    def agent_ops(self, db_backend):
        try:
            from db.agents import AgentOperations
            from db.users import UserOperations
        except ImportError:  # pragma: no cover - env guard
            pytest.skip("backend venv required (no `db.agents` import)")
        return AgentOperations(UserOperations())

    def _seed(self, name, deleted_at=None):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
            "VALUES (:n, 1, :ts, :d)",
            n=name, ts="2026-01-01T00:00:00Z", d=deleted_at,
        )

    def test_unrenamed_agent_owns_its_own_base(self, agent_ops):
        self._seed("plain")
        # NULL volume_base_name ⇒ "same as agent_name": no backfill needed.
        assert agent_ops.is_volume_base_reserved("plain") is True
        assert agent_ops.get_volume_base_name("plain") == "plain"
        assert agent_ops.is_volume_base_reserved("nobody") is False
        assert agent_ops.is_volume_base_reserved("") is False

    def test_rename_pins_the_old_name_as_the_volume_base(self, agent_ops):
        self._seed("old-name")
        assert agent_ops.rename_agent("old-name", "new-name") is True

        # The volume `agent-old-name-workspace` still belongs to someone...
        assert agent_ops.is_volume_base_reserved("old-name") is True
        # ...namely new-name.
        assert agent_ops.get_volume_base_name("new-name") == "old-name"
        # The predicate the sweep used before #1664 is exactly the trap.
        assert agent_ops.is_agent_name_reserved("old-name") is False

        # ...AND it still owns its CURRENT name: `get_public_volume_name` /
        # `get_shared_volume_name` name off the LIVE agent name, so enabling
        # file-sharing after a rename creates `agent-new-name-public`. Ownership
        # is a union of both identities — anything less deletes those.
        assert agent_ops.is_volume_base_reserved("new-name") is True

    def test_second_rename_keeps_the_original_base(self, agent_ops):
        self._seed("v1")
        agent_ops.rename_agent("v1", "v2")
        agent_ops.rename_agent("v2", "v3")

        # The volumes never moved — the pin must not follow the name.
        assert agent_ops.get_volume_base_name("v3") == "v1"
        assert agent_ops.is_volume_base_reserved("v1") is True
        assert agent_ops.is_volume_base_reserved("v2") is False

    def test_renamed_agent_owns_volumes_under_both_bases(self, agent_ops):
        """A renamed agent's public/shared volumes are created under its CURRENT
        name (they name off the live name), while its workspace keeps the old
        base. Both are live data; the public one is unmounted whenever
        file-sharing is off, so the sweep's attached-check cannot save it —
        only the DB predicate can."""
        self._seed("old-name")
        agent_ops.rename_agent("old-name", "new-name")

        assert agent_ops.is_volume_base_reserved("old-name") is True   # workspace
        assert agent_ops.is_volume_base_reserved("new-name") is True   # public/shared
        assert agent_ops.is_volume_base_reserved("unrelated") is False

    def test_soft_deleted_renamed_agent_still_owns_its_volumes(self, agent_ops):
        """The recovery window (#834) applies to the renamed base too."""
        self._seed("old-name")
        agent_ops.rename_agent("old-name", "new-name")
        _hrun(
            "UPDATE agent_ownership SET deleted_at = :d WHERE agent_name = 'new-name'",
            d="2026-02-01T00:00:00Z",
        )
        assert agent_ops.is_volume_base_reserved("old-name") is True

    def test_purged_agent_releases_its_base(self, agent_ops):
        self._seed("old-name")
        agent_ops.rename_agent("old-name", "new-name")
        _hrun("DELETE FROM agent_ownership WHERE agent_name = 'new-name'")
        # Row gone ⇒ genuinely an orphan ⇒ reclaimable.
        assert agent_ops.is_volume_base_reserved("old-name") is False

    def test_set_volume_base_name_never_overwrites_a_pin(self, agent_ops):
        self._seed("old-name")
        agent_ops.rename_agent("old-name", "new-name")

        # The boot heal must not clobber the rename's pin with a later guess.
        assert agent_ops.set_volume_base_name("new-name", "wrong") is False
        assert agent_ops.get_volume_base_name("new-name") == "old-name"

    def test_set_volume_base_name_fills_an_unpinned_row(self, agent_ops):
        self._seed("healed")
        assert agent_ops.set_volume_base_name("healed", "pre-rename") is True
        assert agent_ops.get_volume_base_name("healed") == "pre-rename"
        assert agent_ops.set_volume_base_name("missing-agent", "x") is False
        assert agent_ops.get_volume_base_name("missing-agent") is None


class TestVolumeBaseHeal:
    @pytest.mark.asyncio
    async def test_heal_pins_base_for_agent_renamed_before_the_column(self):
        import services.cleanup_service as cs

        svc = _Svc.build()
        db = MagicMock()
        db.set_volume_base_name.return_value = True

        with patch.object(cs, "db", db), \
             patch("services.docker_utils.get_agent_workspace_volume_map",
                   AsyncMock(return_value={
                       "new-name": "agent-old-name-workspace",  # renamed
                       "plain": "agent-plain-workspace",        # never renamed
                   })):
            healed = await svc._heal_renamed_volume_bases()

        assert healed == 1
        db.set_volume_base_name.assert_called_once_with("new-name", "old-name")

    @pytest.mark.asyncio
    async def test_heal_survives_docker_failure(self):
        import services.cleanup_service as cs

        svc = _Svc.build()
        with patch.object(cs, "db", MagicMock()), \
             patch("services.docker_utils.get_agent_workspace_volume_map",
                   AsyncMock(side_effect=RuntimeError("docker down"))):
            assert await svc._heal_renamed_volume_bases() == 0


class TestCreateRefusesAReservedVolumeBase:
    """#1664: rename frees the NAME while the volumes stay. `crud.py`'s volume
    block is get-then-create (an existing volume is REUSED), so without a gate a
    new agent created under the freed name boots on the renamed agent's
    `/home/developer` — its `.env` included, across owners."""

    def test_gate_refuses_when_the_volume_base_is_still_owned(self):
        """The predicate the create gate must consult, on the real schema."""
        try:
            from db.agents import AgentOperations
            from db.users import UserOperations
        except ImportError:  # pragma: no cover - env guard
            pytest.skip("backend venv required")
        ops = AgentOperations(UserOperations())
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES ('victim', 1, '2026-01-01T00:00:00Z')"
        )
        ops.rename_agent("victim", "victim-renamed")

        # The name is free — this is exactly what fooled the create path...
        assert ops.is_agent_name_reserved("victim") is False
        # ...but `agent-victim-workspace` is still the renamed agent's home.
        assert ops.is_volume_base_reserved("victim") is True

    @pytest.mark.asyncio
    async def test_create_returns_409_for_a_renamed_agents_volume_base(self):
        import services.agent_service.crud as crud
        from fastapi import HTTPException

        db = MagicMock()
        db.get_agent_owner.return_value = None
        db.is_agent_name_reserved.return_value = False   # name freed by a rename
        db.is_volume_base_reserved.return_value = True   # volumes still owned

        config = MagicMock(name="cfg")
        config.name = "victim"
        config.ephemeral = False

        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await crud.create_agent_internal(config, MagicMock())

        assert exc.value.status_code == 409
        assert "data volumes" in str(exc.value.detail)
        # Fail BEFORE any container/volume work — no half-built agent.
        db.get_agents_by_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_ephemeral_creation_is_not_gated(self):
        """Ghosts are volume-less by construction, so there is nothing to
        collide with — and the gate must not put a DB read on the burst path."""
        import services.agent_service.crud as crud
        from fastapi import HTTPException

        db = MagicMock()
        db.get_agent_owner.return_value = None
        db.is_agent_name_reserved.return_value = False
        db.is_volume_base_reserved.return_value = True   # would 409 a durable agent

        config = MagicMock()
        config.name = "ghost"
        config.ephemeral = True

        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await crud.create_agent_internal(config, MagicMock())

        # It fails later (entitlement/quota), never on the volume-base gate.
        assert "data volumes" not in str(exc.value.detail)
        db.is_volume_base_reserved.assert_not_called()


class TestCreateRefusesAnUnclaimedVolume:
    """#1667: the create path is get-then-create — an existing volume is mounted
    as the new agent's `/home/developer`. #1664's gate covers a volume some ROW
    still claims (rename); this covers the volume NOTHING claims: a purge whose
    removal hit an in-use 409, a crash between volume_create and the ownership
    INSERT, or a restored backup. Adopting is now a declared decision."""

    def _cfg(self, name="fresh", ephemeral=False):
        config = MagicMock()
        config.name = name
        config.ephemeral = ephemeral
        return config

    def _db(self):
        db = MagicMock()
        db.get_agent_owner.return_value = None
        db.is_agent_name_reserved.return_value = False
        db.is_volume_base_reserved.return_value = False   # nothing claims it
        return db

    @pytest.mark.asyncio
    async def test_refuses_a_pre_existing_unclaimed_volume(self):
        import services.agent_service.crud as crud
        from fastapi import HTTPException

        db = self._db()
        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)), \
             patch.object(crud, "docker_client", MagicMock()), \
             patch.object(crud, "volume_get", AsyncMock(return_value=MagicMock())):
            with pytest.raises(HTTPException) as exc:
                await crud.create_agent_internal(self._cfg(), MagicMock())

        assert exc.value.status_code == 409
        assert "already exists" in str(exc.value.detail)
        # Actionable, not just a refusal.
        assert "docker volume rm" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_deploy_may_adopt_its_prepopulated_volume(self):
        """deploy-local (#950) pre-populates the workspace BEFORE create, so for
        it a pre-existing volume is the expected state, not a stranger's data."""
        import services.agent_service.crud as crud
        from fastapi import HTTPException

        db = self._db()
        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)), \
             patch.object(crud, "docker_client", MagicMock()), \
             patch.object(crud, "volume_get", AsyncMock(return_value=MagicMock())):
            with pytest.raises(HTTPException) as exc:
                await crud.create_agent_internal(
                    self._cfg(), MagicMock(), adopt_existing_workspace=True
                )

        # Proceeds past the gate (fails later on quota/docker), never on it.
        assert "already exists" not in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_no_volume_is_the_normal_path(self):
        import services.agent_service.crud as crud
        from fastapi import HTTPException
        import docker as _docker

        db = self._db()
        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)), \
             patch.object(crud, "docker_client", MagicMock()), \
             patch.object(crud, "volume_get",
                          AsyncMock(side_effect=_docker.errors.NotFound("nope"))):
            with pytest.raises(HTTPException) as exc:
                await crud.create_agent_internal(self._cfg(), MagicMock())

        assert "already exists" not in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_probe_failure_does_not_block_creation(self):
        """Fail-open: a Docker probe error must not make creation unavailable."""
        import services.agent_service.crud as crud
        from fastapi import HTTPException

        db = self._db()
        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)), \
             patch.object(crud, "docker_client", MagicMock()), \
             patch.object(crud, "volume_get",
                          AsyncMock(side_effect=RuntimeError("docker down"))):
            with pytest.raises(HTTPException) as exc:
                await crud.create_agent_internal(self._cfg(), MagicMock())

        assert "already exists" not in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_ghosts_skip_the_probe(self):
        """Ghosts are volume-less by construction — no volume, no probe."""
        import services.agent_service.crud as crud
        from fastapi import HTTPException

        db = self._db()
        probe = AsyncMock(return_value=MagicMock())
        with patch.object(crud, "db", db), \
             patch.object(crud, "get_agent_by_name", MagicMock(return_value=None)), \
             patch.object(crud, "docker_client", MagicMock()), \
             patch.object(crud, "volume_get", probe):
            with pytest.raises(HTTPException):
                await crud.create_agent_internal(
                    self._cfg(name="ghost", ephemeral=True), MagicMock()
                )

        probe.assert_not_awaited()
