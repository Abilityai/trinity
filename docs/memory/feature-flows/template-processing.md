# Feature: Template Processing

## Overview
Template processing enables agent creation from pre-configured templates, supporting both local templates (in `config/agent-templates/`) and GitHub-based templates. The system extracts credential requirements, parses template.yaml metadata, processes .mcp.json templates, and initializes agent workspaces.

## User Story
As a platform user, I want to create agents from templates so that I can quickly deploy pre-configured agents with the correct MCP servers and credential requirements.

## Entry Points
- **UI**: `src/frontend/src/views/Templates.vue` - Dedicated templates page (primary)
- **UI**: `src/frontend/src/components/CreateAgentModal.vue` - Create agent form with template selection
- **API**: `GET /api/templates` - List available templates
- **API**: `GET /api/templates/{template_id}` - Get template details
- **API**: `POST /api/agents` - Create agent with template

---

## Frontend Layer

### Templates.vue (`src/frontend/src/views/Templates.vue`)

Dedicated templates page that dynamically loads templates from the API (previously static hardcoded cards).

| Line | Element | Purpose |
|------|---------|---------|
| 16-24 | Refresh button | `@click="fetchTemplates"` with loading spinner |
| 55-134 | GitHub Templates section | Grid of GitHub template cards |
| 137-216 | Local Templates section | Grid of local template cards |
| 218-247 | Custom Agent section | "Blank Agent" card |
| 262-267 | CreateAgentModal | Opens with `initial-template` prop pre-selected |
| 304-318 | `fetchTemplates()` | Fetches from `/api/templates` API |
| 320-323 | `useTemplate()` | Sets `selectedTemplateId` and opens modal |
| 325-332 | `onAgentCreated()` | Navigates to `/agents/{name}` after creation |

**Template Card Display**:
- Name and description (GitHub shows repo, local shows display_name)
- MCP Servers list (shows up to 4, then "+N more")
- Resources: CPU and memory allocation
- Credentials count
- "Use Template" button

**Computed Properties** (Lines 290-296):
```javascript
const githubTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'github')
})

const localTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'local' || !t.source)
})
```

**getDisplayName helper** (Lines 299-302):
```javascript
const getDisplayName = (template) => {
  const name = template.display_name || template.id
  return name.replace(' (GitHub)', '')
}
```

### CreateAgentModal.vue (`src/frontend/src/components/CreateAgentModal.vue`)

| Line | Element | Purpose |
|------|---------|---------|
| 9 | Form submission | `@submit.prevent="createAgent"` |
| 47-68 | Blank agent option | `form.template = ''` selection |
| 71-102 | Local templates section | Shows templates with `source === 'local'` |
| 105-136 | GitHub templates section | Shows templates from API with `source === 'github'` |
| 191-196 | `initialTemplate` prop | Pre-selects template when modal opens |
| 198 | `emit('created', agent)` | Emits created agent data for navigation |
| 208-210 | Watch initialTemplate | Syncs form.template when prop changes |
| 263-285 | createAgent method | Posts to API and emits `created` event |

**Props** (Lines 191-196):
```javascript
const props = defineProps({
  initialTemplate: {
    type: String,
    default: ''
  }
})
```

**Events** (Line 198):
```javascript
const emit = defineEmits(['close', 'created'])
```

**Watch for initialTemplate** (Lines 208-210):
```javascript
watch(() => props.initialTemplate, (newVal) => {
  form.template = newVal || ''
})
```

**Computed Properties** (Lines 219-230):
```javascript
const githubTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'github')
})

const localTemplates = computed(() => {
  return templates.value.filter(t => t.source === 'local' || !t.source)
})

const selectedTemplate = computed(() => {
  if (!form.template) return null
  return templates.value.find(t => t.id === form.template)
})
```

---

## Backend Layer

### Template Endpoints (`src/backend/routers/templates.py`)

| Line | Endpoint | Purpose |
|------|----------|---------|
| 19-59 | `GET /api/templates` | List all templates (GitHub + local) |
| 62-172 | `GET /api/templates/env-template` | Get .env template for bulk import |
| 174-220 | `GET /api/templates/{template_id:path}` | Get template details |

### List Templates (`routers/templates.py:19-59`)
```python
@router.get("")
async def list_templates(current_user: User = Depends(get_current_user)):
    # 1. Load ALL_GITHUB_TEMPLATES from config.py (lines 29-30)
    # 2. Scan config/agent-templates/ for local templates (lines 33-55)
    # 3. Parse template.yaml for each local template
    # 4. Extract credential requirements via extract_agent_credentials()
    # 5. Sort by priority, then display_name (line 58)
    # 6. Return merged list
```

**Catalog curation (#1513).** `get_local_templates()` excludes any template
whose `template.yaml` sets `hidden: true` — the internal test/canary fixtures
(`test-*`, `sleep-echo`) and demo/system agents (`demo-researcher`,
`demo-analyst`, `trinity-system`). Hidden templates stay resolvable by id via
`get_local_template()` and creatable by id (the create path resolves by
directory name), so the canary/test harness is unaffected — only the
user-facing list hides them. Both `_build_local_template` and `_build_template`
now surface a coerced-int `priority` (`_coerce_priority`; a present-but-null or
string value would otherwise `TypeError` the router sort), so the step-5 sort
actually orders the real starters (`scout`/`sage`/`scribe`, `priority: 20`)
ahead of the rest. `Templates.vue` renders a **Starter Templates** (local)
section in addition to GitHub, so it shows the same curated set as
`CreateAgentModal`.

### Template Response Schema
```json
{
  "id": "github:abilityai/agent-ruby",
  "display_name": "Ruby - Content & Publishing",
  "source": "github",
  "resources": {"cpu": "2", "memory": "4g"},
  "mcp_servers": ["heygen", "twitter-mcp"],
  "required_credentials": [
    {"name": "HEYGEN_API_KEY", "source": "mcp:heygen"}
  ]
}
```

---

## Template Processing Logic

Template processing is handled by `services/agent_service/crud.py` (function `create_agent_internal`).

### GitHub Templates (`services/agent_service/crud.py:96-144`)
```python
if config.template.startswith("github:"):
    gh_template = get_github_template(config.template)  # Line 97

    if gh_template:
        # Pre-defined GitHub template from config.py
        github_repo = gh_template["github_repo"]

        # Get system GitHub PAT from settings (SQLite) or env var (lines 105-111)
        github_pat = get_github_pat()
        if not github_pat:
            raise HTTPException(500, "GitHub PAT not configured. Set GITHUB_PAT in .env or add via Settings.")

        github_repo_for_agent = github_repo
        github_pat_for_agent = github_pat
        config.resources = gh_template.get("resources", config.resources)
        config.mcp_servers = gh_template.get("mcp_servers", config.mcp_servers)
    else:
        # Dynamic GitHub template - use any github:owner/repo format (lines 117-137)
        repo_path = config.template[7:]  # Remove "github:" prefix
        github_pat = get_github_pat()  # From settings (SQLite) or env var
        if not github_pat:
            raise HTTPException(500, "GitHub PAT not configured.")
        github_repo_for_agent = repo_path
        github_pat_for_agent = github_pat

    # Generate git sync instance ID and branch (lines 143-144)
    git_instance_id = git_service.generate_instance_id()
    git_working_branch = git_service.generate_working_branch(config.name, git_instance_id)
```

### Local Templates (`services/agent_service/crud.py:145-182`)
```python
elif config.template.startswith("local:"):
    template_name = config.template[6:]  # Remove "local:" prefix (line 147)
    templates_dir = Path("/agent-configs/templates")
    if not templates_dir.exists():
        templates_dir = Path("./config/agent-templates")

    template_path = templates_dir / template_name
    template_yaml = template_path / "template.yaml"

    if template_yaml.exists():
        with open(template_yaml) as f:
            template_data = yaml.safe_load(f)
            config.type = template_data.get("type", config.type)
            config.resources = template_data.get("resources", config.resources)
            config.tools = template_data.get("tools", config.tools)
            # Extract MCP servers from credentials section (lines 162-165)
            creds = template_data.get("credentials", {})
            mcp_servers = list(creds.get("mcp_servers", {}).keys())
            if mcp_servers:
                config.mcp_servers = mcp_servers
            # Multi-runtime support (lines 167-172)
            runtime_config = template_data.get("runtime", {})
            # Shared folder config (lines 173-179)
            shared_folders_config = template_data.get("shared_folders", {})
```

---

## Credential Extraction

### Template Service (`src/backend/services/template_service.py`)

### extract_agent_credentials (`services/template_service.py:143-225`)
```python
def extract_agent_credentials(repo_path: Path) -> Dict:
    """Extract credential requirements from:
    1. .mcp.json or .mcp.json.template (${VAR_NAME} patterns)
    2. template.yaml (credentials schema)
    3. .env.example (env file vars)

    Returns:
        {
            "required_credentials": [{"name": "VAR", "source": "mcp:server"}],
            "mcp_servers": {"server": ["VAR1", "VAR2"]},
            "env_file_vars": ["VAR3"]
        }
    """
    pattern = r'\$\{([A-Z][A-Z0-9_]*)\}'  # Matches ${VAR_NAME}
```

### extract_env_vars_from_mcp_json (`services/template_service.py:64-103`)
```python
def extract_env_vars_from_mcp_json(file_path: Path) -> Dict[str, List[str]]:
    # Parse JSON and extract ${VAR_NAME} patterns from:
    # - env section of each MCP server config (lines 88-92)
    # - args array of each MCP server config (lines 94-98)
    pattern = r'\$\{([A-Z][A-Z0-9_]*)\}'
    for server_name, server_config in mcp_servers.items():
        if "env" in server_config:
            matches = re.findall(pattern, value)  # ${VAR_NAME}
        if "args" in server_config:
            matches = re.findall(pattern, arg)
```

### extract_credentials_from_template_yaml (`services/template_service.py:106-118`)
```python
def extract_credentials_from_template_yaml(file_path: Path) -> Dict:
    """Extract credentials section from template.yaml."""
    # Returns data.get("credentials", {})
```

### extract_credentials_from_env_example (`services/template_service.py:121-140`)
```python
def extract_credentials_from_env_example(file_path: Path) -> List[str]:
    """Extract variable names from .env.example."""
    # Parses KEY=value lines, returns list of uppercase variable names
```

### generate_credential_files (`services/template_service.py:228-299`)
```python
def generate_credential_files(
    template_data: dict,
    agent_credentials: dict,
    agent_name: str,
    template_base_path: Optional[Path] = None
) -> dict:
    """
    Generate credential files (.mcp.json, .env, config files) with real values.
    Returns dict of {filepath: content} to write into container.

    1. Generate .mcp.json with credentials (lines 241-276)
       - Replace ${VAR_NAME} with actual credential values
    2. Generate .env file (lines 278-285)
    3. Generate config files from templates (lines 287-297)
    """
```

### Trinity-Compatible Validation (`services/template_service.py:608-728`)
```python
def is_trinity_compatible(path: Path) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Check if a directory contains a Trinity-compatible agent.

    A Trinity-compatible agent must have:
    1. template.yaml file
    2. name field in template.yaml
    3. resources field in template.yaml
    4. a non-empty, UTF-8-readable CLAUDE.md (#950 — blocking, was a warning)
    """
```
See [local-agent-deploy.md](local-agent-deploy.md) for the deploy-time behavior
change and the companion `collect_mcp_credential_warnings()` advisory.

### get_name_from_template (`services/template_service.py:361-380`)
```python
def get_name_from_template(path: Path) -> Optional[str]:
    """Extract agent name from template.yaml."""
```

---

## Agent Container Initialization

### startup.sh (`docker/base-image/startup.sh`)

**GitHub Template - Git Sync Enabled** (lines 6-125):
```bash
if [ -n "${GITHUB_REPO}" ] && [ -n "${GITHUB_PAT}" ]; then
    CLONE_URL="https://oauth2:${GITHUB_PAT}@github.com/${GITHUB_REPO}.git"

    if [ "${GIT_SYNC_ENABLED}" = "true" ]; then
        # Check if repo already exists on persistent volume (lines 16-22)
        if [ -d "/home/developer/.git" ]; then
            echo "Repository already exists - skipping clone"
            git fetch origin
        else
            # Full clone with history for bidirectional sync (lines 24-89)
            git clone "${CLONE_URL}" /home/developer
            git config user.email "trinity-agent@ability.ai"
            git config user.name "Trinity Agent (${AGENT_NAME:-unknown})"

            # SOURCE MODE: Track source branch directly (lines 43-53)
            if [ "${GIT_SOURCE_MODE}" = "true" ]; then
                git checkout "${GIT_SOURCE_BRANCH:-main}"
            # WORKING BRANCH MODE: Create unique branch (lines 56-70)
            elif [ -n "${GIT_WORKING_BRANCH}" ]; then
                git checkout -b "${GIT_WORKING_BRANCH}"
            fi
        fi
    else
        # Shallow clone without .git for non-sync agents (lines 91-124)
        git clone --depth 1 "${CLONE_URL}" /tmp/repo-clone
        cp -r /tmp/repo-clone/* /home/developer/
        rm -rf /tmp/repo-clone
        touch /home/developer/.trinity-initialized
    fi
fi
```

**Local Template** (lines 127-157):
```bash
elif [ -n "${TEMPLATE_NAME}" ] && [ -d "/template" ]; then
    # Copy ALL template files to workspace
    cd /template
    for item in $(ls -A); do
        cp -r "${item}" /home/developer/
    done
    touch /home/developer/.trinity-initialized
fi
```

**Credential Files** (lines 164-222):
```bash
if [ -d "/generated-creds" ]; then
    # Copy .mcp.json with real credentials (lines 169-171)
    cp /generated-creds/.mcp.json . 2>/dev/null || true
    # Copy .env with real credentials (lines 175-177)
    cp /generated-creds/.env . 2>/dev/null || true
    # Copy other generated config files (lines 181-198)
    # Copy credential files (e.g., service account JSON) (lines 203-218)
fi
```

**Content Folder Convention** (lines 275-286):
```bash
# Create content/ directory for large generated assets
mkdir -p /home/developer/content/{videos,audio,images,exports}
# Add to .gitignore to prevent syncing large files
echo "content/" >> /home/developer/.gitignore
```

---

## Data Structures

### template.yaml Schema
```yaml
name: ruby-social-media-agent
display_name: "Ruby - Social Media Content Manager"
description: |
  Multi-platform content production agent...
version: "1.3"

resources:
  cpu: "2"
  memory: "4g"

# Multi-runtime support (optional)
runtime:
  type: "claude-code"  # or "gemini-cli"
  model: ""            # optional model override

# Shared folders (optional, Phase 9.11)
shared_folders:
  expose: true
  consume: false

credentials:
  mcp_servers:
    heygen:
      env_vars:
        - HEYGEN_API_KEY
    twitter-mcp:
      env_vars:
        - TWITTER_API_KEY
        - TWITTER_API_SECRET_KEY

  env_file:
    - BLOTATO_API_KEY
```

### .mcp.json.template
```json
{
  "mcpServers": {
    "heygen": {
      "command": "uvx",
      "args": ["heygen-mcp"],
      "env": {
        "HEYGEN_API_KEY": "${HEYGEN_API_KEY}"
      }
    }
  }
}
```

### GITHUB_TEMPLATES Definition (`config.py:91-164`)
```python
# GitHub PAT for template cloning (auto-uploaded to Redis on startup)
GITHUB_PAT = os.getenv("GITHUB_PAT", "")
GITHUB_PAT_CREDENTIAL_ID = "github-pat-templates"  # Fixed ID (line 55)

GITHUB_TEMPLATES = [
    {
        "id": "github:abilityai/agent-ruby",
        "display_name": "Ruby - Content & Publishing",
        "description": "Content creation and multi-platform social media distribution agent",
        "github_repo": "abilityai/agent-ruby",
        "github_credential_id": GITHUB_PAT_CREDENTIAL_ID,
        "source": "github",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [],
        "required_credentials": ["HEYGEN_API_KEY", "TWITTER_API_KEY", "CLOUDINARY_API_KEY"]
    },
    # ... more templates (cornelius, corbin, ruby multi-agent system)
]

# Combined templates list
ALL_GITHUB_TEMPLATES = GITHUB_TEMPLATES  # Line 164
```

### GitHub PAT Configuration

> **CRED-002 (2026-02-05)**: The Redis-based credential system has been removed.
> GitHub PAT is now stored in SQLite system_settings or read from environment variable.

The `get_github_pat()` function in `services/settings_service.py` retrieves the PAT:

```python
def get_github_pat() -> Optional[str]:
    """Get GitHub PAT from system settings or environment variable."""
    # First check SQLite system_settings table
    setting = db.get_setting_value("github_pat")
    if setting:
        return setting
    # Fall back to environment variable
    return os.environ.get("GITHUB_PAT")
```

**Configuration:**
1. **Option A**: Set `GITHUB_PAT` in `.env` file (environment variable)
2. **Option B**: Configure via Settings page in UI (saved to SQLite)
3. Settings page value takes precedence over environment variable

---

## Side Effects

### WebSocket Broadcast
```json
{
  "event": "agent_created",
  "data": {
    "name": "agent-name",
    "type": "business-assistant",
    "status": "running",
    "port": 2222,
    "created": "2026-01-23T10:00:00Z",
    "resources": {"cpu": "2", "memory": "4g"},
    "container_id": "abc123..."
  }
}
```

### Docker Labels
```python
labels={
    'trinity.platform': 'agent',
    'trinity.agent-name': config.name,
    'trinity.agent-type': config.type,
    'trinity.template': config.template or '',
    'trinity.agent-runtime': config.runtime or 'claude-code',
    # ... more labels
}
```

### Docker Volumes Created
- `agent-{name}-workspace` - Persistent workspace volume for `/home/developer`
- `agent-{name}-shared` - Shared folder volume (if expose enabled)

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Unknown GitHub template | 400 | "Unknown GitHub template" |
| GitHub PAT not found | 500 | "GitHub credential not found in credential store" |
| GitHub PAT secret missing | 500 | "GitHub credential secret not found" |
| GitHub PAT token missing | 500 | "GitHub PAT not found in credential" |
| Dynamic template no PAT | 500 | "GitHub PAT not configured. Set GITHUB_PAT in .env or add via Settings." |
| Template not found | 404 | "Template not found" |
| Template config not found | 404 | "Template configuration not found" |
| Agent already exists | 400 | "Agent already exists" |

---

## Security Considerations

1. **GitHub PAT Storage**: Stored in SQLite `system_settings` table (encrypted at rest via SQLite)
2. **PAT Retrieval**: `get_github_pat()` checks settings first, then env var
3. **PAT Injection**: Passed to agent container via `GITHUB_PAT` environment variable
4. **Shallow Clone**: `--depth 1` limits history exposure (when git sync disabled)
5. **Read-Only Mount**: Template volume mounted as `:ro`
6. **Never Logged**: PAT values are never written to logs or API responses
7. **Credential Files**: Written with 600 permissions in container

---

## Testing

### Manual Testing
```bash
# List all templates
curl http://localhost:8000/api/templates \
  -H "Authorization: Bearer $TOKEN"

# Get template details
curl http://localhost:8000/api/templates/local:ruby-social-media-agent \
  -H "Authorization: Bearer $TOKEN"

# Get .env template for bulk import
curl "http://localhost:8000/api/templates/env-template?template_id=github:abilityai/agent-ruby" \
  -H "Authorization: Bearer $TOKEN"

# Create agent from GitHub template
curl -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ruby-test",
    "template": "github:abilityai/agent-ruby"
  }'

# Create agent from local template
curl -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "local-test",
    "template": "local:ruby-social-media-agent"
  }'
```

---

## Fork-to-Own Creation (trinity-enterprise#93)

A GitHub template can declare `fork_to_own: required` in its `template.yaml`; `_build_template` surfaces the flag (plus `tagline`) on `GET /api/templates`, and creation from such a template copies the repo into a **user-owned** destination first. Cornelius is the first user; the mechanism is template-generic.

**Request**: `POST /api/agents` with `fork_to_own: {destination_repo: "owner/name", github_pat (SecretStr), private: true}`. Backend enforces the `required` flag (400 `FORK_TO_OWN_REQUIRED`), rejects `@branch` syntax and non-`github:` templates with the block, and pins `source_mode=True` + `source_branch=<template default branch>`.

**Copy pipeline** (`services/agent_service/fork_to_own.py`, runs in the `github:` branch of `create_agent_internal` BEFORE the docker try-block so structured `FORK_*` errors reach the UI):
1. `validate_token(user_pat)` → login; USER-owner mismatch → 400 `FORK_DESTINATION_FORBIDDEN`.
2. Destination state: missing → `create_repository(private=…)` (user PAT) + REST visibility poll (≤10s); exists+empty → reuse; exists holding exactly the template tip (single branch, head SHA match) → idempotent reuse, skip push; else → 409 `FORK_DESTINATION_EXISTS`.
3. Bare single-branch full-history clone of the template (read auth: platform PAT or none) staged under `/data/agent-fork-tmp` (backend `/tmp` is a 100 MB tmpfs), push to destination with the **user PAT via `GIT_CONFIG_*` env** (`http.extraHeader` — token never on argv/URL), `git ls-remote` poll (≤15s) so the agent's startup clone can't race an empty repo (#1439 class). All output passes `scrub_secret` (plain + b64 forms) before logging.

**Binding guard**: destination already referenced by any `agent_git_config` row → 409 `FORK_DESTINATION_IN_USE` (agent name disclosed only when the caller can access it, #186). Re-checked **after** `reserve_and_generate_instance_id` (source-mode rows bypass the partial UNIQUE index; the whole copy sits between check and act) — concurrent losers (all but the lexicographically-first name) roll back their row deterministically.

**Ownership wiring**: `github_repo_for_agent=destination`, `GITHUB_PAT`=user PAT (SecretStr unwrapped exactly once at the crud boundary); the PAT is persisted via `db.set_agent_github_pat` (#347, AES-256-GCM) INSIDE the docker try-block — failure rolls back the reserved row + MCP key, so a fork agent can never silently fall back to the platform PAT on recreate (`get_github_pat_for_agent` resolves per-agent first). `GIT_UPSTREAM_REPO=<template>` is baked; `startup.sh` adds a credential-less `upstream` remote in both the fresh-clone and restart paths, making `git pull upstream <branch>` the documented template-update path. Fork agents get `GIT_SYNC_AUTO=true` + the auto-sync DB flag even in source mode (pushing captures to your own main is the point).

**Frontend** (`CreateAgentModal.vue`): templates with `fork_to_own === 'required'` render as featured cards (tagline subtitle) above the standard list; selecting one reveals destination/PAT/visibility fields. Private is the default; Public sits behind a loud warning. PAT hint steers to a fine-grained single-repo token (the agent can read its own git credential — see `docs/security-reports/cso-2026-07-06.md`). The PAT ref is cleared after create.

**Known limitations**: `fork_to_own: required` enforcement is fail-open when the template.yaml metadata fetch fails (advisory gate; empty metadata → flag unseen for that 10-min cache window). MCP `create_agent` deliberately does NOT accept `fork_to_own` (tool args are audit-logged — a PAT arg would persist in plaintext). Repos pre-created WITH a README are non-empty → 409. Soft-deleted agents keep their destination binding until purge (blocking is intentional — admin recovery would resurrect it). Upstream template flag for `Abilityai/cornelius` ships separately in that repo.

Tests: `tests/unit/test_fork_to_own.py` (40 — model validation, orchestrator collision/scrub/timeout paths, crud gates, deep-slice env/PAT-persist/rollback, destination race, facade delegation, startup.sh static).

---

## Status
**Working** - Template processing fully functional for both GitHub and local templates

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-07-07 | **Catalog curation (#1513)**: `get_local_templates()` now excludes `hidden: true` fixtures (test/canary/demo/system) from the user-facing list while keeping them creatable by id; `_build_local_template`/`_build_template` surface a coerced-int `priority` so the router sort actually orders real starters first; fixed `scribe/template.yaml` (unquoted `usage:` broke YAML parse → silently dropped from catalog); `Templates.vue` renders a Starter (local) section matching CreateAgentModal. See "Catalog curation" note above. |
| 2026-07-06 | **Fork-to-own creation (trinity-enterprise#93)**: `fork_to_own: required` template flag → copy into user-owned repo (private by default) at creation; user PAT as per-agent git identity; upstream remote for template updates; featured create-modal cards. New section above. |
| 2026-02-05 | **CRED-002**: Removed Redis credential_manager references. GitHub PAT now retrieved via `get_github_pat()` from SQLite settings or env var. Removed `initialize_github_pat()` documentation. Updated Security Considerations. |
| 2026-01-23 | **Full verification**: Updated all line numbers for Templates.vue (16-24, 55-134, 137-216, 218-247, 262-267, 290-296, 299-302, 304-332), CreateAgentModal.vue (191-196, 198, 208-210, 219-230, 263-285), template_service.py (64-103, 106-118, 121-140, 143-225, 228-299, 309-358, 361-380), crud.py (96-144, 145-182), config.py (91-164), and startup.sh (6-125, 127-157, 164-222, 275-286). Added multi-runtime support and shared_folders template config. Updated credential file handling details. |
| 2025-12-30 | **Flow verification**: Updated line numbers for Templates.vue, CreateAgentModal.vue. Updated template processing to reference services/agent_service/create.py. Added startup.sh Git Sync details, content folder convention, Trinity-compatible validation. Updated config.py line numbers for GITHUB_TEMPLATES. |
| 2025-12-11 | **GitHub PAT Auto-Upload**: Added `GITHUB_PAT` env var support. Backend auto-uploads PAT to Redis on startup with fixed credential ID `github-pat-templates`. All templates now reference `GITHUB_PAT_CREDENTIAL_ID` constant. |
| 2025-12-07 | **Templates.vue rewrite**: Now dynamically fetches templates from `/api/templates` API instead of static hardcoded cards. Added GitHub/Local template sections with full metadata display. CreateAgentModal enhanced with `initialTemplate` prop and `created` event for navigation to new agent. |

---

## Related Flows

- **Upstream**: User authentication
- **Downstream**: Credential Injection (hot-reload), Agent Lifecycle (start after creation)
