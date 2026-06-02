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
    validate_base_image,
)
from .lifecycle import (
    inject_assigned_credentials,
    start_agent_internal,
    recreate_container_with_updated_config,
)
from .crud import (
    create_agent_internal,
)
from .deploy import (
    deploy_local_agent_logic,
    MAX_ARCHIVE_SIZE,
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
    delete_agent_file_logic,
    preview_agent_file_logic,
    update_agent_file_logic,
    create_agent_folder_logic,
)
from .queue import (
    get_agent_queue_status_logic,
    clear_agent_queue_logic,
    force_release_agent_logic,
)
from .metrics import (
    get_agent_metrics_logic,
)
from .dashboard import (
    get_agent_dashboard_logic,
)
from .stats import (
    get_agents_context_stats_logic,
    get_agent_stats_logic,
    invalidate_context_stats_cache,
    invalidate_agent_stats_cache,
)
from .api_key import (
    get_agent_api_key_setting_logic,
    update_agent_api_key_setting_logic,
)
from .autonomy import (
    get_autonomy_status_logic,
    set_autonomy_status_logic,
    get_all_autonomy_status_logic,
)
from .read_only import (
    get_read_only_status_logic,
    set_read_only_status_logic,
    inject_read_only_hooks,
    remove_read_only_hooks,
)
from .file_sharing import (
    get_file_sharing_status_logic,
    set_file_sharing_status_logic,
    check_public_folder_mount_matches,
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
    "validate_base_image",
    # Lifecycle
    "inject_assigned_credentials",
    "start_agent_internal",
    "recreate_container_with_updated_config",
    # CRUD
    "create_agent_internal",
    # Deploy
    "deploy_local_agent_logic",
    "MAX_ARCHIVE_SIZE",
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
    "delete_agent_file_logic",
    "preview_agent_file_logic",
    "update_agent_file_logic",
    "create_agent_folder_logic",
    # Queue
    "get_agent_queue_status_logic",
    "clear_agent_queue_logic",
    "force_release_agent_logic",
    # Metrics
    "get_agent_metrics_logic",
    # Dashboard
    "get_agent_dashboard_logic",
    # Stats
    "get_agents_context_stats_logic",
    "get_agent_stats_logic",
    "invalidate_context_stats_cache",
    "invalidate_agent_stats_cache",
    # API Key
    "get_agent_api_key_setting_logic",
    "update_agent_api_key_setting_logic",
    # Autonomy
    "get_autonomy_status_logic",
    "set_autonomy_status_logic",
    "get_all_autonomy_status_logic",
    # Read-Only Mode
    "get_read_only_status_logic",
    "set_read_only_status_logic",
    "inject_read_only_hooks",
    "remove_read_only_hooks",
    # File Sharing (FILES-001 Step 2)
    "get_file_sharing_status_logic",
    "set_file_sharing_status_logic",
    "check_public_folder_mount_matches",
]
