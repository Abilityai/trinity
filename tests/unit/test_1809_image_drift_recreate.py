"""#1809: a rebuilt agent base image must be picked up on the next COLD start.

Unit tests for the ninth ``needs_recreation`` predicate
``check_base_image_matches`` (services/agent_service/helpers.py) and
source-pins for its wiring in ``start_agent_internal``
(services/agent_service/lifecycle.py) — cold-start gate, lazy evaluation,
ephemeral exclusion, #1560 ordering — plus the recreate-race hardening and
version-label refresh in ``recreate_container_with_updated_config``.

Related issue: https://github.com/abilityai/trinity/issues/1809
"""
import asyncio
import re
from pathlib import Path

import docker
import pytest

import services.agent_service.helpers as helpers

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

OLD_ID = "sha256:" + "b" * 64
NEW_ID = "sha256:" + "a" * 64
TAG = "trinity-agent-base:latest"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Container:
    """Minimal docker-SDK container stand-in: only ``.attrs`` is read."""

    def __init__(self, running_id=OLD_ID, ref=TAG):
        self.attrs = {"Image": running_id, "Config": {"Image": ref}}


class _Image:
    def __init__(self, image_id):
        self.id = image_id
        self.labels = {}


def _image_get_returning(image_id):
    async def _fake(_ref):
        return _Image(image_id)

    return _fake


def _image_get_raising(exc):
    async def _fake(_ref):
        raise exc

    return _fake


class TestCheckBaseImageMatches:
    pytestmark = pytest.mark.unit

    def test_match_means_no_recreate(self, monkeypatch):
        monkeypatch.setattr(helpers, "image_get", _image_get_returning(OLD_ID))
        assert _run(helpers.check_base_image_matches(_Container(OLD_ID), "a1")) is True

    def test_drift_detected_when_tag_resolves_elsewhere(self, monkeypatch):
        # The #1809 repro: base image rebuilt, tag moved, container still on OLD.
        monkeypatch.setattr(helpers, "image_get", _image_get_returning(NEW_ID))
        assert _run(helpers.check_base_image_matches(_Container(OLD_ID), "a1")) is False

    def test_missing_running_image_id_fails_open(self, monkeypatch):
        monkeypatch.setattr(helpers, "image_get", _image_get_returning(NEW_ID))
        c = _Container(running_id=None)
        assert _run(helpers.check_base_image_matches(c, "a1")) is True

    def test_empty_config_reference_fails_open(self, monkeypatch):
        # Falsy check, not None-check: Config.Image can be "".
        monkeypatch.setattr(helpers, "image_get", _image_get_returning(NEW_ID))
        c = _Container(ref="")
        assert _run(helpers.check_base_image_matches(c, "a1")) is True

    def test_missing_config_section_fails_open(self, monkeypatch):
        monkeypatch.setattr(helpers, "image_get", _image_get_returning(NEW_ID))
        c = _Container()
        c.attrs = {"Image": OLD_ID}  # no Config at all
        assert _run(helpers.check_base_image_matches(c, "a1")) is True

    def test_image_not_found_fails_open(self, monkeypatch):
        # Tag deleted/pruned: a recreate would fail on the same missing tag, so
        # the availability-preserving answer is "no drift".
        monkeypatch.setattr(
            helpers, "image_get", _image_get_raising(docker.errors.ImageNotFound("gone"))
        )
        assert _run(helpers.check_base_image_matches(_Container(OLD_ID), "a1")) is True

    def test_generic_docker_error_fails_open(self, monkeypatch):
        # 2am-Friday case: daemon down/slow → start proceeds on the old image;
        # a transient error must never trigger a fleet recreate.
        monkeypatch.setattr(
            helpers, "image_get", _image_get_raising(RuntimeError("daemon unreachable"))
        )
        assert _run(helpers.check_base_image_matches(_Container(OLD_ID), "a1")) is True

    def test_id_pinned_container_is_a_documented_noop(self, monkeypatch):
        # Config.Image = sha256:… (ID-pinned): resolving the ID yields itself,
        # so drift is never detected — deliberate tautology, pinned here so it
        # stays a decision rather than an accident.
        monkeypatch.setattr(helpers, "image_get", _image_get_returning(OLD_ID))
        c = _Container(running_id=OLD_ID, ref=OLD_ID)
        assert _run(helpers.check_base_image_matches(c, "a1")) is True

    def test_version_pinned_tag_compares_its_own_tag_only(self, monkeypatch):
        # A container created from trinity-agent-base:0.8.0 recreates only when
        # THAT tag is re-pointed — a :latest-only rebuild leaves it untouched
        # (image_get is called with the container's own reference).
        seen = []

        async def _fake(ref):
            seen.append(ref)
            return _Image(OLD_ID)

        monkeypatch.setattr(helpers, "image_get", _fake)
        c = _Container(running_id=OLD_ID, ref="trinity-agent-base:0.8.0")
        assert _run(helpers.check_base_image_matches(c, "a1")) is True
        assert seen == ["trinity-agent-base:0.8.0"]


def _src(rel_path: str) -> str:
    return (_BACKEND / rel_path).read_text(encoding="utf-8")


class TestLifecycleWiring:
    """Source-pins (the test_1560 idiom): a defined-but-unwired predicate is the
    classic silent failure, and the gate SHAPE is load-bearing."""

    pytestmark = pytest.mark.unit

    def test_predicate_awaited_lazily_and_cold_start_gated(self):
        src = _src("services/agent_service/lifecycle.py")
        assert re.search(
            r"if not needs_recreation and not was_already_running:"
            r".*?await check_base_image_matches\(container, agent_name\)",
            src,
            re.S,
        ), (
            "image drift must be evaluated lazily (only when nothing else "
            "forces recreation) and ONLY on a cold start — start-on-running is "
            "a load-bearing idempotent no-op (MCP ensure-running, auto-switch, "
            "restart_system)"
        )

    def test_ephemeral_ghosts_are_excluded(self):
        src = _src("services/agent_service/lifecycle.py")
        gate_at = src.index("if not needs_recreation and not was_already_running:")
        breaker_gate_at = src.index("if needs_recreation or not was_already_running:")
        block = src[gate_at:breaker_gate_at]
        assert "get_agent_ephemeral_info" in block and "is_ephemeral" in block, (
            "ghosts are volume-less ('ghosts never recreate') — an image-drift "
            "recreate would silently destroy their workspace mid-budget"
        )

    def test_image_check_assigned_above_the_1560_breaker_gate(self):
        # test_1560_breaker_cleared_on_lifecycle pins the gate's own shape; this
        # pins that the image check contributes to needs_recreation BEFORE it.
        src = _src("services/agent_service/lifecycle.py")
        image_at = src.index("await check_base_image_matches(")
        breaker_gate_at = src.index("if needs_recreation or not was_already_running:")
        assert image_at < breaker_gate_at, (
            "the image-drift assignment must land before the #1560 "
            "clear_agent_breakers gate so a drift-recreate clears breakers"
        )

    def test_recreate_delivers_the_upgrade_via_the_containers_own_tag(self):
        # The mechanism that makes any recreate adopt the fresh image.
        src = _src("services/agent_service/lifecycle.py")
        assert 'old_config.get("Image", "trinity-agent-base:latest")' in src

    def test_recreate_refreshes_the_base_image_version_label(self):
        src = _src("services/agent_service/lifecycle.py")
        assert 'labels["trinity.base-image-version"]' in src, (
            "AgentStatus readers prefer the container label — without a refresh "
            "the UI reports the OLD version while running the NEW image"
        )

    def test_recreate_race_remove_tolerates_not_found(self):
        src = _src("services/agent_service/lifecycle.py")
        assert re.search(
            r"try:\s*\n\s*await container_remove\(old_container\)\s*\n"
            r"\s*except docker\.errors\.NotFound",
            src,
        ), "concurrent start already removed the container → proceed, not 500"

    def test_recreate_race_run_conflict_adopts_winner(self):
        src = _src("services/agent_service/lifecycle.py")
        assert re.search(r"status_code\W+\s*==\s*409|==\s*409", src) and (
            "adopting the concurrently" in src
        ), "409 name-conflict → adopt the winner's container (start is idempotent)"

    def test_start_response_surfaces_recreate_reason(self):
        src = _src("services/agent_service/lifecycle.py")
        assert '"recreated": needs_recreation' in src
        assert '"recreate_reason"' in src

    def test_router_forwards_recreate_fields_to_the_api_caller(self):
        # The endpoint REBUILDS its response dict rather than returning the
        # internal result — a field added only to start_agent_internal's return
        # never reaches the API caller (caught live during #1809 verification).
        src = _src("routers/agents.py")
        assert '"recreated": bool(result.get("recreated"))' in src
        assert '"recreate_reason": result.get("recreate_reason")' in src

    def test_skip_inject_reset_fixture_stubs_the_new_predicate(self):
        # Mock-auto-child trap: without an explicit AsyncMock in _reset, the
        # sibling suite would "pass" while never exercising the predicate.
        sibling = (Path(__file__).parent / "test_start_agent_skip_inject.py").read_text(
            encoding="utf-8"
        )
        assert "check_base_image_matches" in sibling
