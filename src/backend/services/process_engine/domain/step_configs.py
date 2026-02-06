"""
Process Engine Step Configurations

Configuration classes for different step types.
These are Value Objects that define step behavior.

Reference: IT3 Section 4.3 (Value Objects - StepConfig)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .value_objects import Duration


@dataclass(frozen=True)
class AgentTaskConfig:
    """
    Configuration for agent_task step type.

    Defines which agent to use and what message to send.
    """
    agent: str  # Agent name to execute the task
    message: str  # Message/prompt to send to agent (supports {{}} templating)
    timeout: Duration = field(default_factory=lambda: Duration.from_minutes(5))

    # Optional: specific model or parameters
    model: Optional[str] = None
    temperature: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> AgentTaskConfig:
        """Create from dictionary (YAML parsing)."""
        timeout = data.get("timeout", "5m")
        if isinstance(timeout, str):
            timeout = Duration.from_string(timeout)
        elif isinstance(timeout, int):
            timeout = Duration.from_seconds(timeout)

        return cls(
            agent=data["agent"],
            message=data["message"],
            timeout=timeout,
            model=data.get("model"),
            temperature=data.get("temperature"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "agent": self.agent,
            "message": self.message,
            "timeout": str(self.timeout),
        }
        if self.model:
            result["model"] = self.model
        if self.temperature is not None:
            result["temperature"] = self.temperature
        return result


@dataclass(frozen=True)
class HumanApprovalConfig:
    """
    Configuration for human_approval step type (stub for Core phase).

    Defines approval requirements and assignees.
    """
    title: str  # Title shown in approval UI
    description: str  # Description of what needs approval
    assignees: list[str] = field(default_factory=list)  # User IDs or emails
    timeout: Duration = field(default_factory=lambda: Duration.from_hours(24))

    # Options for approval
    allow_comments: bool = True
    require_reason_on_reject: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> HumanApprovalConfig:
        """Create from dictionary (YAML parsing)."""
        timeout = data.get("timeout", "24h")
        if isinstance(timeout, str):
            timeout = Duration.from_string(timeout)
        elif isinstance(timeout, int):
            timeout = Duration.from_seconds(timeout)

        return cls(
            title=data.get("title", "Approval Required"),
            description=data.get("description", ""),
            assignees=data.get("assignees", []),
            timeout=timeout,
            allow_comments=data.get("allow_comments", True),
            require_reason_on_reject=data.get("require_reason_on_reject", True),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "title": self.title,
            "description": self.description,
            "assignees": self.assignees,
            "timeout": str(self.timeout),
            "allow_comments": self.allow_comments,
            "require_reason_on_reject": self.require_reason_on_reject,
        }


@dataclass(frozen=True)
class GatewayConfig:
    """
    Configuration for gateway step type (stub for Core phase).

    Defines conditional routing based on expressions.
    """
    gateway_type: str = "exclusive"  # exclusive, parallel, inclusive
    routes: list[dict] = field(default_factory=list)  # List of {condition, target}
    default_route: Optional[str] = None  # Default target if no condition matches

    @classmethod
    def from_dict(cls, data: dict) -> GatewayConfig:
        """Create from dictionary (YAML parsing)."""
        return cls(
            gateway_type=data.get("gateway_type", "exclusive"),
            routes=data.get("routes", []),
            default_route=data.get("default_route"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "gateway_type": self.gateway_type,
            "routes": self.routes,
        }
        if self.default_route:
            result["default_route"] = self.default_route
        return result


@dataclass(frozen=True)
class TimerConfig:
    """
    Configuration for timer step type (stub for Advanced phase).

    Defines delays or scheduled execution.
    """
    delay: Duration = field(default_factory=lambda: Duration.from_minutes(1))

    @classmethod
    def from_dict(cls, data: dict) -> TimerConfig:
        """Create from dictionary (YAML parsing)."""
        delay = data.get("delay", "1m")
        if isinstance(delay, str):
            delay = Duration.from_string(delay)
        elif isinstance(delay, int):
            delay = Duration.from_seconds(delay)
        return cls(delay=delay)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {"delay": str(self.delay)}


@dataclass(frozen=True)
class NotificationConfig:
    """
    Configuration for notification step type.

    Defines notification channel, recipients, and message template.
    Supports Slack webhooks, Telegram, email (email requires SMTP config).

    Reference: BACKLOG_CORE.md - E14-01
    """
    channel: str = "slack"  # slack, telegram, email, webhook
    message: str = ""  # Message template with {{...}} substitution

    # Slack-specific
    webhook_url: Optional[str] = None  # Slack webhook URL (can use env var)

    # Telegram-specific
    bot_token: Optional[str] = None  # Telegram bot token (can use env var)
    chat_id: Optional[str] = None  # Telegram chat ID (can use env var)

    # Email-specific
    recipients: list[str] = field(default_factory=list)  # Email addresses
    subject: str = ""  # Email subject

    # Generic webhook
    url: Optional[str] = None  # Generic webhook URL

    @classmethod
    def from_dict(cls, data: dict) -> NotificationConfig:
        """Create from dictionary (YAML parsing)."""
        return cls(
            channel=data.get("channel", "slack"),
            message=data.get("message", data.get("template", "")),
            webhook_url=data.get("webhook_url"),
            bot_token=data.get("bot_token"),
            chat_id=data.get("chat_id"),
            recipients=data.get("recipients", []),
            subject=data.get("subject", ""),
            url=data.get("url"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "channel": self.channel,
            "message": self.message,
        }
        if self.webhook_url:
            result["webhook_url"] = self.webhook_url
        if self.bot_token:
            result["bot_token"] = self.bot_token
        if self.chat_id:
            result["chat_id"] = self.chat_id
        if self.recipients:
            result["recipients"] = self.recipients
        if self.subject:
            result["subject"] = self.subject
        if self.url:
            result["url"] = self.url
        return result


@dataclass(frozen=True)
class SubProcessConfig:
    """
    Configuration for sub_process step type.

    Allows calling another process as a step, enabling composable workflows.
    The parent process waits for the child to complete and receives its output.

    Reference: BACKLOG_ADVANCED.md - E10-01
    """
    process_name: str  # Name of process to call
    version: Optional[str] = None  # Specific version or None for latest published
    input_mapping: dict = field(default_factory=dict)  # Map parent context to child input
    output_key: str = "result"  # Key to store child output in parent context
    wait_for_completion: bool = True  # Sync (True) vs async (False)
    timeout: Duration = field(default_factory=lambda: Duration.from_hours(1))

    @classmethod
    def from_dict(cls, data: dict) -> "SubProcessConfig":
        """Create from dictionary (YAML parsing)."""
        timeout = data.get("timeout", "1h")
        if isinstance(timeout, str):
            timeout = Duration.from_string(timeout)
        elif isinstance(timeout, int):
            timeout = Duration.from_seconds(timeout)

        return cls(
            process_name=data["process_name"],
            version=data.get("version"),
            input_mapping=data.get("input_mapping", {}),
            output_key=data.get("output_key", "result"),
            wait_for_completion=data.get("wait_for_completion", True),
            timeout=timeout,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "process_name": self.process_name,
            "output_key": self.output_key,
            "wait_for_completion": self.wait_for_completion,
            "timeout": str(self.timeout),
        }
        if self.version:
            result["version"] = self.version
        if self.input_mapping:
            result["input_mapping"] = self.input_mapping
        return result


# Type alias for step configuration
StepConfig = AgentTaskConfig | HumanApprovalConfig | GatewayConfig | TimerConfig | NotificationConfig | SubProcessConfig


# =============================================================================
# Compensation Configuration
# =============================================================================


@dataclass(frozen=True)
class CompensationConfig:
    """
    Configuration for step compensation (rollback action).

    Compensation actions are executed when a process fails after a step
    has completed. They allow cleanup/rollback of side effects.

    Supports two types:
    - agent_task: Send a message to an agent to perform rollback
    - notification: Send a notification about the failure

    Reference: BACKLOG_MVP.md - E13-03
    """
    type: str = "agent_task"  # "agent_task" | "notification"

    # For agent_task compensation
    agent: Optional[str] = None
    message: str = ""  # Message template with {{...}} substitution
    timeout: Duration = field(default_factory=lambda: Duration.from_minutes(5))

    # For notification compensation
    channel: str = "slack"  # slack, email, webhook
    webhook_url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "CompensationConfig":
        """Create from dictionary (YAML parsing)."""
        if not data:
            return cls()

        timeout = data.get("timeout", "5m")
        if isinstance(timeout, str):
            timeout = Duration.from_string(timeout)
        elif isinstance(timeout, int):
            timeout = Duration.from_seconds(timeout)

        return cls(
            type=data.get("type", "agent_task"),
            agent=data.get("agent"),
            message=data.get("message", ""),
            timeout=timeout,
            channel=data.get("channel", "slack"),
            webhook_url=data.get("webhook_url"),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {"type": self.type}

        if self.type == "agent_task":
            if self.agent:
                result["agent"] = self.agent
            if self.message:
                result["message"] = self.message
            result["timeout"] = str(self.timeout)
        elif self.type == "notification":
            result["channel"] = self.channel
            if self.message:
                result["message"] = self.message
            if self.webhook_url:
                result["webhook_url"] = self.webhook_url

        return result


# =============================================================================
# Trigger Configurations
# =============================================================================


@dataclass(frozen=True)
class WebhookTriggerConfig:
    """
    Configuration for webhook triggers.

    Allows processes to be started via HTTP POST.

    Reference: BACKLOG_CORE.md - E8-01
    """
    id: str  # Unique trigger ID (used in webhook URL)
    enabled: bool = True
    secret: Optional[str] = None  # Optional secret for authentication
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "WebhookTriggerConfig":
        """Create from dictionary (YAML parsing)."""
        return cls(
            id=data.get("id", ""),
            enabled=data.get("enabled", True),
            secret=data.get("secret"),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "type": "webhook",
            "id": self.id,
            "enabled": self.enabled,
            "description": self.description,
        }
        # Don't serialize secret in output
        return result


@dataclass(frozen=True)
class ManualTriggerConfig:
    """
    Configuration for manual triggers.

    Allows processes to be started manually via UI or API.
    This is the simplest trigger type - just a marker that manual start is allowed.
    """
    id: str  # Unique trigger ID
    enabled: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ManualTriggerConfig":
        """Create from dictionary (YAML parsing)."""
        return cls(
            id=data.get("id", ""),
            enabled=data.get("enabled", True),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "type": "manual",
            "id": self.id,
            "enabled": self.enabled,
            "description": self.description,
        }


# =============================================================================
# Cron Presets
# =============================================================================

CRON_PRESETS: dict[str, str] = {
    "hourly": "0 * * * *",      # Every hour at :00
    "daily": "0 9 * * *",       # Every day at 9:00 AM
    "weekly": "0 9 * * 1",      # Every Monday at 9:00 AM
    "monthly": "0 9 1 * *",     # 1st of each month at 9:00 AM
    "weekdays": "0 9 * * 1-5",  # Weekdays at 9:00 AM
}


def expand_cron_preset(cron: str) -> str:
    """
    Expand cron preset to full expression.

    Args:
        cron: Either a preset name (hourly, daily, etc.) or a cron expression

    Returns:
        Full 5-field cron expression
    """
    return CRON_PRESETS.get(cron, cron)


@dataclass(frozen=True)
class ScheduleTriggerConfig:
    """
    Configuration for scheduled triggers.

    Allows processes to run on a cron schedule.

    Supports human-readable presets:
    - hourly: Every hour at :00
    - daily: Every day at 9:00 AM
    - weekly: Every Monday at 9:00 AM
    - monthly: 1st of each month at 9:00 AM
    - weekdays: Weekdays at 9:00 AM

    Or standard 5-field cron expressions: "minute hour day month day_of_week"
    """
    id: str
    cron: str = ""  # Cron expression or preset (e.g., "daily", "0 9 * * 1-5")
    enabled: bool = True
    timezone: str = "UTC"
    description: str = ""

    @property
    def cron_expression(self) -> str:
        """Get the full cron expression (expands presets)."""
        return expand_cron_preset(self.cron)

    @property
    def is_preset(self) -> bool:
        """Check if using a preset rather than explicit cron."""
        return self.cron in CRON_PRESETS

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleTriggerConfig":
        """Create from dictionary (YAML parsing)."""
        return cls(
            id=data.get("id", ""),
            cron=data.get("cron", ""),
            enabled=data.get("enabled", True),
            timezone=data.get("timezone", "UTC"),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "type": "schedule",
            "id": self.id,
            "cron": self.cron,
            "cron_expression": self.cron_expression,  # Include expanded form
            "enabled": self.enabled,
            "timezone": self.timezone,
            "description": self.description,
        }


# Type alias for trigger configurations
TriggerConfig = ManualTriggerConfig | WebhookTriggerConfig | ScheduleTriggerConfig


def parse_trigger_config(trigger_data: dict) -> TriggerConfig:
    """
    Parse trigger configuration based on type.

    Args:
        trigger_data: Dictionary with trigger configuration

    Returns:
        Appropriate TriggerConfig subclass instance
    """
    trigger_type = trigger_data.get("type", "webhook")

    if trigger_type == "manual":
        return ManualTriggerConfig.from_dict(trigger_data)
    elif trigger_type == "webhook":
        return WebhookTriggerConfig.from_dict(trigger_data)
    elif trigger_type == "schedule":
        return ScheduleTriggerConfig.from_dict(trigger_data)
    else:
        raise ValueError(f"Unknown trigger type: {trigger_type}")


def parse_step_config(step_type: str, config_data: dict) -> StepConfig:
    """
    Parse step configuration based on step type.

    Args:
        step_type: The type of step (agent_task, human_approval, etc.)
        config_data: Dictionary with configuration data

    Returns:
        Appropriate StepConfig subclass instance
    """
    parsers = {
        "agent_task": AgentTaskConfig.from_dict,
        "human_approval": HumanApprovalConfig.from_dict,
        "gateway": GatewayConfig.from_dict,
        "timer": TimerConfig.from_dict,
        "notification": NotificationConfig.from_dict,
        "sub_process": SubProcessConfig.from_dict,
    }

    parser = parsers.get(step_type)
    if not parser:
        raise ValueError(f"Unknown step type: {step_type}")

    return parser(config_data)
