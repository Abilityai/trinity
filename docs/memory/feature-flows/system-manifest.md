# Feature: System Manifest Deployment

## Overview
Deploy multi-agent systems from YAML manifests via a single API call. This is a "recipe" deployment model where agents are created from a manifest but become independent after creation (not declaratively bound to the manifest). Feature includes agent creation, conflict resolution, permissions, shared folders, schedules, auto-start, MCP tools, backend management endpoints, **automatic tagging** (ORG-001 Phase 4), and **System View auto-creation**.

## User Story
As a platform user, I want to deploy multiple coordinated agents from a YAML configuration so that I can quickly stand up multi-agent systems without manually creating each agent, configuring permissions, setting up shared folders, and creating schedules.

## YAML Manifest Format

### Minimal Example
```yaml
name: my-system
agents:
  worker:
    template: local:default
```

### Complete Example
```yaml
name: content-production
description: Autonomous content pipeline with orchestrator and workers

prompt: |
  System-wide instructions injected into all agents.
  This updates the global trinity_prompt setting.
  All agents receive this via CLAUDE.md injection.

# ORG-001 Phase 4: Default tags applied to ALL agents in system
default_tags:
  - production
  - content-team

# ORG-001 Phase 4: Auto-create System View on deploy
system_view:
  name: Content Production
  icon: "📝"
  color: "#8B5CF6"
  shared: true  # Visible to all users

agents:
  orchestrator:
    template: github:YourOrg/orchestrator-agent
    resources:
      cpu: "2"
      memory: "4g"
    folders:
      expose: true    # Share files with other agents
      consume: true   # Mount shared folders from others
    tags:             # ORG-001 Phase 4: Per-agent tags (in addition to default_tags)
      - lead
      - orchestrator
    schedules:
      - name: daily-planning
        cron: "0 9 * * *"
        message: "Create today's content plan"
        timezone: "UTC"
        enabled: true
      - name: hourly-check
        cron: "0 * * * *"
        message: "Check progress and adjust"

  writer:
    template: local:default
    folders:
      expose: true
      consume: true
    tags:
      - worker

  editor:
    template: local:default
    folders:
      expose: false
      consume: true
    tags:
      - worker
      - review

permissions:
  preset: orchestrator-workers  # Options: full-mesh, orchestrator-workers, none, explicit
```

### Tag Application Order (ORG-001 Phase 4)

When deploying a system, tags are applied to each agent in this order:

1. **System name** (auto-applied): The system name becomes a tag (e.g., `content-production`)
2. **Default tags** (from manifest root): Applied to ALL agents (e.g., `production`, `content-team`)
3. **Per-agent tags** (from agent config): Applied to specific agents (e.g., `lead`, `worker`)

All tags are normalized (lowercase, deduplicated) before being stored.

### System View Auto-Creation (ORG-001 Phase 4)

If `system_view` is specified in the manifest:
- A System View is created with the specified name, icon, and color
- Filter tags include: system name + default_tags
- The view appears in the Dashboard sidebar for quick agent filtering
- If `shared: true`, all users can see the view

### Permission Presets

**full-mesh**: Every agent can communicate with every other agent
```yaml
permissions:
  preset: full-mesh
```

**orchestrator-workers**: Only orchestrator can call workers, workers cannot call anyone
```yaml
permissions:
  preset: orchestrator-workers
  # Requires an agent named "orchestrator" in the agents section
```

**none**: Clear all default permissions (isolated agents)
```yaml
permissions:
  preset: none
```

**explicit**: Custom permission matrix
```yaml
permissions:
  explicit:
    manager:
      - analyst
      - reporter
    analyst:
      - reporter
    reporter: []  # No permissions
```

## Entry Points

### API Endpoints
- **Deployment**: `POST /api/systems/deploy` - Deploy system from YAML
- **Management**: `GET /api/systems` - List all deployed systems
- **Details**: `GET /api/systems/{name}` - Get system details
- **Restart**: `POST /api/systems/{name}/restart` - Restart all system agents
- **Export**: `GET /api/systems/{name}/manifest` - Export system as YAML
- **Bundled catalog** (ent#126): `GET /api/systems/manifests` - List the manifests
  shipped in `config/manifests/`
- **Bundled manifest** (ent#126): `GET /api/systems/manifests/{manifest_id}` - The same
  summary plus the raw YAML, for loading into the install editor

> **Naming adjacency**: `/api/systems/manifests` (the bundled *catalog*) and
> `/api/systems/{name}/manifest` (export an *already-deployed* system) read alike and are
> unrelated. Both catalog routes must stay declared **above** the parameterized routes —
> Invariant #4 applies **twice** here: `/manifests` collides with `GET /{system_name}`, and
> `/manifests/manifest` collides with `GET /{system_name}/manifest`.

### MCP Tools
- `deploy_system` - Deploy from YAML manifest
- `list_systems` - List deployed systems
- `restart_system` - Restart all system agents
- `get_system_manifest` - Export system configuration as YAML

### UI
- **Status**: ✅ Implemented (trinity-enterprise#126, 2026-07-30) — install surface only
- **Where**: the `#systems` section of the Library page (`/library#systems`). `/templates` redirects, hash preserved.
- **Not built**: a browser for *already-deployed* systems (see Frontend Layer below)

## Frontend Layer

**Status**: ✅ Install surface implemented (trinity-enterprise#126, 2026-07-30).

Pick a bundled manifest / upload a file / paste YAML → preview → deploy. Home is a
stacked `#systems` section on the **Library** page rather than a new NavBar entry (the bar
already has six). It first shipped as a `?tab=` strip on `Templates.vue`; ent#263 then
renamed that page to Library and deliberately chose stacked sections + jump anchors over
tabs, so this conforms to the page's model instead of reintroducing a competing one.
Systems sits directly after Agent Templates because both **install agents** — one template
makes one agent, one manifest makes a whole wired fleet — leaving Skills last as the kind
that configures agents that already exist.

**Components** (`src/frontend/src/components/systems/`):

| Component | Responsibility |
|---|---|
| `SystemInstallPanel.vue` | Source picker (bundled cards / upload / paste), the manifest textarea, Preview + Deploy actions, and the outcome-unknown state |
| `ManifestPreview.vue` | Dry-run render: agents table, permission topology grouped by source, schedules table, blockers, warnings, and the acknowledgement gate |
| `DeployResult.vue` | Post-deploy render: switches on all five `status` values, created/failed lists, prominent warnings, next-action navigation |

**Store**: `src/frontend/src/stores/systems.js` — a new domain-scoped store (Invariant
#6), deliberately **not** added to `systemViews.js`: a *System* is a manifest-deployed
set of agents sharing a name prefix; a *System View* is a saved tag filter over agents.
Different domains that share a word. All calls go through the single `api` axios
instance (Invariant #7).

**Host**: `views/Library.vue` gained a third `<section id="systems">`, plus a "Systems"
jump anchor in the header nav. It is gated on `hasMinRole('creator')`, mirroring
`POST /api/systems/deploy`, and is **hidden outright** below that role rather than
shown-and-disabled — a browse surface gains nothing from a dead panel, and the anchor is
gated with it so the nav never points at a section that is not rendered. `/templates`
still redirects to `/library` with query **and** hash preserved, so older links survive. Note `hasMinRole` is a plain **function** — `composables/useRole.js`'s
own usage docstring says `hasMinRole.value(...)` and is stale.

**Editor**: a plain `<textarea>`. The repo's orphaned monaco-based `components/YamlEditor.vue`
(zero consumers since the Process Engine was decommissioned) was **not** revived: the
production CSP is `script-src 'self'` with no `unsafe-eval` and no `worker-src`, while the
dev CSP *does* allow `unsafe-eval` — so `npm run dev` cannot prove production. Every
acceptance criterion is satisfiable without it. Reviving it (or deleting the unreachable
`monaco-editor` dependency) is a scoped follow-up gated on proving it against the real
nginx CSP.

### Error contract the store must honour

The store never switches on the HTTP status code, because the code alone identifies
none of the six outcomes:

| Outcome | HTTP | Body |
|---|---|---|
| `deployed` / `partial` / `valid` / `invalid` | **200** | full report |
| `failed` (0 agents created) | **500** | **full report AS THE BODY** |
| parse / validation error | 400 | `{detail: "<string>"}` |
| request-model violation (e.g. over the size cap) | 422 | `{detail: [ … ]}` — a **list** |
| unexpected error, possibly after agents exist | 500 | `{detail: "<string>"}` |
| client timeout — **the server keeps deploying** | — | none |

Two are traps. `partial` is a 200, so a naive `.then()` renders a degraded outcome as
clean success. `failed` is a 500 whose body *is* the report, so a naive `catch` discards
exactly the `failed[]` list the "show created + failed" requirement needs. `normalizeError`
in the store collapses all six into one renderable shape and returns the 500-with-a-report
as a **result**, not an error.

### Honesty constraints in the UI

Each corresponds to a real way the backend can mislead a reader:

- **"agents created", never "success"** — `status` describes agent creation only.
  Folder/permission/schedule/tag/start failures land in `warnings[]` with `status` still
  `deployed`, so warnings render as their own prominent panel rather than a footnote.
- **Never "this will deploy"** — `github:` templates are not probed by the preview, and the
  permission topology is resolved against *all* agents while a partial deploy wires up only
  those created. Both limits are stated in the UI.
- **Acknowledgement, not a banner** — a manifest setting `prompt:` replaces the
  platform-wide `trinity_prompt` for **every agent on the instance**, and enabled schedules
  start recurring autonomous executions that spend API budget. Deploy is gated on an
  explicit checkbox.
- **Duplicate names are confirm-grade** — on a fresh install, re-installing a bundled
  manifest resolves to `_N` suffixes by default and recovery is manual, per agent.
- **No blind retry** — a timeout or bare 5xx renders "outcome unknown — may still be
  running" and offers the agent list instead of a retry, because cancelling the request does
  not cancel the (synchronous, serial) server-side deploy.
- **Preview is bound to its text** — `previewedText` is compared against the textarea, so
  editing after a preview disables Deploy until re-previewed.
- **Plain text only** — manifest descriptions, failure `reason`s and warnings never render
  via `v-html` (H-005). `reason` is credential-sanitized server-side but **not**
  HTML-sanitized.

### Still not built (deliberately out of scope)

- **`SystemDetail.vue` / a deployed-systems browser.** `GET /api/systems` and
  `GET /api/systems/{name}` exist and have no frontend consumer. Unowned; ent#126 scoped
  itself to *installing*, not browsing. This doc previously listed `SystemManifestEditor.vue`,
  `SystemsList.vue` and `SystemDetail.vue` as "planned"; only the install surface shipped, so
  the other two names are retired rather than left as a standing promise.
- **Async deploy with a job id + reconciliation** — the real fix for the timeout window.
- **Remote / registry manifest sources** — ent#14 and ent#108 own those; this ships the tab
  they slot into.
- **Per-agent credential setup after deploy** — ent#127.

## Backend Layer

### Models (`src/backend/models.py`)

**SystemAgentConfig** (Lines 221-227)
```python
class SystemAgentConfig(BaseModel):
    """Configuration for a single agent in a system manifest."""
    template: str  # e.g., "github:Org/repo" or "local:default"
    resources: Optional[dict] = None  # {"cpu": "2", "memory": "4g"}
    folders: Optional[dict] = None  # {"expose": bool, "consume": bool}
    schedules: Optional[List[dict]] = None  # [{name, cron, message, ...}]
    tags: Optional[List[str]] = None  # ORG-001 Phase 4: Per-agent tags
```

**SystemPermissions** (Lines 230-233)
```python
class SystemPermissions(BaseModel):
    """Permission configuration for system agents."""
    preset: Optional[str] = None  # "full-mesh", "orchestrator-workers", "none"
    explicit: Optional[Dict[str, List[str]]] = None  # {"orchestrator": ["worker1", "worker2"]}
```

**SystemViewConfig** (Lines 236-241) - ORG-001 Phase 4
```python
class SystemViewConfig(BaseModel):
    """Configuration for auto-creating a System View on deploy."""
    name: str
    icon: Optional[str] = None  # Emoji (e.g., "📝")
    color: Optional[str] = None  # Hex color (e.g., "#8B5CF6")
    shared: bool = True  # Visible to all users?
```

**SystemManifest** (Lines 244-253)
```python
class SystemManifest(BaseModel):
    """Parsed system manifest from YAML."""
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    agents: Dict[str, SystemAgentConfig]
    permissions: Optional[SystemPermissions] = None
    # ORG-001 Phase 4: Tags and System View support
    default_tags: Optional[List[str]] = None  # Applied to all agents in manifest
    system_view: Optional[SystemViewConfig] = None  # Auto-create System View on deploy
```

**SystemDeployRequest**
```python
class SystemDeployRequest(BaseModel):
    """Request to deploy a system from YAML manifest."""
    manifest: str  # Raw YAML string
    dry_run: bool = False
    # trinity-enterprise#125: abort on first agent-create failure (legacy behavior)
    strict: bool = False
```

**SystemDeployFailure** (trinity-enterprise#125)
```python
class SystemDeployFailure(BaseModel):
    """One agent that failed to create during a system deploy."""
    name: str  # Final (resolved) agent name
    short_name: str  # Short name from the manifest
    template: str
    reason: str  # Sanitized, truncated failure reason
    status_code: Optional[int] = None  # Original HTTP status when HTTPException
```

**SystemDeployResponse**
```python
class SystemDeployResponse(BaseModel):
    """Response from system deployment."""
    # "deployed" (all created) | "partial" (some failed) | "failed" (none created)
    # | "valid" (dry_run) — trinity-enterprise#125
    status: str
    system_name: str
    agents_created: List[str]  # Final agent names created
    agents_to_create: Optional[List[dict]] = None  # For dry_run preview
    prompt_updated: bool
    permissions_configured: int = 0
    schedules_created: int = 0
    tags_configured: int = 0  # ORG-001 Phase 4: Total tags applied
    system_view_created: Optional[str] = None  # ORG-001 Phase 4: View ID if created
    warnings: List[str] = []
    failed: List[SystemDeployFailure] = []  # trinity-enterprise#125
```

### Service Layer (`src/backend/services/system_service.py`)

#### parse_manifest() (Lines 18-90)
```python
def parse_manifest(yaml_str: str) -> SystemManifest:
    """
    Parse YAML string into SystemManifest.

    Validates:
    - YAML syntax (using yaml.safe_load)
    - Required fields: name, agents (at least 1)
    - Each agent has required field: template
    - ORG-001 Phase 4: Parses default_tags, system_view, per-agent tags

    Raises:
        ValueError: If YAML is invalid or missing required fields
    """
```

**Example Error**:
```
ValueError: "YAML parse error: mapping values are not allowed here..."
ValueError: "Missing required field: name"
ValueError: "Missing required field: agents (must have at least 1)"
ValueError: "Agent 'worker' missing required field: template"
```

#### validate_manifest() (Lines 93-194)
```python
def validate_manifest(manifest: SystemManifest) -> List[str]:
    """
    Validate manifest and return warnings.

    Validates:
    - System name: 1-50 chars, lowercase alphanumeric + hyphens, start/end with alphanumeric
    - Agent names: lowercase alphanumeric + hyphens
    - Template format: must start with "github:" or "local:"
    - Permissions: can't have both preset and explicit
    - Permission preset values: full-mesh, orchestrator-workers, none
    - Permission references: all agents must exist
    - Schedules: name, cron, message required
    - ORG-001 Phase 4: Tag format validation (lowercase alphanumeric + hyphens)
    - ORG-001 Phase 4: System view name required if system_view specified

    Returns:
        List of warning messages (empty if all valid)

    Raises:
        ValueError: If validation fails
    """
```

**Validation Rules**:
- System name regex: `^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$|^[a-z0-9]{1,2}$`
- Agent name regex: `^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$`
- Template prefix: `github:` or `local:`
- Permission presets: `full-mesh`, `orchestrator-workers`, `none`

**Example Warning**:
```
"Permission preset 'orchestrator-workers' specified but no 'orchestrator' agent defined. No permissions will be granted."
```

#### resolve_agent_names() (Lines 179-208)
```python
def resolve_agent_names(
    system_name: str,
    agents: Dict[str, SystemAgentConfig]
) -> Tuple[Dict[str, str], List[str]]:
    """
    Resolve short agent names to final names, handling conflicts.

    Naming convention: {system}-{agent}
    Example: "content-production-orchestrator"

    Conflict resolution: Add _N suffix
    Example: my-agent → my-agent_2 → my-agent_3

    Args:
        system_name: The system name prefix
        agents: Dict of short_name -> agent config

    Returns:
        Tuple of (name_mapping, warnings)
        - name_mapping: {short_name: final_name}
        - warnings: List of conflict warnings
    """
```

**Helper Functions**:
- `agent_exists(name)` (Lines 154-158): Check database for agent ownership record
- `get_next_agent_name(base_name)` (Lines 161-176): Find next available name with _N suffix

**Example Flow**:
```python
# First deployment
resolve_agent_names("my-system", {"worker": ...})
# Returns: ({"worker": "my-system-worker"}, [])

# Second deployment (conflict)
resolve_agent_names("my-system", {"worker": ...})
# Returns: ({"worker": "my-system-worker_2"},
#          ["Agent 'my-system-worker' already exists, will create 'my-system-worker_2'"])
```

#### configure_permissions() (Lines 215-293)
```python
async def configure_permissions(
    agent_names: Dict[str, str],  # {short_name: final_name}
    permissions: Optional[SystemPermissions],
    created_by: str
) -> int:
    """
    Apply permission configuration based on preset or explicit rules.

    Presets:
    - full-mesh: Every agent can call every other agent
    - orchestrator-workers: Only orchestrator can call workers
    - none: Clear all default permissions
    - explicit: Apply custom permission matrix

    Returns:
        Number of permissions configured
    """
```

**Permission Logic**:
- **full-mesh**: For each agent, grant permission to call all other agents
- **orchestrator-workers**: Grant orchestrator → all workers, clear worker permissions
- **none**: Clear all agent permissions (set to empty list)
- **explicit**: First clear unlisted agents, then apply explicit rules

#### configure_folders() (Lines 296-329)
```python
def configure_folders(
    agent_names: Dict[str, str],
    agents_config: Dict[str, SystemAgentConfig]
) -> int:
    """
    Configure shared folder settings for all agents.

    Calls: db.upsert_shared_folder_config(agent_name, expose_enabled, consume_enabled)

    Returns:
        Number of folder configs created
    """
```

#### create_schedules() (Lines 332-383)
```python
def create_schedules(
    agent_names: Dict[str, str],
    agents_config: Dict[str, SystemAgentConfig],
    owner_username: str
) -> int:
    """
    Create schedules for all agents.

    For each agent with schedules:
    - Create ScheduleCreate object from manifest data
    - Call db.create_schedule()
    - If enabled, add to scheduler_service

    Returns:
        Number of schedules created
    """
```

**Name-match skip (trinity-enterprise#89).** `deploy_manifest` creates each agent
(`create_agent_internal`) and calls `create_schedules` **afterwards**. Since ent#89,
creation itself materializes the *template's* declared `schedules:` block — so this
function is now the **second** schedule producer for the same agent, and a manifest
declaring a name the template also declares would insert a duplicate row (there is no
`UNIQUE(agent_name, name)` index on `agent_schedules`, and adding one is a dual-track
schema change that would fail on installs already holding duplicates).

It therefore reads the agent's existing schedule names once per agent and skips any
manifest entry whose `name` already exists — the same read-then-skip the creation-time
materializer uses — adding each created name to the set so a manifest that repeats a
name within its own block also yields one row. The skip **does not overwrite**: the
template's row survives, including its `enabled` value (manifest entries default
`enabled=True`, template entries default `False`). An unreadable existing set **fails
open** and creates unfiltered — dropping a manifest's schedules would be worse than the
duplicate the guard prevents.

Full flow: [scheduling.md](scheduling.md#1c-template-declared-schedules-at-creation-trinity-enterprise89).
Coverage: `tests/unit/test_ent89_manifest_no_duplicate.py`.

#### configure_tags() (Lines 429-480) - ORG-001 Phase 4
```python
def configure_tags(
    system_name: str,
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig],
    default_tags: Optional[List[str]] = None
) -> int:
    """
    Configure tags for all agents.

    Tag sources (applied in order):
    1. system_name (auto-applied as tag to all agents)
    2. default_tags (from manifest root, applied to all agents)
    3. per-agent tags (from agent config, applied to specific agents)

    All tags are normalized (lowercase, deduplicated).

    Returns:
        Total number of tags configured
    """
```

#### create_system_view() (Lines 483-534) - ORG-001 Phase 4
```python
def create_system_view(
    system_name: str,
    system_view: SystemViewConfig,
    default_tags: Optional[List[str]],
    owner_id: str
) -> Optional[str]:
    """
    Create a System View for the deployed system.

    The view filters by:
    - system_name tag (always included)
    - default_tags (if specified)

    Creates a SystemView in the database with the specified
    name, icon, color, and filter tags.

    Returns:
        View ID if created, None if failed
    """
```

#### start_all_agents() (Lines 537-562)
```python
async def start_all_agents(agent_names: List[str]) -> Dict[str, str]:
    """
    Start all created agents.

    This triggers Trinity meta-prompt injection with the updated trinity_prompt.

    Calls: start_agent_internal(agent_name) for each agent

    Returns:
        Dict of {agent_name: status} where status is 'started' or error message
    """
```

#### export_manifest() (Lines 414-551)
```python
def export_manifest(system_name: str, agents: List[Dict]) -> str:
    """
    Export a system as a YAML manifest.

    Process:
    1. Extract short names (remove system prefix)
    2. Retrieve template, resources from Docker labels
    3. Fetch folders config from database
    4. Fetch schedules from database
    5. Detect permission pattern (full-mesh, explicit, or none)
    6. Include global trinity_prompt if set
    7. Convert to YAML

    Returns:
        YAML string representing the system configuration
    """
```

**Permission Detection Logic**:
- Check first agent's permissions
- If matches full-mesh pattern (can call all other agents), verify all agents
- If full-mesh verified, use `preset: full-mesh`
- Otherwise, export explicit permission matrix

**Bug Fix (2025-12-18)**:
- Line 542: Changed `db.get_setting()` → `db.get_setting_value()` to avoid Python object serialization in YAML

### Router Layer (`src/backend/routers/systems.py`)

> **ent#124 (2026-07-24):** the full deploy orchestration (parse → validate →
> resolve → create loop → prompt → config phases → start) now lives in
> `services/system_service.py::deploy_manifest(manifest_yaml, current_user,
> request=None, *, dry_run, strict, create_agent_fn=None)` — the router below
> is a thin HTTP wrapper: it calls `deploy_manifest` and maps
> `status == "failed"` to a 500 `JSONResponse` (everything else, including
> HTTPException 400/strict-abort propagation, passes through unchanged).
> `create_agent_fn=None` lazily resolves the `routers/agents.py`
> `create_agent_internal` FACADE (which injects `ws_manager`), so
> `agent_created` WebSocket broadcasts are preserved on every deploy path.
> The pseudocode below documents the orchestration behavior wherever it lives;
> read `deploy_manifest` for the current line numbers.

#### POST /api/systems/deploy (Lines 31-196)
```python
@router.post("/deploy", response_model=SystemDeployResponse)
async def deploy_system(
    body: SystemDeployRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Deploy a multi-agent system from YAML manifest.

    This is a "recipe" deployment - agents become independent after creation.
    """
```

**Deployment Flow** (best-effort by default, trinity-enterprise#125):
```
1. parse_manifest(body.manifest) -> SystemManifest
   └─ Raises ValueError on YAML syntax error or missing required fields
   └─ ORG-001 Phase 4: Parses default_tags, system_view, per-agent tags

2. validate_manifest(manifest) -> warnings
   └─ Raises ValueError on validation failure (invalid names, templates, permissions)
   └─ ORG-001 Phase 4: Validates tag format and system_view.name

3. resolve_agent_names(manifest.name, manifest.agents) -> (agent_names, name_warnings)
   └─ Returns {short_name: final_name} mapping and conflict warnings

4. If dry_run=True:
   └─ Return preview: {status: "valid", agents_to_create: [...], warnings: [...]}

5. Create agents — BEST-EFFORT loop (trinity-enterprise#125):
   For each agent in manifest.agents:
     a. Build AgentConfig(name=final_name, template, resources)
     b. create_agent_internal(config, current_user, request, skip_name_sanitization=True)
     c. Success → append to created_agents
     d. Failure → _failure_reason(exc) normalizes (dict detail → 'error' field,
        credential-sanitize + URL-userinfo redact + 500-char truncate — learnings
        2026-07-14: git errors embed PAT-bearing remote URLs) and appends a
        SystemDeployFailure {name, short_name, template, reason, status_code}.
        Each failed create self-cleans via crud.py's own rollback (#1484).
     e. strict=True → abort on first failure instead, re-raising with the
        ORIGINAL status code (4xx no longer flattened to 500) and detail
        {error: "Deployment failed", failed_at, created, reason}

6. Total failure (created == [], not dry_run):
   └─ status="failed"; HTTP 500 with the FULL report as the body (JSONResponse)
      so code-only callers (curl -f) don't read it as success
   └─ Skips prompt write, all config phases, system view, and start

6b. Update trinity_prompt (if manifest.prompt AND ≥1 agent created):
   └─ db.set_setting("trinity_prompt", manifest.prompt)
   └─ Moved post-loop (trinity-enterprise#125) so a totally-failed deploy
      never mutates the platform-wide prompt

6c. Survivor scoping + partial warnings:
   └─ created_map = agent_names filtered to created agents; all config phases
      below receive created_map (the config fns skip missing short_names)
   └─ Partial → warning: re-deploying this manifest creates _N-suffixed
      duplicates of already-created agents (converge: trinity-enterprise#124)
   └─ orchestrator-workers preset with failed orchestrator → warning:
      workers have no inter-agent permissions, system may be non-functional

7. Configure shared folders (guarded — failure degrades to a warning):
   └─ configure_folders(created_map, manifest.agents) -> folders_configured

8. Configure permissions (if manifest.permissions; guarded):
   └─ configure_permissions(created_map, manifest.permissions, current_user.username) -> permissions_count

9. Create schedules (guarded):
   └─ create_schedules(created_map, manifest.agents, current_user.username) -> schedules_count

10. Configure tags (ORG-001 Phase 4; guarded):
    └─ configure_tags(manifest.name, created_map, manifest.agents, manifest.default_tags) -> tags_count
    └─ Applies: system_name tag + default_tags + per-agent tags
    └─ All tags normalized (lowercase, deduplicated)

11. Create System View (ORG-001 Phase 4, if manifest.system_view):
    └─ create_system_view(manifest.name, manifest.system_view, manifest.default_tags, current_user.id) -> system_view_id
    └─ Filter tags: system_name + default_tags
    └─ View appears in Dashboard sidebar (self-guards: returns None on error)

12. Start all agents:
    └─ start_all_agents(created_agents) -> start_results
    └─ Count agents_started and agents_failed (start failures stay warnings)

13. Return response:
    └─ {status: "deployed" | "partial", system_name, agents_created, prompt_updated,
        permissions_configured, schedules_created, tags_configured,
        system_view_created, warnings, failed: [SystemDeployFailure]}
```

Steps 7–10 are individually wrapped (trinity-enterprise#125): once agents exist, a
config-phase exception appends a sanitized warning and the deploy report is still
returned — it never converts a partial success into an opaque 500.

**Response status contract** (trinity-enterprise#125):
| status | Meaning | HTTP |
|--------|---------|------|
| `valid` | dry_run validation passed | 200 |
| `deployed` | all agents created | 200 |
| `partial` | some agents failed; survivors deployed + configured | 200 |
| `failed` | zero agents created | 500 (full report body) |

`SystemDeployRequest.strict` (default false) restores legacy abort-on-first-error.
Callers must check `status`/`failed[]`, not just the HTTP code.

**Error Handling**:
- YAML parse error - 400 with ValueError message
- Validation error - 400 with ValueError message
- Agent creation failure (default) - collected into `failed[]`, deploy continues
- Agent creation failure (strict=true) - abort with the failure's original status
  code, detail {error, failed_at, created, reason}
- Config-phase failure after creation - warning appended, report still returned
- Unexpected error outside the guarded phases - 500 with error message

**Failure entry** (`SystemDeployFailure`, models.py):
```json
{
  "name": "my-system-worker2",
  "short_name": "worker2",
  "template": "github:invalid/repo",
  "reason": "Failed to validate GitHub repository access: repository not found",
  "status_code": 502
}
```

**Known redeploy caveat**: a redeploy after partial failure `_N`-suffixes every
already-created agent (resolve_agent_names conflict handling), and a create that
failed after volume creation can 409 on the same name until the orphan-volume
sweep reclaims it (#1667 guard; the 409 reason carries the `docker volume rm`
remediation verbatim). Idempotent converge (`on_conflict: skip`) is deferred to
trinity-enterprise#124.

#### GET /api/systems (Lines 199-244)
```python
@router.get("")
async def list_systems(current_user: User = Depends(get_current_user)):
    """
    List all systems (agents grouped by prefix).

    Groups agents by system prefix (before last '-').
    Example: "my-system-abc-worker1" -> system "my-system-abc"

    Returns system summaries with agent counts and details.
    """
```

**Response**:
```json
{
  "systems": [
    {
      "name": "content-production",
      "agent_count": 3,
      "agents": [
        {
          "name": "content-production-orchestrator",
          "status": "running",
          "template": "github:Org/orchestrator-agent"
        },
        {
          "name": "content-production-writer",
          "status": "running",
          "template": "local:default"
        }
      ],
      "created_at": "2025-12-18T10:00:00Z"
    }
  ]
}
```

**Implementation**:
- Line 212: Get all agents via `get_accessible_agents(current_user)`
- Lines 214-234: Group by prefix (all parts except last after splitting on '-')
- Lines 237-238: Sort by created_at (newest first)

**Bug Fix (2025-12-18)**:
- Line 221: Changed from `parts[0]` to `'-'.join(parts[:-1])` to handle hyphenated system names

#### GET /api/systems/{system_name} (Lines 247-319)
```python
@router.get("/{system_name}")
async def get_system(
    system_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get system details with all agents.

    Returns detailed information about a system including all its agents,
    permissions, folders, and schedules.
    """
```

**Response**:
```json
{
  "name": "content-production",
  "agent_count": 2,
  "agents": [
    {
      "name": "content-production-orchestrator",
      "status": "running",
      "template": "github:Org/orchestrator-agent",
      "created_at": "2025-12-18T10:00:00Z",
      "permissions": ["content-production-writer", "content-production-editor"],
      "folders": {
        "expose": true,
        "consume": true
      },
      "schedules": [
        {
          "id": 1,
          "name": "daily-planning",
          "cron_expression": "0 9 * * *",
          "message": "Create today's content plan",
          "enabled": true,
          "timezone": "UTC"
        }
      ]
    }
  ]
}
```

**Implementation**:
- Lines 264-268: Filter agents by `{system_name}-` prefix
- Lines 270-274: Return 404 if no agents found
- Lines 277-307: Enrich each agent with permissions, folders, schedules from database
- Lines 289-290: Get permissions via `db.get_agent_permissions()`
- Lines 293-298: Get folders via `db.get_agent_folder_config()`
- Lines 301-302: Get schedules via `db.get_agent_schedules()`

#### POST /api/systems/{system_name}/restart (Lines 322-384)
```python
@router.post("/{system_name}/restart")
async def restart_system(
    system_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Restart all agents in a system.

    Finds all agents with the given system prefix and stops then starts them.
    Useful after configuration changes.
    """
```

**Response**:
```json
{
  "restarted": ["content-production-orchestrator", "content-production-writer"],
  "failed": []
}
```

**Implementation**:
- Lines 341-351: Filter agents by `{system_name}-` prefix, return 404 if none found
- Lines 356-373: For each agent:
  - Stop container if running via `container.stop()`
  - Start agent via `start_agent_internal(agent_name)` (triggers Trinity injection)
  - Track restarted and failed agents
- Lines 375-378: Return restarted and failed lists

#### GET /api/systems/{system_name}/manifest (Lines 387-426)
```python
@router.get("/{system_name}/manifest", response_class=PlainTextResponse)
async def get_system_manifest(
    system_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Export system as YAML manifest.

    Generates a YAML manifest from the current system configuration.
    Useful for backup, documentation, or replicating systems.
    """
```

**Response** (text/plain):
```yaml
name: content-production
description: Exported system configuration for content-production
agents:
  orchestrator:
    template: github:Org/orchestrator-agent
    resources:
      cpu: '2'
      memory: 4g
    folders:
      expose: true
      consume: true
    schedules:
    - name: daily-planning
      cron: 0 9 * * *
      message: Create today's content plan
      enabled: true
      timezone: UTC
  writer:
    template: local:default
    folders:
      expose: true
      consume: true
permissions:
  preset: full-mesh
prompt: |
  System-wide instructions for all agents...
```

**Implementation**:
- Lines 405-415: Filter agents by prefix, return 404 if none
- Line 418: Call `export_manifest(system_name, system_agents)` from service
- Line 420: Return YAML content as PlainTextResponse

#### GET /api/systems/manifests + /manifests/{manifest_id} (ent#126)

The read-only bundled-manifest catalog backing the install surface's "pick a bundled
manifest" source. Both are `require_role("creator")` — mirroring `POST /deploy`, since the
catalog exists only to feed it — and both reject connector principals. Thin routers over
`system_service.list_bundled_manifests()` / `read_bundled_manifest(id)`; **not** exported
over MCP (Invariant #13): this is a UI affordance, and `deploy_system` already exists there.

```
GET /api/systems/manifests        → [BundledManifestSummary]   (200 always; see fail-soft)
GET /api/systems/manifests/{id}   → BundledManifestDetail      (400 malformed id, 404 unknown)
```

**Root**: `TRINITY_MANIFESTS_DIR`, read **at call time**, defaulting to the bundled
`config/manifests/` — already bind-mounted `:ro` into the backend in both compose files. The
var is wired into `docker-compose.yml`, `docker-compose.prod.yml` and `.env.example` (prod
compose launches standalone, so dev-only wiring would not carry over — the #1056 packaging
class).

**`valid` = parse + validate + the dry-run's own template/resource preflight.** Not
parse-only: `parse_manifest` alone accepts invalid agent names, unsupported template prefixes
and bogus presets, so a parse-only check would advertise an undeployable manifest as valid.
The field is named `valid` only because all three ran; if it is ever reduced to parsing,
rename it `parseable`.

**Fail-soft per file.** An unreadable / oversized / invalid manifest is listed with
`valid: false` and a short `reason` — never a 500 for the whole catalog, because one bad file
must not hide the others. Every `reason` leaves through `_failure_reason`, the same exit point
the deploy report's `failed[].reason` uses (credential-sanitized, URL-userinfo redacted,
length-capped) — PyYAML parse errors **echo the offending source line**, and a joined list of
blockers grows with the manifest, so both the individual reasons and their join are capped.

**Path confinement on `{manifest_id}` is layered** — no single check is load-bearing:

| Layer | Guards against |
|-------|----------------|
| Character allowlist | Separators, NUL, control bytes |
| **Explicit** reject of `""` / `.` / `..` / embedded `..` | Traversal — the regex does **not** reject these; `.` is inside its character class |
| Length cap | Overlong ids |
| Suffix appended by us | Reading arbitrary file types |
| `resolve()` + `is_relative_to(base)` | A symlink whose target escapes the directory |
| `entry.is_symlink()` on the **unresolved** entry | A symlink whose target stays *inside* the directory — checked pre-`resolve()`, because `resolve()` has already erased the link. Parity with `list_bundled_manifests`, which skips symlinks outright; without it a symlink is invisible in the catalog yet readable by id, and "not listed" stops meaning "not served" |

**Reads open once**, with `O_NOFOLLOW`, and `fstat` **that descriptor**, capping at
`MANIFEST_MAX_BYTES + 1`. `config/manifests` is a host bind mount, so a stat-then-read
sequence has a real swap window.

### Agent Creation Integration

System deployment reuses existing agent creation logic via `create_agent_internal()` from `src/backend/routers/agents.py`.

**create_agent_internal()** (in agents.py - see agent-lifecycle.md for details):
```python
async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Request,
    skip_name_sanitization: bool = False  # True for system deployment
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by:
    - POST /api/agents endpoint
    - POST /api/systems/deploy endpoint

    Steps:
    1. Validate/sanitize name (skipped if skip_name_sanitization=True)
    2. Load template configuration (github: or local:)
    3. Resolve credentials
    4. Create Docker container with volumes, env vars, labels
    5. Create MCP API key for agent
    6. Register ownership in database
    7. Grant default permissions
    8. WebSocket broadcast "agent_created"
    9. Audit log
    """
```

**Key Parameters for System Deployment**:
- `skip_name_sanitization=True` - Names already validated by manifest validation
- `config.template` - From manifest agent config (github: or local:)
- `config.resources` - From manifest agent config (or default {"cpu": "2", "memory": "4g"})

## Request Flow

### Phase 1: Dry Run (Validation Only)

**Request**:
```http
POST /api/systems/deploy
Content-Type: application/json
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGc...

{
  "manifest": "name: test-system\nagents:\n  worker:\n    template: local:default",
  "dry_run": true
}
```

**Flow**:
```
1. API receives request → deploy_system() handler
2. parse_manifest(yaml_str) → SystemManifest
3. validate_manifest(manifest) → warnings: List[str]
4. resolve_agent_names("test-system", {"worker": ...})
   → ({"worker": "test-system-worker"}, warnings)
5. Return preview (no agents created)
```

**Response** (200 OK):
```json
{
  "status": "valid",
  "system_name": "test-system",
  "agents_created": [],
  "agents_to_create": [
    {
      "name": "test-system-worker",
      "short_name": "worker",
      "template": "local:default"
    }
  ],
  "prompt_updated": false,
  "warnings": []
}
```

### Phase 2: Actual Deployment

**Request**:
```http
POST /api/systems/deploy
Content-Type: application/json
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGc...

{
  "manifest": "name: my-system\nprompt: Custom instructions\nagents:\n  worker:\n    template: local:default\n    folders:\n      expose: true\n      consume: true\n    schedules:\n      - name: daily\n        cron: '0 9 * * *'\n        message: 'Daily task'\npermissions:\n  preset: none",
  "dry_run": false
}
```

**Flow**:
```
1. Parse & validate manifest
2. Resolve agent names: {"worker": "my-system-worker"}

3. Update trinity_prompt:
   db.set_setting("trinity_prompt", "Custom instructions")
   → INSERT INTO system_settings (key, value, updated_at)
      VALUES ('trinity_prompt', 'Custom instructions', datetime('now'))
      ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')

4. Create agent:
   AgentConfig(
     name="my-system-worker",
     template="local:default",
     resources={"cpu": "2", "memory": "4g"}
   )
   → create_agent_internal() →
     - Create Docker container
     - Register in agent_ownership table
     - WebSocket broadcast "agent_created"

5. Configure folders:
   db.upsert_shared_folder_config(
     agent_name="my-system-worker",
     expose_enabled=True,
     consume_enabled=True
   )

6. Configure permissions (preset: none):
   db.set_agent_permissions("my-system-worker", [], current_user.username)
   → DELETE FROM agent_permissions WHERE source_agent = 'my-system-worker'

7. Create schedules:
   schedule = ScheduleCreate(
     name="daily",
     cron_expression="0 9 * * *",
     message="Daily task",
     enabled=True,
     timezone="UTC"
   )
   db.create_schedule("my-system-worker", current_user.username, schedule)
   → INSERT INTO agent_schedules (next_run_at calculated)
   # Dedicated scheduler syncs within 60s and adds APScheduler job

8. Start agent:
   start_agent_internal("my-system-worker")
   → container.start()
   → inject_trinity_meta_prompt(agent_name, custom_prompt="Custom instructions")
     → POST http://agent-my-system-worker:8080/inject-trinity
        {"custom_prompt": "Custom instructions"}

9. Audit log:
   log_audit_event(
     event_type="system_deployment",
     action="deploy",
     user_id=current_user.username,
     ip_address=request.client.host,
     result="success",
     details={
       "system_name": "my-system",
       "agents_created": ["my-system-worker"],
       "agents_started": 1,
       "prompt_updated": True,
       "permissions_configured": 0,
       "schedules_created": 1,
       "folders_configured": 1
     }
   )
```

**Response** (200 OK):
```json
{
  "status": "deployed",
  "system_name": "my-system",
  "agents_created": ["my-system-worker"],
  "prompt_updated": true,
  "permissions_configured": 0,
  "schedules_created": 1,
  "tags_configured": 2,
  "system_view_created": null,
  "warnings": []
}
```

**Response with System View** (200 OK):
```json
{
  "status": "deployed",
  "system_name": "content-production",
  "agents_created": ["content-production-orchestrator", "content-production-writer"],
  "prompt_updated": true,
  "permissions_configured": 2,
  "schedules_created": 2,
  "tags_configured": 8,
  "system_view_created": "sv_abc123xyz",
  "warnings": []
}
```

### Phase 3: Permission Presets

#### Full-Mesh Example

**Manifest**:
```yaml
name: collab-system
agents:
  agent1:
    template: local:default
  agent2:
    template: local:default
  agent3:
    template: local:default
permissions:
  preset: full-mesh
```

**Permission Configuration Flow**:
```
configure_permissions(
  agent_names={"agent1": "collab-system-agent1", "agent2": "collab-system-agent2", "agent3": "collab-system-agent3"},
  permissions=SystemPermissions(preset="full-mesh"),
  created_by="user@example.com"
)

For agent1:
  targets = ["collab-system-agent2", "collab-system-agent3"]
  db.set_agent_permissions("collab-system-agent1", targets, "user@example.com")

For agent2:
  targets = ["collab-system-agent1", "collab-system-agent3"]
  db.set_agent_permissions("collab-system-agent2", targets, "user@example.com")

For agent3:
  targets = ["collab-system-agent1", "collab-system-agent2"]
  db.set_agent_permissions("collab-system-agent3", targets, "user@example.com")

Returns: 6 (total permissions configured)
```

#### Orchestrator-Workers Example

**Manifest**:
```yaml
name: workflow-system
agents:
  orchestrator:
    template: github:Org/orchestrator
  worker1:
    template: local:default
  worker2:
    template: local:default
permissions:
  preset: orchestrator-workers
```

**Permission Configuration Flow**:
```
orchestrator = "workflow-system-orchestrator"
workers = ["workflow-system-worker1", "workflow-system-worker2"]

# Grant orchestrator → all workers
db.set_agent_permissions(orchestrator, workers, created_by)

# Clear worker permissions
db.set_agent_permissions("workflow-system-worker1", [], created_by)
db.set_agent_permissions("workflow-system-worker2", [], created_by)

Returns: 2 (permissions configured for orchestrator)
```

#### Explicit Permissions Example

**Manifest**:
```yaml
name: pipeline-system
agents:
  manager:
    template: local:default
  analyst:
    template: local:default
  reporter:
    template: local:default
permissions:
  explicit:
    manager:
      - analyst
      - reporter
    analyst:
      - reporter
    reporter: []
```

**Permission Configuration Flow**:
```
# First, clear default permissions for all agents
db.set_agent_permissions("pipeline-system-manager", [], created_by)
db.set_agent_permissions("pipeline-system-analyst", [], created_by)
db.set_agent_permissions("pipeline-system-reporter", [], created_by)

# Then apply explicit rules
db.set_agent_permissions(
  "pipeline-system-manager",
  ["pipeline-system-analyst", "pipeline-system-reporter"],
  created_by
)

db.set_agent_permissions(
  "pipeline-system-analyst",
  ["pipeline-system-reporter"],
  created_by
)

db.set_agent_permissions(
  "pipeline-system-reporter",
  [],
  created_by
)

Returns: 3 (total permissions configured)
```

## MCP Tools Layer

### Tool Definitions (`src/mcp-server/src/tools/systems.ts`)

All tools support MCP API key authentication when `requireApiKey=true` in server config.

#### deploy_system
```typescript
{
  name: "deploy_system",
  description: "Deploy a multi-agent system from a YAML manifest... " +
    "Deploy is best-effort: check `status` ('deployed' | 'partial' | 'failed') " +
    "and `failed[]` in the response. Pass strict: true to restore abort-on-first-error.",
  parameters: z.object({
    manifest: z.string().describe("YAML manifest as a string..."),
    dry_run: z.boolean().optional().describe("If true, validates without creating agents"),
    strict: z.boolean().optional().describe("Abort on first agent-create failure (legacy)")
  }),
  execute: async ({ manifest, dry_run, strict }, context?) => {
    const apiClient = getClient(context?.session);
    const response = await apiClient.request("POST", "/api/systems/deploy", {
      manifest,
      dry_run: dry_run || false,
      strict: strict || false
    });
    return JSON.stringify(response, null, 2);
  }
}
```

On a total failure the backend answers HTTP 500 with the report as the body; the
MCP client's `ApiError` embeds that body in the tool error message, so the report
is still visible to the calling agent (trinity-enterprise#125).

#### list_systems (Lines 105-133)
```typescript
{
  name: "list_systems",
  description: "List all deployed systems with their agents...",
  parameters: z.object({}),
  execute: async (_params, context?) => {
    const apiClient = getClient(context?.session);
    const response = await apiClient.request("GET", "/api/systems");
    return JSON.stringify(response, null, 2);
  }
}
```

#### restart_system (Lines 138-167)
```typescript
{
  name: "restart_system",
  description: "Restart all agents belonging to a system...",
  parameters: z.object({
    system_name: z.string().describe("System prefix to restart...")
  }),
  execute: async ({ system_name }, context?) => {
    const apiClient = getClient(context?.session);
    const response = await apiClient.request(
      "POST",
      `/api/systems/${encodeURIComponent(system_name)}/restart`
    );
    return JSON.stringify(response, null, 2);
  }
}
```

#### get_system_manifest (Lines 172-202)
```typescript
{
  name: "get_system_manifest",
  description: "Generate a YAML manifest for a deployed system...",
  parameters: z.object({
    system_name: z.string().describe("System prefix to export...")
  }),
  execute: async ({ system_name }, context?) => {
    const apiClient = getClient(context?.session);
    const yaml = await apiClient.request(
      "GET",
      `/api/systems/${encodeURIComponent(system_name)}/manifest`
    );
    return yaml;  // Returns plain YAML string
  }
}
```

**Authentication Flow** (Lines 25-38):
```typescript
const getClient = (authContext?: McpAuthContext): TrinityClient => {
  if (requireApiKey) {
    // MCP API key is REQUIRED
    if (!authContext?.mcpApiKey) {
      throw new Error("MCP API key authentication required but no API key found in request context");
    }
    // Create authenticated client with user's MCP API key
    const userClient = new TrinityClient(client.getBaseUrl());
    userClient.setToken(authContext.mcpApiKey);
    return userClient;
  }
  // Backward compatibility: use base client
  return client;
};
```

## Data Layer

### Database Operations

#### System Settings Table (Lines 524-528 in database.py)
```sql
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

**Trinity Prompt Operations**:
```python
# Set trinity_prompt (upsert)
db.set_setting("trinity_prompt", prompt_value)
→ INSERT INTO system_settings (key, value, updated_at)
  VALUES ('trinity_prompt', ?, datetime('now'))
  ON CONFLICT(key) DO UPDATE SET
    value = ?,
    updated_at = datetime('now')

# Get trinity_prompt
db.get_setting_value("trinity_prompt", default=None)
→ SELECT value FROM system_settings WHERE key = 'trinity_prompt'
```

#### Agent Ownership Table
```sql
-- Check if agent exists (conflict detection)
SELECT owner_username FROM agent_ownership WHERE agent_name = ?

-- Register new agent
INSERT INTO agent_ownership (agent_name, owner_username, created_at)
VALUES (?, ?, datetime('now'))
```

#### Agent Permissions Table
```sql
-- Grant permissions (replaces existing)
DELETE FROM agent_permissions WHERE source_agent = ?
INSERT INTO agent_permissions (source_agent, target_agent, granted_by, granted_at)
VALUES (?, ?, ?, datetime('now'))

-- Clear permissions
DELETE FROM agent_permissions WHERE source_agent = ?
```

#### Shared Folders Table
```sql
-- Upsert folder config
INSERT INTO shared_folder_config (agent_name, expose_enabled, consume_enabled, updated_at)
VALUES (?, ?, ?, datetime('now'))
ON CONFLICT(agent_name) DO UPDATE SET
  expose_enabled = ?,
  consume_enabled = ?,
  updated_at = datetime('now')
```

#### Agent Schedules Table
```sql
-- Create schedule
INSERT INTO agent_schedules (
  agent_name, name, cron_expression, message, enabled, timezone,
  description, owner_username, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
```

### Docker Operations

**Container Creation** (via create_agent_internal):
```python
container = docker_client.containers.run(
    config.base_image,
    detach=True,
    name=f"agent-{config.name}",
    ports={'22/tcp': config.port},
    volumes={
        # Persistent volume mounted to agent home directory
        agent_volume_name: {
            'bind': '/home/developer',  # Agent home directory (no workspace subdirectory)
            'mode': 'rw'
        }
    },
    environment={
        'ANTHROPIC_API_KEY': '***',
        'MCP_API_KEY': '***',
        'TRINITY_BACKEND_URL': 'http://backend:8000',
        'TRINITY_AGENT_NAME': config.name
    },
    labels={
        'trinity.platform': 'agent',
        'trinity.agent-name': config.name,
        'trinity.ssh-port': str(config.port),
        'trinity.template': config.template or '',
        'trinity.owner': current_user.username
    },
    network='trinity-agent-network',
    cpu_period=100000,
    cpu_quota=int(config.resources.get('cpu', '2')) * 100000,
    mem_limit=config.resources.get('memory', '4g'),
    restart_policy={'Name': 'unless-stopped'}
)
```

> **Note**: Agent files live directly in `/home/developer` (the home directory). There is no workspace subdirectory.

**Container Start** (triggers Trinity injection):
```python
# Get trinity_prompt from database
custom_prompt = db.get_setting_value("trinity_prompt", default=None)

# Start container
container.start()

# Wait for agent-server to be ready
await wait_for_agent_server(agent_name, timeout=30)

# Inject Trinity meta-prompt with custom instructions
await inject_trinity_meta_prompt(agent_name, custom_prompt)
```

**Trinity Injection Request**:
```http
POST http://agent-{agent_name}:8080/inject-trinity
Content-Type: application/json

{
  "custom_prompt": "System-wide instructions for all agents..."
}
```

**Agent Server Response**:
```json
{
  "status": "success",
  "message": "Trinity meta-prompt injected successfully",
  "file": "/home/developer/.trinity/CLAUDE.md"
}
```

## Side Effects

### Logging

System deployment operations are logged via the standard Python logger. Detailed audit logging via `log_audit_event()` was removed in a refactoring pass - operations are now tracked via Vector log aggregation.

**Key Log Messages**:
```python
# Deployment progress (systems.py)
logger.info(f"Updated trinity_prompt for system '{manifest.name}'")
logger.info(f"Created agent '{final_name}' for system '{manifest.name}'")
logger.info(f"Configured {folders_configured} folder configs for system '{manifest.name}'")
logger.info(f"Configured {permissions_count} permissions for system '{manifest.name}'")
logger.info(f"Created {schedules_count} schedules for system '{manifest.name}'")
logger.info(f"Started {agents_started}/{len(created_agents)} agents for system '{manifest.name}'")

# Errors
logger.error(f"Failed to create agent '{final_name}': {e.detail}")
logger.exception(f"System deployment failed: {e}")
```

**Note**: Audit logging could be re-added if required for compliance. The infrastructure exists via `log_audit_event()` in the codebase but is not currently used in systems.py.

### WebSocket Broadcasts

**Agent Creation** (via create_agent_internal):
```python
await manager.broadcast({
    "event": "agent_created",
    "data": {
        "name": agent_name,
        "type": agent_type,
        "status": "running",
        "port": ssh_port,
        "created": created_timestamp,
        "resources": resources,
        "container_id": container_id,
        "template": template
    }
})
```

**Frontend Handling** (agents.js store):
```javascript
// Listen for WebSocket events
socket.on('agent_created', (data) => {
  agents.value.push(data);
  // UI auto-updates with new agent
});
```

### Trinity Prompt Injection

**Process**:
1. System manifest includes `prompt` field
2. Backend calls `db.set_setting("trinity_prompt", manifest.prompt)`
3. All agents created in the system
4. Each agent started via `start_agent_internal()`
5. Start function retrieves `trinity_prompt` from database
6. Agent-server receives injection request with `custom_prompt`
7. Agent-server updates `/home/developer/.trinity/CLAUDE.md`:

**CLAUDE.md Structure**:
```markdown
# Trinity Platform Agent

[Standard Trinity sections...]

## Custom Instructions

System-wide instructions for all agents...

[Rest of CLAUDE.md...]
```

## Agent Naming Convention

### Format
`{system}-{agent}`

**Examples**:
- `content-production-orchestrator`
- `content-production-writer`
- `workflow-system-worker1`
- `my-research-team-analyst`

### Conflict Resolution

**First Deployment**:
```
System: my-system
Agents: worker
Result: my-system-worker
```

**Second Deployment (conflict)**:
```
System: my-system
Agents: worker
Conflict: my-system-worker exists
Result: my-system-worker_2
Warning: "Agent 'my-system-worker' already exists, will create 'my-system-worker_2'"
```

**Third Deployment (multiple conflicts)**:
```
System: my-system
Agents: worker
Conflicts: my-system-worker, my-system-worker_2 exist
Result: my-system-worker_3
Warning: "Agent 'my-system-worker' already exists, will create 'my-system-worker_3'"
```

### System Grouping

**List Systems Endpoint**:
- Splits agent name on `-`
- Takes all parts except last as system prefix
- Groups agents by prefix

**Examples**:
```
Agent: content-production-orchestrator
Split: ["content", "production", "orchestrator"]
Prefix: "content-production" (all except last)
System: content-production

Agent: my-research-team-analyst
Split: ["my", "research", "team", "analyst"]
Prefix: "my-research-team"
System: my-research-team

Agent: test-list-abc123-worker1
Split: ["test", "list", "abc123", "worker1"]
Prefix: "test-list-abc123"
System: test-list-abc123
```

## Error Handling

### Validation Errors (HTTP 400)

| Error Case | Detail Message |
|------------|----------------|
| Invalid YAML syntax | `"YAML parse error: {yaml_error_details}"` |
| Empty manifest | `"Empty manifest"` |
| Missing `name` field | `"Missing required field: name"` |
| Missing `agents` field | `"Missing required field: agents (must have at least 1)"` |
| Invalid system name | `"Invalid system name '{name}': must be 1-50 chars, lowercase alphanumeric and hyphens, start/end with alphanumeric"` |
| Invalid agent name | `"Invalid agent name '{name}': must be lowercase alphanumeric and hyphens"` |
| Invalid template prefix | `"Agent '{name}': template must start with 'github:' or 'local:'"` |
| Both preset and explicit permissions | `"Cannot specify both preset and explicit permissions"` |
| Invalid permission preset | `"Invalid permission preset '{preset}': must be one of [full-mesh, orchestrator-workers, none]"` |
| Unknown agent in permissions | `"Unknown agent in permissions: {name}"` |
| Missing schedule fields | `"Agent '{agent}' schedule {i}: missing '{field}'"` |

### Runtime Errors (HTTP 500)

#### Partial Deployment Failure
```json
{
  "error": "Deployment failed",
  "failed_at": "my-system-worker2",
  "created": ["my-system-worker1"],
  "reason": "Template not found: github:invalid/repo"
}
```

**Behavior**:
- Agents created before failure remain in system
- User must manually clean up partial deployment
- Audit log records partial failure with created agents list

#### Generic Deployment Error
```json
{
  "detail": "Deployment failed: Database connection lost"
}
```

### Not Found Errors (HTTP 404)

| Endpoint | Condition | Detail Message |
|----------|-----------|----------------|
| `GET /api/systems/{name}` | No agents with prefix | `"System '{name}' not found or no accessible agents"` |
| `POST /api/systems/{name}/restart` | No agents with prefix | `"System '{name}' not found or no accessible agents"` |
| `GET /api/systems/{name}/manifest` | No agents with prefix | `"System '{name}' not found or no accessible agents"` |

### Authentication Errors (HTTP 401)

All endpoints require valid JWT token via `get_current_user` dependency.

**Missing or Invalid Token**:
```json
{
  "detail": "Not authenticated"
}
```

## Security Considerations

### Authentication & Authorization
- **JWT Required**: All endpoints use `get_current_user` dependency
- **Ownership**: Agents created are owned by authenticated user
- **Access Control**: Only accessible agents returned in list/get operations
- **Audit Trail**: All operations logged with user ID and IP address

### Input Validation
- **YAML Parsing**: Uses `yaml.safe_load` (prevents code execution)
- **Name Validation**: Regex validation for system and agent names
- **Template Validation**: Must start with `github:` or `local:`
- **Permission Validation**: All referenced agents must exist in manifest

### Partial Failure Handling
- **Atomic Per-Agent**: Each agent creation is atomic
- **Rollback**: No automatic rollback - partial deployments remain
- **Transparency**: Response includes list of successfully created agents
- **Audit Log**: Partial failures logged with error details

### Credential Security
- **No Exposure**: Credentials never included in manifests or exports
- **Agent Isolation**: Each agent has isolated credential storage (Redis)
- **MCP API Keys**: Generated per-agent for MCP tool access
- **Audit Logging**: Credential operations logged (values masked)

### YAML Export Security

**Bug Fixed (2025-12-18)**:
- **Issue**: Python object tags in exported YAML (security risk)
- **Root Cause**: ORM objects serialized instead of values
- **Fix**: Use `get_setting_value()` and convert Schedule objects to dicts
- **Impact**: Eliminated remote code execution vector

**Safe Export**:
```yaml
# GOOD (after fix)
prompt: System-wide instructions

# BAD (before fix)
prompt: !!python/object:db_models.Setting
  key: trinity_prompt
  value: System-wide instructions
```

## Testing

### Test Suite Organization

**Test File**: `tests/test_systems.py` (~870 lines, 9 test classes)

#### Test Categories
- **Smoke Tests** (`@pytest.mark.smoke`): YAML parsing and validation (no agent creation)
- **Core Tests**: Deployment, permissions, folders, schedules
- **Slow Tests** (`@pytest.mark.slow`): Full multi-agent system tests with all features
- **Integration Tests**: Complete workflows (export and redeploy)
- **Edge Cases**: Error handling, authentication, validation
- **Resilient Deploy** (`TestResilientDeploy`, trinity-enterprise#125): partial deploy
  continues + reports `failed[]`, total failure returns 500 + report body, strict
  aborts with the original status. Failure vector: `resources: {cpu: "3"}`
  (deterministic pre-side-effect 400). Hermetic router coverage lives in
  `tests/unit/test_ent125_resilient_system_deploy.py` (14 tests: survivor scoping,
  config-phase degradation, reason normalization/sanitization, prompt-write gating,
  orchestrator-failed warning).

### Phase 1 Tests: YAML Parsing and Validation

#### TestSystemManifestParsing (Line 130)

**test_dry_run_minimal_manifest**
- Action: POST with minimal valid manifest, dry_run=true
- Expected: 200, status="valid", agents_to_create populated
- Verifies: Basic YAML parsing and response structure

**test_dry_run_invalid_yaml**
- Action: POST with malformed YAML syntax
- Expected: 400, error contains "YAML parse error" or "parse"
- Verifies: YAML syntax validation

**test_dry_run_missing_name**
- Action: POST without "name" field
- Expected: 400, error contains "name"
- Verifies: Required field validation

**test_dry_run_missing_agents**
- Action: POST without "agents" field
- Expected: 400, error contains "agents"
- Verifies: Required field validation

**test_dry_run_invalid_system_name**
- Action: POST with uppercase system name
- Expected: 400
- Verifies: System name format validation

**test_dry_run_invalid_template_prefix**
- Action: POST with template not starting with github: or local:
- Expected: 400, error mentions "github:" or "local:"
- Verifies: Template format validation

**test_dry_run_invalid_permission_preset**
- Action: POST with unknown permission preset
- Expected: 400, error contains "preset"
- Verifies: Permission preset validation

**test_dry_run_both_preset_and_explicit**
- Action: POST with both preset and explicit permissions
- Expected: 400
- Verifies: Mutually exclusive permission config validation

### Phase 2 Tests: System Deployment

#### TestSystemDeployment (Line 262)

**test_deploy_minimal_system**
- Action: Deploy minimal system with one agent
- Expected: 200, status="deployed", agent created with correct name
- Verifies: Basic deployment flow
- Cleanup: Delete created agent

**test_deploy_conflict_resolution**
- Action: Deploy same manifest twice
- Expected: Second deployment creates agent with _2 suffix, warnings present
- Verifies: Conflict resolution with incremental suffix
- Cleanup: Delete both agents

**test_deploy_updates_trinity_prompt**
- Action: Deploy with prompt field
- Expected: 200, prompt_updated=true, GET /api/settings/trinity_prompt returns updated value
- Verifies: Trinity prompt database update
- Cleanup: Delete created agent

### Phase 3 Tests: Permissions

#### TestSystemPermissions (Line 368)

**test_full_mesh_permissions**
- Action: Deploy 3 agents with preset: full-mesh
- Expected: Each agent has permissions to call 2 other agents
- Verifies: Full-mesh permission pattern
- Cleanup: Delete 3 agents

**test_orchestrator_workers_permissions**
- Action: Deploy with preset: orchestrator-workers
- Expected: Orchestrator has 2 permissions, workers have 0
- Verifies: Orchestrator-workers permission pattern
- Cleanup: Delete 3 agents

**test_explicit_permissions**
- Action: Deploy with explicit permission matrix
- Expected: Manager can call analyst, analyst has no permissions
- Verifies: Explicit permission configuration
- Cleanup: Delete 2 agents

**test_none_permissions_preset**
- Action: Deploy with preset: none
- Expected: All agents have 0 permissions
- Verifies: Permission clearing
- Cleanup: Delete 2 agents

### Phase 4 Tests: Folders and Schedules

#### TestSystemFolders (Line 553)

**test_shared_folders_configuration**
- Action: Deploy with folders config (expose/consume settings)
- Expected: GET /api/agents/{name}/folders returns correct expose/consume values
- Verifies: Shared folder configuration
- Timeout: 120s (folder configuration is slow)
- Cleanup: Delete 2 agents

#### TestSystemSchedules (Line 605)

**test_schedules_created**
- Action: Deploy with 2 schedules
- Expected: schedules_created >= 2, GET /api/agents/{name}/schedules returns schedules
- Verifies: Schedule creation and database storage
- Cleanup: Delete agent

### Phase 5 Tests: Auto-Start

#### TestSystemAutoStart (Line 656)

**test_agents_started_after_deployment**
- Action: Deploy 2 agents
- Expected: Both agents have status "running" or "starting" after 10s
- Verifies: Auto-start after deployment
- Cleanup: Delete 2 agents

### Phase 6 Tests: Backend Endpoints

#### TestSystemBackendEndpoints (Line 702)

**test_list_systems_endpoint**
- Action: Deploy system with 2 agents, call GET /api/systems
- Expected: System appears in list with agent_count=2, 2 agents in array
- Verifies: System grouping and listing
- Cleanup: Delete 2 agents

**test_get_system_endpoint**
- Action: Deploy system, call GET /api/systems/{name}
- Expected: Detailed system info with agent list
- Verifies: System detail retrieval
- Cleanup: Delete agent

**test_get_nonexistent_system_returns_404**
- Action: GET /api/systems/nonexistent-system-12345
- Expected: 404
- Verifies: Not found error handling

**test_restart_system_endpoint**
- Action: Deploy system, call POST /api/systems/{name}/restart
- Expected: Agent name in "restarted" array
- Verifies: System restart functionality
- Cleanup: Delete agent

**test_export_manifest_endpoint**
- Action: Deploy system, call GET /api/systems/{name}/manifest
- Expected: Valid YAML with system name and agents
- Verifies: YAML export functionality
- Cleanup: Delete agent

### Phase 7 Tests: Complete Workflows

#### TestSystemCompleteWorkflows (Line 862)

**test_complete_system_deployment** (@pytest.mark.slow)
- Action: Deploy full system with prompt, folders, schedules, permissions
- Expected: All configurations applied, agents running, trinity_prompt updated
- Verifies: End-to-end deployment with all features
- Timeout: 120s (complex multi-agent deployment)
- Cleanup: Delete 2 agents

**test_export_and_redeploy** (@pytest.mark.slow)
- Action: Deploy system, export manifest, redeploy from export
- Expected: Second deployment creates with _2 suffix
- Verifies: Export-import round trip
- Cleanup: Delete 2 agents

### Phase 8 Tests: Edge Cases

#### TestSystemEdgeCases (Line 1009)

**test_deploy_requires_authentication**
- Action: POST /api/systems/deploy without auth
- Expected: 401
- Verifies: Authentication requirement

**test_deploy_empty_manifest**
- Action: POST with empty manifest string
- Expected: 400
- Verifies: Empty manifest validation

**test_deploy_with_unknown_agent_in_permissions**
- Action: POST with nonexistent agent in explicit permissions
- Expected: 400
- Verifies: Permission reference validation

**test_list_systems_requires_auth**
- Action: GET /api/systems without auth
- Expected: 401
- Verifies: Authentication requirement

**test_restart_nonexistent_system**
- Action: POST /api/systems/nonexistent-12345/restart
- Expected: 404
- Verifies: Not found error handling

### Test Status

**Note**: Test suite line numbers are approximate as tests are frequently updated. Run `pytest tests/test_systems.py -v --collect-only` to see current test list.

**Test Coverage**:
- ✅ YAML parsing and validation
- ✅ Dry run mode
- ✅ Agent creation with naming convention
- ✅ Conflict resolution
- ✅ Trinity prompt updates
- ✅ All permission presets (full-mesh, orchestrator-workers, none, explicit)
- ✅ Shared folder configuration
- ✅ Schedule creation
- ✅ Agent auto-start
- ✅ Backend endpoints (list, get, restart, export)
- ✅ Complete workflow (export and redeploy)
- ✅ Error handling and edge cases
- ✅ Authentication requirements

### Running Tests

**All System Tests**:
```bash
pytest tests/test_systems.py -v
```

**Smoke Tests Only** (fast, no agent creation):
```bash
pytest tests/test_systems.py -m smoke -v
```

**Skip Slow Tests**:
```bash
pytest tests/test_systems.py -m "not slow" -v
```

**Single Test**:
```bash
pytest tests/test_systems.py::TestSystemManifestParsing::test_dry_run_minimal_manifest -v
```

**With Coverage**:
```bash
pytest tests/test_systems.py --cov=src/backend/routers/systems --cov=src/backend/services/system_service --cov-report=html
```

## Related Flows

### Upstream Flows (Dependencies)
- **[Agent Lifecycle](agent-lifecycle.md)** - Agent creation via `create_agent_internal()`
- **[Template Processing](template-processing.md)** - GitHub and local template loading
- **[Credential Management](credential-management.md)** - Credential resolution for agents

### Downstream Flows (Triggered by System Deployment)
- **[Trinity Prompt Injection](system-wide-trinity-prompt.md)** - Global prompt injection into agents
- **[Agent Permissions](agent-permissions.md)** - Permission configuration (Phase 2)
- **[Agent Shared Folders](agent-shared-folders.md)** - Shared folder setup (Phase 2)
- **[Scheduling](scheduling.md)** - Cron-based automation (Phase 2)
- **[Agent Start](agent-start.md)** - Auto-start with Trinity injection
- **[Agent Tags & System Views](agent-tags.md)** - Automatic tagging and System View creation (ORG-001 Phase 4)

### Related Features
- **[MCP Orchestration](mcp-orchestration.md)** - MCP tools for system management
- **[Activity Monitoring](activity-monitoring.md)** - Track system deployment activities
- **[Audit Logging](audit-logging.md)** - Comprehensive audit trail

## Implementation History

### Phase 1: Basic Deployment (2025-12-17)
- YAML parsing with Pydantic validation
- `POST /api/systems/deploy` endpoint
- Dry run mode for validation preview
- Agent creation with `{system}-{agent}` naming
- Conflict resolution with `_N` suffix
- Global `trinity_prompt` update from manifest
- Audit logging (success and partial failure)
- WebSocket broadcasts for agent creation
- Reuses `create_agent_internal()` for consistent agent creation

### Phase 2: Permissions, Folders, Schedules (2025-12-18)
- Permission presets: `full-mesh`, `orchestrator-workers`, `none`
- Explicit permission matrix support
- Shared folder configuration per agent
- Schedule creation per agent
- Auto-start all agents after configuration
- Trinity injection on agent start

### Phase 3: MCP Tools and Backend APIs (2025-12-18)
- MCP tools: `deploy_system`, `list_systems`, `restart_system`, `get_system_manifest`
- Backend endpoints: `GET /systems`, `GET /systems/{name}`, `POST /systems/{name}/restart`, `GET /systems/{name}/manifest`
- Export manifest function to generate YAML from deployed systems
- System grouping by prefix (hyphenated name support)
- MCP API key authentication support

### Bug Fixes (2025-12-18)
- **P0**: YAML export serialization (eliminated Python object tags)
- **P1**: List systems prefix extraction (handle hyphenated names)
- **P2**: Test configuration (default password, timeouts)
- **Import Errors**: Fixed missing `list_agents_for_user` references
- **Template Endpoints**: Fixed 500 errors in template retrieval

### ORG-001 Phase 4: Tags and System Views (2026-02-17)
- **Automatic Tagging**: System name auto-applied as tag to all agents
- **Default Tags**: `default_tags` in manifest applied to all agents
- **Per-Agent Tags**: `tags` field in agent config for specific agents
- **System View Creation**: `system_view` in manifest auto-creates Dashboard sidebar view
- **Migration Script**: `scripts/management/migrate_prefixes_to_tags.py` for existing agents

### Future Phases
- **Phase 5 (Planned)**: Frontend UI
  - Manifest editor with syntax highlighting
  - System list and detail views
  - Deployment history and rollback
  - Visual permission matrix editor

## Key Files Reference

| File | Lines | Purpose |
|------|-------|---------|
| `src/backend/models.py` | 221-273 | Pydantic models for manifests and requests (ORG-001 Phase 4: SystemViewConfig, tags fields) |
| `src/backend/services/system_service.py` | 1-560 | YAML parsing, validation, deployment logic, configure_tags(), create_system_view() |
| `src/backend/routers/systems.py` | 1-230 | FastAPI endpoints for system management |
| `src/backend/database.py` | 524-528 | Settings table definition |
| `src/backend/db/tags.py` | 1-222 | TagOperations for agent tagging (ORG-001 Phase 1) |
| `src/backend/db/system_views.py` | 1-259 | SystemViewOperations (ORG-001 Phase 2) |
| `src/mcp-server/src/tools/systems.ts` | 1-205 | MCP tools for system operations |
| `scripts/management/migrate_prefixes_to_tags.py` | 1-100 | Migration script for existing agent prefixes |
| `tests/test_systems.py` | 1-870 | Comprehensive test suite (9 test classes) |

---

## First-Run Default Seed (trinity-enterprise#124)

On a genuinely fresh install, Trinity auto-deploys a bundled default manifest
once, right after first-time setup — the multi-agent generalization of the
Cornelius seeder (ent#107).

**Service**: `src/backend/services/system_seed_service.py`

**Flow**: `routers/setup.py` (setup-completion background task) and `main.py`
lifespan (safety-net, gated on `setup_completed`, strong task ref) both call
`ensure_first_run_seeded()`, which:

1. **Resolves the persisted first-run verdict** (`first_run_fresh`
   system-setting): stored value wins; else computed ONCE as
   `count_non_system_agents() == 0`, with `cornelius_seeded == "true"` forcing
   NOT-fresh (an established ent#107-era install — even one whose agents were
   all deleted — never wakes up to a surprise fleet). Persisting the verdict
   is load-bearing: recomputing per pass is poisoned by the first seeder's own
   agents (a failed fleet deploy could never retry; a failed Cornelius would
   self-mark seeded after the fleet lands). DB error ⇒ verdict `None`
   (undetermined, nothing persisted, everything defers).
2. Runs `cornelius_agent_service.ensure_seeded(fresh=verdict)` (unchanged
   behavior; `fresh=None` falls back to its legacy internal count).
3. Runs `system_seed_service.ensure_seeded(fresh=verdict)`:
   - Skips (no flag burn): Docker unavailable, disable sentinel, verdict
     undetermined, admin missing (pre-setup), manifest unavailable, lock held.
     The `system_seed:provision` lock is taken + released through the shared
     `redis_breaker_util.SingleFlightLock` (#1920): a unique per-acquire token
     + compare-and-delete release (kept LOCAL to the pass, never on the module
     singleton), so a slow pass whose TTL lapsed can no longer delete a
     *successor's* live lock — the pre-#1920 tokenless `delete`. This is class
     hygiene, NOT the double-seed guard (the existence backstop below is that).
   - Converges the flag without deploying: verdict not-fresh, or **existence
     backstop** — any final `{system}-{short}` name already reserved
     (`is_agent_name_reserved`, soft-deleted included). The backstop is the
     real duplicate guard: the deploy path suffixes name collisions (`_N`)
     instead of 409ing, so a fail-open SETNX race would otherwise double-seed.
   - Deploys via `system_service.deploy_manifest(strict=False)` as the admin
     owner, `request=None`.
   - Flag policy: `deployed`/`partial` → `default_system_seeded=true`
     (+ `default_system_seed_info` JSON: manifest name/sha256/source/status/
     counts/timestamp; a partial fleet is never re-deployed — that would
     suffix-duplicate survivors); `failed` (0 created) / exception → flag NOT
     set, later pass retries safely.
   - Honest status: partial / failed / seed-path error (e.g. parse-broken
     override) / unreadable-override / crash-interrupted partial fleet (the
     converge backstop finding only SOME names reserved) each raise ONE
     operator-queue alert (direct DB create on `trinity-system`,
     deterministic `system-seed-<kind>` id — a #1632 reserved prefix, so an
     agent cannot pre-create-and-silence it; best-effort).

**Manifest resolution**: `TRINITY_DEFAULT_SYSTEM_MANIFEST` env, read at call
time and `strip()`ed (compose `${VAR:-}` arrives set-but-empty ⇒ bundled):
a path ⇒ operator override (unreadable ⇒ loud failure, NO bundled fallback);
`disabled`/`none`/`off`/`0`/`false` ⇒ seeding disabled (flag not burned, so
re-enabling on a still-fresh install seeds); unset ⇒ bundled
`config/manifests/default-system.yaml` — baked into the backend image
(Dockerfile COPY) **and** bind-mounted via `./config/manifests` in both
compose files (the dev compose `./src/backend:/app` mount shadows image
COPYs, so the mount is load-bearing locally). The e2e CI stack sets the
`disabled` sentinel explicitly to keep its zero-agent baseline.

**Bundled fleet**: the in-tree acme trio (`local:scout`/`sage`/`scribe`)
deployed as the coherent team its CLAUDE.md content assumes (`name: acme`,
shared folders, `preset: full-mesh`); no `schedules:` (zero-credential
installs must not accumulate failing crons), no `prompt:` (never mutates the
platform-wide `trinity_prompt`). Content is data — ent#137's curated public
fleet replaces the manifest without touching the mechanism.

**Other bundled manifests are inert** — `system_seed_service.BUNDLED_MANIFEST_PATH`
is hard-coded to `default-system.yaml` and nothing globs the directory, so
adding a manifest to `config/manifests/` ships **data, not a trigger**. #1931
added `vc-due-diligence.yaml` (the eleven `hidden: true` `dd-*` demo templates,
~40 GB of declared memory limits) on exactly that basis: it can only be
incurred by a deliberate authenticated `POST /api/systems/deploy`.

**Tests**: `tests/unit/test_ent124_default_system_seed.py` (includes
executor's-own-parser validation of the real bundled manifest + in-tree
template existence — learnings 2026-07-23 blank-agent trap). #1931 widened the
two on-disk checks (`local:` → `template.yaml` + `CLAUDE.md`; declared
`commands:` → `.claude/commands/<n>.md`) from `default-system.yaml` to a
**glob** over `config/manifests/*.yaml`, parametrised so a failure names the
manifest — they are properties of any bundled manifest, and this is what
auto-enrols the next one. `tests/unit/test_1931_manifest_roster.py` adds
`validate_manifest` over the same glob plus the roster check: a template's
CLAUDE.md may name its collaborators literally (`acme-scout`,
`research-network-researcher`, `vc-due-diligence-dd-founder`), and since
deployed names are `f"{manifest.name}-{short}"`, a renamed system or short name
deploys a healthy fleet that cannot talk to itself.

## Post-deploy endpoints (#2373)

The four post-deploy endpoints were essentially untouched since 2025 while every
commit since ent#124 hardened the *deploy* half. What changed:

**Membership is one predicate — `system_service.system_member_names`.**
`get_system`, `restart_system` and `export_manifest` each matched
`startswith(f"{system_name}-")`, so an operation on `acme` also captured every
agent of a system named `acme-extra` — including `restart`, which stops and
starts containers. Tags come first, because `configure_tags` already applies the
system name to every member: a tag is a **record** of membership where a prefix
is an inference from a naming convention. The prefix survives only as a fallback
for pre-tag deployments, and it is narrowed — an agent carrying some other tag
`T` whose own prefix claims it (`name.startswith(f"{T}-")`) is excluded, so a
tagged `acme-extra` agent is never captured by `acme` even on that path. A tag
read that fails degrades to the prefix rather than 500ing, narrowed only where
the ROSTER evidences a longer sibling — an agent literally named `acme-extra`
sitting beside `acme-extra-worker`. It deliberately does NOT narrow on name
SHAPE: excluding any member whose short name contains a hyphen is strictly
narrower than the prefix it claims to fall back to, and dropped all eleven
`vc-due-diligence-dd-*` agents of the bundled flagship manifest — turning a
healthy fleet into `404 System not found` and an empty export on a transient
error. Residual, stated rather than hidden: two systems deployed BEFORE tagging
where one name is a prefix of the other stay ambiguous — on a roster of names
alone `acme-extra-worker` and `vc-due-diligence-dd-lead` are indistinguishable,
so with no evidence `acme` may still capture the former. Losing a healthy system
entirely is the worse of the two errors. This predicate
is also the prerequisite for the teardown verb, where the same collision would
delete rather than restart.

**`GET /{name}` returns real schedules.** It called `db.get_agent_schedules`,
which does not exist — the facade exposes `list_agent_schedules`, and
`database.py` deliberately has no `__getattr__` fallback. The `AttributeError`
was swallowed by the surrounding `except Exception`, so every response omitted
`schedules` for every agent and logged one warning each, while the suite never
asserted on the key. Exactly the failure mode the db facade's own comment warns
about.

**`POST /{name}/restart` is `require_role("creator")` AND human-only.** It was
bare `get_current_user` — below `POST /deploy` and below even the read-only
bundled-catalog routes — so any authenticated principal could stop and start
every container in a system whose agents it could see.

The second half is a separate gate, and an earlier draft of this paragraph got
it backwards: **`require_role` does NOT reject agent principals.** It rejects
CONNECTOR principals only, and its own docstring says so deliberately —
`require_role("creator")` on `POST /api/agents` is what makes ent#69 Part 2
agent-spawned creation work, so a blanket rejection there would break ghost
spawning. Do not "fix" `require_role`; the guard belongs at the endpoint, which
now calls `reject_agent_principal` explicitly.

That rejection is not merely a wider gate — it closes a **bypass**. The
per-agent equivalents (`POST /agents/{name}/start`, `/stop`, `/delete`) each
call `enforce_agent_spawn_scope`, so an agent-scoped caller may only start or
stop agents it actually SPAWNED (name *and* key id). `restart_system` loops
every member calling `container_stop` + `start_agent_internal` with **no**
per-member check, so reaching it with an agent key performs, in bulk and
unscoped, exactly the operation that is spawn-scoped one at a time — and on a
default admin-owned install an agent key resolves to its owner carrying the
owner's role, so the role gate alone admits the whole fleet
(trinity-ops-agent#232 class). Scoping per member was considered and rejected:
a system whose every member the caller spawned is a near-empty set, so it would
be a strange capability rather than a useful one.

**Export round-trips.** The non-full-mesh permissions branch sliced
`target_agent[len(system_name)+1:]` with no membership filter, so an edge
pointing outside the system exported as a garbage short name that then failed
`validate_manifest`'s unknown-agent check on re-deploy — the export broke its
own round trip. Both branches now test membership. And the export no longer
embeds the instance-global `trinity_prompt` as the manifest's `prompt:`:
deploying that elsewhere overwrote *that* instance's platform-wide prompt, and
since nothing records whether the source system ever set one, the only honest
export of an unknown is to omit it.

**Preview hardenings.** Unknown PER-AGENT keys now warn like top-level ones
(ent#126) — `credentials:`, `skills:`, `display_label:` are the fields users try
first and they vanished in silence. And preview and deploy resolve the identical
resource default through `_manifest_default_resources()` → the create path's
`_get_default_resource`; deploy hardcoded `{"cpu": "2", "memory": "4g"}` while
the preflight validated against the admin-configurable value, so they disagreed
the moment an admin moved the fleet default.

## Revision History

| Date | Changes |
|------|---------|
| 2026-07-31 | **trinity-enterprise#126**: UI install surface (a stacked `#systems` section on the Library page — rebased onto ent#263, which renamed Templates -> Library and chose stacked sections over the `?tab=` strip this originally shipped; `components/systems/*` over the new `stores/systems.js`) + the read-only bundled-manifest catalog `GET /manifests` / `/manifests/{id}`. Dry-run gains `permission_edges` / `schedules_preview` / `system_view_requested` from **pure resolvers shared with the writers** (`configure_permissions` / `create_schedules` now loop over them), pinned by characterization tests captured green before the refactor. `_preflight_template` now validates **merged** resources through the create path's own `normalize_cpu` / `normalize_memory` — a shipped bundled manifest carried `cpu: 1.0`, previewed `valid`, and failed 100% of its agents (that manifest, a broken duplicate of the live seed, is deleted). `status` becomes five-valued (`invalid` added). `parse_manifest` warns on unrecognised top-level keys (coercing them with `str()` — YAML 1.1 renders bare `on`/`off`/`yes`/`no` as booleans, so a mixed-type key set made `sorted` raise and turned the hygiene check into the unnamed 500 it existed to prevent). Catalog `reason`s exit through `_failure_reason`; symlinked manifests are refused for catalog/read parity. Merged with trinity#1884 (landed on `dev` mid-review), which moves the manifest size cap into `parse_manifest` alongside its alias-budget and duplicate-key guards — so ent#126's request-model cap is dropped and `MANIFEST_MAX_BYTES` stays a single definition in `models.py` that both modules import. |
| 2026-07-24 | **trinity-enterprise#124**: deploy orchestration extracted to `system_service.deploy_manifest` (router now a thin HTTP wrapper; `create_agent_fn` seam defaults to the ws-broadcasting `routers/agents` facade); first-run default seed added (`system_seed_service.py`, persisted `first_run_fresh` verdict shared with the Cornelius seeder, bundled `config/manifests/default-system.yaml`, `TRINITY_DEFAULT_SYSTEM_MANIFEST` override/disable). Partial-deploy warning no longer points at #124 for converge support. |
| 2026-02-17 | **ORG-001 Phase 4**: Added tags and System View integration - `default_tags`, `system_view`, per-agent `tags` in manifest. Updated models.py (221-273), system_service.py (configure_tags, create_system_view), routers/systems.py (steps 10-11 in deploy flow). Added response fields `tags_configured`, `system_view_created`. |
| 2026-02-11 | Fixed Docker volume mount path - now mounts to `/home/developer` (not workspace subdirectory) |
| 2026-01-23 | Line number verification: Updated models.py (248-286), systems.py (1-427), MCP tools. Removed outdated audit logging references (not in current implementation). Updated test section with correct class line numbers. |
| 2025-12-30 | Line numbers verified |
| 2025-12-18 | Bug fixes: YAML export serialization (P0), list systems prefix extraction (P1) |
| 2025-12-18 | Phase 3: MCP tools and backend endpoints completed |
| 2025-12-18 | Phase 2: Permissions, folders, schedules, auto-start completed |
| 2025-12-17 | Phase 1: Basic deployment, YAML parsing, conflict resolution |

---

**Last Updated**: 2026-02-17 (ORG-001 Phase 4 integration)
**Status**: Complete (Phases 1, 2, 3, ORG-001 Phase 4 - API only, no frontend UI)
**Feature Flag**: None (always enabled)
