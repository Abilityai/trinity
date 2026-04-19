# Feature: GitHub Repository Initialization

## Overview
Allows users to initialize GitHub synchronization for any existing agent by configuring a GitHub Personal Access Token (PAT) in Settings, then using a modal form to create a GitHub repository and push the agent's workspace to it. This enables bidirectional sync for agents that were created without Git support.

## User Story
As a **Trinity platform user**, I want to **enable GitHub synchronization for an existing agent** so that **I can version-control the agent's workspace and collaborate via GitHub without recreating the agent**.

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-04-19 | S7 Layers 0/2 (#382): consolidated three independent `generate_instance_id()` call sites behind new `reserve_and_generate_instance_id()` helper in `services/git_service.py:115-206`. Reservation now atomically (a) generates a UUID, (b) probes the remote with `git ls-remote --heads --exit-code` (Layer 1), and (c) inserts the `agent_git_config` row under a new partial UNIQUE index `UNIQUE(github_repo, working_branch) WHERE source_mode = 0` (Layer 2). Retries up to `MAX_INSTANCE_ID_RETRIES` (5) on either remote or DB collision before raising `RuntimeError`. The reservation is performed BEFORE container creation in `agent_service/crud.py` and BEFORE `initialize_git_in_container` in `routers/git.py`, and is rolled back via `db.delete_git_config(...)` if any later step fails so a retry can claim a fresh branch. The duplicate post-container `db.create_git_config` call previously made by `crud.py` was removed. Schema added in `db/schema.py:757-758`; existing databases get the index via migration `agent_git_config_branch_ownership` (`db/migrations.py:1454-1511`), which refuses to install when `_find_duplicate_working_branches` finds existing collisions and prints every offending row so the operator can rebind one of the duplicates first. Failure mode: agent creation that previously could silently bind to an in-use working branch now fails fast with `RuntimeError` after 5 reservation attempts. Regression coverage: `tests/git-sync/test_s7_reserve_instance_id.py`. (Layer 3 push-time `--force-with-lease` guard documented in `github-sync.md`.) |
| 2026-04-10 | Fix (#256): `initialize_git_in_container` now pushes after commit in the `remote_has_main` code path. Previously the workspace was committed locally inside the container but never reached GitHub when the target repo already had a `main` branch, making the UI report success while no files synced. |
| 2026-02-11 | Added LEGACY notes for workspace path checks - new agents (2026-02+) use `/home/developer` directly, workspace checks exist for backward compatibility with pre-2026-02 agents |
| 2026-01-23 | Verified line numbers against current implementation, updated MCP tool lines (530-604), verified git_service.py structure |
| 2025-12-31 | Refactored to clean architecture with service layer |

---

## Architecture Overview (Updated 2026-01-23)

The feature was refactored to follow clean architecture patterns:

| Layer | Old Location | New Location | Purpose |
|-------|--------------|--------------|---------|
| GitHub API | `routers/git.py` (inline) | `services/github_service.py` | All GitHub REST API calls |
| Git Init | `routers/git.py` (inline) | `services/git_service.py` | Container git operations |
| Access Control | `routers/git.py` (inline) | `dependencies.py` | `OwnedAgentByName` dependency |
| Settings | `routers/settings.py` | `services/settings_service.py` | `get_github_pat()` function |

**Key Benefits**:
- Router reduced from ~280 lines to ~115 lines
- Testable service classes
- Reusable GitHub client
- Type-safe dataclasses for API responses

---

## Entry Points

| Type | Location | Description |
|------|----------|-------------|
| **UI** | `src/frontend/src/views/Settings.vue:126-218` | GitHub PAT configuration with test/save |
| **UI** | `src/frontend/src/components/GitPanel.vue:17-26` | "Initialize GitHub Sync" button |
| **UI** | `src/frontend/src/components/GitPanel.vue:29-166` | Initialize modal form |
| **API** | `POST /api/agents/{agent_name}/git/initialize` | Initialize git sync endpoint |
| **MCP** | `initialize_github_sync` tool | MCP tool for programmatic initialization |

---

## Sequence Diagram

```mermaid
sequenceDiagram
    participant User
    participant Settings
    participant GitPanel
    participant Store
    participant Backend
    participant GitService
    participant GitHubService
    participant GitHub
    participant Container

    Note over User,Container: Phase 1: Configure GitHub PAT
    User->>Settings: Navigate to Settings page
    Settings->>Settings: Load API key status
    User->>Settings: Enter GitHub PAT
    User->>Settings: Click "Test" button
    Settings->>Backend: POST /api/settings/api-keys/github/test
    Backend->>GitHub: GET /user (validate token)
    GitHub->>Backend: Return user info + permissions
    Backend->>Settings: {valid: true, username, has_repo_access}
    Settings->>User: Show success with username
    User->>Settings: Click "Save" button
    Settings->>Backend: PUT /api/settings/api-keys/github
    Backend->>Backend: Store in system_settings table
    Backend->>Settings: {success: true, masked: "...xxxx"}

    Note over User,Container: Phase 2: Initialize Git Sync
    User->>GitPanel: Navigate to agent's Git tab
    GitPanel->>GitPanel: Detect git_enabled: false
    GitPanel->>User: Show "Initialize GitHub Sync" button
    User->>GitPanel: Click "Initialize GitHub Sync"
    GitPanel->>User: Open modal form
    User->>GitPanel: Enter repo_owner, repo_name
    User->>GitPanel: Check "Create repository" + "Private"
    User->>GitPanel: Click "Initialize"
    GitPanel->>Store: initializeGitHub(agentName, config)
    Store->>Backend: POST /api/agents/{name}/git/initialize (120s timeout)

    Note over Backend,Container: Phase 3: Backend Processing (Refactored)
    Backend->>Backend: OwnedAgentByName dependency validates ownership
    Backend->>Backend: Verify agent is running
    Backend->>GitService: check_git_initialized() - detect orphaned config
    Backend->>Backend: Clean up orphaned records if necessary
    Backend->>SettingsService: get_github_pat()
    Backend->>GitHubService: check_repo_exists(owner, name)
    GitHubService->>GitHub: GET /repos/{owner}/{name}
    alt Repository doesn't exist
        Backend->>GitHubService: create_repository(owner, name, private)
        GitHubService->>GitHub: POST /orgs/{owner}/repos or /user/repos
        GitHub->>GitHubService: 201 Created
        GitHubService->>Backend: GitHubCreateResult(success=True)
    else Repository exists
        GitHub->>GitHubService: 200 OK
        GitHubService->>Backend: GitHubRepoInfo(exists=True)
    end

    Note over Backend,Container: Phase 4a: Reserve Working Branch (S7 Layer 0)
    Backend->>GitService: reserve_and_generate_instance_id(agent, repo)
    loop up to MAX_INSTANCE_ID_RETRIES (5)
        GitService->>GitService: generate_instance_id() (UUID prefix)
        GitService->>GitService: working_branch = trinity/{agent}/{id}
        GitService->>GitHub: git ls-remote --heads --exit-code (Layer 1)
        alt Remote already has branch
            Note over GitService: Retry with fresh UUID
        else Remote clear
            GitService->>GitService: db.create_git_config() (Layer 2 partial UNIQUE)
            alt sqlite3.IntegrityError on (repo, branch)
                Note over GitService: Retry with fresh UUID
            else Insert succeeds
                GitService->>Backend: (instance_id, working_branch)
            end
        end
    end
    Note over GitService: After 5 collisions: raise RuntimeError

    Note over Backend,Container: Phase 4b: Git Initialization (via git_service)
    Backend->>GitService: initialize_git_in_container(working_branch=reserved, create_working_branch=False)
    GitService->>Container: Check for existing git directory
    alt Has existing /home/developer/workspace with content (LEGACY)
        Note over GitService: LEGACY: Agents created before 2026-02
        GitService->>GitService: Use /home/developer/workspace
    else Standard (new agents)
        GitService->>GitService: Use /home/developer
        GitService->>Container: Create .gitignore
    end
    GitService->>Container: Execute git init, add, commit, push
    GitService->>Container: Check out + push pre-reserved working branch
    GitService->>Container: Verify git directory
    GitService->>Backend: GitInitResult(success=True, git_dir, working_branch)
    Note over Backend: On any failure: db.delete_git_config(agent) to release reservation

    Note over Backend,Container: Phase 5: Response
    Backend->>Store: Return success + repo details (config row already persisted in Phase 4a)
    Store->>GitPanel: Close modal
    GitPanel->>GitPanel: Reload git status
    GitPanel->>User: Show git sync UI with remote URL
```

---

## Frontend Layer

### Settings Page - GitHub PAT Configuration

**Location**: `src/frontend/src/views/Settings.vue`

| Lines | Component | Description |
|-------|-----------|-------------|
| 126-218 | GitHub PAT section | Input field with test/save buttons |
| 128-130 | Label | "GitHub Personal Access Token (PAT)" |
| 133-140 | Password input | Toggleable visibility, masked placeholder |
| 155-165 | Test button | Calls `testGithubPat()` |
| 166-177 | Save button | Calls `saveGithubPat()` |
| 179-210 | Status display | Shows validation result with permissions info |
| 211-217 | Help text | Links to GitHub token creation |

**Key Methods**:

```javascript
// Line 592-630: Test GitHub PAT
async function testGithubPat() {
  const response = await axios.post('/api/settings/api-keys/github/test', {
    api_key: githubPat.value
  })

  if (response.data.valid) {
    // Display username, token type (fine-grained/classic), repo permissions
    githubPatTestMessage.value = `Valid! GitHub user: ${response.data.username} ...`
  }
}

// Line 633-663: Save GitHub PAT
async function saveGithubPat() {
  const response = await axios.put('/api/settings/api-keys/github', {
    api_key: githubPat.value
  })

  // Update status, clear input, show success
  githubPatStatus.value = {
    configured: true,
    masked: response.data.masked,
    source: 'settings'
  }
}
```

### GitPanel Component - Initialize UI

**Location**: `src/frontend/src/components/GitPanel.vue`

| Lines | Component | Description |
|-------|-----------|-------------|
| 10-27 | Git Not Enabled state | Shows when `!gitStatus?.git_enabled` |
| 17-26 | Initialize button | Primary action to open modal |
| 29-166 | Initialize modal | Full-screen form with inputs |
| 54-68 | Repository owner input | GitHub username/org |
| 70-84 | Repository name input | Repo name |
| 87-98 | Create repo checkbox | Whether to create if doesn't exist |
| 100-111 | Private checkbox | Make repository private (if creating) |
| 113-137 | Info box | Explains what will happen + timing warning |
| 143-154 | Initialize button | Calls `initializeGitHub()` with form data |
| 350-385 | `initializeGitHub()` | Method with console logging |
| 387-393 | `closeInitializeModal()` | Modal close handler |

**Key Methods**:

```javascript
// Line 350-385: Initialize GitHub Sync (with console logging)
const initializeGitHub = async () => {
  if (!repoOwner.value || !repoName.value) return

  initializing.value = true
  initializeError.value = null

  console.log('[GitPanel] Starting GitHub initialization...')

  try {
    const response = await agentsStore.initializeGitHub(props.agentName, {
      repo_owner: repoOwner.value,
      repo_name: repoName.value,
      create_repo: createRepo.value,
      private: privateRepo.value
    })

    console.log('[GitPanel] GitHub initialization successful:', response)

    // Success! Reload git status and close modal
    console.log('[GitPanel] Reloading git status...')
    await loadGitStatus()

    console.log('[GitPanel] Closing modal...')
    showInitializeModal.value = false

    // Clear form
    repoOwner.value = ''
    repoName.value = ''
  } catch (error) {
    console.error('[GitPanel] GitHub initialization failed:', error)
    initializeError.value = error.response?.data?.detail || error.message || 'Failed to initialize GitHub sync'
  } finally {
    console.log('[GitPanel] Initialization complete, setting initializing = false')
    initializing.value = false
  }
}
```

### State Management

**Location**: `src/frontend/src/stores/agents.js`

| Lines | Method | Description |
|-------|--------|-------------|
| 443-450 | `initializeGitHub()` | Store action with 120-second timeout |

```javascript
// Line 443-450: Initialize GitHub Sync (with timeout)
async initializeGitHub(name, config) {
  const authStore = useAuthStore()
  const response = await axios.post(`/api/agents/${name}/git/initialize`, config, {
    headers: authStore.authHeader,
    timeout: 120000 // 120 seconds (2 minutes) for git operations
  })
  return response.data
}
```

---

## Backend Layer

### Settings Service

**Location**: `src/backend/services/settings_service.py`

| Lines | Component | Description |
|-------|-----------|-------------|
| 49-100 | `SettingsService` class | Centralized settings retrieval |
| 71-76 | `get_github_pat()` method | Get PAT from DB or env var |
| 113-115 | `get_github_pat()` function | Convenience function export |

```python
# Line 71-76: Get GitHub PAT
def get_github_pat(self) -> str:
    """Get GitHub PAT from settings, fallback to env var."""
    key = self.get_setting('github_pat')
    if key:
        return key
    return os.getenv('GITHUB_PAT', '')
```

### GitHub Service

**Location**: `src/backend/services/github_service.py`

| Lines | Component | Description |
|-------|-----------|-------------|
| 19-22 | `OwnerType` enum | USER or ORGANIZATION |
| 25-34 | `GitHubRepoInfo` dataclass | Repository existence info |
| 37-42 | `GitHubCreateResult` dataclass | Repo creation result |
| 45-57 | Exception classes | `GitHubError`, `GitHubAuthError`, `GitHubPermissionError` |
| 60-265 | `GitHubService` class | GitHub API client |
| 101-118 | `validate_token()` | Validate PAT and get username |
| 120-139 | `get_owner_type()` | Detect user vs organization |
| 141-175 | `check_repo_exists()` | Check if repo exists |
| 177-264 | `create_repository()` | Create new repo (user or org) |
| 271-281 | `get_github_service()` | Factory function |

**Key Class**:

```python
# Line 60-265: GitHub API Service
class GitHubService:
    """Service for GitHub API interactions."""

    API_BASE = "https://api.github.com"
    API_VERSION = "2022-11-28"

    def __init__(self, pat: str):
        self.pat = pat
        self._headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.API_VERSION
        }

    async def validate_token(self) -> Tuple[bool, Optional[str]]:
        """Validate PAT and return (is_valid, username)."""
        response = await self._request("GET", "/user")
        if response.status_code == 200:
            return True, response.json().get("login")
        return False, None

    async def check_repo_exists(self, owner: str, name: str) -> GitHubRepoInfo:
        """Check if repository exists, return info."""
        response = await self._request("GET", f"/repos/{owner}/{name}")
        if response.status_code == 200:
            return GitHubRepoInfo(exists=True, ...)
        return GitHubRepoInfo(exists=False, ...)

    async def create_repository(self, owner: str, name: str, ...) -> GitHubCreateResult:
        """Create repository for user or org."""
        owner_type = await self.get_owner_type(owner)
        if owner_type == OwnerType.ORGANIZATION:
            path = f"/orgs/{owner}/repos"
        else:
            path = "/user/repos"
        response = await self._request("POST", path, json=payload)
        ...
```

### Git Service - Initialization

**Location**: `src/backend/services/git_service.py`

| Lines | Component | Description |
|-------|-----------|-------------|
| 29 | `MAX_INSTANCE_ID_RETRIES` | Max reservation retries before `RuntimeError` (5) — S7 Layer 0 |
| 32-40 | `generate_instance_id()` | Raw 8-char UUID prefix. Kept for the reserve helper; new callers MUST use `reserve_and_generate_instance_id` |
| 43-45 | `generate_working_branch()` | Generate branch name `trinity/{name}/{id}` |
| 48-112 | `check_remote_branch_exists()` | `git ls-remote --heads --exit-code` probe (S7 Layer 1) — fails-open on network/timeout because Layer 2 is the real guarantee |
| 115-206 | `reserve_and_generate_instance_id()` | **S7 Layer 0 single entry point**: UUID + remote probe + DB insert under partial UNIQUE index. Retries `MAX_INSTANCE_ID_RETRIES` times then raises `RuntimeError` |
| 209-238 | `create_git_config_for_agent()` | Legacy create-without-reserve helper (still used in tests/non-S7 paths) |
| 430-436 | `GitInitResult` dataclass | Result of git init in container |
| 439-658 | `initialize_git_in_container()` | Full git init workflow. New `working_branch` kwarg accepts pre-reserved branch; `create_working_branch=True` is deprecated and logs a warning |
| 661-690 | `check_git_initialized()` | Check if git exists in container |

**Key Functions**:

```python
# Line 115-206: S7 Layer 0 single entry point — atomic reserve
async def reserve_and_generate_instance_id(
    agent_name: str,
    github_repo: str,
    source_branch: str = "main",
    source_mode: bool = False,
    sync_paths: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Atomically reserve a fresh working branch.

    Combines:
      1. UUID generation
      2. ``git ls-remote`` probe (Layer 1)
      3. DB insert into ``agent_git_config`` under the partial UNIQUE
         index UNIQUE(github_repo, working_branch) WHERE source_mode = 0
         (Layer 2)

    Retries on either remote or DB collision up to
    MAX_INSTANCE_ID_RETRIES (5) before raising RuntimeError.
    For source_mode=True the remote probe is skipped (intentional shared
    branch) and the DB insert bypasses the partial UNIQUE index by design.
    """


# Line 430-436: GitInitResult dataclass
@dataclass
class GitInitResult:
    """Result of git initialization in container."""
    success: bool
    git_dir: str
    working_branch: Optional[str] = None
    error: Optional[str] = None


# Line 439-658: Initialize git in container (S7 Layer 0 signature)
async def initialize_git_in_container(
    agent_name: str,
    github_repo: str,
    github_pat: str,
    create_working_branch: bool = True,  # DEPRECATED — emits warning
    working_branch: Optional[str] = None,  # NEW: pre-reserved branch
) -> GitInitResult:
    """
    Initialize git in an agent container.

    Performs:
    1. Detect git directory (workspace or home)
    2. Create .gitignore
    3. Initialize git repo
    4. Configure remote
    5. Create initial commit
    6. Push to GitHub
    7. Create working branch — prefers the pre-reserved path:
       - working_branch=<reserved> (preferred)  → just check out + push
       - create_working_branch=True (legacy)    → generates an instance ID
         internally, bypassing the Layer 0/1/2 guarantees. Warning logged.
    """
    container_name = f"agent-{agent_name}"

    # Step 1: Directory detection with LEGACY workspace support
    # LEGACY: Agents created before 2026-02 may have /home/developer/workspace
    # New agents use /home/developer directly (no workspace subdirectory)
    check_workspace = execute_command_in_container(
        container_name=container_name,
        command='bash -c "[ -d /home/developer/workspace ] && find /home/developer/workspace -mindepth 1 -maxdepth 1 | head -1 | wc -l"',
        timeout=5
    )

    workspace_has_content = (
        check_workspace.get("exit_code") == 0 and
        "1" in check_workspace.get("output", "")
    )
    # LEGACY support: use workspace if it exists with content (pre-2026-02 agents)
    # Otherwise use home directory directly (new standard)
    git_dir = "/home/developer/workspace" if workspace_has_content else "/home/developer"

    # Step 2: Create .gitignore (standard path - /home/developer)
    if git_dir == "/home/developer":
        gitignore_content = """# Exclude sensitive and temporary files
.bash_logout
.bashrc
.profile
.bash_history
.cache/
.local/
.npm/
.ssh/
"""
        execute_command_in_container(...)

    # Step 3-6: Git commands (lines 329-356)
    commands = [
        'git config --global user.email "trinity@agent.local"',
        'git config --global user.name "Trinity Agent"',
        'git config --global init.defaultBranch main',
        'git init',
        f'git remote get-url origin >/dev/null 2>&1 && '
        f'git remote set-url origin https://oauth2:{github_pat}@github.com/{github_repo}.git || '
        f'git remote add origin https://oauth2:{github_pat}@github.com/{github_repo}.git',
        'git add .',
        'git commit -m "Initial commit from Trinity Agent" || echo "Nothing to commit"',
        'git push -u origin main --force'
    ]

    # Step 7: Create working branch (lines 358-377)
    if create_working_branch:
        instance_id = generate_instance_id()
        working_branch = generate_working_branch(agent_name, instance_id)
        # Create and push branch

    # Step 8: Verify (lines 379-393)
    verify_result = execute_command_in_container(
        command=f'bash -c "cd {git_dir} && git rev-parse --git-dir"',
        timeout=5
    )

    return GitInitResult(success=True, git_dir=git_dir, working_branch=working_branch)
```

### Access Control Dependencies

**Location**: `src/backend/dependencies.py`

| Lines | Component | Description |
|-------|-----------|-------------|
| 258-285 | `get_owned_agent_by_name()` | Validates owner access to agent |
| 295 | `OwnedAgentByName` | Type alias for Annotated dependency |

```python
# Line 258-285: Owner access validation
def get_owned_agent_by_name(
    agent_name: str = Path(..., description="Agent name from path"),
    current_user: User = Depends(get_current_user)
) -> str:
    """
    Dependency that validates user owns or can share an agent.
    For routes using {agent_name} path parameter.

    Used for endpoints that require owner-level access (delete, share, configure).
    Returns the agent name if authorized.
    """
    # First check if agent exists
    if not db.get_agent_owner(agent_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    # Then check if user has owner access
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required"
        )
    return agent_name


# Line 295: Type alias
OwnedAgentByName = Annotated[str, Depends(get_owned_agent_by_name)]
```

### Git Routes - Initialize Endpoint

**Location**: `src/backend/routers/git.py`

| Lines | Component | Description |
|-------|-----------|-------------|
| 34-41 | `GitInitializeRequest` model | Request body schema |
| 251-410 | `POST /{agent_name}/git/initialize` | Main initialization endpoint (S7 Layer 0 reserve-then-init) |
| 346-349 | `reserve_and_generate_instance_id()` call | Atomic reservation BEFORE container init |
| 351-394 | `try`/`except` around `initialize_git_in_container` | Rolls back via `db.delete_git_config` if init fails so retries can claim a fresh branch |

**Endpoint Implementation**:

```python
# Line 251-389: Initialize GitHub sync
@router.post("/{agent_name}/git/initialize")
async def initialize_github_sync(
    agent_name: OwnedAgentByName,  # Dependency validates ownership
    body: GitInitializeRequest,
    request: Request
):
    """Initialize GitHub synchronization for an agent."""
    from services.docker_service import get_agent_container
    from services.settings_service import get_github_pat
    from services.github_service import GitHubService, GitHubError

    # Check if agent exists and is running (lines 277-282)
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to initialize Git sync")

    # Check if already configured with orphan cleanup (lines 284-298)
    existing_config = git_service.get_agent_git_config(agent_name)
    if existing_config:
        git_dir = git_service.check_git_initialized(agent_name)
        if git_dir:
            raise HTTPException(status_code=409, detail=f"Git sync already configured...")
        else:
            # Clean up orphaned record
            db.execute_query("DELETE FROM agent_git_config WHERE agent_name = ?", (agent_name,))

    # Get GitHub PAT from settings_service (lines 300-306)
    github_pat = get_github_pat()
    if not github_pat:
        raise HTTPException(status_code=400, detail="GitHub Personal Access Token not configured...")

    repo_full_name = f"{body.repo_owner}/{body.repo_name}"

    try:
        gh = GitHubService(github_pat)

        # Step 1: Check repo existence and handle create_repo flag (lines 313-336)
        repo_info = await gh.check_repo_exists(body.repo_owner, body.repo_name)

        if body.create_repo:
            if not repo_info.exists:
                create_result = await gh.create_repository(
                    owner=body.repo_owner,
                    name=body.repo_name,
                    private=body.private,
                    description=body.description
                )
                if not create_result.success:
                    raise HTTPException(status_code=400, detail=f"Failed to create repository: {create_result.error}")
        else:
            # create_repo=False: Repository MUST exist
            if not repo_info.exists:
                raise HTTPException(status_code=400, detail=f"Repository '{repo_full_name}' does not exist...")

        # Step 2: Reserve the working branch BEFORE touching the container.
        # S7 Layer 0 (#382): single-entry helper — remote probe + DB insert
        # under the partial UNIQUE index in one shot. Raises RuntimeError
        # after MAX_INSTANCE_ID_RETRIES collisions.
        instance_id, reserved_branch = await git_service.reserve_and_generate_instance_id(
            agent_name=agent_name,
            github_repo=repo_full_name,
        )

        try:
            # Step 3: Initialize git in container using the reserved branch.
            # create_working_branch=False tells the helper not to generate
            # its own ID — the caller owns the reservation now.
            init_result = await git_service.initialize_git_in_container(
                agent_name=agent_name,
                github_repo=repo_full_name,
                github_pat=github_pat,
                create_working_branch=False,
                working_branch=reserved_branch,
            )

            if not init_result.success:
                raise HTTPException(status_code=400, detail=f"Git initialization failed: {init_result.error}")
        except Exception:
            # Release the reservation so a retry can grab a fresh branch.
            try:
                db.delete_git_config(agent_name)
            except Exception as cleanup_exc:
                logger.warning("Failed to roll back agent_git_config for %s after init failure: %s", agent_name, cleanup_exc)
            raise

        return {
            "success": True,
            "message": "GitHub sync initialized successfully",
            "github_repo": repo_full_name,
            "working_branch": reserved_branch,
            "instance_id": instance_id,
            "repo_url": f"https://github.com/{repo_full_name}"
        }

    except HTTPException:
        raise
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize GitHub sync: {str(e)}")
```

### Docker Service - Command Execution

**Location**: `src/backend/services/docker_service.py`

| Lines | Function | Description |
|-------|----------|-------------|
| 208-238 | `execute_command_in_container()` | Execute commands in container |

**Function Definition** (Lines 208-238):
```python
def execute_command_in_container(container_name: str, command: str, timeout: int = 60) -> dict:
    """Execute a command in a Docker container.

    Args:
        container_name: Name of the container (e.g., "agent-myagent")
        command: Command to execute
        timeout: Timeout in seconds

    Returns:
        Dictionary with 'exit_code' and 'output' keys
    """
    if not docker_client:
        return {"exit_code": 1, "output": "Docker client not available"}

    try:
        container = docker_client.containers.get(container_name)
        result = container.exec_run(
            command,
            user="developer",
            demux=False
        )
        output = result.output.decode('utf-8') if isinstance(result.output, bytes) else str(result.output)
        return {"exit_code": result.exit_code, "output": output}
    except docker.errors.NotFound:
        return {"exit_code": 1, "output": f"Container {container_name} not found"}
    except Exception as e:
        return {"exit_code": 1, "output": f"Error executing command: {str(e)}"}
```

---

## MCP Layer

### MCP Tool - initialize_github_sync

**Location**: `src/mcp-server/src/tools/agents.ts`

| Lines | Component | Description |
|-------|-----------|-------------|
| 530-604 | `initializeGithubSync` tool | MCP tool definition |

**Tool Definition** (Lines 530-604):

```typescript
initializeGithubSync: {
  name: "initialize_github_sync",
  description:
    "Initialize GitHub synchronization for an existing agent (not created from GitHub template). " +
    "Creates a GitHub repository (if requested), initializes git in the agent workspace, " +
    "commits the current state, pushes to GitHub, and creates a working branch for sync. " +
    "Requires GitHub Personal Access Token (PAT) to be configured in system settings with 'repo' scope. " +
    "Note: Agents created from GitHub templates already have git sync enabled in source mode (pull-only). " +
    "Agent must be running.",
  parameters: z.object({
    agent_name: z.string().describe("The name of the agent to initialize GitHub sync for"),
    repo_owner: z.string().describe("GitHub username or organization name (e.g., 'your-username')"),
    repo_name: z.string().describe("Repository name (e.g., 'my-agent')"),
    create_repo: z.boolean().optional().default(true)
      .describe("Whether to create the repository if it doesn't exist (default: true)"),
    private: z.boolean().optional().default(true)
      .describe("Whether the new repository should be private (default: true)"),
    description: z.string().optional().describe("Repository description (optional)")
  }),
  execute: async (args, context) => {
    const authContext = context?.session;
    const apiClient = getClient(authContext);

    console.log(`[initialize_github_sync] Initializing GitHub sync for agent '${args.agent_name}' -> ${args.repo_owner}/${args.repo_name}`);

    interface GitInitializeResponse {
      success: boolean;
      message: string;
      github_repo: string;
      working_branch: string;
      instance_id: string;
      repo_url: string;
    }

    const response = await apiClient.request<GitInitializeResponse>(
      "POST",
      `/api/agents/${args.agent_name}/git/initialize`,
      {
        repo_owner: args.repo_owner,
        repo_name: args.repo_name,
        create_repo: args.create_repo ?? true,
        private: args.private ?? true,
        description: args.description
      }
    );

    return JSON.stringify(response, null, 2);
  }
}
```

**Example Usage**:

```json
{
  "name": "initialize_github_sync",
  "arguments": {
    "agent_name": "my-agent",
    "repo_owner": "your-username",
    "repo_name": "my-agent-workspace",
    "create_repo": true,
    "private": true,
    "description": "Agent workspace managed by Trinity"
  }
}
```

---

## Data Layer

### Database Schema

**Table**: `agent_git_config`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Row id |
| `agent_name` | TEXT UNIQUE NOT NULL | Agent identifier |
| `github_repo` | TEXT | Full repo name (owner/name) |
| `working_branch` | TEXT | Branch name (trinity/{name}/{id}) |
| `instance_id` | TEXT | Unique instance identifier (8 chars) |
| `source_branch` | TEXT | Source branch (default `'main'`) |
| `source_mode` | INTEGER | 0 = working-branch agent, 1 = read-only source-mode |
| `created_at` | TIMESTAMP | When config was created |
| `last_sync_at` | TIMESTAMP | Last successful sync |
| `last_commit_sha` | TEXT | Last synced commit SHA |
| `sync_enabled` | BOOLEAN | Whether sync is active |
| `sync_paths` | TEXT | Optional JSON list of allowed paths |
| `github_pat_encrypted` | TEXT | Per-agent PAT (#347) |

**Indexes**:
- `idx_git_config_agent` on `agent_name`
- `idx_git_config_repo` on `github_repo`
- `idx_git_config_repo_branch_unique` — **S7 Layer 2 partial UNIQUE**:
  `UNIQUE(github_repo, working_branch) WHERE source_mode = 0`. Defined in
  `src/backend/db/schema.py:757-758` for fresh installs and added to existing
  databases by migration `agent_git_config_branch_ownership`
  (`src/backend/db/migrations.py:1454-1511`).

**Table**: `system_settings`

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT PRIMARY KEY | Setting name |
| `value` | TEXT | Setting value |
| `updated_at` | TIMESTAMP | Last update time |

GitHub PAT stored as key: `github_pat`

---

## Service Layer Summary (NEW)

### GitHubService Dataclasses

```python
# src/backend/services/github_service.py

class OwnerType(Enum):
    USER = "user"
    ORGANIZATION = "org"

@dataclass
class GitHubRepoInfo:
    exists: bool
    full_name: str
    owner: str
    name: str
    owner_type: Optional[OwnerType] = None
    private: Optional[bool] = None
    default_branch: Optional[str] = None

@dataclass
class GitHubCreateResult:
    success: bool
    repo_url: Optional[str] = None
    error: Optional[str] = None
```

### GitService Dataclasses

```python
# src/backend/services/git_service.py

@dataclass
class GitInitResult:
    success: bool
    git_dir: str
    working_branch: Optional[str] = None
    error: Optional[str] = None
```

### Exception Hierarchy

```python
# src/backend/services/github_service.py

class GitHubError(Exception):
    """Base exception for GitHub API errors."""
    pass

class GitHubAuthError(GitHubError):
    """GitHub authentication failed."""
    pass

class GitHubPermissionError(GitHubError):
    """Insufficient permissions for operation."""
    pass
```

---

## Side Effects

### Audit Logging

All operations are logged via the audit service:

```python
# Settings: Test PAT
await log_audit_event(
    event_type="system_settings",
    action="test_github_pat",
    user_id=current_user.username,
    result="success",
    details={
        "valid": True,
        "github_user": username,
        "token_type": "fine-grained" or "classic",
        "has_repo_access": bool
    }
)

# Settings: Save PAT
await log_audit_event(
    event_type="system_settings",
    action="update_github_pat",
    user_id=current_user.username,
    result="success",
    details={"key_masked": "...xxxx"}
)

# Git: Initialize
await log_audit_event(
    event_type="git_operation",
    action="initialize",
    user_id=current_user.username,
    agent_name=agent_name,
    result="success",
    details={
        "github_repo": repo_full_name,
        "working_branch": working_branch,
        "created_repo": bool
    }
)
```

### WebSocket Broadcasts

None - this is a one-time configuration operation

### GitHub API Calls

1. **Test PAT**: `GET https://api.github.com/user`
2. **Check owner type**: `GET https://api.github.com/users/{owner}`
3. **Check repo exists**: `GET https://api.github.com/repos/{owner}/{name}`
4. **Create repo (user)**: `POST https://api.github.com/user/repos`
5. **Create repo (org)**: `POST https://api.github.com/orgs/{owner}/repos`
6. **Push commits**: Git protocol via authenticated URL

---

## Error Handling

| Error Case | HTTP Status | Message | Recovery |
|------------|-------------|---------|----------|
| Agent not found | 404 | "Agent not found" | Create agent first |
| Agent not running | 400 | "Agent must be running to initialize Git sync" | Start agent |
| Not agent owner | 403 | "Owner access required" | Share agent or login as owner |
| Already configured | 409 | "Git sync already configured for this agent. Repository: {repo}" | Cannot re-initialize |
| Orphaned DB record | - | Auto-cleanup + allow re-init | System detects and fixes automatically |
| No GitHub PAT | 400 | "GitHub Personal Access Token not configured. Please add it in Settings." | Configure PAT in Settings |
| Invalid PAT format | 400 | "Invalid token format" | Use PAT starting with ghp_ or github_pat_ |
| Owner not found | 400 | "Owner '{owner}' not found on GitHub" | Check owner name |
| Repo creation failed | 400 | "Failed to create repository: {message}" | Check PAT permissions |
| Git command failed | 500 | "Git initialization failed: {error}" | Check container logs |
| Git verification failed | 500 | "Git initialization verification failed" | Check container state |
| GitHubError | 400 | Exception message | Check PAT and permissions |

---

## Security Considerations

### Authentication & Authorization

1. **Admin-only Settings access**: Only admins can configure GitHub PAT
   - Checked via `require_admin(current_user)` in settings routes

2. **Owner-only initialization**: Only agent owners can initialize Git sync
   - Checked via `OwnedAgentByName` dependency (Plan 02)
   - Uses `db.can_user_share_agent(current_user.username, agent_name)`

3. **PAT storage**: Stored in system_settings table (not encrypted at rest)
   - Consider encryption for production deployments

4. **PAT masking**: Only last 4 characters shown in UI
   - Prevents accidental exposure

### GitHub PAT Requirements

**Classic PAT**: Requires `repo` scope
- Full control of private repositories
- Includes `repo:status`, `repo_deployment`, `public_repo`

**Fine-grained PAT**: Requires these permissions:
- **Repository permissions**:
  - Contents: Read and write
  - Metadata: Read-only (default)
  - Administration: Read and write (for creating repos)

### Git Authentication

Repository remote URL includes PAT:
```
https://oauth2:{PAT}@github.com/{owner}/{name}.git
```

This allows push/pull without interactive authentication.

**Security implications**:
- PAT is visible in git remote URL inside container
- Container compromise could expose PAT
- Use fine-grained PATs with minimal permissions
- Rotate PATs regularly

---

## Testing

### Prerequisites

1. **Running Trinity platform** with database and backend
2. **Admin user account** for Settings access
3. **GitHub account** with ability to create repositories
4. **Valid GitHub PAT** with `repo` scope (classic) or Contents + Administration (fine-grained)
5. **Test agent** that is running but doesn't have git sync enabled

### Test Steps

#### 1. Configure GitHub PAT

**Action**:
1. Login as admin user
2. Navigate to Settings page (`/settings`)
3. Scroll to "GitHub Personal Access Token (PAT)" section
4. Enter a valid GitHub PAT
5. Click "Test" button

**Expected**:
- Button shows loading spinner
- After 1-2 seconds, shows green checkmark
- Message displays: "Valid! GitHub user: {username}. Has repo scope" (classic) or "Fine-grained PAT with repository permissions" (fine-grained)

**Verify**:
```bash
# Check database
sqlite3 ~/trinity-data/trinity.db "SELECT key, substr(value, 1, 10) || '...' FROM system_settings WHERE key = 'github_pat'"
# Should show: github_pat|ghp_...
```

**Action (continued)**:
6. Click "Save" button

**Expected**:
- Green success notification: "Settings saved successfully!"
- Input field clears
- Status shows: "Configured (from settings)" with masked value "...xxxx"

#### 2. Initialize Git Sync for Agent

**Action**:
1. Navigate to an agent's detail page (agent must be running)
2. Click "Git" tab
3. Verify message: "Git sync not enabled for this agent"
4. Click "Initialize GitHub Sync" button

**Expected**:
- Modal opens with form
- Form has fields: Repository Owner, Repository Name
- Checkboxes: "Create repository if it doesn't exist" (checked), "Make repository private" (checked)
- Info box explains what will happen and includes timing warning: "This may take 10-60 seconds depending on the size of your agent's files."

**Action (continued)**:
5. Enter repository owner: `your-github-username`
6. Enter repository name: `test-agent-workspace`
7. Ensure both checkboxes are checked
8. Click "Initialize" button

**Expected**:
- Button shows "Initializing..." with spinner
- Console logs show progress:
  - `[GitPanel] Starting GitHub initialization...`
  - `[GitPanel] GitHub initialization successful: {...}`
  - `[GitPanel] Reloading git status...`
  - `[GitPanel] Closing modal...`
  - `[GitPanel] Initialization complete, setting initializing = false`
- After 10-60 seconds (depending on workspace size):
  - Modal closes
  - Git tab shows repository information
  - Remote URL: `https://github.com/your-username/test-agent-workspace`
  - Branch indicator: `trinity/test-agent/xxxxxxxx` (8-char instance ID)
  - Status: "Synced" (green badge)
  - "Last Commit" section shows "Initial commit from Trinity Agent"

**Verify on GitHub**:
```bash
# Visit https://github.com/your-username/test-agent-workspace
# Should see:
# - Repository exists
# - Private badge (if selected)
# - Two branches: main, trinity/test-agent/xxxxxxxx
# - Commits on both branches
# - Agent files: CLAUDE.md, .claude/, .trinity/, .mcp.json
# - .gitignore excludes: .bashrc, .cache/, .ssh/ (if using home directory)
```

**Verify in Database**:
```bash
sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_git_config WHERE agent_name = 'test-agent'"
# Should show config record with repo, branch, instance_id
```

**Verify Directory Detection**:
- New agents (2026-02+): Check backend logs for `Using home directory: /home/developer`
- LEGACY agents (pre-2026-02): May show `Using workspace directory: /home/developer/workspace` if workspace exists with content
- Check for: `Git initialization verified successfully in {directory}`

#### 3. MCP Tool Usage

**Action**:
1. Use Claude Code or MCP client
2. Call `initialize_github_sync` tool:
```json
{
  "agent_name": "another-agent",
  "repo_owner": "your-username",
  "repo_name": "another-agent-workspace",
  "create_repo": true,
  "private": true
}
```

**Expected**:
- Tool returns JSON with:
  - `success: true`
  - `github_repo: "your-username/another-agent-workspace"`
  - `working_branch: "trinity/another-agent/xxxxxxxx"`
  - `repo_url: "https://github.com/your-username/another-agent-workspace"`

**Verify**:
- Repository created on GitHub
- Agent has git sync enabled in UI

### Edge Cases

#### 1. Agent Not Running

**Action**: Try to initialize git sync for a stopped agent

**Expected**: Error message: "Agent must be running to initialize Git sync"

**Verify**: Modal shows error in red box

#### 2. Already Configured

**Action**: Try to initialize git sync for an agent that already has it

**Expected**: Error message: "Git sync already configured for this agent. Repository: {repo}"

**Verify**: Cannot re-initialize, must delete and recreate agent

#### 3. Orphaned Database Record

**Action**:
1. Create orphaned DB record: Insert into `agent_git_config` without actually initializing git in container
2. Try to initialize git sync

**Expected**:
- System detects orphaned record via `git_service.check_git_initialized()`
- Backend logs: `Warning: Found orphaned git config for {agent_name}. Cleaning up and allowing re-initialization.`
- Record deleted from database
- Initialization proceeds successfully

**Verify**: Check backend logs for cleanup message

#### 4. Directory Detection (Standard vs LEGACY)

> **Note**: New agents (created 2026-02+) use `/home/developer` directly. LEGACY support exists for agents created before 2026-02 that may have `/home/developer/workspace`.

**Action**: Initialize git sync for a new agent (created 2026-02+)

**Expected**:
- System uses `/home/developer` directly (no workspace subdirectory)
- Backend logs: `Using home directory: /home/developer`
- `.gitignore` created with system file exclusions
- Agent files (CLAUDE.md, .claude/, .trinity/, .mcp.json) pushed to GitHub
- System files (.bashrc, .ssh/, .cache/) excluded via .gitignore

**LEGACY Case** (agents created before 2026-02):
- If `/home/developer/workspace/` exists with content, that directory is used
- Backend logs: `Using workspace directory: /home/developer/workspace`

**Verify**:
- Check GitHub repo contains agent files but not system files
- Check for `.gitignore` in repository root

#### 5. No GitHub PAT

**Action**:
1. Delete GitHub PAT from settings: `DELETE /api/settings/api-keys/github`
2. Try to initialize git sync

**Expected**: Error message: "GitHub Personal Access Token not configured. Please add it in Settings."

#### 6. Invalid PAT

**Action**: Try to save a PAT that doesn't start with `ghp_` or `github_pat_`

**Expected**: Error message: "Invalid token format. GitHub PATs start with 'ghp_' or 'github_pat_'"

#### 7. Insufficient Permissions

**Action**: Use a PAT without `repo` scope (classic) or without Contents/Administration (fine-grained)

**Expected**:
- Test shows: "Missing repo scope" (classic) or "Missing repository permissions" (fine-grained)
- Initialize fails: "Failed to create GitHub repository: Bad credentials or insufficient permissions"

#### 8. Repository Already Exists

**Action**:
1. Create repository on GitHub manually
2. Initialize git sync with same owner/name

**Expected**: Initialization succeeds, uses existing repository

**Verify**: Check GitHub - should see Trinity's initial commit pushed

#### 9. Organization Repository (NEW)

**Action**: Initialize git sync with an organization owner

**Expected**:
- `GitHubService.get_owner_type()` detects organization
- Uses `POST /orgs/{owner}/repos` endpoint instead of `/user/repos`
- Helpful error message if missing org permissions

**Verify**: Check that org repo is created correctly

#### 10. Large Agent Workspace

**Action**: Initialize git sync for an agent with large `.claude/` directory (many sessions, large memory files)

**Expected**:
- Frontend timeout set to 120 seconds (2 minutes)
- Console logs track progress
- Operation completes successfully within timeout
- No frontend timeout error

**Verify**:
- Check browser console for timing logs
- Verify all files pushed to GitHub

---

## Troubleshooting

### Issue: Frontend Timeout

**Symptoms**:
- Modal shows "Initializing..." for >2 minutes
- Error appears: "timeout of 120000ms exceeded"
- Agent may actually have git initialized successfully

**Root Cause**: Large `.claude/` directory or slow GitHub connection

**Verification**:
```bash
# Check if git was actually initialized
docker exec agent-{name} bash -c "cd /home/developer && git status"

# Check backend logs
docker logs trinity-backend | grep "Git initialization verified"
```

**Solution**:
1. If git is initialized but frontend timed out: Just reload the page
2. If timeout is consistent: Increase timeout in `src/frontend/src/stores/agents.js:366` (currently 120000ms)
3. If using slow network: Consider using GitHub CLI for initial push, then enable sync

### Issue: Empty Repository

**Symptoms**:
- Repository created on GitHub but has no files
- Commits exist but no agent files visible

**Root Cause**: Git initialized in wrong directory (empty workspace instead of home)

**Verification**:
```bash
# Check where git was initialized
docker exec agent-{name} bash -c "find /home/developer -name .git -type d"

# Check what was committed
docker exec agent-{name} bash -c "cd /home/developer && git log --stat"
```

**Solution**:
- FIXED in latest version with smart directory detection in `git_service.initialize_git_in_container()`
- System now checks if workspace has content and falls back to home directory
- System creates .gitignore to exclude system files

### Issue: Orphaned Database Records

**Symptoms**:
- Error: "Git sync already configured" but git not actually initialized
- Cannot re-initialize git sync
- Git tab shows "Agent must be running" even when agent is running

**Root Cause**: Database record created but git initialization failed

**Verification**:
```bash
# Check database
sqlite3 ~/trinity-data/trinity.db "SELECT * FROM agent_git_config WHERE agent_name = '{name}'"

# Check if git actually exists in container
docker exec agent-{name} bash -c "[ -d /home/developer/.git ] && echo exists || echo notexists"
```

**Solution**:
- FIXED in latest version with auto-cleanup via `git_service.check_git_initialized()`
- System detects orphaned records and removes them automatically
- Allows re-initialization after failed attempts

---

## Testing Status

**Status**: Verified Working (as of 2026-01-23)

**Architecture Components**:
- `services/github_service.py` - GitHub API client with dataclasses (282 lines)
- `services/settings_service.py` - Settings retrieval service (139 lines)
- `services/git_service.py` - Git operations including `initialize_git_in_container()`, `check_git_initialized()` (428 lines)
- `routers/git.py` - Git routes including initialize endpoint (389 lines)
- `dependencies.py` - Access control with `OwnedAgentByName` type alias

**Recent Bug Fixes** (2025-12-26):
- Fixed empty repositories: Smart directory detection chooses correct location
- Fixed orphaned records: Auto-cleanup of DB records when git doesn't exist
- Fixed timeout issue: Increased frontend timeout to 120 seconds + console logging
- Fixed verification: Git initialization verified before DB insert
- Fixed system files: Created .gitignore to exclude .bashrc, .ssh/, etc.

**Test Coverage**:
- Settings UI: PAT configuration and testing
- GitPanel UI: Modal form and initialization flow
- Backend: Repository creation and git commands
- Smart directory detection: Workspace vs home directory
- Orphaned record cleanup: Auto-detection and removal
- Git verification: Pre-DB insert validation
- Timeout handling: 120-second frontend timeout
- MCP Tool: Programmatic initialization
- Error handling: All edge cases covered
- Database persistence: Config stored correctly
- Audit logging: All operations logged
- Organization repos: Correct endpoint selection

**Known Issues**: None

**Verification Date**: 2026-01-23 - All line numbers verified against current implementation.

---

## Related Flows

- **Upstream**: [credential-injection.md](credential-injection.md) - GitHub PAT stored via credentials system
- **Downstream**: [github-sync.md](github-sync.md) - Bidirectional sync after initialization
- **Related**: [agent-lifecycle.md](agent-lifecycle.md) - Git sync can be enabled during agent creation with `github:` template prefix

---

## Implementation Notes

### Working Branch Strategy

Each agent instance gets a unique working branch:
```
trinity/{agent-name}/{instance-id}
```

**Benefits**:
- Multiple instances can work on same repo without conflicts
- Clear ownership of changes
- Easy to create PRs from working branch to main
- Instance ID ensures uniqueness across restarts

### Branch Ownership (S7 Layers 0 & 2 — #382, 2026-04-19)

Working-branch agents must be exclusively bound to their `(github_repo,
working_branch)` pair. This is enforced in three layers; Layers 0 and 2
live in this flow (Layer 3, the push-time `--force-with-lease` guard,
lives in [github-sync.md](github-sync.md)).

**Layer 0 — single-entry reservation helper**
- `services/git_service.py:reserve_and_generate_instance_id()` (lines 115-206)
  is the only sanctioned way to allocate a working branch.
- Atomically: generates a UUID prefix → probes the remote with
  `git ls-remote --heads --exit-code` → inserts into `agent_git_config`.
- Retries up to `MAX_INSTANCE_ID_RETRIES` (5, defined at line 29) on any
  collision before raising `RuntimeError`.
- Source-mode agents (`source_mode=True`) intentionally share branches
  (e.g. every reader pointing at `main`); the remote probe is skipped and
  the DB insert bypasses the partial UNIQUE index by design.

**Layer 1 — remote probe**
- `services/git_service.py:check_remote_branch_exists()` (lines 48-112).
- Uses `https://github.com/<repo>.git` so the check works without a PAT
  for public repos. Fails open (returns `False`) on network errors,
  timeout, or `git` missing — Layer 2 is the real guarantee.

**Layer 2 — partial UNIQUE index**
- `db/schema.py:757-758` for fresh installs.
- Migration `agent_git_config_branch_ownership` in
  `db/migrations.py:1454-1511` for existing databases.
- Pre-flight `_find_duplicate_working_branches()`
  (`db/migrations.py:1411-1451`) scans for pre-existing duplicates.
  If any are found the migration **refuses to install the index** and
  prints every offending `(repo, branch, agents)` group plus each
  individual agent name so the operator can rebind one of the colliding
  agents (via the UI / MCP `initialize_github_sync` flow) before
  re-running. The migration **never auto-deletes rows** — that would
  mask the bug.

**Call-site consolidation**
| Caller | Location | Behavior change |
|--------|----------|-----------------|
| Agent creation | `services/agent_service/crud.py:232-239` | Reserves BEFORE container creation. The duplicate post-container `db.create_git_config` (previously around line 619) was removed. On any failure later in the function the `except` block at lines 626-639 calls `db.delete_git_config(config.name)` to release the reservation. |
| `POST /git/initialize` | `routers/git.py:346-394` | Reserves BEFORE `initialize_git_in_container`. Passes `working_branch=<reserved>, create_working_branch=False`. Wraps the container init in a `try`/`except` that rolls back the reservation on failure. |
| `initialize_git_in_container` | `services/git_service.py:439-658` | Adds `working_branch: Optional[str] = None` kwarg for the pre-reserved path (lines 595-611). The legacy `create_working_branch=True` path (lines 612-636) emits a deprecation warning on every use but is kept so older callers don't break. |

**Failure mode**
Agent creation that previously could silently bind to an in-use working
branch (the 2026-04-17 alpaca incident) now fails fast with a
`RuntimeError` if all 5 reservation attempts collide. The partial
transaction is rolled back; the caller is expected to retry agent
creation, which will pick a fresh UUID.

**Regression coverage**
`tests/git-sync/test_s7_reserve_instance_id.py` covers:
- (i) `reserve_and_generate_instance_id` retries on `ls-remote` hits
- (ii) raises after `MAX_INSTANCE_ID_RETRIES`
- (iii) the partial UNIQUE index rejects duplicate working-branch
  bindings and accepts duplicates for source-mode agents
- (iv) the migration pre-flight surfaces existing duplicates and
  refuses to install the index until they're resolved

### Repository Structure

After initialization:
```
your-repo/
  .git/
  .gitignore (if using home directory)
  CLAUDE.md
  .claude/
  .trinity/
  .mcp.json
  (any template-provided files)
```

**Branches**:
- `main`: Initial commit from Trinity, force-pushed
- `trinity/{name}/{id}`: Working branch, auto-created

**Files Excluded** (when using home directory):
- `.bash_logout`
- `.bashrc`
- `.profile`
- `.bash_history`
- `.cache/`
- `.local/`
- `.npm/`
- `.ssh/`

### Directory Detection Logic

The system uses smart detection to find the correct directory (in `git_service.initialize_git_in_container()`):

1. **Standard path (new agents, 2026-02+)**: Use `/home/developer` directly
   - Agent home directory is `/home/developer`
   - All files live there directly (no workspace subdirectory)

2. **LEGACY support (agents created before 2026-02)**: Check for workspace
   - If `/home/developer/workspace` exists AND has content -> Use workspace
   - Otherwise -> Use `/home/developer`

3. **If using home directory**: Create `.gitignore` to exclude system files

4. **Verify**: Run `git rev-parse --git-dir` before creating DB record

> **Note**: The workspace check is LEGACY backward compatibility for agents created before February 2026. New agents do not have a workspace subdirectory.

### Git Authentication

PAT embedded in remote URL allows push/pull without interactive auth:
```bash
git remote add origin https://oauth2:{PAT}@github.com/{owner}/{name}.git
```

This works with both classic and fine-grained PATs.

### GitHub API Rate Limits

- **Unauthenticated**: 60 requests/hour
- **Authenticated**: 5,000 requests/hour
- **Fine-grained PAT**: 5,000 requests/hour per repo

Testing and initialization count as:
- Test: 1-2 API calls
- Initialize: 3-4 API calls (check owner + check repo + create)

---

## Future Enhancements

### Potential Improvements

1. **PAT Encryption**: Encrypt GitHub PAT in database using SECRET_KEY
2. **OAuth App Flow**: Use GitHub OAuth app instead of PATs for better security
3. **Branch Protection**: Automatically configure branch protection rules
4. **PR Automation**: Auto-create PR when sync happens
5. **Multi-repo Support**: Allow agent to sync to multiple repositories
6. **Selective Sync**: Choose which files/directories to sync
7. **Sync Conflicts UI**: Better handling of merge conflicts in UI
8. **Repository Templates**: Pre-configured repo templates with Actions, README, etc.
9. **Progress Indicator**: Show real-time progress during initialization (file count, upload progress)
10. **Workspace Detection UI**: Show which directory will be used before initialization

### GitHub Apps Integration

Consider migrating from PATs to GitHub Apps for:
- Better security (short-lived tokens)
- Granular permissions per installation
- Activity appears as bot user
- Higher rate limits
- Revocable by organization admins

---

## References

- **GitHub PAT Documentation**: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- **GitHub REST API**: https://docs.github.com/en/rest
- **Fine-grained PAT Permissions**: https://docs.github.com/en/rest/overview/permissions-required-for-fine-grained-personal-access-tokens
- **Git Reference**: https://git-scm.com/docs
