"""Git sync operations for GitHub-native agents.

#1028: this was one 2,322-line module. It is now a package of six
responsibility modules — conflicts, gitignore, remotes, trinity_files, sync,
provisioning — with the full public surface re-exported here, so
`from services.git_service import sync_to_github` and
`git_service.sync_to_github` are unchanged.

Collaborators (`db`, `execute_command_in_container`, …) are deliberately NOT
re-exported: tests patch them as module attributes, and after the split such a
patch must land on the module that owns the function under test. An absent
attribute makes a stale patch raise AttributeError instead of applying to a
module nobody reads — the loud direction.

Cross-module calls inside the package go through the sibling module object
(`gitignore._detect_git_dir(...)`), never a from-import of the function, so a
patch on the owning module reaches every caller.
"""
from .conflicts import (  # noqa: F401
    NO_WRITE_CREDENTIALS_MESSAGE,
    ConflictClass,
    classify_conflict,
)
# Private names are re-exported ONLY where another src/backend module imports
# them (`lifecycle` reads `_git_auto_sync_baked`, `deploy` reads
# `_detect_git_dir`, …) or where a test reads them as DATA
# (`_GITIGNORE_PATTERNS`). A private FUNCTION that exists here as well as on
# its owning module can be monkeypatched on the wrong one and silently detach
# — which is exactly how test_2069's readiness probes went dark during this
# split — so the collaborator-shaped ones are deliberately not mirrored.
from .gitignore import (  # noqa: F401
    _GITIGNORE_PATTERNS,
    _TRINITY_AUTHORED_PATHS,
    _detect_git_dir,
    _git_auto_sync_baked,
    merge_gitignore_after_clone,
    spawn_gitignore_merge_after_clone,
)
from .provisioning import (  # noqa: F401
    GitInitResult,
    check_git_initialized,
    check_remote_branch_exists,
    create_git_config_for_agent,
    generate_instance_id,
    generate_working_branch,
    initialize_git_in_container,
    probe_anonymous_repo_access,
    reserve_and_generate_instance_id,
)
from .remotes import (  # noqa: F401
    ContainerGitState,
    RebindResult,
    _git_remote_url,
    _parse_repo_from_remote_url,
    _scrub_git_output,
    inspect_container_git,
    rebind_origin_and_push,
    update_remote_pat,
)
from .sync import (  # noqa: F401
    _agent_has_write_credentials,
    delete_agent_git_config,
    get_agent_git_config,
    get_git_log,
    get_git_status,
    pull_from_github,
    reset_to_main_preserve_state,
    sync_to_github,
)
from .trinity_files import (  # noqa: F401
    DEFAULT_DATA_PATHS,
    DEFAULT_PERSISTENT_STATE,
    _SAFE_DATA_PATH_RE,
    _data_paths_for,
    _is_safe_data_path,
    materialize_data_paths,
    materialize_persistent_state,
    materialize_plugins,
    materialize_trinity_yaml_list,
)
