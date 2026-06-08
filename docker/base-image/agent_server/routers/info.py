"""
Agent info, template info, health, and metrics endpoints.
"""
import os
import json
import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter

from ..models import AgentInfo
from ..state import agent_state

logger = logging.getLogger(__name__)
router = APIRouter()


def _diagnostics() -> Dict[str, Any]:
    """Lightweight runtime gauges for spotting accumulator leaks. #333."""
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = -1

    try:
        loop = asyncio.get_running_loop()
        task_count = len(asyncio.all_tasks(loop))
    except RuntimeError:
        task_count = -1

    running_executions = -1
    try:
        from ..services.process_registry import get_process_registry
        running_executions = len(get_process_registry().list_running())
    except Exception:
        pass

    return {
        "thread_count": thread_count,
        "asyncio_task_count": task_count,
        "running_executions": running_executions,
        "conversation_history_size": len(agent_state.conversation_history),
        "conversation_history_limit": agent_state.history_limit,
    }


@router.get("/")
async def root():
    """Root endpoint - no UI, just API info"""
    return {
        "service": "Trinity Agent API",
        "agent": agent_state.agent_name,
        "status": "running",
        "note": "This is an internal API. Use the Trinity web interface to chat with agents.",
        "endpoints": {
            "chat": "POST /api/chat",
            "history": "GET /api/chat/history",
            "info": "GET /api/agent/info",
            "health": "GET /health"
        }
    }


@router.get("/api/agent/info")
async def get_agent_info():
    """Get agent information"""

    # Read agent config if available
    config_path = "/config/agent-config.yaml"
    mcp_servers = []

    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f)
                mcp_servers = config.get("agent", {}).get("mcp_servers", [])
        except Exception as e:
            logger.error(f"Failed to read agent config: {e}")

    # Determine runtime version
    runtime_version = None
    if agent_state.runtime_available:
        runtime_version = "available"

    return AgentInfo(
        name=agent_state.agent_name,
        status="running",
        claude_version=runtime_version if agent_state.agent_runtime == "claude-code" else None,
        mcp_servers=mcp_servers,
        uptime=None  # TODO: Calculate uptime
    )


@router.get("/health")
async def health_check():
    """Health check endpoint.

    Includes lightweight runtime gauges (thread count, asyncio task count,
    running executions, history size) so a curl against /health is enough to
    spot accumulator leaks without strace or pprof. #333.
    """
    return {
        "status": "healthy",
        "agent_name": agent_state.agent_name,
        "runtime": agent_state.agent_runtime,
        "runtime_available": agent_state.runtime_available,
        # Backward compatibility
        "claude_available": agent_state.claude_code_available,
        "message_count": len(agent_state.conversation_history),
        # #1020: richer health signal (target-arch §Agent Runtime). Named,
        # contractual fields the platform consumes for the dispatch circuit
        # breaker (#526) and fleet-health scoring (#307). `mailbox_depth` is
        # intentionally absent — there is no agent-side mailbox yet (actor
        # model, #945); the backend derives queue depth from CapacityManager.
        "active_tasks": agent_state.active_task_count,
        "last_task_at": agent_state.last_task_at,
        "consecutive_failures": agent_state.consecutive_failures,
        "diagnostics": _diagnostics(),
    }


@router.get("/api/template/info")
async def get_template_info():
    """
    Get template metadata from template.yaml if available.
    Returns information about what this agent is, its capabilities, commands, etc.
    """
    template_path = Path("/home/developer/template.yaml")
    template_data = None

    if template_path.exists():
        try:
            import yaml
            with open(template_path) as f:
                template_data = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Failed to read template.yaml: {e}")

    if not template_data:
        # Return basic info from environment if no template.yaml
        return {
            "has_template": False,
            "agent_name": agent_state.agent_name,
            "template_name": os.getenv("TEMPLATE_NAME", ""),
            "message": "No template.yaml found - this agent was created without a template"
        }

    # Extract and return template metadata
    # Handle mcp_servers - can be in new format (list of {name, description}) or old format (in credentials)
    mcp_servers_raw = template_data.get("mcp_servers", [])
    if not mcp_servers_raw:
        # Fallback to old format: extract from credentials.mcp_servers keys
        mcp_servers_raw = list(template_data.get("credentials", {}).get("mcp_servers", {}).keys())

    return {
        "has_template": True,
        "template_path": str(template_path),
        "agent_name": agent_state.agent_name,
        # Core metadata
        "name": template_data.get("name", ""),
        "display_name": template_data.get("display_name", template_data.get("name", "")),
        "tagline": template_data.get("tagline", ""),
        "description": template_data.get("description", ""),
        "version": template_data.get("version", ""),
        "author": template_data.get("author", ""),
        "updated": template_data.get("updated", ""),
        # Type and resources
        "type": template_data.get("type", ""),
        "resources": template_data.get("resources", {}),
        # Use cases - example prompts for users
        "use_cases": template_data.get("use_cases", []),
        # Capabilities and features (can be strings or {name, description} objects)
        "capabilities": template_data.get("capabilities", []),
        "sub_agents": template_data.get("sub_agents", []),
        "commands": template_data.get("commands", []),
        "platforms": template_data.get("platforms", []),
        "tools": template_data.get("tools", []),
        "skills": template_data.get("skills", []),
        # MCP servers (can be strings or {name, description} objects)
        "mcp_servers": mcp_servers_raw,
        # Avatar customization
        "avatar_prompt": template_data.get("avatar_prompt"),
    }


def get_template_path() -> Path:
    """Get the fixed path to template.yaml."""
    return Path("/home/developer/template.yaml")


@router.get("/api/metrics")
async def get_metrics():
    """
    Get agent custom metrics.

    Returns metric definitions from template.yaml and current values from metrics.json.

    Response:
    - has_metrics: Whether agent has custom metrics defined
    - definitions: List of metric definitions from template.yaml
    - values: Current metric values from metrics.json
    - last_updated: Timestamp from metrics.json (if available)
    """
    # 1. Read template.yaml for metric definitions
    template_path = get_template_path()
    if not template_path.exists():
        return {
            "has_metrics": False,
            "message": "No template.yaml found"
        }

    try:
        import yaml
        template_data = yaml.safe_load(template_path.read_text())
    except Exception as e:
        logger.warning(f"Failed to read template.yaml: {e}")
        return {
            "has_metrics": False,
            "message": f"Failed to read template.yaml: {str(e)}"
        }

    metric_definitions = template_data.get("metrics", [])

    if not metric_definitions:
        return {
            "has_metrics": False,
            "message": "No metrics defined in template.yaml"
        }

    # 2. Read current values from metrics.json
    metrics_path = Path("/home/developer/metrics.json")

    values: Dict[str, Any] = {}
    last_updated: Optional[str] = None

    if metrics_path.exists():
        try:
            data = json.loads(metrics_path.read_text())
            last_updated = data.pop("last_updated", None)
            values = data
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse metrics.json: {e}")
        except Exception as e:
            logger.warning(f"Failed to read metrics.json: {e}")

    return {
        "has_metrics": True,
        "definitions": metric_definitions,
        "values": values,
        "last_updated": last_updated
    }
