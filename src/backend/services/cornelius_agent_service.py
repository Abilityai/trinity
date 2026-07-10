"""
Cornelius Agent Service — first-run seeding of the default Cornelius agent (ent#107).

Cornelius is Trinity's flagship "second brain" / knowledge-base agent and the
showcase for the Brain Orb (the Self-Rendering Mind page). This service bundles it
into the default install: a fresh Trinity comes up with a working, Brain-Orb-enabled
Cornelius present, zero manual steps.

Unlike the system agent (`system_agent_service.py`) — a privileged, deletion-protected
orchestrator recreated on EVERY boot — Cornelius is a NORMAL owned/deletable agent
seeded exactly ONCE on a genuinely-fresh install:

  * Provisioned from the bundled LOCAL template `local:cornelius`
    (`config/agent-templates/cornelius/`) via the ordinary create path — no GitHub
    clone, no PAT, no network dependency at boot. (This is why the local bundle was
    chosen over github-native: `create_agent_internal`'s dynamic-github branch hard-
    requires a system PAT even for a public repo, and a boot-time clone would add a
    network SPOF to startup.)
  * First-run-only: a durable `cornelius_seeded` system-setting is the "already done"
    marker. A user who DELETES Cornelius is NOT re-provisioned (the flag stays set) —
    the opposite of the system agent's resurrect-every-boot behaviour.
  * Fresh-install scoped: skipped when any non-system agent already exists, so
    upgrading an established fleet never spawns a surprise container.
  * Redis SETNX lock guards the `--workers 2` double-provision race (fail-open, mirrors
    the #1464 monitoring leader-lock rationale). `create`'s own 409-on-exists is the
    final backstop.
  * Enables the Brain Orb platform flag by default, existence-guarded so an admin who
    deliberately turned it OFF is never overridden.

Two fire-and-forget call sites (neither ever blocks its caller):
  1. setup-completion (`routers/setup.py`) — fresh installs, right after the admin
     account is created, so the owner exists. Runs as a FastAPI BackgroundTask.
  2. lifespan safety-net (`main.py`) — upgrades of already-set-up fresh/empty installs;
     backgrounded via `asyncio.create_task`.
"""
import logging

from database import db
from models import AgentConfig, User
from services.docker_service import docker_client
from redis_breaker_util import get_breaker_redis

logger = logging.getLogger(__name__)

CORNELIUS_AGENT_NAME = "cornelius"
CORNELIUS_TEMPLATE = "local:cornelius"
CORNELIUS_TYPE = "knowledge-base"
CORNELIUS_OWNER = "admin"  # seeded under the admin account, like the system agent

# Durable "already seeded" marker (system_settings KV — no migration).
_SEEDED_FLAG = "cornelius_seeded"
# Brain Orb platform flag we default-on at seed time (existence-guarded).
_BRAIN_ORB_FLAG = "brain_orb_enabled"

# Cross-worker provisioning lock (defends the prod `--workers 2` double-provision
# race). TTL covers a slow container create but auto-expires so a crashed
# provisioner lets a later boot retry. Fail-open when Redis is down.
_PROVISION_LOCK_KEY = "cornelius:provision"
_PROVISION_LOCK_TTL = 300  # seconds


class CorneliusAgentService:
    """Seeds the default Cornelius agent exactly once on a fresh install."""

    async def ensure_seeded(self) -> dict:
        """
        Seed the default Cornelius agent if — and only if — this is a genuinely
        fresh install that hasn't been seeded yet.

        Idempotent and safe to call from multiple triggers / workers. Never raises:
        returns a result dict (mirrors `system_agent_service.ensure_deployed`) so a
        background task or lifespan caller can't be broken by provisioning failure.
        """
        result = {"agent_name": CORNELIUS_AGENT_NAME, "action": None, "status": None, "message": None}

        # 0. Docker required (demo mode has no containers).
        if docker_client is None:
            return self._skip(result, "docker_unavailable", "Docker not available — skipping Cornelius seed")

        # 1. Already seeded? Durable flag survives a user deleting the agent, so we
        #    never resurrect a deliberately-removed Cornelius.
        if db.get_setting_value(_SEEDED_FLAG, "false") == "true":
            return self._skip(result, "none", "Cornelius already seeded")

        # 2. Fresh-install only. Any pre-existing non-system agent means this is an
        #    established install being upgraded — do NOT surprise it with a heavy
        #    container. Mark seeded so we stop re-checking every boot.
        try:
            if db.count_non_system_agents() > 0:
                db.set_setting(_SEEDED_FLAG, "true")
                return self._skip(
                    result, "skipped_not_fresh",
                    "Existing agents present — not a fresh install; marking seeded without provisioning",
                )
        except Exception as e:  # never let a count query break startup
            logger.warning("Cornelius seed: non-system agent count failed (%s) — skipping this pass", e)
            return self._skip(result, "skipped_error", f"agent count failed: {e}")

        # 3. Owner must exist. On a truly-fresh pre-setup boot the admin row is not
        #    created until first-time setup completes; skip WITHOUT setting the flag
        #    so the setup-completion trigger (or a later boot) retries.
        admin_row = db.get_user_by_username(CORNELIUS_OWNER)
        if not admin_row:
            return self._skip(
                result, "deferred",
                "Admin user not present yet (pre-setup) — deferring Cornelius seed",
            )

        # 4. Cross-worker lock (fail-open). Only the winner provisions this pass.
        if not self._acquire_lock():
            return self._skip(result, "skipped_locked", "Another worker is provisioning Cornelius")

        try:
            admin_user = User(
                id=admin_row["id"],
                username=admin_row["username"],
                email=admin_row.get("email"),
                role=admin_row.get("role", "admin"),
            )
            await self._provision(admin_user)
            # Default the Brain Orb ON for the install (existence-guarded).
            self._seed_brain_orb_flag()
            # Mark done only after a successful provision.
            db.set_setting(_SEEDED_FLAG, "true")
            result.update(action="created", status="running",
                          message="Cornelius agent seeded (Brain Orb enabled)")
            logger.info("Cornelius agent seeded and Brain Orb enabled by default")
            return result
        except Exception as e:
            # 409 = the agent already exists (another worker won, or a prior partial
            # run). Treat as success: converge the flag so we stop retrying.
            status_code = getattr(e, "status_code", None)
            if status_code == 409:
                db.set_setting(_SEEDED_FLAG, "true")
                self._seed_brain_orb_flag()
                return self._skip(result, "already_exists", "Cornelius already exists (409) — marking seeded")
            # Any other failure: do NOT set the flag — retry on the next trigger/boot.
            result.update(action="create_failed", status="error",
                          message=f"Failed to seed Cornelius: {e}")
            logger.error("Failed to seed Cornelius agent: %s", e)
            return result
        finally:
            self._release_lock()

    async def _provision(self, admin_user: User) -> None:
        """Create the Cornelius agent from the bundled local template.

        Uses the ordinary service-layer create path (not the system agent's bespoke
        direct-Docker build): a local template needs no PAT/network, and this gives
        Cornelius a normal, deletable agent lifecycle. `request=None` — the HTTP
        request object is not dereferenced by the create path (verified).
        """
        # Imported lazily to avoid a router/service import cycle at module load.
        from services.agent_service.crud import create_agent_internal

        config = AgentConfig(
            name=CORNELIUS_AGENT_NAME,
            type=CORNELIUS_TYPE,
            template=CORNELIUS_TEMPLATE,
            # resources / tools / capabilities / runtime are read from the bundled
            # template.yaml inside create_agent_internal.
        )
        await create_agent_internal(config, admin_user, request=None)

    def _seed_brain_orb_flag(self) -> None:
        """Enable the Brain Orb platform flag by default — but only if no explicit
        value is stored, so an admin who turned it OFF is never overridden
        (`_resolve_bool_flag` treats a stored value as authoritative both ways)."""
        if db.get_setting_value(_BRAIN_ORB_FLAG, None) is None:
            db.set_setting(_BRAIN_ORB_FLAG, "true")
            logger.info("Brain Orb enabled by default (seeded %s=true)", _BRAIN_ORB_FLAG)

    def _acquire_lock(self) -> bool:
        """SETNX provisioning lock. Fail-open (no Redis → sole-worker behaviour)."""
        r = get_breaker_redis()
        if r is None:
            return True
        try:
            return bool(r.set(_PROVISION_LOCK_KEY, "1", nx=True, ex=_PROVISION_LOCK_TTL))
        except Exception as e:
            logger.warning("Cornelius provision lock failed-open (%s)", e)
            return True

    def _release_lock(self) -> None:
        """Best-effort release so a legitimate retry isn't blocked for the TTL."""
        r = get_breaker_redis()
        if r is None:
            return
        try:
            r.delete(_PROVISION_LOCK_KEY)
        except Exception:
            pass  # TTL will expire it

    @staticmethod
    def _skip(result: dict, action: str, message: str) -> dict:
        result.update(action=action, status="skipped", message=message)
        logger.info("Cornelius seed: %s", message)
        return result


# Global service instance
cornelius_agent_service = CorneliusAgentService()
