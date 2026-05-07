"""
SSH Service for ephemeral SSH credential management.

Provides functionality to:
1. Accept client-supplied public keys for SSH access
2. Generate ephemeral passwords
3. Inject credentials into agent containers
4. Clean up expired credentials from containers
5. Track credentials in Redis with TTL for auto-expiry

Security: Private keys are NEVER generated or handled server-side.
Clients generate their own keypairs and supply only the public key.
"""

import os
import json
import logging
import secrets
import string
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Literal

import redis

from services.docker_service import docker_client, get_agent_container
from services.docker_utils import container_exec_run

logger = logging.getLogger(__name__)

# Configuration
SSH_ACCESS_DEFAULT_TTL_HOURS = int(os.getenv("SSH_ACCESS_DEFAULT_TTL_HOURS", "4"))
SSH_ACCESS_MAX_TTL_HOURS = int(os.getenv("SSH_ACCESS_MAX_TTL_HOURS", "24"))
SSH_ACCESS_CLEANUP_INTERVAL = int(os.getenv("SSH_ACCESS_CLEANUP_INTERVAL", "900"))  # 15 minutes

# Redis key prefix
SSH_ACCESS_PREFIX = "ssh_access:"


class SshService:
    """Service for managing ephemeral SSH access to agent containers."""

    def __init__(self, redis_url: Optional[str] = None):
        if redis_url is None:
            from config import REDIS_URL
            redis_url = REDIS_URL
        self.redis_client = redis.from_url(redis_url, decode_responses=True)

    async def inject_ssh_key(self, agent_name: str, public_key: str) -> bool:
        """
        Inject SSH public key into agent's authorized_keys file.

        Args:
            agent_name: Name of the agent
            public_key: Full public key line (including comment)

        Returns:
            True if successful, False otherwise
        """
        container = get_agent_container(agent_name)
        if not container:
            logger.error(f"Container not found for agent: {agent_name}")
            return False

        try:
            # Ensure .ssh directory exists with correct permissions
            await container_exec_run(
                container,
                'sh -c "mkdir -p /home/developer/.ssh && chmod 700 /home/developer/.ssh"',
                user="developer"
            )

            # Append public key to authorized_keys (escape the key for shell)
            # Use printf to handle potential special characters
            escaped_key = public_key.replace("'", "'\"'\"'")
            result = await container_exec_run(
                container,
                f"sh -c 'printf \"%s\\n\" '\"'{escaped_key}'\"' >> /home/developer/.ssh/authorized_keys'",
                user="developer"
            )

            if result.exit_code != 0:
                logger.error(f"Failed to append key: {result.output}")
                return False

            # Set correct permissions on authorized_keys
            await container_exec_run(
                container,
                'chmod 600 /home/developer/.ssh/authorized_keys',
                user="developer"
            )

            logger.info(f"Injected SSH key into agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"Error injecting SSH key into {agent_name}: {e}")
            return False

    def generate_password(self, length: int = 24) -> str:
        """
        Generate a secure random password for SSH access.

        Args:
            length: Password length (default 24 chars)

        Returns:
            Random password string (alphanumeric, safe for shell)
        """
        # Use alphanumeric characters only - safe for shell commands
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    async def set_container_password(self, agent_name: str, password: str) -> bool:
        """
        Set SSH password for developer user in agent container.

        Args:
            agent_name: Name of the agent
            password: Password to set

        Returns:
            True if successful, False otherwise
        """
        import crypt

        container = get_agent_container(agent_name)
        if not container:
            logger.error(f"Container not found for agent: {agent_name}")
            return False

        try:
            # Generate encrypted password using SHA-512
            salt = crypt.mksalt(crypt.METHOD_SHA512)
            encrypted = crypt.crypt(password, salt)

            # Use usermod -p with single-quoted password (handles $ in hash correctly)
            # SHA-512 hashes look like: $6$salt$hash - the $ signs are literal
            # Single quotes in shell preserve all special characters
            result = await container_exec_run(
                container,
                f"usermod -p '{encrypted}' developer",
                user="root"
            )

            if result.exit_code != 0:
                logger.error(f"Failed to set password via usermod: {result.output}")
                # Fallback to chpasswd with plaintext password
                logger.info("Trying chpasswd fallback...")
                result = await container_exec_run(
                    container,
                    f"sh -c 'echo \"developer:{password}\" | chpasswd'",
                    user="root"
                )
                if result.exit_code != 0:
                    logger.error(f"chpasswd fallback also failed: {result.output}")
                    return False

            # Ensure password authentication is enabled in sshd_config
            await container_exec_run(
                container,
                "sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config",
                user="root"
            )

            # Restart SSH daemon to pick up changes (HUP isn't enough for auth changes)
            await container_exec_run(container, "pkill sshd", user="root")
            await container_exec_run(container, "sh -c '/usr/sbin/sshd'", user="root")

            logger.info(f"Set ephemeral password for agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"Error setting password for {agent_name}: {e}")
            return False

    async def clear_container_password(self, agent_name: str) -> bool:
        """
        Clear/lock the developer user password in agent container.

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
            comment: The key comment to search for and remove

        Returns:
            True if successful, False otherwise
        """
        container = get_agent_container(agent_name)
        if not container:
            # Container may have been deleted - that's ok
            logger.info(f"Container not found for agent {agent_name} during key cleanup")
            return True

        try:
            # Use sed to remove lines containing the comment
            # Escape special characters in comment for sed
            escaped_comment = comment.replace("/", "\\/").replace(".", "\\.")
            result = await container_exec_run(
                container,
                f"sed -i '/{escaped_comment}/d' /home/developer/.ssh/authorized_keys",
                user="developer"
            )

            if result.exit_code != 0:
                logger.warning(f"sed command returned non-zero: {result.output}")
                # File might not exist, which is fine

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
            credential_id: Unique identifier (key comment or password session id)
            auth_type: Type of authentication ("key" or "password")
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

        # Store with TTL
        ttl_seconds = int(ttl_hours * 3600)
        self.redis_client.setex(
            redis_key,
            ttl_seconds,
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

    def list_active_keys(self, agent_name: Optional[str] = None) -> list:
        """
        List active SSH keys, optionally filtered by agent.

        Args:
            agent_name: Optional agent name filter

        Returns:
            List of key metadata dictionaries
        """
        pattern = f"{SSH_ACCESS_PREFIX}{agent_name or '*'}:*"
        keys = self.redis_client.keys(pattern)

        result = []
        for key in keys:
            data = self.redis_client.get(key)
            if data:
                result.append(json.loads(data))

        return result

    async def cleanup_expired_credentials(self) -> int:
        """
        Clean up expired SSH credentials from containers.

        This is called periodically by a background task.
        Redis TTL handles the metadata cleanup automatically,
        but we need to remove credentials from containers.

        Returns:
            Number of credentials cleaned up
        """
        # Note: This is a best-effort cleanup. Credentials might remain in containers
        # if the container was stopped during the credential's lifetime.
        # The main security guarantee comes from short TTLs.

        cleaned = 0
        pattern = f"{SSH_ACCESS_PREFIX}*"

        # Get all credentials that are about to expire (within cleanup interval)
        for redis_key in self.redis_client.keys(pattern):
            ttl = self.redis_client.ttl(redis_key)

            # If TTL is very low or negative, the credential is about to expire
            # Clean it from the container proactively
            if ttl is not None and 0 <= ttl <= 60:
                try:
                    data = self.redis_client.get(redis_key)
                    if data:
                        metadata = json.loads(data)
                        agent_name = metadata.get("agent_name")
                        auth_type = metadata.get("auth_type", "key")
                        credential_id = metadata.get("credential_id") or metadata.get("comment")

                        if agent_name and credential_id:
                            if auth_type == "password":
                                await self.clear_container_password(agent_name)
                            else:
                                await self.remove_ssh_key(agent_name, credential_id)
                            cleaned += 1
                except Exception as e:
                    logger.warning(f"Error during credential cleanup for {redis_key}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} expired SSH credentials")

        return cleaned

    # Backwards compatibility alias
    async def cleanup_expired_keys(self) -> int:
        """Backwards compatible wrapper for cleanup_expired_credentials."""
        return await self.cleanup_expired_credentials()

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
        redis_keys = self.redis_client.keys(pattern)

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

    # Backwards compatibility alias
    async def cleanup_agent_keys(self, agent_name: str) -> int:
        """Backwards compatible wrapper for cleanup_agent_credentials."""
        return await self.cleanup_agent_credentials(agent_name)


def get_ssh_host() -> str:
    """
    Get the host IP/domain for SSH connections.

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
        return ssh_host

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
                return host
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
            return ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Option 4: Try host.docker.internal (works on Docker Desktop Mac/Windows)
    try:
        ip = socket.gethostbyname("host.docker.internal")
        if ip and not ip.startswith("172.") and not ip.startswith("127."):
            logger.debug(f"SSH host from host.docker.internal: {ip}")
            return ip
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
                        return gateway_ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Option 6: Fallback to localhost (user will need to set SSH_HOST)
    logger.warning(
        "Could not detect SSH host IP. Falling back to localhost. "
        "Set SSH_HOST or FRONTEND_URL environment variable for proper host detection."
    )
    return "localhost"


# Singleton instance
_ssh_service: Optional[SshService] = None


def get_ssh_service() -> SshService:
    """Get the SSH service singleton."""
    global _ssh_service
    if _ssh_service is None:
        _ssh_service = SshService()  # resolves REDIS_URL via config
    return _ssh_service
