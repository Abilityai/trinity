"""
SSH Service for ephemeral SSH credential management.

Provides functionality to:
1. Accept client-supplied public keys for SSH access
2. Inject credentials into agent containers
3. Clean up expired credentials from containers
4. Track credentials in Redis with TTL for auto-expiry

Key-based auth is the only method (#1615 removed password auth: the agent
sshd runs `PasswordAuthentication no`, so it could never succeed, and the
host-side hashing imported the stdlib `crypt` module removed in Python 3.13).

Security: Private keys are NEVER generated or handled server-side.
Clients generate their own keypairs and supply only the public key.
"""

import asyncio
import os
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Literal

import redis

from services import docker_service
from services.docker_service import get_agent_container
from services.docker_utils import container_exec_run
from services.agent_ssh_binding import (
    AGENT_SSH_BIND_ADDRESS_ENV,
    is_all_interfaces,
    published_ssh_host_ip,
    resolve_agent_ssh_bind_address_detail,
)

logger = logging.getLogger(__name__)

# trinity-enterprise#1168: agent containers are created with ENABLE_SSH=false, so
# startup.sh starts no sshd; the grant path starts one on demand with this exec.
# Root, because the privilege-separation directory and the daemon are root's —
# and `mkdir -p /var/run/sshd` is load-bearing: startup.sh creates it only when
# it starts sshd itself, and sshd refuses to start without it (G1). Idempotent:
# a container from an older image that already runs sshd is left alone.
# `--runstates RSD` (procps-ng ≥ 3.3.16; bookworm ships 4.0.2) is load-bearing:
# the container's PID 1 does not reap an exec-started sshd that the boot sweep
# later stops, so a `[sshd] <defunct>` zombie stays in the process table and a
# bare `pgrep -x sshd` would match it and skip the start (verified live).
SSHD_START_CMD = [
    "sh", "-c",
    "mkdir -p /var/run/sshd && chmod 0755 /var/run/sshd && "
    "(pgrep -x --runstates RSD sshd >/dev/null || /usr/sbin/sshd)",
]
# `pkill -x sshd` exits 1 when nothing matched; that is success for a stop.
SSHD_STOP_CMD = ["sh", "-c", "pkill -x sshd || true"]

# Configuration
SSH_ACCESS_DEFAULT_TTL_HOURS = int(os.getenv("SSH_ACCESS_DEFAULT_TTL_HOURS", "4"))
SSH_ACCESS_MAX_TTL_HOURS = int(os.getenv("SSH_ACCESS_MAX_TTL_HOURS", "24"))
SSH_ACCESS_CLEANUP_INTERVAL = int(os.getenv("SSH_ACCESS_CLEANUP_INTERVAL", "900"))  # 15 minutes

# Grace kept on the Redis metadata TTL *beyond* a key's true `expires_at` (#1616).
# The expired-key sweep removes the `authorized_keys` line at `expires_at`, but it
# can only act on a row Redis still holds — so the metadata must outlive the true
# expiry long enough for at least one sweep to observe it as expired. This MUST
# exceed the cleanup cadence (`cleanup_service.CLEANUP_INTERVAL_SECONDS` = 300);
# 600s = two cycles of headroom, so a skipped/slow cycle still catches the key.
# `expires_at` in the stored metadata stays the TRUE expiry — grace applies ONLY
# to the Redis TTL, never to when the key stops granting login.
SSH_ACCESS_CLEANUP_GRACE_SECONDS = int(
    os.getenv("SSH_ACCESS_CLEANUP_GRACE_SECONDS", "600")
)

# Redis key prefix
SSH_ACCESS_PREFIX = "ssh_access:"


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp — as written by ``store_credential_metadata``
    (``isoformat() + "Z"``) — into an aware UTC ``datetime``.

    Returns ``None`` when the value is absent or unparseable, so a malformed /
    legacy row degrades to the Redis-TTL fallback in the sweep rather than
    raising and aborting the whole cycle.
    """
    if not raw:
        return None
    try:
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class SshService:
    """Service for managing ephemeral SSH access to agent containers."""

    def __init__(self, redis_url: Optional[str] = None):
        if redis_url is None:
            from config import REDIS_URL
            redis_url = REDIS_URL
        self.redis_client = redis.from_url(redis_url, decode_responses=True)

    async def inject_ssh_key(
        self, agent_name: str, public_key: str, skip_if_present: bool = True
    ) -> bool:
        """
        Inject SSH public key into agent's authorized_keys file.

        Args:
            agent_name: Name of the agent
            public_key: Full public key line (including comment)
            skip_if_present: When True (default), the key is appended only if the
                exact line is not already present — idempotent, so a repeat inject
                (or a retried on-demand request) never duplicates a line. When
                False, the key is always appended.

        Returns:
            True if successful, False otherwise

        Security (#1616): the public key is passed to the container via the
        exec ``environment`` — it is NEVER interpolated into the command string.
        The old ``sh -c 'printf ... '<key>' ...'`` form was run through TWO
        unquoting layers (docker-py ``shlex.split``s a string ``cmd`` before the
        container's ``sh -c`` re-parses it), so a single quote in the key raised
        ``ValueError`` and a ``$(...)``/backtick opened an in-container command-
        substitution. Passing the value as an environment entry (raw, no shell
        parsing) closes that class entirely; the whole operation is one atomic
        ``sh -c`` script whose text carries no caller data.
        """
        container = get_agent_container(agent_name)
        if not container:
            logger.error(f"Container not found for agent: {agent_name}")
            return False

        if skip_if_present:
            # grep -qxF: whole-line (-x), fixed-string (-F), quiet (-q); `--`
            # ends option parsing. Matches the exact injected line so a repeat
            # inject is a no-op rather than a duplicate.
            append_block = (
                'if ! grep -qxF -- "$TRINITY_SSH_KEY" "$f"; then\n'
                "  printf '%s\\n' \"$TRINITY_SSH_KEY\" >> \"$f\"\n"
                'fi'
            )
        else:
            append_block = "printf '%s\\n' \"$TRINITY_SSH_KEY\" >> \"$f\""

        # Static script — no caller data is interpolated. The key flows in via
        # the container environment only (see docstring).
        script = (
            "set -e\n"
            "d=/home/developer/.ssh\n"
            'f="$d/authorized_keys"\n'
            'mkdir -p "$d"\n'
            'chmod 700 "$d"\n'
            'touch "$f"\n'
            f"{append_block}\n"
            'chmod 600 "$f"\n'
        )

        try:
            result = await container_exec_run(
                container,
                ["sh", "-c", script],
                user="developer",
                environment={"TRINITY_SSH_KEY": public_key},
            )

            if result.exit_code != 0:
                logger.error(f"Failed to inject SSH key: {result.output}")
                return False

            logger.info(f"Injected SSH key into agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"Error injecting SSH key into {agent_name}: {e}")
            return False

    # NOTE (#1615): password SSH auth was removed. `generate_password` and
    # `set_container_password` (which imported the stdlib `crypt` module removed
    # in Python 3.13, and which couldn't work anyway since the agent sshd runs
    # `PasswordAuthentication no`) are deleted. Key-based auth is the only path.
    #
    # `clear_container_password` below is deliberately KEPT as a cleanup-only
    # path: a backend on Python <3.13 could set a password successfully, so a
    # pre-upgrade Redis credential row (`auth_type="password"`) may still be in
    # flight when this ships. Those rows carry a TTL of at most
    # SSH_ACCESS_MAX_TTL_HOURS, so the branch is dead within a day of deploy —
    # it exists so the last legacy credentials still get locked rather than
    # left set. Nothing writes `auth_type="password"` any more.

    async def ensure_sshd_running(self, agent_name: str) -> bool:
        """Start the agent container's sshd if it is not already running
        (trinity-enterprise#1168).

        Containers are created with ``ENABLE_SSH=false`` so no listener exists
        before a grant; this is the grant path's first step, before the key is
        injected. Runs as root (see ``SSHD_START_CMD``). Returns False — and the
        caller refuses the grant — when the container is missing, the exec
        fails, or sshd exits non-zero, so a grant never reports a connection
        that nothing is listening for.
        """
        container = get_agent_container(agent_name)
        if not container:
            logger.error(f"Container not found for agent: {agent_name}")
            return False
        try:
            result = await container_exec_run(container, SSHD_START_CMD, user="root")
        except Exception as e:
            logger.error(f"Error starting sshd in {agent_name}: {e}")
            return False
        if result.exit_code != 0:
            logger.error(f"Failed to start sshd in {agent_name}: {result.output}")
            return False
        return True

    async def clear_container_password(self, agent_name: str) -> bool:
        """
        Clear/lock the developer user password in agent container.

        Cleanup-only (#1615): no code sets a password any more. This locks a
        password left behind by a pre-#1615 backend; see the note above.

        Args:
            agent_name: Name of the agent

        Returns:
            True if successful, False otherwise
        """
        container = get_agent_container(agent_name)
        if not container:
            # Container may have been deleted - that's ok
            logger.info(f"Container not found for agent {agent_name} during password cleanup")
            return True

        try:
            # Lock the account password (user can still use key auth)
            result = await container_exec_run(container, "passwd -l developer", user="root")

            if result.exit_code != 0:
                logger.warning(f"Failed to lock password: {result.output}")

            logger.info(f"Cleared password for agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"Error clearing password for {agent_name}: {e}")
            return False

    async def remove_ssh_key(self, agent_name: str, comment: str) -> bool:
        """
        Remove SSH key by comment from agent's authorized_keys.

        Args:
            agent_name: Name of the agent
            comment: The key comment to search for and remove. Trinity injects a
                single-token comment (``trinity-ephemeral-{agent}-{ts}``) as the
                key line's LAST whitespace-delimited field, so an exact last-field
                match targets precisely that line.

        Returns:
            True if successful, False otherwise

        Security (#1616): this is now load-bearing for the expired-key cleanup
        sweep, so it must never over-delete or be injectable. The comment is
        passed to the container via the exec ``environment`` and read by awk
        through ``ENVIRON`` — it is NEVER interpolated into the command string
        (which docker-py would ``shlex.split`` before the container ``sh -c``
        re-parsed it) and never compiled into a regex. awk compares the raw
        comment to each line's last field (``$NF``) by exact string equality:
        no metacharacter escaping is needed, and a crafted comment (containing
        ``/`` ``.`` ``[`` ``*`` ``\\`` a quote or a newline) can neither match a
        neighbouring key nor run a shell. The old ``sed -i '/{comment}/d'`` did a
        substring match with partial escaping — it could delete an unrelated key
        whose line merely contained the comment as a substring.
        """
        container = get_agent_container(agent_name)
        if not container:
            # Container may have been deleted - that's ok
            logger.info(f"Container not found for agent {agent_name} during key cleanup")
            return True

        # Static script — the comment flows in via the container environment
        # only. Writes to a temp file first and only swaps it in if awk
        # succeeds, so a mid-rewrite failure never truncates authorized_keys.
        # chmod 600 after the swap restores the perms `mv` would otherwise carry
        # over from the umask-created temp file (sshd StrictModes).
        script = (
            "f=/home/developer/.ssh/authorized_keys\n"
            '[ -f "$f" ] || exit 0\n'
            't="$f.trinity-remove.$$"\n'
            'if awk \'BEGIN { c = ENVIRON["TRINITY_SSH_COMMENT"] } $NF != c\' '
            '"$f" > "$t"; then\n'
            '  mv "$t" "$f" && chmod 600 "$f"\n'
            "else\n"
            '  rm -f "$t"\n'
            "  exit 1\n"
            "fi\n"
        )

        try:
            result = await container_exec_run(
                container,
                ["sh", "-c", script],
                user="developer",
                environment={"TRINITY_SSH_COMMENT": comment},
            )

            if result.exit_code != 0:
                logger.warning(f"SSH key removal returned non-zero: {result.output}")
                # Best-effort: the file may be absent or awk unavailable — the
                # short Redis TTL is the primary expiry guarantee.

            logger.info(f"Removed SSH key '{comment}' from agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"Error removing SSH key from {agent_name}: {e}")
            return False

    def store_credential_metadata(
        self,
        agent_name: str,
        credential_id: str,
        auth_type: Literal["key", "password"],
        created_by: str,
        ttl_hours: float,
        public_key: Optional[str] = None
    ) -> None:
        """
        Store SSH credential metadata in Redis with TTL.

        Args:
            agent_name: Name of the agent
            credential_id: Unique identifier (key comment)
            auth_type: Always "key" for new rows. `"password"` stays in the type
                only because pre-#1615 rows still in Redis carry it and the
                cleanup paths read it back; nothing writes it any more.
            created_by: Email/username of creator
            ttl_hours: Time-to-live in hours
            public_key: Public key line (for key auth only)
        """
        now = datetime.utcnow()
        expires_at = now + timedelta(hours=ttl_hours)

        redis_key = f"{SSH_ACCESS_PREFIX}{agent_name}:{credential_id}"

        metadata = {
            "agent_name": agent_name,
            "credential_id": credential_id,
            "auth_type": auth_type,
            "created_at": now.isoformat() + "Z",
            "expires_at": expires_at.isoformat() + "Z",
            "created_by": created_by
        }

        if public_key:
            metadata["public_key"] = public_key

        # Store with TTL + grace (#1616). The metadata deliberately outlives the
        # key's TRUE `expires_at` by SSH_ACCESS_CLEANUP_GRACE_SECONDS so the 5-min
        # cleanup sweep is guaranteed at least one observation of the key AFTER it
        # has expired but BEFORE Redis auto-deletes the row — the sweep removes the
        # authorized_keys line at `expires_at`, not at Redis-forget time. Without
        # the grace, ~80% of keys expired from Redis between two 5-min cycles and
        # their file line was never removed (still granting login). `expires_at`
        # above stays the true expiry; the grace touches only the Redis TTL.
        ttl_seconds = int(ttl_hours * 3600)
        self.redis_client.setex(
            redis_key,
            ttl_seconds + SSH_ACCESS_CLEANUP_GRACE_SECONDS,
            json.dumps(metadata)
        )

        logger.info(f"Stored SSH {auth_type} metadata: {redis_key} (TTL: {ttl_hours}h)")

    # Backwards compatibility alias
    def store_key_metadata(
        self,
        agent_name: str,
        comment: str,
        public_key: str,
        created_by: str,
        ttl_hours: float
    ) -> None:
        """Backwards compatible wrapper for store_credential_metadata."""
        self.store_credential_metadata(
            agent_name=agent_name,
            credential_id=comment,
            auth_type="key",
            created_by=created_by,
            ttl_hours=ttl_hours,
            public_key=public_key
        )

    async def cleanup_expired_credentials(self) -> int:
        """
        Remove EXPIRED ephemeral SSH keys from the container ``authorized_keys``
        file ``sshd`` actually reads.

        Called every 5 min by ``cleanup_service`` (#1616). For a key-auth
        credential the Redis TTL governs only the *metadata* — expiring it does
        nothing to the file, so this file-side removal is the ONLY thing that
        revokes access on a running, preserved-volume agent.

        Expiry is decided from the stored ``expires_at`` (the key's true
        deadline), NOT from a Redis-TTL window. Because ``store_credential_metadata``
        keeps the Redis row alive ``SSH_ACCESS_CLEANUP_GRACE_SECONDS`` past that
        deadline, every expired key is observed by at least one sweep before Redis
        forgets it — closing the ~80% cross-cycle miss the old ``ttl in [0,60]``
        heuristic had against the 5-min cadence. Best-effort: a stopped/deleted
        agent is a no-op (``remove_ssh_key`` tolerates a missing container).

        Returns:
            Number of credentials cleaned up
        """
        cleaned = 0
        pattern = f"{SSH_ACCESS_PREFIX}*"
        now = datetime.now(timezone.utc)

        # SCAN, not KEYS: the backend Redis ACL user is `-@dangerous`, which
        # blocks `KEYS` (#1616 — caught live: it raises NoPermissionError, which
        # would make this whole sweep fail-open to 0 every cycle and leave the
        # security fix inert). SCAN is allowed and is production-safe anyway.
        for redis_key in self.redis_client.scan_iter(match=pattern):
            try:
                data = self.redis_client.get(redis_key)
                if not data:
                    continue
                metadata = json.loads(data)

                expires_at = _parse_iso_utc(metadata.get("expires_at"))
                if expires_at is not None:
                    is_expired = now >= expires_at
                else:
                    # Legacy / malformed row with no parseable expires_at: fall
                    # back to the old Redis-TTL heuristic so we never do WORSE
                    # than before this fix for such a row.
                    ttl = self.redis_client.ttl(redis_key)
                    is_expired = ttl is not None and 0 <= ttl <= 60

                if not is_expired:
                    continue

                agent_name = metadata.get("agent_name")
                auth_type = metadata.get("auth_type", "key")
                credential_id = metadata.get("credential_id") or metadata.get("comment")
                if not (agent_name and credential_id):
                    continue

                if auth_type == "password":
                    removed = await self.clear_container_password(agent_name)
                else:
                    removed = await self.remove_ssh_key(agent_name, credential_id)

                if removed:
                    # Forget the metadata only AFTER the file line is gone, so a
                    # transient removal failure retries on the next cycle (still
                    # inside the grace window) instead of orphaning the file line.
                    self.redis_client.delete(redis_key)
                    cleaned += 1
            except Exception as e:
                logger.warning(f"Error during credential cleanup for {redis_key}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} expired SSH credentials")

        return cleaned

    async def revoke_key(self, agent_name: str, comment: str) -> bool:
        """
        Immediately revoke an SSH key.

        Args:
            agent_name: Name of the agent
            comment: Key comment to revoke

        Returns:
            True if successful
        """
        # Remove from container
        await self.remove_ssh_key(agent_name, comment)

        # Remove from Redis
        redis_key = f"{SSH_ACCESS_PREFIX}{agent_name}:{comment}"
        self.redis_client.delete(redis_key)

        logger.info(f"Revoked SSH key: {comment} for agent {agent_name}")
        return True

    async def cleanup_agent_credentials(self, agent_name: str) -> int:
        """
        Clean up all SSH credentials for an agent (called on agent stop/delete).

        Args:
            agent_name: Name of the agent

        Returns:
            Number of credentials cleaned up
        """
        pattern = f"{SSH_ACCESS_PREFIX}{agent_name}:*"
        # SCAN, not KEYS: the backend Redis ACL user is `-@dangerous` and blocks
        # `KEYS` (#1616). This method is called on agent stop/delete — under KEYS
        # it raised NoPermissionError, so per-agent SSH cleanup was silently
        # broken too. SCAN is allowed.
        redis_keys = list(self.redis_client.scan_iter(match=pattern))

        has_password_creds = False
        for key in redis_keys:
            try:
                data = self.redis_client.get(key)
                if data:
                    metadata = json.loads(data)
                    auth_type = metadata.get("auth_type", "key")
                    credential_id = metadata.get("credential_id") or metadata.get("comment")

                    if auth_type == "password":
                        has_password_creds = True
                    elif credential_id:
                        await self.remove_ssh_key(agent_name, credential_id)
            except Exception as e:
                logger.warning(f"Error cleaning up credential {key}: {e}")

            self.redis_client.delete(key)

        # Clear password once if any password credentials existed
        if has_password_creds:
            await self.clear_container_password(agent_name)

        if redis_keys:
            logger.info(f"Cleaned up {len(redis_keys)} SSH credentials for agent {agent_name}")

        return len(redis_keys)


def get_ssh_host() -> str:
    """The host IP/domain for SSH connections — see `get_ssh_host_detail`."""
    return get_ssh_host_detail()[0]


def get_ssh_host_detail() -> tuple[str, str]:
    """
    Get the host IP/domain for SSH connections, and where it came from.

    Returns ``(host, source)``; the source is one of ``ssh_host``,
    ``frontend_url``, ``tailscale``, ``host_docker_internal``, ``gateway``,
    ``localhost``. The grant endpoint uses it to decide whether a jump-host
    hint makes sense (trinity-enterprise#1168): only the first two name a host
    an admin can dial from their own machine.

    Priority:
    1. SSH_HOST environment variable (explicit configuration)
    2. FRONTEND_URL domain extraction (production deployment)
    3. Tailscale IP detection
    4. host.docker.internal (Docker Desktop for Mac/Windows)
    5. Default gateway IP (often the Docker host on Linux)
    6. Fallback to localhost

    Note: This runs inside the backend container, so we need special
    handling to get the actual Docker host IP, not the container's IP.
    """
    import socket
    import subprocess
    from urllib.parse import urlparse

    # Option 1: Explicit environment variable (most reliable)
    ssh_host = os.getenv("SSH_HOST")
    if ssh_host:
        logger.debug(f"SSH host from SSH_HOST env: {ssh_host}")
        return ssh_host, "ssh_host"

    # Option 2: Extract host from FRONTEND_URL (production deployment)
    # FRONTEND_URL is set to production domain like "https://trinity.abilityai.dev"
    frontend_url = os.getenv("FRONTEND_URL", "")
    if frontend_url:
        try:
            parsed = urlparse(frontend_url)
            host = parsed.hostname or parsed.netloc
            # Only use if it's not localhost/127.0.0.1
            if host and host not in ("localhost", "127.0.0.1", ""):
                logger.debug(f"SSH host from FRONTEND_URL: {host}")
                return host, "frontend_url"
        except Exception as e:
            logger.debug(f"Failed to parse FRONTEND_URL: {e}")

    # Option 3: Try to detect Tailscale IP (if running in container or on host)
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            ip = result.stdout.strip()
            logger.debug(f"SSH host from Tailscale: {ip}")
            return ip, "tailscale"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Option 4: Try host.docker.internal (works on Docker Desktop Mac/Windows)
    try:
        ip = socket.gethostbyname("host.docker.internal")
        if ip and not ip.startswith("172.") and not ip.startswith("127."):
            logger.debug(f"SSH host from host.docker.internal: {ip}")
            return ip, "host_docker_internal"
    except socket.gaierror:
        pass

    # Option 5: Get default gateway IP (often the Docker host on Linux)
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            # Output like: "default via 192.168.1.1 dev eth0"
            parts = result.stdout.strip().split()
            if "via" in parts:
                idx = parts.index("via")
                if idx + 1 < len(parts):
                    gateway_ip = parts[idx + 1]
                    # Gateway is often the Docker host, but filter Docker IPs
                    if not gateway_ip.startswith("172."):
                        logger.debug(f"SSH host from default gateway: {gateway_ip}")
                        return gateway_ip, "gateway"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Option 6: Fallback to localhost (user will need to set SSH_HOST)
    logger.warning(
        "Could not detect SSH host IP. Falling back to localhost. "
        "Set SSH_HOST or FRONTEND_URL environment variable for proper host detection."
    )
    return "localhost", "localhost"


async def sweep_agent_ssh_posture() -> Dict[str, Any]:
    """One boot-time pass over agent containers (trinity-enterprise#1168).

    Two things, both about containers that already exist and so never pass
    through the fixed create path until they are recreated:

    1. **Name every container still publishing SSH on every interface** — one
       WARNING listing them, when the configured bind address is not itself
       every interface. Stopped containers are included: `docker start`
       re-applies the same `HostConfig`.
    2. **Stop idle listeners.** Where `ssh_access_enabled` is false no grant can
       be issued, so a running sshd serves nobody; `pkill -x sshd` as root in
       each RUNNING container. The grant path restarts it on demand once the
       setting is on, so this is safe to run on every boot.

    Also logs one INFO line with the resolved bind address and its source, so
    the posture can be read from the boot log. Fail-open throughout: a Docker
    fault or one failing exec is logged and the sweep goes on; nothing here can
    block readiness. Returns ``{checked, all_interfaces, stopped}``.
    """
    report: Dict[str, Any] = {"checked": 0, "all_interfaces": [], "stopped": []}
    configured, source = resolve_agent_ssh_bind_address_detail()
    logger.info(
        "Agent SSH ports are published on %s (%s=%s, source: %s)",
        configured, AGENT_SSH_BIND_ADDRESS_ENV, configured, source,
    )
    docker_client = docker_service.get_docker_client()
    if not docker_client:
        return report

    def _sweep() -> None:
        from services.settings_service import get_ops_setting

        try:
            ssh_enabled = bool(get_ops_setting("ssh_access_enabled", as_type=bool))
        except Exception as e:  # noqa: BLE001 — an unreadable setting must not stop listeners
            logger.warning(f"SSH posture sweep: could not read ssh_access_enabled ({e}); leaving listeners alone")
            ssh_enabled = True
        try:
            containers = docker_client.containers.list(all=True, filters={"label": "trinity.platform=agent"})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SSH posture sweep skipped: could not list agent containers ({e})")
            return
        for container in containers:
            report["checked"] += 1
            name = (getattr(container, "name", "") or "").removeprefix("agent-")
            try:
                container.reload()  # list() returns a summary without HostConfig.PortBindings
            except Exception:  # noqa: BLE001
                pass
            published = published_ssh_host_ip(getattr(container, "attrs", None))
            if is_all_interfaces(published) and not is_all_interfaces(configured):
                report["all_interfaces"].append(name)
            if ssh_enabled or getattr(container, "status", None) != "running":
                continue
            try:
                result = container.exec_run(SSHD_STOP_CMD, user="root")
                if result.exit_code == 0:
                    report["stopped"].append(name)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"SSH posture sweep: could not stop sshd in {name} ({e})")

    try:
        await asyncio.to_thread(_sweep)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"SSH posture sweep failed: {e}")
    if report["all_interfaces"]:
        logger.warning(
            "%d agent container(s) still publish SSH on every host interface "
            "(created before %s=%s applied): %s. Recreate them to apply the "
            "binding — see docs/migrations/AGENT_SSH_BIND_2026-11.md.",
            len(report["all_interfaces"]), AGENT_SSH_BIND_ADDRESS_ENV, configured,
            ", ".join(sorted(report["all_interfaces"])),
        )
    if report["stopped"]:
        logger.info(
            "Stopped idle sshd in %d agent container(s) (ssh_access_enabled is off; "
            "a grant starts it again on demand): %s",
            len(report["stopped"]), ", ".join(sorted(report["stopped"])),
        )
    return report


# Singleton instance
_ssh_service: Optional[SshService] = None


def get_ssh_service() -> SshService:
    """Get the SSH service singleton."""
    global _ssh_service
    if _ssh_service is None:
        _ssh_service = SshService()  # resolves REDIS_URL via config
    return _ssh_service
