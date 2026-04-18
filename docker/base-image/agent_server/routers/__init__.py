"""
API routers for the agent server.
"""
from .chat import router as chat_router
from .activity import router as activity_router
from .credentials import router as credentials_router
from .git import router as git_router
from .files import router as files_router
from .trinity import router as trinity_router
from .info import router as info_router
from .dashboard import router as dashboard_router
from .skills import router as skills_router
from .snapshot import router as snapshot_router

__all__ = [
    "chat_router",
    "activity_router",
    "credentials_router",
    "git_router",
    "files_router",
    "trinity_router",
    "info_router",
    "dashboard_router",
    "skills_router",
    "snapshot_router",
]
