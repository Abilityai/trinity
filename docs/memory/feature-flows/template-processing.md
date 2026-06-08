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

## Status
**Working** - Template processing fully functional for both GitHub and local templates

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-05 | **CRED-002**: Removed Redis credential_manager references. GitHub PAT now retrieved via `get_github_pat()` from SQLite settings or env var. Removed `initialize_github_pat()` documentation. Updated Security Considerations. |
| 2026-01-23 | **Full verification**: Updated all line numbers for Templates.vue (16-24, 55-134, 137-216, 218-247, 262-267, 290-296, 299-302, 304-332), CreateAgentModal.vue (191-196, 198, 208-210, 219-230, 263-285), template_service.py (64-103, 106-118, 121-140, 143-225, 228-299, 309-358, 361-380), crud.py (96-144, 145-182), config.py (91-164), and startup.sh (6-125, 127-157, 164-222, 275-286). Added multi-runtime support and shared_folders template config. Updated credential file handling details. |
| 2025-12-30 | **Flow verification**: Updated line numbers for Templates.vue, CreateAgentModal.vue. Updated template processing to reference services/agent_service/create.py. Added startup.sh Git Sync details, content folder convention, Trinity-compatible validation. Updated config.py line numbers for GITHUB_TEMPLATES. |
| 2025-12-11 | **GitHub PAT Auto-Upload**: Added `GITHUB_PAT` env var support. Backend auto-uploads PAT to Redis on startup with fixed credential ID `github-pat-templates`. All templates now reference `GITHUB_PAT_CREDENTIAL_ID` constant. |
| 2025-12-07 | **Templates.vue rewrite**: Now dynamically fetches templates from `/api/templates` API instead of static hardcoded cards. Added GitHub/Local template sections with full metadata display. CreateAgentModal enhanced with `initialTemplate` prop and `created` event for navigation to new agent. |

---

## Related Flows

- **Upstream**: User authentication
- **Downstream**: Credential Injection (hot-reload), Agent Lifecycle (start after creation)
