"""
Monitoring Alert Service (MON-001 Phase 2)

Evaluates health check results and sends alerts via the notification system.
Handles:
- Threshold evaluation for status changes
- Cooldown tracking to prevent alert storms
- Recovery notifications when agents return to healthy
- Alert prioritization based on severity
"""

import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from database import db
from db_models import (
    AgentHealthStatus,
    MonitoringConfig,
    NotificationCreate,
)
from utils.helpers import utc_now_iso


# Default cooldowns in seconds
DEFAULT_COOLDOWNS = {
    AgentHealthStatus.CRITICAL: 300,      # 5 min
    AgentHealthStatus.UNHEALTHY: 600,     # 10 min
    AgentHealthStatus.DEGRADED: 1800,     # 30 min
}

# Priority mapping
STATUS_PRIORITIES = {
    AgentHealthStatus.CRITICAL: "urgent",
    AgentHealthStatus.UNHEALTHY: "high",
    AgentHealthStatus.DEGRADED: "normal",
    AgentHealthStatus.HEALTHY: "normal",  # For recovery
}


class MonitoringAlertService:
    """
    Service for evaluating health checks and sending alerts.

    Usage:
        alert_service = MonitoringAlertService(config)
        await alert_service.evaluate_and_alert(
            agent_name="my-agent",
            previous_status="healthy",
            current_status="unhealthy",
            issues=["Network unreachable"]
        )
    """

    def __init__(self, config: MonitoringConfig = None):
        """Initialize with optional configuration."""
        self.config = config or MonitoringConfig()

        # In-memory cache for recent states (for recovery detection)
        self._previous_states: Dict[str, str] = {}

    def get_cooldown_seconds(self, status: AgentHealthStatus) -> int:
        """Get cooldown duration for a status level."""
        if status == AgentHealthStatus.CRITICAL:
            return self.config.critical_cooldown
        elif status == AgentHealthStatus.UNHEALTHY:
            return self.config.unhealthy_cooldown
        elif status == AgentHealthStatus.DEGRADED:
            return self.config.degraded_cooldown
        return 0  # No cooldown for healthy/unknown

    async def evaluate_and_alert(
        self,
        agent_name: str,
        previous_status: str,
        current_status: str,
        issues: List[str],
        details: Dict[str, Any] = None
    ) -> Optional[str]:
        """
        Evaluate health status change and send alert if needed.

        Args:
            agent_name: Name of the agent
            previous_status: Previous health status
            current_status: Current health status
            issues: List of issues detected
            details: Additional details for the alert

        Returns:
            Notification ID if alert was sent, None otherwise
        """
        # Normalize to enum for comparison
        try:
            prev = AgentHealthStatus(previous_status)
        except ValueError:
            prev = AgentHealthStatus.UNKNOWN

        try:
            curr = AgentHealthStatus(current_status)
        except ValueError:
            curr = AgentHealthStatus.UNKNOWN

        # Skip if no change
        if prev == curr:
            return None

        # Determine if this is a degradation or recovery
        is_degradation = self._is_degradation(prev, curr)
        is_recovery = self._is_recovery(prev, curr)

        if is_degradation:
            return await self._send_degradation_alert(
                agent_name, prev, curr, issues, details
            )
        elif is_recovery:
            return await self._send_recovery_alert(
                agent_name, prev, curr, details
            )

        return None

    def _is_degradation(
        self,
        prev: AgentHealthStatus,
        curr: AgentHealthStatus
    ) -> bool:
        """Check if status changed to a worse state."""
        severity_order = {
            AgentHealthStatus.HEALTHY: 0,
            AgentHealthStatus.DEGRADED: 1,
            AgentHealthStatus.UNHEALTHY: 2,
            AgentHealthStatus.CRITICAL: 3,
            AgentHealthStatus.UNKNOWN: 0,  # Unknown treated as healthy for alerts
        }
        return severity_order.get(curr, 0) > severity_order.get(prev, 0)

    def _is_recovery(
        self,
        prev: AgentHealthStatus,
        curr: AgentHealthStatus
    ) -> bool:
        """Check if status improved from unhealthy/degraded to healthy."""
        unhealthy_states = {
            AgentHealthStatus.CRITICAL,
            AgentHealthStatus.UNHEALTHY,
            AgentHealthStatus.DEGRADED,
        }
        return prev in unhealthy_states and curr == AgentHealthStatus.HEALTHY

    async def _send_degradation_alert(
        self,
        agent_name: str,
        prev: AgentHealthStatus,
        curr: AgentHealthStatus,
        issues: List[str],
        details: Dict[str, Any] = None
    ) -> Optional[str]:
        """Send an alert for a degradation in health status."""
        # Build condition key for cooldown
        condition = f"status:{curr.value}"

        # Check cooldown
        cooldown_seconds = self.get_cooldown_seconds(curr)
        if db.is_in_alert_cooldown(agent_name, condition, cooldown_seconds):
            return None

        # Build alert notification
        priority = STATUS_PRIORITIES.get(curr, "normal")
        title = f"Agent {agent_name} is {curr.value}"
        message = "; ".join(issues) if issues else f"Status changed from {prev.value} to {curr.value}"

        metadata = {
            "agent_name": agent_name,
            "previous_status": prev.value,
            "current_status": curr.value,
            "issues": issues,
            "check_timestamp": utc_now_iso(),
        }
        if details:
            metadata.update(details)

        # Create notification via NOTIF-001
        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=title,
                message=message,
                priority=priority,
                category="health",
                metadata=metadata
            )
        )

        # Set cooldown
        db.set_alert_cooldown(agent_name, condition)

        # Broadcast the notification
        await self._broadcast_alert(notification)

        return notification.id

    async def _send_recovery_alert(
        self,
        agent_name: str,
        prev: AgentHealthStatus,
        curr: AgentHealthStatus,
        details: Dict[str, Any] = None
    ) -> Optional[str]:
        """Send a notification when an agent recovers to healthy status."""
        # Clear any active cooldowns for this agent
        db.cleanup_alert_cooldowns(agent_name)

        # Build recovery notification
        title = f"Agent {agent_name} recovered"
        message = f"Status changed from {prev.value} to {curr.value}"

        metadata = {
            "agent_name": agent_name,
            "previous_status": prev.value,
            "current_status": curr.value,
            "recovery_timestamp": utc_now_iso(),
        }
        if details:
            metadata.update(details)

        # Create notification
        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="info",
                title=title,
                message=message,
                priority="normal",
                category="health",
                metadata=metadata
            )
        )

        # Broadcast the notification
        await self._broadcast_alert(notification)

        return notification.id

    async def _broadcast_alert(self, notification: 'Notification'):
        """Broadcast an alert via WebSocket."""
        # Import here to avoid circular dependency
        from routers.monitoring import _websocket_manager, _filtered_websocket_manager

        event = {
            "type": "monitoring_alert",
            "notification_id": notification.id,
            "agent_name": notification.agent_name,
            "alert_type": notification.notification_type,
            "priority": notification.priority,
            "title": notification.title,
            "timestamp": notification.created_at
        }
        event_json = json.dumps(event)

        if _websocket_manager:
            await _websocket_manager.broadcast(event_json)

        if _filtered_websocket_manager:
            await _filtered_websocket_manager.broadcast_filtered(event)

    # =========================================================================
    # Specific Alert Types
    # =========================================================================

    async def alert_container_stopped(
        self,
        agent_name: str,
        exit_code: int = None,
        oom_killed: bool = False
    ) -> Optional[str]:
        """Send alert when container stops unexpectedly."""
        condition = "container_stopped"

        if db.is_in_alert_cooldown(agent_name, condition, self.config.critical_cooldown):
            return None

        if oom_killed:
            title = f"Agent {agent_name} killed by OOM"
            message = "Container was killed due to out-of-memory condition"
        elif exit_code and exit_code != 0:
            title = f"Agent {agent_name} crashed"
            message = f"Container exited with code {exit_code}"
        else:
            title = f"Agent {agent_name} stopped"
            message = "Container stopped unexpectedly"

        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=title,
                message=message,
                priority="urgent" if oom_killed else "high",
                category="health",
                metadata={
                    "agent_name": agent_name,
                    "exit_code": exit_code,
                    "oom_killed": oom_killed,
                    "timestamp": utc_now_iso()
                }
            )
        )

        db.set_alert_cooldown(agent_name, condition)
        await self._broadcast_alert(notification)
        return notification.id

    async def alert_high_restart_count(
        self,
        agent_name: str,
        restart_count: int
    ) -> Optional[str]:
        """Send alert when restart count exceeds threshold."""
        condition = "high_restarts"

        if db.is_in_alert_cooldown(agent_name, condition, self.config.unhealthy_cooldown):
            return None

        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Agent {agent_name} restarting frequently",
                message=f"Container has restarted {restart_count} times",
                priority="high",
                category="health",
                metadata={
                    "agent_name": agent_name,
                    "restart_count": restart_count,
                    "timestamp": utc_now_iso()
                }
            )
        )

        db.set_alert_cooldown(agent_name, condition)
        await self._broadcast_alert(notification)
        return notification.id

    async def alert_stuck_execution(
        self,
        agent_name: str,
        execution_id: str,
        duration_minutes: int
    ) -> Optional[str]:
        """Send alert when execution is stuck."""
        condition = f"stuck:{execution_id}"

        if db.is_in_alert_cooldown(agent_name, condition, self.config.degraded_cooldown):
            return None

        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Agent {agent_name} has stuck execution",
                message=f"Execution {execution_id} has been running for {duration_minutes} minutes",
                priority="high",
                category="health",
                metadata={
                    "agent_name": agent_name,
                    "execution_id": execution_id,
                    "duration_minutes": duration_minutes,
                    "timestamp": utc_now_iso()
                }
            )
        )

        db.set_alert_cooldown(agent_name, condition)
        await self._broadcast_alert(notification)
        return notification.id

    async def alert_resource_critical(
        self,
        agent_name: str,
        resource_type: str,  # "cpu" or "memory"
        percent: float
    ) -> Optional[str]:
        """Send alert for critical resource usage."""
        condition = f"resource:{resource_type}"

        if db.is_in_alert_cooldown(agent_name, condition, self.config.degraded_cooldown):
            return None

        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Agent {agent_name} has critical {resource_type.upper()} usage",
                message=f"{resource_type.upper()} usage at {percent:.1f}%",
                priority="high",
                category="health",
                metadata={
                    "agent_name": agent_name,
                    "resource_type": resource_type,
                    "usage_percent": percent,
                    "timestamp": utc_now_iso()
                }
            )
        )

        db.set_alert_cooldown(agent_name, condition)
        await self._broadcast_alert(notification)
        return notification.id

    async def alert_heartbeat_lost(
        self,
        agent_name: str,
        missed_beats: int
    ) -> Optional[str]:
        """Send a soft alert when an agent stops sending liveness heartbeats.

        RELIABILITY-004 / #307: fired by the heartbeat watch loop on the
        alive->stale transition (after the N-consecutive-miss guard). Soft
        severity + the existing cooldown keep a transient agent->backend POST
        failure from storming the operator — the 30s monitoring loop stays the
        authoritative status signal.
        """
        condition = "heartbeat_lost"

        if db.is_in_alert_cooldown(agent_name, condition, self.config.degraded_cooldown):
            return None

        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Agent {agent_name} heartbeat lost",
                message=f"No liveness heartbeat for {missed_beats} consecutive checks",
                priority="high",
                category="health",
                metadata={
                    "agent_name": agent_name,
                    "missed_beats": missed_beats,
                    "timestamp": utc_now_iso()
                }
            )
        )

        db.set_alert_cooldown(agent_name, condition)
        await self._broadcast_alert(notification)
        return notification.id

    async def alert_heartbeat_recovered(
        self,
        agent_name: str
    ) -> Optional[str]:
        """Send a notification when an agent's heartbeat returns (#307).

        Fired only when the watch loop had previously downgraded the agent, so
        the operator who saw the loss also sees the recovery. No cooldown — a
        recovery is rare and always worth surfacing.
        """
        condition = "heartbeat_recovered"

        notification = db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Agent {agent_name} heartbeat recovered",
                message="Liveness heartbeat resumed",
                priority="normal",
                category="health",
                metadata={
                    "agent_name": agent_name,
                    "timestamp": utc_now_iso()
                }
            )
        )

        db.set_alert_cooldown(agent_name, condition)
        await self._broadcast_alert(notification)
        return notification.id


# Global service instance
_alert_service: Optional[MonitoringAlertService] = None


def get_alert_service() -> MonitoringAlertService:
    """Get or create the global alert service instance."""
    global _alert_service
    if _alert_service is None:
        _alert_service = MonitoringAlertService()
    return _alert_service
