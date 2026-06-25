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

from database import db
from db.agents import SYSTEM_AGENT_NAME
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_next_available_port,
)
from services.docker_utils import container_reload, container_start, containers_run
from services.settings_service import get_anthropic_api_key
from services.agent_service.lifecycle import FULL_CAPABILITIES, AGENT_TMPFS_MOUNT, AGENT_DEFAULT_TMPDIR
from services.agent_service.capabilities import normalize_cpu, normalize_memory
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Constants
SYSTEM_AGENT_TEMPLATE = "local:trinity-system"
SYSTEM_AGENT_TYPE = "system-orchestrator"
SYSTEM_AGENT_OWNER = "admin"  # System agent is owned by admin


class SystemAgentService:
    """Service for managing the Trinity system agent."""

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

            # If running, nothing to do
            if container.status == "running":
                result["action"] = "none"
                result["status"] = "running"
                result["message"] = "System agent already running"
                logger.info("System agent already running")
                return result

            # If stopped, start it
            try:
                await container_start(container)
                result["action"] = "started"
                result["status"] = "running"
                result["message"] = "System agent started"
                logger.info("System agent started")
                return result
            except Exception as e:
                result["action"] = "start_failed"
                result["status"] = "error"
                result["message"] = f"Failed to start system agent: {e}"
                logger.error(f"Failed to start system agent: {e}")
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
        }

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
        }

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
