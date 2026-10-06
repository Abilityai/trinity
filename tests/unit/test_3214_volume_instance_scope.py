"""#3214 — the #1581 orphan agent-volume sweep is scoped to THIS stack.

Docker volumes are daemon-global; the sweep's ownership check reads one stack's
database. Before this fix a second stack on the same daemon read every other
stack's unattached agent volumes as its own orphans and force-removed them,
logged at INFO as routine housekeeping (207 of 238 on one developer machine,
docs/memory/learnings/2026-09-24-a-second-stack-reads-other-stacks-volumes-as-orphans.md).

Pinned here:
  * every agent data volume is created with a `trinity.instance` label carrying
    this install's durable id (`installation_id`, via instance_identity);
  * the sweep's candidate listing filters on that label's key AND value, so a
    foreign volume is never a candidate;
  * the last-line guard refuses a volume labelled for another instance — on the
    retention-purge path too (two stacks can both have an agent named `alpha`);
  * legacy (unlabelled) volumes are never auto-reclaimed by the sweep
    (fail-closed); unowned+unattached ones are named in a WARNING for a human;
  * the reclaim is logged at WARNING as unrecoverable, naming what was removed;
  * the original #1581 case (this stack's own orphan) is still reclaimed.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend = str(_project_root / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

OURS = "11111111-1111-4111-8111-111111111111"
THEIRS = "22222222-2222-4222-8222-222222222222"


def _load_docker_utils():
    mock_client = Mock()
    with patch.dict("sys.modules", {"services.docker_service": Mock(docker_client=mock_client)}):
        spec = importlib.util.spec_from_file_location("docker_utils", f"{_backend}/services/docker_utils.py")
        mod = importlib.util.module_from_spec(spec)
        mod.docker_client = mock_client
        spec.loader.exec_module(mod)
    return mod, mock_client


def _volume(name, base, platform="agent-workspace", instance=OURS, created="2020-01-01T00:00:00Z"):
    v = Mock()
    v.name = name
    labels = {"trinity.agent-name": base, "trinity.platform": platform}
    if instance is not None:
        labels["trinity.instance"] = instance
    v.attrs = {"Labels": labels, "CreatedAt": created}
    v.remove = Mock()
    return v


# --------------------------------------------------------------------------- #
# One identity source
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestInstanceId:
    def test_is_the_full_installation_id(self, monkeypatch):
        import services.operator_intake_service as ois
        from services import instance_identity
        monkeypatch.setattr(ois, "get_or_create_installation_id", lambda: OURS)
        assert instance_identity.get_instance_id() == OURS

    def test_is_none_when_unresolvable_and_never_raises(self, monkeypatch):
        import services.operator_intake_service as ois
        from services import instance_identity

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(ois, "get_or_create_installation_id", boom)
        assert instance_identity.get_instance_id() is None
        monkeypatch.setattr(ois, "get_or_create_installation_id", lambda: "")
        assert instance_identity.get_instance_id() is None


# --------------------------------------------------------------------------- #
# Creation: every path stamps the label
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestLabelsAtCreation:
    def test_the_label_builder_stamps_this_instance(self):
        du, _ = _load_docker_utils()
        with patch.object(du, "_resolve_instance_id", lambda: OURS):
            labels = du.agent_volume_labels("alpha", "agent-public")
        assert labels == {"trinity.platform": "agent-public", "trinity.agent-name": "alpha",
                          "trinity.instance": OURS}

    def test_an_unresolvable_id_creates_an_unlabelled_volume_never_a_wrong_one(self):
        du, _ = _load_docker_utils()
        with patch.object(du, "_resolve_instance_id", lambda: None):
            labels = du.agent_volume_labels("alpha", "agent-workspace")
        assert "trinity.instance" not in labels

    def test_no_creation_site_builds_volume_labels_by_hand(self):
        """Every agent data volume goes through `agent_volume_labels`: a site
        that spells the platform label itself is a site that can forget the
        instance label. Scans the backend for agent-data platform literals
        outside the builder."""
        import re
        offenders = []
        for path in Path(_backend, "services").rglob("*.py"):
            if path.name == "docker_utils.py":
                continue
            text = path.read_text()
            for m in re.finditer(r"['\"]trinity\.platform['\"]\s*:\s*['\"]agent-(workspace|public|shared)['\"]", text):
                offenders.append(f"{path.relative_to(_backend)}:{text[:m.start()].count(chr(10)) + 1}")
        assert offenders == [], f"build agent volume labels with agent_volume_labels(): {offenders}"


# --------------------------------------------------------------------------- #
# Guard: a foreign volume is never reclaimable — sweep OR purge
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestGuard:
    def setup_method(self):
        self.du, _ = _load_docker_utils()

    def test_own_labelled_volume_is_reclaimable(self):
        v = _volume("agent-alpha-workspace", "alpha")
        assert self.du.is_reclaimable_agent_volume(v, "alpha", instance_id=OURS) is True

    def test_a_volume_labelled_for_another_instance_is_refused(self):
        v = _volume("agent-alpha-workspace", "alpha", instance=THEIRS)
        assert self.du.is_reclaimable_agent_volume(v, "alpha", instance_id=OURS) is False

    def test_a_labelled_volume_is_refused_when_our_id_is_unknown(self):
        v = _volume("agent-alpha-workspace", "alpha", instance=THEIRS)
        assert self.du.is_reclaimable_agent_volume(v, "alpha", instance_id=None) is False

    def test_a_legacy_unlabelled_volume_keeps_todays_guard(self):
        """The purge path is driven by this stack's own ownership row."""
        v = _volume("agent-alpha-workspace", "alpha", instance=None)
        assert self.du.is_reclaimable_agent_volume(v, "alpha", instance_id=OURS) is True

    @pytest.mark.asyncio
    async def test_purge_does_not_remove_another_stacks_same_named_agent(self):
        du, client = _load_docker_utils()
        foreign = _volume("agent-alpha-workspace", "alpha", instance=THEIRS)
        client.volumes.get.side_effect = lambda n: foreign if n == "agent-alpha-workspace" else (_ for _ in ()).throw(
            __import__("docker").errors.NotFound("gone"))
        with patch.object(du, "_resolve_instance_id", lambda: OURS):
            removed = await du.remove_agent_volumes("alpha")
        assert removed == 0
        foreign.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_removed_volume_is_logged_as_unrecoverable(self, caplog):
        du, client = _load_docker_utils()
        own = _volume("agent-alpha-workspace", "alpha")
        client.volumes.get.side_effect = lambda n: own if n == "agent-alpha-workspace" else (_ for _ in ()).throw(
            __import__("docker").errors.NotFound("gone"))
        with patch.object(du, "_resolve_instance_id", lambda: OURS), caplog.at_level(logging.INFO):
            assert await du.remove_agent_volumes("alpha") == 1
        lines = [r for r in caplog.records if "agent-alpha-workspace" in r.getMessage() and "removed" in r.getMessage()]
        assert lines and all(r.levelno == logging.WARNING for r in lines)
        assert "unrecoverable" in lines[0].getMessage()


# --------------------------------------------------------------------------- #
# Enumeration: key AND value
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestEnumeration:
    @pytest.mark.asyncio
    async def test_the_sweep_listing_filters_on_this_instance(self):
        du, client = _load_docker_utils()
        client.volumes.list.return_value = []
        await du.list_agent_data_volumes(instance_id=OURS)
        filters = client.volumes.list.call_args.kwargs["filters"]
        assert f"trinity.instance={OURS}" in filters["label"]
        assert "trinity.agent-name" in filters["label"]


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def _service():
    from services.cleanup_service import CleanupService
    svc = CleanupService(poll_interval=300)
    return svc


async def _sweep(svc, *, scoped, unscoped=None, attached=frozenset(), owned=frozenset(), instance=OURS, cycles=None):
    from services.cleanup_service import CleanupReport
    import services.cleanup_service as cs
    report = CleanupReport()
    db = MagicMock()
    db.is_volume_base_reserved.side_effect = lambda base: base in owned
    removed = []

    async def fake_remove(base, instance_id=None):
        removed.append((base, instance_id))
        return 1

    async def fake_list(instance_id=None):
        # The real listing filters key+value at the daemon; model that here.
        return [v for v in scoped if (v.attrs["Labels"].get("trinity.instance") == instance_id)]

    with patch.object(cs, "db", db), \
         patch("services.instance_identity.get_instance_id", lambda: instance), \
         patch("services.docker_utils.list_agent_data_volumes", AsyncMock(side_effect=fake_list)), \
         patch("services.docker_utils.list_all_agent_data_volumes",
               AsyncMock(return_value=list(unscoped if unscoped is not None else scoped))), \
         patch("services.docker_utils.list_attached_volume_names", AsyncMock(return_value=set(attached))), \
         patch("services.docker_utils.remove_agent_volumes", AsyncMock(side_effect=fake_remove)):
        for _ in range(cycles or cs.ORPHAN_VOLUME_UNATTACHED_STRIKES):
            await svc._sweep_orphan_agent_volumes(report)
    return report, removed


@pytest.mark.unit
class TestSweep:
    @pytest.mark.asyncio
    async def test_a_foreign_volume_survives_any_number_of_cycles(self):
        """AC2: labelled for another instance, unattached, old, 6 cycles."""
        foreign = _volume("agent-alpha-workspace", "alpha", instance=THEIRS)
        report, removed = await _sweep(_service(), scoped=[foreign], cycles=6)
        assert removed == []
        assert report.orphan_agent_volumes_reclaimed == 0

    @pytest.mark.asyncio
    async def test_this_stacks_own_orphan_is_still_reclaimed(self):
        """AC5: the original #1581 case."""
        own = _volume("agent-gone-workspace", "gone")
        report, removed = await _sweep(_service(), scoped=[own])
        assert removed == [("gone", OURS)]
        assert report.orphan_agent_volumes_reclaimed == 1

    @pytest.mark.asyncio
    async def test_a_legacy_unlabelled_orphan_is_never_reclaimed(self):
        """AC3: fail-closed for volumes created before the label existed."""
        legacy = _volume("agent-old-workspace", "old", instance=None)
        report, removed = await _sweep(_service(), scoped=[legacy], unscoped=[legacy], cycles=6)
        assert removed == []

    @pytest.mark.asyncio
    async def test_legacy_orphans_are_named_once_in_a_warning(self, caplog):
        legacy = _volume("agent-old-workspace", "old", instance=None)
        owned_legacy = _volume("agent-kept-workspace", "kept", instance=None)
        attached_legacy = _volume("agent-used-workspace", "used", instance=None)
        svc = _service()
        with caplog.at_level(logging.WARNING):
            await _sweep(svc, scoped=[], unscoped=[legacy, owned_legacy, attached_legacy],
                         owned={"kept"}, attached={"agent-used-workspace"}, cycles=3)
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING and "unlabelled" in r.getMessage()]
        assert len(warnings) == 1                      # once, not every cycle
        assert "agent-old-workspace" in warnings[0]
        assert "agent-kept-workspace" not in warnings[0]   # owned: not an orphan
        assert "agent-used-workspace" not in warnings[0]   # mounted: not an orphan

    @pytest.mark.asyncio
    async def test_the_sweep_does_nothing_when_this_instance_cannot_be_identified(self):
        own = _volume("agent-gone-workspace", "gone")
        report, removed = await _sweep(_service(), scoped=[own], instance=None)
        assert removed == []

    @pytest.mark.asyncio
    async def test_the_reclaim_is_a_warning_that_says_unrecoverable(self, caplog):
        """AC4."""
        own = _volume("agent-gone-workspace", "gone")
        with caplog.at_level(logging.INFO):
            await _sweep(_service(), scoped=[own])
        lines = [r for r in caplog.records if "reclaimed" in r.getMessage()]
        assert lines and all(r.levelno == logging.WARNING for r in lines)
        assert "unrecoverable" in lines[-1].getMessage()
        assert "gone" in lines[-1].getMessage()
