"""
System Agent Service - Auto-deployment and management of the Trinity system agent.

The system agent is a privileged platform orchestrator that:
- Is automatically deployed on platform startup
- Cannot be deleted (only re-initialized)
- Has full access to all Trinity MCP tools
- Can communicate with any agent regardless of permissions
"""
import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import docker

from database import db
from db.agents import SYSTEM_AGENT_NAME
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_next_available_port,
    is_port_available,
)
from services.docker_utils import (
    container_reload,
    container_start,
    containers_run,
    network_get,
)
from services.agent_runtime_state import clear_agent_breakers
from services.agent_auth import derive_agent_token
from services.settings_service import get_anthropic_api_key
from services.agent_service.helpers import check_base_image_state
from services.agent_service.lifecycle import (
    FULL_CAPABILITIES,
    AGENT_TMPFS_MOUNT,
    AGENT_DEFAULT_TMPDIR,
    start_agent_internal,
)
from services.agent_service.capabilities import normalize_cpu, normalize_memory
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Constants
SYSTEM_AGENT_TEMPLATE = "local:trinity-system"
SYSTEM_AGENT_TYPE = "system-orchestrator"
SYSTEM_AGENT_OWNER = "admin"  # System agent is owned by admin
SYSTEM_AGENT_NETWORK = "trinity-agent-network"

# #1816: the operator-facing vocabulary for `check_base_image_state`. The
# internal predicate speaks in terms of the comparison ("match"/"drift"); the
# operator wants to know whether the container is CURRENT. An ENUM only —
# never image ids or digests, mirroring the `/health clone_status` contract
# (#1439) — because this reaches an API response and a persisted alarm.
_BASE_IMAGE_STATE_LABELS = {
    "match": "current",
    "drift": "stale",
    "unknown": "unknown",
}

# #1816: reserved operator-queue id prefix for the staleness alarm. Registered
# in operator_queue_service._RESERVED_ID_PREFIXES (#1632) so an agent cannot
# pre-create — and, via create_item's on_conflict_do_nothing, silently
# suppress — the alarm raised about it.
BASE_IMAGE_STALE_ALERT_PREFIX = "base-image-stale-"

# #1816: minimum gap between two staleness alarms. The alarm is already
# edge-triggered on a state transition, but "running + stale" persists across
# every backend restart until an operator acts, and a restart resets the
# in-memory edge — so a restart loop would file one item per boot without this.
BASE_IMAGE_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60


class SystemAgentService:
    """Service for managing the Trinity system agent."""

    # #1816: last staleness-alarm emission (monotonic-ish wall clock), for the
    # cooldown. Per-process and deliberately NOT persisted: the alarm is
    # advisory, an extra item after a redeploy is harmless, and a persisted
    # cursor would be one more thing that can wedge the alarm shut forever.
    _last_base_image_alert_at: Optional[datetime] = None

    def is_deployed(self) -> bool:
        """Check if the system agent container exists."""
        container = get_agent_container(SYSTEM_AGENT_NAME)
        return container is not None

    async def is_running(self) -> bool:
        """Check if the system agent is running."""
        container = get_agent_container(SYSTEM_AGENT_NAME)
        if not container:
            return False
        await container_reload(container)
        return container.status == "running"

    def is_registered(self) -> bool:
        """Check if the system agent is registered in the database."""
        owner = db.get_agent_owner(SYSTEM_AGENT_NAME)
        return owner is not None

    async def ensure_deployed(self) -> dict:
        """
        Ensure the system agent is deployed and running.

        This is the main entry point called on platform startup.

        Returns:
            dict with deployment status and details
        """
        import yaml

        result = {
            "agent_name": SYSTEM_AGENT_NAME,
            "action": None,
            "status": None,
            "message": None
        }

        # Check if already deployed and running
        if self.is_deployed():
            container = get_agent_container(SYSTEM_AGENT_NAME)
            await container_reload(container)

            # Ensure database record has is_system=True (fixes regression if record exists without flag)
            db.register_agent_owner(SYSTEM_AGENT_NAME, SYSTEM_AGENT_OWNER, is_system=True)

            # #1816 — RUNNING: report, never act.
            #
            # This branch is READ-ONLY by design and is the AC2 boundary at
            # boot. It must contain no recreate call, no container_stop and no
            # container_remove: `trinity-system` is the platform orchestrator,
            # it may be mid-execution, and nothing an operator did caused the
            # drift. `check_base_image_state` only inspects; the adoption
            # happens at the next COLD boundary (a stopped-container boot, or
            # an explicit /api/system-agent/restart).
            #
            # Before #1816 this branch returned `action: none` without
            # evaluating anything at all, which — combined with
            # `restart_policy: unless-stopped` and a canonical upgrade path
            # (build-base-image.sh → start.sh) that never touches agent
            # containers — made the system agent the most-stale agent in every
            # fleet, indefinitely and silently.
            if container.status == "running":
                state = await check_base_image_state(container, SYSTEM_AGENT_NAME)
                result["action"] = "none"
                result["status"] = "running"
                result["base_image_state"] = _BASE_IMAGE_STATE_LABELS[state]

                if state == "drift":
                    result["message"] = (
                        "System agent already running, but on a STALE base image "
                        "— restart it (POST /api/system-agent/restart) to adopt "
                        "the rebuilt image"
                    )
                    logger.warning(
                        "System agent is running a stale base image. It will NOT "
                        "be recreated while running (#1816 AC2). Adopt it with "
                        "POST /api/system-agent/restart, or stop it and let the "
                        "next backend boot pick it up."
                    )
                    self._emit_base_image_stale_alert()
                elif state == "unknown":
                    # Fail-open probe. Reported honestly and NEVER alarmed on:
                    # an alert manufactured by a check that could not run is
                    # worse than no alert, and it is exactly the conflation
                    # (#1809's boolean) that the 3-state split exists to avoid.
                    result["message"] = (
                        "System agent already running (base-image check "
                        "unavailable — see the preceding warning)"
                    )
                    logger.warning(
                        "System agent base-image check could not run; staleness "
                        "is unknown and no alarm was raised"
                    )
                else:
                    result["message"] = "System agent already running"
                    logger.info("System agent already running on a current base image")
                return result

            # #1816 — STOPPED: the cold boundary. Delegate to the shared
            # lifecycle instead of a bare `container_start`, which evaluated
            # nothing.
            #
            # Everything below comes for free and stays correct as the
            # lifecycle evolves: the #1809 cold-start image gate (this is a
            # cold start, so it fires and ADOPTS), #1560 clear-before-recreate
            # ordering, the 409/NotFound concurrent-recreate hardening, the
            # post-recreate handle re-lookup, and every future predicate. The
            # alternative — a second, permanently divergent lifecycle for one
            # agent — is the bug class that produced this issue.
            #
            # Cost, accepted: credential/skill/read-only injection now runs on
            # the boot path. Bounded by AGENT_READINESS_TIMEOUT_S, on the
            # STOPPED branch only, and inside main.py's existing broad lifespan
            # try/except.
            if not await self._preflight_ok_for_delegated_start():
                # R1: a recreate REMOVES the old container before running the
                # replacement, so a run that cannot succeed leaves the platform
                # with NO system agent. When a precondition for the run is
                # already known-bad, do not hand the container to a path that
                # might remove it — fall back to today's plain start, which
                # cannot destroy anything.
                try:
                    await container_start(container)
                    result["action"] = "started"
                    result["status"] = "running"
                    result["message"] = (
                        "System agent started without base-image adoption "
                        "(pre-flight failed — see the preceding warning)"
                    )
                    logger.info("System agent started (adoption skipped by pre-flight)")
                    return result
                except Exception as e:
                    result["action"] = "start_failed"
                    result["status"] = "error"
                    result["message"] = f"Failed to start system agent: {e}"
                    logger.error(f"Failed to start system agent: {e}")
                    return result

            try:
                start_result = await start_agent_internal(SYSTEM_AGENT_NAME)
                result["action"] = "started"
                result["status"] = "running"
                result["recreated"] = bool(start_result.get("recreated"))
                result["recreate_reason"] = start_result.get("recreate_reason")
                result["message"] = (
                    "System agent started and adopted a rebuilt base image"
                    if start_result.get("recreate_reason") == "image_drift"
                    else "System agent started"
                )
                logger.info(
                    "System agent started (recreated=%s, reason=%s)",
                    result["recreated"], result["recreate_reason"],
                )
                return result
            except Exception as e:
                result["action"] = "start_failed"
                result["status"] = "error"
                result["message"] = f"Failed to start system agent: {e}"
                logger.error(f"Failed to start system agent: {e}")
                # R1: if the recreate got as far as removing the old container,
                # the platform now has no orchestrator and no one is watching a
                # boot log. Alarm on it.
                self._emit_start_failed_alert(str(e))
                return result

        # System agent doesn't exist - create it
        try:
            creation_result = await self._create_system_agent()
            result["action"] = "created"
            result["status"] = "running"
            result["message"] = "System agent created and started"
            result["details"] = creation_result
            logger.info("System agent created and started")
            return result
        except Exception as e:
            result["action"] = "create_failed"
            result["status"] = "error"
            result["message"] = f"Failed to create system agent: {e}"
            logger.error(f"Failed to create system agent: {e}")
            return result

    async def _create_system_agent(self) -> dict:
        """
        Create the system agent container.

        Returns:
            dict with creation details
        """
        import yaml
        import json

        # Ensure admin user exists for ownership
        admin_user = db.get_user_by_username(SYSTEM_AGENT_OWNER)
        if not admin_user:
            logger.error(f"Admin user '{SYSTEM_AGENT_OWNER}' not found. Cannot create system agent.")
            raise ValueError(f"Admin user '{SYSTEM_AGENT_OWNER}' not found")

        # Load template configuration
        templates_dir = Path("/agent-configs/templates")
        if not templates_dir.exists():
            templates_dir = Path("./config/agent-templates")

        template_name = SYSTEM_AGENT_TEMPLATE.replace("local:", "")
        template_path = templates_dir / template_name
        template_yaml = template_path / "template.yaml"

        if not template_yaml.exists():
            raise FileNotFoundError(f"System agent template not found: {template_yaml}")

        with open(template_yaml) as f:
            template_data = yaml.safe_load(f)

        # Get configuration from template
        agent_type = template_data.get("type", SYSTEM_AGENT_TYPE)
        resources = template_data.get("resources", {"cpu": "4", "memory": "8g"})
        mcp_servers = template_data.get("mcp_servers", [])

        # Get next available port
        ssh_port = get_next_available_port()

        # Create agent MCP API key with system scope
        agent_mcp_key = None
        trinity_mcp_url = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
        try:
            agent_mcp_key = db.create_agent_mcp_api_key(
                agent_name=SYSTEM_AGENT_NAME,
                owner_username=SYSTEM_AGENT_OWNER,
                description="Auto-generated system agent MCP key"
            )
            if agent_mcp_key:
                # Update the key to have system scope
                self._set_system_scope(agent_mcp_key.id)
                logger.info(f"Created system-scoped MCP API key for system agent: {agent_mcp_key.key_prefix}...")
        except Exception as e:
            logger.warning(f"Failed to create MCP API key for system agent: {e}")

        # Build environment variables
        env_vars = {
            'AGENT_NAME': SYSTEM_AGENT_NAME,
            'AGENT_TYPE': agent_type,
            'ANTHROPIC_API_KEY': get_anthropic_api_key(),
            'ENABLE_SSH': 'true',
            'ENABLE_AGENT_UI': 'true',
            'AGENT_SERVER_PORT': '8000',
            'TEMPLATE_NAME': SYSTEM_AGENT_TEMPLATE,
            # #1098: redirect scratch off the 100 MB noexec /tmp tmpfs onto the
            # disk-backed home volume (dir created at start by startup.sh).
            'TMPDIR': AGENT_DEFAULT_TMPDIR,
            # #1816 convergence: creation must satisfy every predicate the
            # shared recreate path checks, or the system agent is born with a
            # PERMANENT mismatch. This one (#1159's
            # check_agent_auth_token_env_matches) was silently false since
            # #1159 — the token's only three writers were crud.py and the two
            # lifecycle recreates, never here. That matters far beyond a stray
            # recreate: `recreate_container_with_updated_config` resolves the
            # image from the container's own Config.Image *tag*, so a config
            # recreate is ALSO an image adoption — a permanently-false predicate
            # means the first `POST /api/agents/trinity-system/start` after any
            # fresh provision replaces a RUNNING orchestrator and swaps its
            # image mid-operation (AC2).
            #
            # Fail-closed, deliberately: derive_agent_token raises when
            # AGENT_AUTH_SECRET is unset, so such an install now fails to CREATE
            # the system agent instead of creating one the backend can never
            # talk to (every backend→agent call already raises today, and
            # check_agent_auth_token_env_matches already raises for every
            # agent). ensure_deployed catches → `create_failed`, and
            # main.py's lifespan catch keeps boot alive either way.
            'TRINITY_AGENT_AUTH_TOKEN': derive_agent_token(SYSTEM_AGENT_NAME),
        }

        # NB: deliberately NO `TRINITY_BACKEND_URL` here. That env var is the
        # agent-side heartbeat loop's gate, and heartbeat_service.
        # authorize_heartbeat accepts ONLY `scope == "agent"` keys — the system
        # agent's key is `scope == "system"`, so arming it would produce a
        # permanent 5-second 403 loop (swallowed agent-side, ~17k backend log
        # lines/day). Whether the orchestrator SHOULD be visible to fleet health
        # is a real design question, and a separate one (#1816 §10).

        # OpenTelemetry Configuration (enabled by default)
        if os.getenv('OTEL_ENABLED', '1') == '1':
            env_vars['CLAUDE_CODE_ENABLE_TELEMETRY'] = '1'
            env_vars['OTEL_METRICS_EXPORTER'] = os.getenv('OTEL_METRICS_EXPORTER', 'otlp')
            env_vars['OTEL_LOGS_EXPORTER'] = os.getenv('OTEL_LOGS_EXPORTER', 'otlp')
            env_vars['OTEL_EXPORTER_OTLP_PROTOCOL'] = os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
            env_vars['OTEL_EXPORTER_OTLP_ENDPOINT'] = os.getenv('OTEL_COLLECTOR_ENDPOINT', 'http://trinity-otel-collector:4317')
            env_vars['OTEL_METRIC_EXPORT_INTERVAL'] = os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000')

        # Inject Trinity MCP credentials
        if agent_mcp_key:
            env_vars['TRINITY_MCP_URL'] = trinity_mcp_url
            env_vars['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key

        # Set up volumes
        # Note: Volume name contains "workspace" but it mounts to /home/developer (consistent with all agents)
        agent_volume_name = f"agent-{SYSTEM_AGENT_NAME}-workspace"
        volumes = {
            agent_volume_name: {'bind': '/home/developer', 'mode': 'rw'}
        }

        # Mount template directory
        # Check existence inside container (at /agent-configs/templates)
        # But mount using HOST path (for Docker to access from host filesystem)
        if template_path.exists():
            host_templates_base = os.getenv("HOST_TEMPLATES_PATH", "./config/agent-templates")
            host_template_path = Path(host_templates_base) / template_name
            volumes[str(host_template_path)] = {'bind': '/template', 'mode': 'ro'}
            logger.info(f"Mounting template from {host_template_path} to /template")

        # Container labels
        labels = {
            'trinity.platform': 'agent',
            'trinity.agent-name': SYSTEM_AGENT_NAME,
            'trinity.agent-type': agent_type,
            'trinity.ssh-port': str(ssh_port),  # Required for port tracking
            'trinity.cpu': str(resources.get('cpu', '4')),
            'trinity.memory': resources.get('memory', '8g'),
            'trinity.created': utc_now_iso(),
            'trinity.template': SYSTEM_AGENT_TEMPLATE,
            'trinity.is-system': 'true',  # Mark as system agent
            # #1816 convergence (second half): the container below really does
            # run with `cap_add=FULL_CAPABILITIES`, but creation never said so
            # in a label — and check_full_capabilities_match defaults a MISSING
            # label to 'false' while the fleet setting defaults to true. Second
            # permanently-false predicate, same AC2 consequence as the token
            # above; every other reader of this label was also being lied to.
            #
            # Safe to pin ONLY because the predicate is system-aware (#1816,
            # helpers.is_system_agent_name): on an
            # `agent_full_capabilities=false` install, a pinned 'true' label
            # compared against the fleet default would mismatch FOREVER —
            # recreate on every start. The predicate exemption and the recreate
            # path's `full_capabilities` override are the checker and writer
            # halves of the same contract.
            'trinity.full-capabilities': 'true',
        }

        # #1560: `SYSTEM_AGENT_NAME` is a fixed, permanently-recycled name — if the
        # container was removed, this recreates it under exactly the same name and
        # would otherwise inherit the previous incarnation's breaker verdict. Same
        # clear the regular create path does in agent_service/crud.py, before the
        # container exists.
        clear_agent_breakers(SYSTEM_AGENT_NAME)

        # Create the container with security settings
        # System agent uses FULL_CAPABILITIES for package installation, etc.
        # Security: Always apply baseline protections even for privileged containers
        container = await containers_run(
            'trinity-agent-base:latest',
            name=f"agent-{SYSTEM_AGENT_NAME}",
            detach=True,
            network='trinity-agent-network',
            ports={'22/tcp': ssh_port},
            volumes=volumes,
            environment=env_vars,
            labels=labels,
            # #1197: normalize/validate before Docker (int(cpu) NanoCpus / mem_limit).
            mem_limit=normalize_memory(resources.get("memory"), "8g"),
            # #1126: nano_cpus (Linux CFS quota), NOT cpu_count (Windows-only → NanoCpus=0).
            nano_cpus=int(normalize_cpu(resources.get("cpu"), "4")) * 1_000_000_000,
            restart_policy={"Name": "unless-stopped"},  # Auto-restart on failure
            # Always apply AppArmor for additional sandboxing
            security_opt=['apparmor:docker-default'],
            # Always drop ALL capabilities first (defense in depth)
            cap_drop=['ALL'],
            # System agent gets full capabilities for operational tasks
            cap_add=FULL_CAPABILITIES,
            # Always apply noexec,nosuid to /tmp for security (#1098: scratch
            # redirected off this tiny tmpfs via the TMPDIR env var).
            tmpfs=AGENT_TMPFS_MOUNT,
        )

        # Register ownership with is_system=True
        db.register_agent_owner(SYSTEM_AGENT_NAME, SYSTEM_AGENT_OWNER, is_system=True)

        # Grant default permissions (system agent can talk to everyone)
        db.grant_default_permissions(SYSTEM_AGENT_NAME, SYSTEM_AGENT_OWNER)

        return {
            "container_id": container.short_id,
            "ssh_port": ssh_port,
            "mcp_key_created": agent_mcp_key is not None
        }

    async def _preflight_ok_for_delegated_start(self) -> bool:
        """#1816 R1: is it safe to hand the stopped container to a path that may
        REMOVE it before running a replacement?

        `recreate_container_with_updated_config` stops and removes the old
        container and only then calls `containers_run`. Only a name-409 is
        hardened; every other run failure — a missing `trinity-agent-network`
        (the macOS runbook documents exactly this: a stack stop→start recreates
        the network with a new id), an ssh-port collision, disk pressure —
        leaves the platform with NO system agent. The pre-#1816 boot path could
        not produce that state, because a stopped system agent was simply
        started, so this is the one genuinely new risk the delegation
        introduces and it is worth a cheap guard.

        Deliberately advisory-and-conservative rather than authoritative: a
        False here does not fail the boot, it just declines the ADOPTION and
        falls back to the plain start. So a false negative costs one stale boot
        (already the pre-#1816 status quo), while a false positive could cost
        the orchestrator.

        Fail-OPEN on an unreadable probe: an unexpected error here must not
        block a start that would otherwise have worked.
        """
        try:
            await network_get(SYSTEM_AGENT_NETWORK)
        except docker.errors.NotFound:
            logger.warning(
                "System agent base-image adoption skipped: the '%s' network does "
                "not exist, so a recreate would remove the container and then "
                "fail to run its replacement. Starting in place instead.",
                SYSTEM_AGENT_NETWORK,
            )
            self._emit_start_failed_alert(
                f"the {SYSTEM_AGENT_NETWORK} Docker network is missing"
            )
            return False
        except Exception as e:
            logger.warning(
                "System agent adoption pre-flight could not read the '%s' network "
                "(%s: %s) — proceeding (fail-open)",
                SYSTEM_AGENT_NETWORK, type(e).__name__, e,
            )
            return True

        # The ssh port the recreate will re-request. The container is stopped,
        # so it holds nothing; anything bound here belongs to another container
        # or process and WILL collide after the removal.
        container = get_agent_container(SYSTEM_AGENT_NAME)
        labels = (getattr(container, "attrs", None) or {}).get("Config", {}).get("Labels") or {}
        try:
            ssh_port = int(labels.get("trinity.ssh-port", 0))
        except (TypeError, ValueError):
            ssh_port = 0
        if ssh_port and not is_port_available(ssh_port):
            logger.warning(
                "System agent base-image adoption skipped: ssh port %s is already "
                "bound, so a recreate would remove the container and then fail to "
                "run its replacement. Starting in place instead.",
                ssh_port,
            )
            self._emit_start_failed_alert(f"ssh port {ssh_port} is already bound")
            return False

        return True

    def _emit_base_image_stale_alert(self) -> None:
        """#1816 (D2): operator-queue alarm for a running-but-stale system agent.

        Follows the established `sync_failing` / `git_bloat` idiom in
        `sync_health_service`: an un-guessable timestamped id under a
        platform-RESERVED prefix (#1632 — otherwise an agent could pre-create
        the row and, via create_item's on_conflict_do_nothing, silence the
        alarm raised about it), `priority: high`, and emit-failure-safe.

        This is what actually delivers AC1 on the canonical upgrade path.
        `build-base-image.sh` only builds and tags, `start.sh`/`stop.sh` never
        touch agent containers, `restart_fleet` skips system agents, and the
        container carries `restart_policy: unless-stopped` — so after a
        canonical upgrade the system agent is RUNNING, which is precisely the
        branch that must not act. Without an alarm the platform would notice the
        staleness and tell no one.

        Raised on `stale` ONLY. Never on `unknown`: a fail-open probe must not
        manufacture an alert.

        The payload carries the agent name and an instruction — never image ids
        or digests (canary G-04's lesson: this row is durable and
        operator-visible).
        """
        now = datetime.utcnow()
        last = SystemAgentService._last_base_image_alert_at
        if last is not None and (now - last).total_seconds() < BASE_IMAGE_ALERT_COOLDOWN_SECONDS:
            logger.debug("base-image staleness alarm suppressed by cooldown")
            return
        SystemAgentService._last_base_image_alert_at = now

        ts = utc_now_iso()
        item = {
            "id": f"{BASE_IMAGE_STALE_ALERT_PREFIX}{SYSTEM_AGENT_NAME}-{ts}",
            "agent_name": SYSTEM_AGENT_NAME,
            "type": "alert",
            "status": "pending",
            "priority": "high",
            "title": "System agent is running a stale base image",
            "question": (
                f"{SYSTEM_AGENT_NAME} is still running the base image it was "
                f"created from, and a newer trinity-agent-base has been built. "
                f"It is never recreated while running. Restart it "
                f"(POST /api/system-agent/restart) to adopt the new image. "
                f"NOTE: adoption replaces the container, so anything installed "
                f"outside /home/developer is lost."
            ),
            "context": {"agent_name": SYSTEM_AGENT_NAME, "detected_at": ts},
            "created_at": ts,
        }
        try:
            db.create_operator_queue_item(SYSTEM_AGENT_NAME, item)
            logger.warning("base-image staleness alarm emitted for %s", SYSTEM_AGENT_NAME)
        except Exception:
            logger.exception("failed to emit base-image staleness alarm")

    def _emit_start_failed_alert(self, reason: str) -> None:
        """#1816 R1: the platform may now have NO system agent, and nobody is
        watching a boot log. Same reserved prefix and emit-failure-safety as the
        staleness alarm; no cooldown, because this is a hard failure rather than
        a standing condition."""
        ts = utc_now_iso()
        item = {
            "id": f"{BASE_IMAGE_STALE_ALERT_PREFIX}start-{SYSTEM_AGENT_NAME}-{ts}",
            "agent_name": SYSTEM_AGENT_NAME,
            "type": "alert",
            "status": "pending",
            "priority": "critical",
            "title": "System agent could not be started",
            "question": (
                f"{SYSTEM_AGENT_NAME} failed to start: {reason}. The platform "
                f"orchestrator may be unavailable."
            ),
            "context": {"agent_name": SYSTEM_AGENT_NAME, "detected_at": ts},
            "created_at": ts,
        }
        try:
            db.create_operator_queue_item(SYSTEM_AGENT_NAME, item)
            logger.warning("system-agent start-failure alarm emitted: %s", reason)
        except Exception:
            logger.exception("failed to emit system-agent start-failure alarm")

    def _set_system_scope(self, key_id: str):
        """Update MCP key to have system scope (bypasses permissions)."""
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import mcp_api_keys

        with get_engine().begin() as conn:
            conn.execute(
                update(mcp_api_keys)
                .where(mcp_api_keys.c.id == key_id)
                .values(scope="system")
            )


# Global service instance
system_agent_service = SystemAgentService()
