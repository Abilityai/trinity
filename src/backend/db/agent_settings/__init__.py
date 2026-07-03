"""
Agent settings submodules organized by concern.

Each module provides a mixin class that AgentOperations inherits from:
- SharingMixin: Agent sharing operations
- ResourcesMixin: Memory, CPU, timeout, parallel capacity
- SecurityMixin: Full capabilities, read-only mode
- AutonomyMixin: Autonomy mode, API key settings
- AvatarMixin: Avatar identity management
- MetadataMixin: Batch queries, rename operations
- GitPATMixin: Per-agent GitHub PAT management (#347)
- FileSharingMixin: Per-agent outbound file-sharing toggle (FILES-001)
- McpExposureMixin: Per-agent MCP-exposure toggle (#846)
"""

from .sharing import SharingMixin
from .resources import ResourcesMixin
from .security import SecurityMixin
from .autonomy import AutonomyMixin
from .avatar import AvatarMixin
from .metadata import MetadataMixin
from .access_policy import AccessPolicyMixin
from .git_pat import GitPATMixin
from .file_sharing import FileSharingMixin
from .mcp_exposure import McpExposureMixin
from .tts import TtsMixin

__all__ = [
    'SharingMixin',
    'ResourcesMixin',
    'SecurityMixin',
    'AutonomyMixin',
    'AvatarMixin',
    'MetadataMixin',
    'AccessPolicyMixin',
    'GitPATMixin',
    'FileSharingMixin',
    'McpExposureMixin',
    'TtsMixin',
]
