"""
System Seed Service — first-run seeding of the default system manifest
(trinity-enterprise#124).

The multi-agent generalization of the Cornelius seeder (ent#107): on a
genuinely fresh install, deploy a bundled `SystemManifest` through the
resilient deploy path (`system_service.deploy_manifest`, ent#125) so a new
instance comes up with a running starter fleet — zero manual steps, zero PAT
(the bundled manifest uses `local:` templates), zero credentials.

Key mechanics (each mirrors a Cornelius-pattern decision or hardens a gap the
multi-agent shape opens):

  * **Persisted first-run verdict** (`first_run_fresh` system-setting): the
    orchestrator `ensure_first_run_seeded()` computes "is this a fresh
    install?" ONCE and stores it durably, then runs BOTH seeders under that
    one decision. Recomputing per pass is incoherent across boots — the first
    seeder's own agents flip every later count to "not fresh", so a failed
    fleet deploy could never retry and a failed Cornelius would self-mark
    seeded after the fleet lands. `cornelius_seeded=="true"` at verdict time
    forces NOT-fresh: that flag predates this feature, so it marks an
    established ent#107-era install (including one whose agents were all
    deleted — deletion must not resurrect a fleet).
  * **First-run-only**: durable `default_system_seeded` flag. Deleting seeded
    agents does NOT re-provision. `default_system_seed_info` (JSON: manifest
    name, sha256, status, counts, timestamp) is stored beside it so support
    and the future curated-fleet upgrade (ent#137) can tell WHAT was seeded.
  * **Existence backstop**: the deploy path has NO create-409 backstop — name
    collisions become `_N` suffixes (`resolve_agent_names`) — so a fail-open
    lock race (`--workers 2` with Redis down) would double-seed silently.
    Inside the locked section, any already-reserved `{system}-{short}` name
    (soft-deleted included) converges the flag WITHOUT deploying.
  * **Flag policy**: `deployed`/`partial` → set (a partial fleet must never be
    re-deployed — that suffixes duplicates of the survivors); `failed`
    (0 created) / exception / unreadable manifest → NOT set (nothing exists,
    a later pass may retry safely).
  * **Operator override**: `TRINITY_DEFAULT_SYSTEM_MANIFEST` env — a path to a
    private distribution manifest, or a disable sentinel (`disabled`/`none`/
    `off`/`0`/`false`). Read at call time, `strip()`ed (compose `${VAR:-}`
    arrives set-but-empty; empty ⇒ bundled). An explicitly-set-but-unreadable
    path fails LOUDLY (ERROR + operator alert) and never silently falls back
    to the bundled manifest.
  * **Honest status**: partial / failed / unreadable-override emit a
    platform-path operator-queue alert (direct DB create on `trinity-system`,
    #1632-exempt by construction, best-effort).
  * **Fail-open**: never raises, never blocks boot or setup completion.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from database import db
from models import User
from services.docker_service import docker_client
from services.cornelius_agent_service import cornelius_agent_service
from redis_breaker_util import get_breaker_redis
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

SEED_OWNER = "admin"  # seeded under the admin account, like Cornelius

# Durable markers (system_settings KV — no migration).
_SEEDED_FLAG = "default_system_seeded"
_SEED_INFO_KEY = "default_system_seed_info"
_FRESH_VERDICT_KEY = "first_run_fresh"

# Cross-worker provisioning lock (Cornelius pattern; fail-open when Redis is
# down — the existence backstop below is the real duplicate guard). TTL covers
# a slow multi-agent deploy + starts, longer than Cornelius's single create.
_PROVISION_LOCK_KEY = "system_seed:provision"
_PROVISION_LOCK_TTL = 600  # seconds

# Manifest resolution. The bundled path is relative to the backend CWD (/app
# in-container): baked into the image by the Dockerfile COPY AND bind-mounted
# via `./config/manifests` in both compose files — the dev compose mounts
# `./src/backend:/app`, shadowing image COPYs, so the mount is load-bearing
# for local installs.
MANIFEST_ENV_VAR = "TRINITY_DEFAULT_SYSTEM_MANIFEST"
BUNDLED_MANIFEST_PATH = "config/manifests/default-system.yaml"
_DISABLE_SENTINELS = {"disabled", "none", "off", "0", "false"}


def _resolve_first_run_verdict() -> Optional[bool]:
    """Resolve the durable first-run verdict, computing + persisting it once.

    Returns True (fresh install), False (established install), or None
    (undetermined — a DB error; nothing is persisted so a later pass retries
    the computation). See the module docstring for why this is persisted.
    """
    try:
        stored = db.get_setting_value(_FRESH_VERDICT_KEY, None)
        if stored is not None:
            return stored == "true"
        if db.get_setting_value("cornelius_seeded", "false") == "true":
            # Established ent#107-era install (possibly with every agent since
            # deleted) — never read it as fresh.
            fresh = False
        else:
            fresh = db.count_non_system_agents() == 0
        db.set_setting(_FRESH_VERDICT_KEY, "true" if fresh else "false")
        return fresh
    except Exception as e:
        logger.warning(
            "First-run verdict resolution failed (%s) — deferring seed decisions this pass", e
        )
        return None


async def ensure_first_run_seeded() -> dict:
    """Single first-run seeding pass — the entry point both call sites use
    (setup-completion background task in `routers/setup.py`; lifespan
    safety-net in `main.py`).

    Resolves the persisted freshness verdict FIRST, then runs the Cornelius
    seeder and the default-system seeder under that one decision. Never raises.
    """
    fresh = _resolve_first_run_verdict()
    try:
        await cornelius_agent_service.ensure_seeded(fresh=fresh)
    except Exception as e:  # belt: the service already never-raises
        logger.error("Cornelius seed pass failed unexpectedly: %s", e)
    try:
        return await system_seed_service.ensure_seeded(fresh=fresh)
    except Exception as e:  # belt: keeps the entry point's never-raises contract
        logger.error("Default-system seed pass failed unexpectedly: %s", e)
        return {"system": None, "action": "create_failed", "status": "error",
                "message": f"seed pass failed: {e}"}


class SystemSeedService:
    """Seeds the default system manifest exactly once on a fresh install."""

    async def ensure_seeded(self, fresh: Optional[bool]) -> dict:
        """Deploy the default system manifest if — and only if — this is a
        genuinely fresh install that hasn't been seeded yet.

        Idempotent and safe to call from multiple triggers / workers. Never
        raises: returns a result dict (Cornelius/`ensure_deployed` shape).

        Args:
            fresh: the persisted first-run verdict from
                `_resolve_first_run_verdict()`. None ⇒ undetermined this pass —
                skip WITHOUT setting the flag so a later pass retries.
        """
        result = {"system": None, "action": None, "status": None, "message": None}

        # 0. Docker required (demo mode has no containers).
        if docker_client is None:
            return self._skip(result, "docker_unavailable", "Docker not available — skipping system seed")

        # 1. Already seeded? Durable flag survives agent deletion — never
        #    resurrect a deliberately-removed fleet.
        try:
            if db.get_setting_value(_SEEDED_FLAG, "false") == "true":
                return self._skip(result, "none", "Default system already seeded")
        except Exception as e:
            return self._skip(result, "skipped_error", f"seed flag read failed: {e}")

        # 2. Operator disable sentinel. Deliberately does NOT set the flag —
        #    un-setting the sentinel on a still-fresh install seeds normally.
        override_raw = os.getenv(MANIFEST_ENV_VAR, "").strip()
        if override_raw.lower() in _DISABLE_SENTINELS:
            return self._skip(
                result, "disabled",
                f"Default system seed disabled via {MANIFEST_ENV_VAR}={override_raw!r}",
            )

        # 3. Freshness verdict (precomputed + persisted by the orchestrator).
        if fresh is None:
            return self._skip(
                result, "skipped_error",
                "First-run verdict undetermined — deferring seed this pass",
            )
        if not fresh:
            try:
                db.set_setting(_SEEDED_FLAG, "true")
            except Exception as e:
                return self._skip(result, "skipped_error", f"flag write failed: {e}")
            return self._skip(
                result, "skipped_not_fresh",
                "Established install — marking seeded without deploying",
            )

        # 4. Owner must exist. On a truly-fresh pre-setup boot the admin row is
        #    not created until first-time setup completes; skip WITHOUT setting
        #    the flag so the setup-completion trigger (or a later boot) retries.
        try:
            admin_row = db.get_user_by_username(SEED_OWNER)
        except Exception as e:  # e.g. SQLite BUSY during boot — retry next pass
            return self._skip(result, "skipped_error", f"admin lookup failed: {e}")
        if not admin_row:
            return self._skip(
                result, "deferred",
                "Admin user not present yet (pre-setup) — deferring system seed",
            )

        # 5. Resolve the manifest text (env override → bundled). Cheap + read-
        #    only, so done before taking the lock.
        manifest_yaml, source, resolve_error = self._resolve_manifest(override_raw)
        if manifest_yaml is None:
            if resolve_error:
                # An EXPLICIT override that doesn't resolve is operator intent
                # gone wrong — fail loudly, never fall back to the bundle.
                logger.error("Default system seed: %s", resolve_error)
                self._notify_operator(
                    "override-unreadable",
                    "Default system seed: override manifest unreadable",
                    f"{MANIFEST_ENV_VAR} is set but the manifest could not be read: {resolve_error}",
                    {"env_var": MANIFEST_ENV_VAR, "value": override_raw},
                )
            return self._skip(result, "manifest_unavailable", resolve_error or "No bundled manifest found")

        # 6. Cross-worker lock (fail-open). Only the winner provisions this pass.
        if not self._acquire_lock():
            return self._skip(result, "skipped_locked", "Another worker is seeding the default system")

        try:
            # 7. Existence backstop — the deploy path suffixes name collisions
            #    instead of 409ing, so this closes the sequential/crash-recovery
            #    duplicate races when the lock fails open. (A truly simultaneous
            #    fail-open race can still slip past it — boot-scoped, accepted;
            #    worst case is a suffixed duplicate or a spurious failed-alert.)
            #    Any reserved final name ⇒ a prior/concurrent seed reached the
            #    create phase ⇒ converge the flag, no deploy.
            manifest_name, short_names = self._manifest_agent_names(manifest_yaml)
            if manifest_name:
                result["system"] = manifest_name
                reserved = self._reserved_agents(manifest_name, short_names)
                if reserved:
                    db.set_setting(_SEEDED_FLAG, "true")
                    self._store_seed_info(
                        manifest_name, manifest_yaml, source, "converged",
                        len(reserved), len(short_names) - len(reserved),
                    )
                    if len(reserved) < len(short_names):
                        # Crash-mid-create partial fleet: honest status — the
                        # deploy-reported "partial" path would have alerted, so
                        # the converge path must too.
                        missing = [s for s in short_names
                                   if f"{manifest_name}-{s}" not in set(reserved)]
                        self._notify_operator(
                            "partial",
                            "Default system seeded partially",
                            f"System '{manifest_name}' has {len(reserved)}/"
                            f"{len(short_names)} agents from an interrupted seed; "
                            f"missing: {missing}. Create the missing agents "
                            "individually — re-deploying the manifest would "
                            "duplicate the others.",
                            {"system": manifest_name, "reserved": reserved,
                             "missing": missing},
                        )
                    return self._skip(
                        result, "already_exists",
                        f"Agent '{reserved[0]}' already exists — marking seeded without deploying",
                    )

            admin_user = User(
                id=admin_row["id"],
                username=admin_row["username"],
                email=admin_row.get("email"),
                role=admin_row.get("role", "admin"),
            )

            # 8. Deploy through the resilient path (ent#125): strict=False so
            #    one bad agent never bricks first-run.
            from services.system_service import deploy_manifest

            deploy = await deploy_manifest(
                manifest_yaml, admin_user, request=None, strict=False
            )
            created = len(deploy.agents_created)
            failed = len(deploy.failed or [])
            result["system"] = deploy.system_name

            # 9. Flag policy. deployed/partial → set: survivors exist, and
            #    re-deploying this manifest would create `_N`-suffixed
            #    duplicates of them. failed (0 created) → NOT set: nothing to
            #    duplicate, a later pass may retry.
            if deploy.status in ("deployed", "partial"):
                db.set_setting(_SEEDED_FLAG, "true")
                self._store_seed_info(
                    deploy.system_name, manifest_yaml, source, deploy.status, created, failed
                )
                if deploy.status == "partial":
                    failed_names = [f.name for f in deploy.failed]
                    logger.warning(
                        "Default system seeded PARTIALLY: %d/%d agents created; failed: %s",
                        created, created + failed, failed_names,
                    )
                    self._notify_operator(
                        "partial",
                        "Default system seeded partially",
                        f"System '{deploy.system_name}' deployed {created}/{created + failed} "
                        f"agents; failed: {failed_names}. Create the missing agents "
                        "individually — re-deploying the manifest would duplicate the others.",
                        {"system": deploy.system_name, "created": deploy.agents_created,
                         "failed": failed_names},
                    )
                result.update(
                    action="created", status=deploy.status,
                    message=f"Default system '{deploy.system_name}' seeded "
                            f"({created} agents, {failed} failed, source={source})",
                )
                logger.info("Default system '%s' seeded: %d created, %d failed (source=%s)",
                            deploy.system_name, created, failed, source)
                return result

            # status == "failed" (0 created): retry-able.
            failed_names = [f.name for f in (deploy.failed or [])]
            logger.error(
                "Default system seed FAILED: 0 agents created for '%s'; failures: %s",
                deploy.system_name, failed_names,
            )
            self._notify_operator(
                "failed",
                "Default system seed failed",
                f"System '{deploy.system_name}' deployed 0 agents. First failure: "
                f"{(deploy.failed[0].reason if deploy.failed else 'unknown')}",
                {"system": deploy.system_name, "failed": failed_names},
            )
            result.update(action="create_failed", status="error",
                          message=f"Default system deploy failed (0 agents created, source={source})")
            return result

        except Exception as e:
            # Any other failure — including deploy_manifest's HTTPException(400)
            # on a parse-broken override — does NOT set the flag: retry on the
            # next pass. (The existence backstop makes that retry duplicate-safe
            # even if this failure happened mid-create.) Alert so a broken
            # override isn't a silent every-boot retry loop (honest status);
            # reason sanitized via the deploy path's own exit-point helper.
            reason = self._sanitized_reason(e)
            result.update(action="create_failed", status="error",
                          message=f"Failed to seed default system: {reason}")
            logger.error("Failed to seed default system: %s", reason)
            self._notify_operator(
                "error",
                "Default system seed failed",
                f"Seeding raised before any agent was created: {reason}",
                {"source": source},
            )
            return result
        finally:
            self._release_lock()

    # ------------------------------------------------------------------ utils

    def _resolve_manifest(self, override_raw: str):
        """Return (manifest_yaml | None, source, error | None).

        An explicitly-set override that can't be read returns an error and NO
        fallback; an absent bundled manifest returns (None, 'bundled', None)
        with a warning log (broken build — visible each boot, recoverable).
        """
        if override_raw:
            try:
                text = Path(override_raw).read_text(encoding="utf-8")
                if not text.strip():
                    return None, "override", f"override manifest at '{override_raw}' is empty"
                return text, "override", None
            except Exception as e:
                return None, "override", f"cannot read '{override_raw}': {e}"
        try:
            text = Path(BUNDLED_MANIFEST_PATH).read_text(encoding="utf-8")
            if not text.strip():
                logger.warning("Bundled default manifest %s is empty", BUNDLED_MANIFEST_PATH)
                return None, "bundled", None
            return text, "bundled", None
        except FileNotFoundError:
            logger.warning(
                "Bundled default manifest %s not found — skipping seed (broken build/mount?)",
                BUNDLED_MANIFEST_PATH,
            )
            return None, "bundled", None
        except Exception as e:
            logger.warning("Bundled default manifest unreadable (%s) — skipping seed", e)
            return None, "bundled", None

    @staticmethod
    def _manifest_agent_names(manifest_yaml: str):
        """Best-effort (name, [short_names]) extraction for the existence
        backstop. Parse errors return (None, []) — deploy_manifest will
        surface them properly as a 400."""
        try:
            from services.system_service import parse_manifest

            manifest = parse_manifest(manifest_yaml)
            return manifest.name, list(manifest.agents.keys())
        except Exception:
            return None, []

    @staticmethod
    def _reserved_agents(system_name: str, short_names) -> list:
        """Already-reserved `{system}-{short}` final names.
        `is_agent_name_reserved` covers soft-deleted rows — a half-discarded
        prior seed still counts. Fail-open (unknown ⇒ not reserved): the
        SETNX lock remains the primary guard."""
        reserved = []
        for short in short_names:
            final = f"{system_name}-{short}"
            try:
                if db.is_agent_name_reserved(final):
                    reserved.append(final)
            except Exception:
                continue
        return reserved

    @staticmethod
    def _sanitized_reason(exc: Exception) -> str:
        """Exception → sanitized, truncated reason via the deploy path's own
        exit-point helper (a git/GitHub error can embed a PAT-bearing URL)."""
        try:
            from services.system_service import _failure_reason
            reason, _ = _failure_reason(exc)
            return reason
        except Exception:
            return str(exc)[:200]

    @staticmethod
    def _store_seed_info(system_name, manifest_yaml, source, status, created, failed):
        """Diagnostics beside the boolean flag (ent#137 upgrade hook)."""
        try:
            db.set_setting(_SEED_INFO_KEY, json.dumps({
                "system": system_name,
                "manifest_sha256": hashlib.sha256(manifest_yaml.encode("utf-8")).hexdigest(),
                "source": source,
                "status": status,
                "agents_created": created,
                "agents_failed": failed,
                "seeded_at": utc_now_iso(),
            }))
        except Exception as e:
            logger.debug("Seed info write failed (non-fatal): %s", e)

    @staticmethod
    def _notify_operator(kind: str, title: str, question: str, context: dict) -> None:
        """Platform-path operator alert (direct DB create, #1632-exempt by
        construction — never routed through an agent queue file). Hosted on
        `trinity-system`, which has an ownership row on every install where the
        system agent deployed (canary L-03 stays green; the residual — a fresh
        install whose base image is broken enough that the system agent never
        registered — fails the seed for the same reason anyway). Deterministic
        `system-seed-<kind>` id (a #1632 reserved prefix, so an agent can't
        pre-create-and-silence it) ⇒ at most one open row per kind. Best-effort."""
        try:
            db.create_operator_queue_item("trinity-system", {
                "id": f"system-seed-{kind}",
                "type": "alert",
                "priority": "high",
                "status": "pending",
                "title": title,
                "question": question,
                "context": context,
                "created_at": utc_now_iso(),
            })
        except Exception as e:
            logger.debug("Seed operator alert failed (non-fatal): %s", e)

    def _acquire_lock(self) -> bool:
        """SETNX provisioning lock. Fail-open (no Redis → sole-worker behaviour;
        the existence backstop covers the race)."""
        r = get_breaker_redis()
        if r is None:
            return True
        try:
            return bool(r.set(_PROVISION_LOCK_KEY, "1", nx=True, ex=_PROVISION_LOCK_TTL))
        except Exception as e:
            logger.warning("System seed lock failed-open (%s)", e)
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
        logger.info("System seed: %s", message)
        return result


# Global service instance
system_seed_service = SystemSeedService()
