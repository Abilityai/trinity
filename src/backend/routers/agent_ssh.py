# mcp: agents.ts (get_agent_ssh_access)
"""SSH access endpoints for Trinity agents."""
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from models import SshAccessRequest, User
from dependencies import require_admin
from services.docker_service import get_agent_container
from services.docker_utils import container_reload

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.post("/{agent_name}/ssh-access")
async def create_ssh_access(
    agent_name: str,
    body: SshAccessRequest = SshAccessRequest(),
    current_user: User = Depends(require_admin)
):
    """
    Generate ephemeral, key-based SSH credentials for direct agent access.

    Authentication is key-based (BYOK): the client supplies their public key and
    the server injects it into the container's authorized_keys. The injected key
    expires automatically after the specified TTL.

    Password auth was removed (#1615) — it never worked (the agent sshd runs with
    ``PasswordAuthentication no``, and the host-side hashing used the stdlib
    ``crypt`` module removed in Python 3.13). Key auth is strictly more secure.

    Args:
        agent_name: Name of the agent to access
        body: Request body with ttl_hours and public_key

    Returns:
        SSH connection details (never includes private keys)
    """
    from services.ssh_service import get_ssh_service, get_ssh_host, SSH_ACCESS_MAX_TTL_HOURS
    from services.settings_service import get_ops_setting

    # Check if SSH access is enabled system-wide
    if not get_ops_setting("ssh_access_enabled", as_type=bool):
        raise HTTPException(
            status_code=403,
            detail="SSH access is disabled. Enable it in Settings → Ops Settings → ssh_access_enabled"
        )

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify agent is running
    await container_reload(container)
    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent must be running to generate SSH access. Start the agent first."
        )

    # Validate TTL
    ttl_hours = body.ttl_hours
    if ttl_hours < 0.1:  # Minimum 6 minutes
        ttl_hours = 0.1
    if ttl_hours > SSH_ACCESS_MAX_TTL_HOURS:
        ttl_hours = SSH_ACCESS_MAX_TTL_HOURS

    # Validate auth method — key-based only (#1615: password auth removed).
    auth_method = (body.auth_method or "key").lower()
    if auth_method != "key":
        raise HTTPException(
            status_code=400,
            detail="Password SSH auth is no longer supported. Use key-based auth: "
                   "generate a key locally (ssh-keygen -t ed25519) and pass its public_key.",
        )

    # Get SSH port from container labels
    labels = container.attrs.get("Config", {}).get("Labels", {})
    ssh_port = int(labels.get("trinity.ssh-port", "2222"))

    # Get host for SSH connection
    host = get_ssh_host()

    ssh_service = get_ssh_service()

    # Calculate expiry
    expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)

    # Key-based authentication — client supplies their public key
    if not body.public_key or not body.public_key.strip():
        raise HTTPException(
            status_code=400,
            detail="public_key is required for key-based authentication. "
                   "Generate a key locally (ssh-keygen -t ed25519) and provide the public key."
        )

    public_key = body.public_key.strip()

    # Basic validation: must look like an SSH public key
    if not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-ssh-", "ssh-dss ")):
        raise HTTPException(
            status_code=400,
            detail="Invalid public key format. Must be an OpenSSH public key "
                   "(starts with ssh-ed25519, ssh-rsa, ecdsa-sha2-, etc.)"
        )

    # Create a tracking comment for cleanup
    timestamp = int(time.time())
    comment = f"trinity-ephemeral-{agent_name}-{timestamp}"

    # Append comment to public key if not already present
    if "trinity-ephemeral-" not in public_key:
        public_key_with_comment = f"{public_key} {comment}"
    else:
        public_key_with_comment = public_key
        # Extract existing comment for tracking
        comment = public_key.split()[-1] if len(public_key.split()) > 2 else comment

    # Inject client's public key into container (async to avoid blocking)
    if not await ssh_service.inject_ssh_key(agent_name, public_key_with_comment):
        raise HTTPException(
            status_code=500,
            detail="Failed to inject SSH key into agent container"
        )

    # Store metadata in Redis with TTL
    ssh_service.store_key_metadata(
        agent_name=agent_name,
        comment=comment,
        public_key=public_key_with_comment,
        created_by=current_user.username,
        ttl_hours=ttl_hours
    )

    # Build SSH command
    ssh_command = f"ssh -p {ssh_port} developer@{host}"

    return {
        "status": "success",
        "agent": agent_name,
        "auth_method": "key",
        "connection": {
            "command": ssh_command,
            "host": host,
            "port": ssh_port,
            "user": "developer"
        },
        "expires_at": expires_at.isoformat() + "Z",
        "expires_in_hours": ttl_hours,
        "instructions": [
            f"Connect: {ssh_command}",
            f"Key expires in {ttl_hours} hours"
        ]
    }
