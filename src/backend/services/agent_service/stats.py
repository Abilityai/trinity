"""
Agent Service Stats - Container and context statistics.

Handles fetching context window and container stats.
"""
import logging
from datetime import datetime, timedelta

import httpx
from fastapi import HTTPException

from models import User
from database import db
from services.docker_service import get_agent_container
from .helpers import get_accessible_agents

logger = logging.getLogger(__name__)


async def get_agents_context_stats_logic(
    current_user: User
) -> dict:
    """
    Get context window stats and activity state for all accessible agents.

    Returns: List of agent stats with context usage and active/idle/offline state
    """
    # Get all accessible agents
    accessible_agents = get_accessible_agents(current_user)
    agent_stats = []

    for agent in accessible_agents:
        agent_name = agent["name"]
        status = agent["status"]

        # Initialize default stats
        stats = {
            "name": agent_name,
            "status": status,  # Docker status: running/stopped/etc
            "activityState": "offline",  # Activity state: active/idle/offline
            "contextPercent": 0,
            "contextUsed": 0,
            "contextMax": 200000,
            "lastActivityTime": None
        }

        # Only fetch context stats for running agents
        if status == "running":
            try:
                # Get agent container
                container = get_agent_container(agent_name)
                if container:
                    # Fetch context stats from agent's internal API
                    agent_url = f"http://{container.name}:8000/api/chat/session"
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        response = await client.get(agent_url)
                        if response.status_code == 200:
                            session_data = response.json()
                            stats["contextPercent"] = session_data.get("context_percent", 0)
                            stats["contextUsed"] = session_data.get("context_tokens", 0)
                            stats["contextMax"] = session_data.get("context_window", 200000)
            except Exception as e:
                # Log error but continue with default values
                logger.debug(f"Error fetching context stats for {agent_name}: {e}")

            # Determine active/idle state based on recent activity
            try:
                # Look for any activity in last 60 seconds
                cutoff_time = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
                recent_activities = db.get_agent_activities(
                    agent_name=agent_name,
                    limit=1
                )

                if recent_activities and len(recent_activities) > 0:
                    last_activity = recent_activities[0]
                    activity_time = last_activity.get("created_at")
                    stats["lastActivityTime"] = activity_time

                    # Check if activity is within last 60 seconds
                    if activity_time and activity_time > cutoff_time:
                        # Check if activity is currently running
                        if last_activity.get("activity_state") == "started":
                            stats["activityState"] = "active"
                        else:
                            stats["activityState"] = "idle"
                    else:
                        stats["activityState"] = "idle"
                else:
                    stats["activityState"] = "idle"
            except Exception as e:
                # Default to idle if we can't determine activity
                logger.debug(f"Error determining activity state for {agent_name}: {e}")
                stats["activityState"] = "idle"

        agent_stats.append(stats)

    return {"agents": agent_stats}


async def get_agent_stats_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get live container stats (CPU, memory, network) for an agent.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent is not running")

    try:
        stats = container.stats(stream=False)

        cpu_percent = 0.0
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})

        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - \
                    precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - \
                       precpu_stats.get("system_cpu_usage", 0)

        if system_delta > 0 and cpu_delta > 0:
            num_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

        memory_stats = stats.get("memory_stats", {})
        memory_used = memory_stats.get("usage", 0)
        memory_limit = memory_stats.get("limit", 0)
        cache = memory_stats.get("stats", {}).get("cache", 0)
        memory_used_actual = max(0, memory_used - cache)

        networks = stats.get("networks", {})
        network_rx = sum(net.get("rx_bytes", 0) for net in networks.values())
        network_tx = sum(net.get("tx_bytes", 0) for net in networks.values())

        started_at = container.attrs.get("State", {}).get("StartedAt", "")
        uptime_seconds = 0
        if started_at:
            try:
                start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00").split(".")[0])
                uptime_seconds = int((datetime.now(start_time.tzinfo) - start_time).total_seconds())
            except Exception:
                pass

        return {
            "cpu_percent": round(cpu_percent, 1),
            "memory_used_bytes": memory_used_actual,
            "memory_limit_bytes": memory_limit,
            "memory_percent": round((memory_used_actual / memory_limit * 100) if memory_limit > 0 else 0, 1),
            "network_rx_bytes": network_rx,
            "network_tx_bytes": network_tx,
            "uptime_seconds": uptime_seconds,
            "status": container.status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")
