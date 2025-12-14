"""
Database submodules for domain-specific operations.

This package provides modular database access organized by domain:
- users: User management operations
- agents: Agent ownership and sharing
- mcp_keys: MCP API key management
- schedules: Schedule and execution management
- chat: Chat session and message persistence
- activities: Activity stream logging
- permissions: Agent-to-agent permission management
- shared_folders: Shared folder configuration

All functions are re-exported here for backward compatibility.
"""

from .users import UserOperations
from .agents import AgentOperations
from .mcp_keys import McpKeyOperations
from .schedules import ScheduleOperations
from .chat import ChatOperations
from .activities import ActivityOperations
from .shared_folders import SharedFolderOperations
from .settings import SettingsOperations

__all__ = [
    'UserOperations',
    'AgentOperations',
    'McpKeyOperations',
    'ScheduleOperations',
    'ChatOperations',
    'ActivityOperations',
    'SharedFolderOperations',
    'SettingsOperations',
]
