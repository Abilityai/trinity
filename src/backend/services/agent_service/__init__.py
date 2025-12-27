"""
Agent Service - Business logic for agent operations.

This module contains the extracted business logic from routers/agents.py.
The router remains the single import point for external modules.
"""
from .helpers import (
    get_accessible_agents,
    sanitize_and_validate_name,
    get_agents_by_prefix,
    get_next_version_name,
    get_latest_version,
    check_shared_folder_mounts_match,
    check_api_key_env_matches,
)
from .lifecycle import (
    inject_trinity_meta_prompt,
    start_agent_internal,
    recreate_container_with_updated_config,
)
from .crud import (
    create_agent_internal,
)
from .deploy import (
    deploy_local_agent_logic,
    MAX_ARCHIVE_SIZE,
    MAX_CREDENTIALS,
    MAX_FILES,
)
from .terminal import (
    TerminalSessionManager,
)
from .permissions import (
    get_agent_permissions_logic,
    set_agent_permissions_logic,
    add_agent_permission_logic,
    remove_agent_permission_logic,
)
from .folders import (
    get_agent_folders_logic,
    update_agent_folders_logic,
    get_available_shared_folders_logic,
    get_folder_consumers_logic,
)
from .files import (
    list_agent_files_logic,
    download_agent_file_logic,
)
from .queue import (
    get_agent_queue_status_logic,
    clear_agent_queue_logic,
    force_release_agent_logic,
)
from .metrics import (
    get_agent_metrics_logic,
)
from .stats import (
    get_agents_context_stats_logic,
    get_agent_stats_logic,
)
from .api_key import (
    get_agent_api_key_setting_logic,
    update_agent_api_key_setting_logic,
)

__all__ = [
    # Helpers
    "get_accessible_agents",
    "sanitize_and_validate_name",
    "get_agents_by_prefix",
    "get_next_version_name",
    "get_latest_version",
    "check_shared_folder_mounts_match",
    "check_api_key_env_matches",
    # Lifecycle
    "inject_trinity_meta_prompt",
    "start_agent_internal",
    "recreate_container_with_updated_config",
    # CRUD
    "create_agent_internal",
    # Deploy
    "deploy_local_agent_logic",
    "MAX_ARCHIVE_SIZE",
    "MAX_CREDENTIALS",
    "MAX_FILES",
    # Terminal
    "TerminalSessionManager",
    # Permissions
    "get_agent_permissions_logic",
    "set_agent_permissions_logic",
    "add_agent_permission_logic",
    "remove_agent_permission_logic",
    # Folders
    "get_agent_folders_logic",
    "update_agent_folders_logic",
    "get_available_shared_folders_logic",
    "get_folder_consumers_logic",
    # Files
    "list_agent_files_logic",
    "download_agent_file_logic",
    # Queue
    "get_agent_queue_status_logic",
    "clear_agent_queue_logic",
    "force_release_agent_logic",
    # Metrics
    "get_agent_metrics_logic",
    # Stats
    "get_agents_context_stats_logic",
    "get_agent_stats_logic",
    # API Key
    "get_agent_api_key_setting_logic",
    "update_agent_api_key_setting_logic",
]
