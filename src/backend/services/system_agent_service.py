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
from credentials import CredentialManager

logger = logging.getLogger(__name__)

# Constants
SYSTEM_AGENT_TEMPLATE = "local:trinity-system"
SYSTEM_AGENT_TYPE = "system-orchestrator"
SYSTEM_AGENT_OWNER = "admin"  # System agent is owned by admin


class SystemAgentService:
    """Service for managing the Trinity system agent."""

    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        self.credential_manager = CredentialManager(self.redis_url)

    def is_deployed(self) -> bool:
        """Check if the system agent container exists."""
        container = get_agent_container(SYSTEM_AGENT_NAME)
        return container is not None

    def is_running(self) -> bool:
        """Check if the system agent is running."""
        container = get_agent_container(SYSTEM_AGENT_NAME)
        if not container:
            return False
        container.reload()
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
            container.reload()

            # If running, nothing to do
            if container.status == "running":
                result["action"] = "none"
                result["status"] = "running"
                result["message"] = "System agent already running"
                logger.info("System agent already running")
                return result

            # If stopped, start it
            try:
                container.start()
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
            'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY', ''),
            'ENABLE_SSH': 'true',
            'ENABLE_AGENT_UI': 'true',
            'AGENT_SERVER_PORT': '8000',
            'TEMPLATE_NAME': SYSTEM_AGENT_TEMPLATE
        }

        # OpenTelemetry Configuration (opt-in)
        if os.getenv('OTEL_ENABLED', '0') == '1':
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
        agent_volume_name = f"agent-{SYSTEM_AGENT_NAME}-workspace"
        volumes = {
            agent_volume_name: {'bind': '/home/developer/workspace', 'mode': 'rw'}
        }

        # Mount template directory
        # Check existence inside container (at /agent-configs/templates)
        # But mount using HOST path (for Docker to access from host filesystem)
        if template_path.exists():
            host_templates_base = os.getenv("HOST_TEMPLATES_PATH", "./config/agent-templates")
            host_template_path = Path(host_templates_base) / template_name
            volumes[str(host_template_path)] = {'bind': '/template', 'mode': 'ro'}
            logger.info(f"Mounting template from {host_template_path} to /template")

        # Mount Trinity meta-prompt for task DAG planning and vector memory docs
        container_meta_prompt_path = Path("/trinity-meta-prompt")
        host_meta_prompt_path = os.getenv("HOST_META_PROMPT_PATH", "./config/trinity-meta-prompt")
        if container_meta_prompt_path.exists():
            volumes[host_meta_prompt_path] = {'bind': '/trinity-meta-prompt', 'mode': 'ro'}
            logger.info(f"Mounting Trinity meta-prompt from {host_meta_prompt_path}")

        # Container labels
        labels = {
            'trinity.platform': 'agent',
            'trinity.agent-name': SYSTEM_AGENT_NAME,
            'trinity.agent-type': agent_type,
            'trinity.template': SYSTEM_AGENT_TEMPLATE,
            'trinity.is-system': 'true',  # Mark as system agent
            'trinity.created-at': datetime.utcnow().isoformat()
        }

        # Create the container
        container = docker_client.containers.run(
            'trinity-agent-base:latest',
            name=f"agent-{SYSTEM_AGENT_NAME}",
            detach=True,
            network='trinity-agent-network',
            ports={'22/tcp': ssh_port},
            volumes=volumes,
            environment=env_vars,
            labels=labels,
            mem_limit=resources.get("memory", "8g"),
            cpu_count=int(resources.get("cpu", "4")),
            restart_policy={"Name": "unless-stopped"}  # Auto-restart on failure
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
        from db.connection import get_db_connection

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE mcp_api_keys SET scope = ? WHERE id = ?",
                ("system", key_id)
            )
            conn.commit()


# Global service instance
system_agent_service = SystemAgentService()
