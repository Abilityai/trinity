# Feature: Template Processing

## Overview
Template processing enables agent creation from pre-configured templates, supporting both local templates (in `config/agent-templates/`) and GitHub-based templates. The system extracts credential requirements, parses template.yaml metadata, processes .mcp.json templates, and initializes agent workspaces.

## User Story
As a platform user, I want to create agents from templates so that I can quickly deploy pre-configured agents with the correct MCP servers and credential requirements.

## Entry Points
- **UI**: `src/frontend/src/views/Library.vue` - Library page's Agent Templates section (primary; renamed from `Templates.vue` in trinity-enterprise#263 — see [library-page.md](library-page.md))
- **UI**: `src/frontend/src/components/CreateAgentModal.vue` - Create agent form with template selection
- **API**: `GET /api/templates` - List available templates
- **API**: `GET /api/templates/{template_id}` - Get template details
- **API**: `POST /api/agents` - Create agent with template

---

## Frontend Layer

### Library.vue (`src/frontend/src/views/Library.vue`)

The Library page's Agent Templates section (formerly the standalone `Templates.vue` page, ent#263) dynamically loads templates from the API (previously static hardcoded cards).

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

| Endpoint | Purpose |
|----------|---------|
| `GET /api/templates` | List all templates (GitHub + local) |
| `GET /api/templates/{template_id:path}` | Get template details |

(`GET /api/templates/env-template` no longer exists — removed alongside `POST /api/templates/refresh`; the router carries exactly these two GET routes.)

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
ahead of the rest. `Library.vue` (the Agent Templates section; renamed from
`Templates.vue` in ent#263) renders a **Starter Templates** (local) section in
addition to GitHub, so it shows the same curated set as `CreateAgentModal`.

**Since #1931 there is no "rest"**: the 11 `dd-*` VC-demo directories declare
`hidden: true`, so the visible local catalog is exactly those three starters and
the ordering clause guards nothing today. It is kept because it is the mechanism
a fourth starter would rely on; what is now enforced instead is that each starter
still declares `priority: 20`, and that every bundled directory declares `hidden:`
at all (see requirements/core-agent.md §4.1).

**Read-path containment (#1900).** `get_local_template("local:<name>")` — the
single-template resolver behind `GET /api/templates/{template_id:path}` — routes
`<name>` through the public `contained_template_dir(name, root)` before any
filesystem call. That is the same two-step barrier the create path has had since
#950 (name allowlist first, so CodeQL sees a `py/path-injection` barrier; then
`resolve()` on **both** sides plus `is_relative_to`, which is what catches the
sibling escape `<root>-evil` that a `str.startswith` check passes, and the
root-escaping symlink the allowlist cannot see). Previously the id was joined
onto the root unvalidated, and because `:path` captures `/`, `local:../<x>`,
`local:/<abs>/<x>` and a symlink each disclosed any parseable `template.yaml` on
the backend filesystem — including other tenants' uploads under
`/data/deployed-templates` — to any authenticated caller of any role. A rejected
id returns `None`, so the router's 404 is byte-identical to an unknown template
(no error code, no path, no root name): a distinguishable rejection would be a
new enumeration oracle, the thing #1759's single-sentence 404 exists to close.
The barrier reads the *name*, never the `hidden` flag, so the #1513 contract
above is preserved.

*Known, deliberate asymmetry:* `get_local_templates()` enumerates via
`iterdir()`, which follows symlinks, so a root-escaping symlink **planted inside
the root** could still be listed while detail and create both refuse it. The
listing is the outlier — create has rejected it since #950 — and planting one
requires local filesystem write access, not a request.

### Template Response Schema
```json
{
  "id": "github:abilityai/agent-ruby",
  "display_name": "Ruby - Content & Publishing",
  "source": "github",
  "resources": {"cpu": "2", "memory": "4g"},
  "mcp_servers": ["heygen", "twitter-mcp"],
  "required_credentials": ["HEYGEN_API_KEY", "TWITTER_API_KEY"],
  "credential_requirements": [
    {
      "name": "HEYGEN_API_KEY",
      "title": "HeyGen API key",
      "description": "Renders the avatar videos.",
      "required": true,
      "secret": true,
      "format": "secret",
      "setup_url": "https://app.heygen.com/settings/api",
      "default": null,
      "source": "template:mcp:heygen",
      "platform_injected": false
    }
  ],
  "credential_errors": []
}
```

**Two shapes, two owners — this is the reconciliation** (ent#128). The doc used to
show objects here and strings further down, which read as a contradiction:

| field | shape | owner |
|---|---|---|
| `required_credentials` (**catalog**) | list of **names** | `operator_supplied_credential_names()` — declared minus platform-injected. `Templates.vue` reads only `.length`, so this is the "N credentials" badge feed |
| `credential_requirements` (**catalog**) | list of **objects** | `normalize_credential_requirements()` — the ent#128 per-variable records |
| `required_credentials` (**extractor**) | list of `{name, source}` | `extract_agent_credentials()`, a repo-scanning helper with **no production caller**. Same key name, different function, different shape — do not conflate them |

`credential_errors` (trinity-enterprise#128) carries one named message per
structural problem in the template's `credentials:` block — empty on a healthy
template. See [Malformed `credentials:` resilience](#malformed-credentials-resilience-trinity-enterprise128).

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

### Local Templates (`services/agent_service/crud.py::_resolve_local_template`)

Two roots, tried in order (#950): the **curated catalog** and the **deploy-local
writable store**. Both go through `_safe_local_template_path`, which is the
CodeQL `py/path-injection` barrier (regex allowlist on the name, then
`.resolve()` + `is_relative_to(root)`), so every subsequent filesystem call on
the returned path is untainted.

```python
_LOCAL_TEMPLATE_ROOTS = (
    _curated_templates_root(),                  # /agent-configs/templates, or the
                                                # in-repo config/agent-templates
                                                # when that bind mount is absent
    Path("/data/deployed-templates").resolve(),  # deploy-local store (#950)
)

raw_name = config.template[6:]                   # strip "local:"
template_path = _resolve_local_template_dir(raw_name)   # the ladder, extracted (#1900)
template_yaml = template_path / "template.yaml"

# _resolve_local_template_dir is the SINGLE definition of "where does
# local:<name> live" — extracted in #1900 so the three seams below cannot drift:
def _resolve_local_template_dir(raw_name: str) -> Path:
    candidate = _safe_local_template_path(raw_name, _LOCAL_TEMPLATE_ROOTS[0])
    if not (candidate / "template.yaml").exists():
        candidate = _safe_local_template_path(raw_name, _LOCAL_TEMPLATE_ROOTS[1])
    return candidate

# The if/else shape is load-bearing — see "CodeQL" below. Do not flatten it.
if template_yaml.exists():
    try:
        with open(template_yaml) as f:
            template_data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        raise HTTPException(400, {"error": ..., "code": "LOCAL_TEMPLATE_INVALID"})   # #1759
    if not isinstance(template_data, dict):                                          # yaml.safe_load("") -> None
        raise HTTPException(400, {"error": ..., "code": "LOCAL_TEMPLATE_INVALID"})   # #1759

    # then mutate config from the template: type / resources / tools /
    # credentials.mcp_servers / runtime / shared_folders
else:
    raise HTTPException(404, {"error": ..., "code": "UNKNOWN_LOCAL_TEMPLATE"})       # #1793
```

**Failure contract (#1793 + #1759).** Before this, an unresolvable `local:` id
returned empty `template_data` and the agent was created anyway — HTTP 200,
blank container, no warning; only *malformed* names failed. The two halves were
filed and fixed separately: #1793 (PR #1803) closed the **absent** case, #1759
the **present-but-invalid** one. Now:

| Condition | Status | Code |
|---|---|---|
| Name fails the regex / traversal barrier | 400 | `INVALID_LOCAL_TEMPLATE_NAME` (checked first) |
| No `template.yaml` under either root | **404** | `UNKNOWN_LOCAL_TEMPLATE` (#1793) |
| `template.yaml` empty / not a mapping / unparseable | 400 | `LOCAL_TEMPLATE_INVALID` (#1759) |
| `template` is `null` or `""` (Blank Agent) | 200 | — never enters this branch |

Both raises sit in the same pre-side-effect band as `FORK_REQUIRES_GITHUB_TEMPLATE`
and the #843 reject: no container, MCP key, volume or slot has been allocated
yet, so there is nothing to roll back, and it is outside the caller's docker
try-block so the 4xx is not flattened to a 500. `create_agent_internal` gains
zero lines (#1484).

**CodeQL — do not flatten the `if/else` into a guard clause.** Dedenting the
load block moves the `.exists()` and `open()` expressions onto new lines and
re-fingerprints `py/path-injection` alerts already dismissed as false positives
on `dev`. #1793 hit this and reverted its own guard-clause refactor for it;
#1759 re-hit it at merge. New bands go **inside** the existing block.

**Disclosure rule:** one identical message whichever root missed, carrying no
filesystem path and no root name. Deploy-local templates are named after *agent*
names, so a root-distinguishing message would let a `creator`-role caller probe
another user's agents (#186 adjacency).

**Three seams read `_LOCAL_TEMPLATE_ROOTS`** and must stay in agreement: this
resolver, the `/template` bind-mount decision in `_stage_config_files` (curated
templates bind their host path read-only at `/template`; deploy-local ones do
not, because `deploy.py` already pre-populated the workspace volume via
`put_archive`), and — since #1900 — the **credential-file stager**, which used
to re-derive the directory from the template's own untrusted `name:` field
instead of reusing this one (see `generate_credential_files` below). #1759 named
the first two; extracting `_resolve_local_template_dir` makes the agreement
structural across all three rather than a convention. The bind source is
`os.getenv("HOST_TEMPLATES_PATH") or _default_host_templates_base()` — an
**empty** env value would otherwise make `Path("") / name` collapse to a bare
name, which Docker resolves as an empty *named volume* at `/template`.

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
    pattern = CREDENTIAL_DETECTOR_REF_RE  # \$\{([A-Za-z_][A-Za-z0-9_]*)\}
```

The pattern is **no longer uppercase-only** (ent#128). Trinity's substitution
engines impose no charset — `${my_var}` IS substituted at runtime — so an
uppercase-only *detector* read narrower than what it audits, which HARD-failed
K-001 on a template that documented every variable it referenced. The shared
constant and its NON-MEMBERS list (patterns that must NOT adopt it, notably the
fail-closed `mcp_validator._ENV_VAR_REF_RE`) live in
`services/credential_charset.py`.

### extract_env_vars_from_mcp_json (`services/template_service.py:64-103`)
```python
def extract_env_vars_from_mcp_json(file_path: Path) -> Dict[str, List[str]]:
    # Parse JSON and extract ${VAR_NAME} patterns from:
    # - env section of each MCP server config (lines 88-92)
    # - args array of each MCP server config (lines 94-98)
    pattern = CREDENTIAL_DETECTOR_REF_RE  # shared detector charset (ent#128)
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

### generate_credential_files (`services/template_service.py::generate_credential_files`)
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

    1. Generate .mcp.json with credentials — replace ${VAR_NAME} with real values
    2. Generate .env file
    3. Generate config files from templates
    """
```

**Where the `.mcp.json` template comes from (#1900).** `crud._stage_config_files`
passes `template_base_path` = the directory `_resolve_local_template_dir`
already validated, so the live path performs **no** derivation of its own.

Before #1900 this arm joined the template.yaml's own `name:` field onto a
hard-coded root and read the resulting `.mcp.json` **into the new agent's**
credential files. `name:` is untrusted — any `creator` supplies one via
`deploy_local_agent_logic` — so `name: ../../data/deployed-templates/<victim>`
read another tenant's credential-bearing `.mcp.json` into the attacker's own
agent. It was also wrong on its own terms: `name:` is a *display string* in 5
shipped templates ("Test Echo Agent"), not a directory name, so
`local:test-echo` looked for `<curated>/Test Echo Agent/.mcp.json`.

The residual `template_base_path is None` arm has no caller today (`github:`
templates never reach this function — their `template_data` stays `{}` and
`_stage_config_files` guards on it) but is kept fail-closed for future callers
of a public function: it resolves through `contained_template_dir`, which also
absorbs a non-string `name:` that previously raised `TypeError` out of agent
creation as an uncaught 500.

*Behaviour delta — measured, not the flattering version.* A **deploy-local**
template that both declares `credentials.mcp_servers` and ships a `.mcp.json`
now has that file staged at all, where the old curated-root lookup always
missed. Two consequences, and the second is a loss, not a gain:

* the staged file **wins** over the archive's raw copy, because `startup.sh`
  copies `/generated-creds/.mcp.json` unconditionally and *after* the
  template-copy block (which is gated on `.trinity-initialized`);
* the sole production caller — `crud._stage_config_files` — passes an **empty**
  `agent_credentials` map (CRED-002: real values are injected after creation,
  not at staging), so `agent_credentials.get(var_name, "")` rewrites every
  `${VAR}` to `""`. The agent's `.mcp.json` therefore lands with blank env
  values / blank `args` entries instead of the archive's placeholders.
  Hardcoded (non-`${VAR}`) entries are preserved verbatim. This mirrors what
  the `.env` arm of this same function has always done for a template
  declaring `credentials.env_file`; it is the platform's staging model, not a
  new one. The durable record of which variables a server needs is
  `.mcp.json.template` (compatibility check S-009), which is pre-populated
  untouched.

Do **not** restate this as "deploy-local templates finally get `${VAR}`
substitution" — nothing is substituted *in* at this seam. No curated template
ships a `.mcp.json`, so those rows are unchanged either way.
`tests/unit/test_ent128a_catalog_resilience.py::test_1900_staging_with_an_empty_credential_map_blanks_placeholders`
pins the measured behaviour against exactly that drift.

### Malformed `credentials:` resilience (trinity-enterprise#128)

A `template.yaml` is untrusted input — `github:` templates come from arbitrary
repos and `local:` ones can be uploaded by any authenticated user via
`deploy_local_agent_logic`. Every reader used to reach straight through the
block (`data.get("credentials", {}).get("mcp_servers", {}).keys()`), which made
one malformed template fatal to the whole catalog.

Four tolerant readers in `template_service.py` are now the only way in:

| Helper | Contract |
|---|---|
| `credential_shape_errors(block)` | Named errors for a malformed block. Never raises. Absent / null / `{}` = zero credentials, **not** an error |
| `credential_mcp_server_names(block)` | Server names, `[]` for any odd shape at either level |
| `credential_env_file_names(block)` | `env_file` variable names, non-empty strings only |
| `CredentialDeclarationError` | HTTP-free domain error (Invariant #1) raised by the **write** path only |

Two deliberately different contracts:

- **Read paths never raise.** `_build_local_template` / `_build_template` degrade
  the derived field to empty, attach `credential_errors` to the entry, and log
  exactly one WARNING naming the template id. The template **still lists** — a
  broken block costs that template its credential metadata, not the catalog.
  `get_local_templates` additionally fences each per-template build, so a future
  unguarded field can't regress the property. The `except` around the YAML parse
  is deliberately `Exception`, not `(OSError, yaml.YAMLError)`: deeply nested
  YAML raises `RecursionError`, a `RuntimeError` that escapes a `YAMLError`
  handler entirely.
- **The write path fails loud.** `generate_credential_files` validates first and
  raises `CredentialDeclarationError`; `crud._stage_config_files` maps it 1:1 to
  **400 `INVALID_CREDENTIAL_DECLARATION`**. Emitting an agent's `.env` from a
  declaration nobody could parse is the silent failure this closes — a string
  `env_file: "OPENAI_API_KEY"` (the list dash forgotten) used to be iterated
  character by character into fifteen single-letter variables, never writing the
  real credential, with no error, warning or crash.

`credentials.config_files[].path` is additionally rejected when absolute or
carrying a `..` segment (it becomes `open(cred_files_dir / path, "w")`, i.e. an
arbitrary-file-write primitive), and re-checked at the write sink by
`crud._safe_cred_file_path` → **400 `INVALID_CREDENTIAL_FILE_PATH`**
(validate-at-boundary **and** at-sink; the same resolve + `is_relative_to`
CodeQL barrier as `_safe_local_template_path`).

`credentials.env_file` stays a **names-only list**. The enriched per-variable
declaration (trinity-enterprise#128 PR-B) lands under its own top-level key
precisely so an older Trinity reading a newer template is structurally
untouched.

### Declared `schedules:` (trinity-enterprise#89)

The same tolerant-reader shape, applied to the second untrusted block Trinity
now acts on. `services/template_schedules.py` is a **leaf** (stdlib +
`services.schedule_validation`) with two public functions over one private
`_parse`, so the reported errors and the accepted entries cannot drift:

| Helper | Contract |
|---|---|
| `schedule_shape_errors(block)` | Named errors for a malformed block. Never raises. Absent / null / `[]` = no declared schedules, **not** an error |
| `normalize_declared_schedules(block)` | Well-formed entries only, each `{name, cron, message, enabled, timezone, description}` — type-checked, bounded, cron/timezone-validated, name-deduped, capped at `MAX_DECLARED_SCHEDULES` (20) |

**Totality is the contract, and three consumers depend on it.** A raise here
would (a) empty the catalog, (b) enter creation's destructive rollback fence, or
(c) fail-open compatibility check T-018. A 40-row matrix plus a Hypothesis
property over recursive JSON-ish values pin it
(`tests/unit/test_ent89_template_schedules.py`).

Three things differ from the `credentials:` readers, each deliberate:

- **The catalog surfaces the NORMALIZED list**, not the raw block (unlike
  `data_paths`, which is surfaced raw). Normalizing at the builder is what makes
  it safe for the frontend to render, and it matches
  `credential_mcp_server_names`.
- **Both GitHub list paths are now fenced.** ent#128 PR-A fenced
  `_build_local_template` only; `get_all_templates`' two GitHub call sites were
  bare list comprehensions. Adding a new untrusted-input reader inside
  `_build_template` would have put a raise-capable call on an unfenced path —
  re-opening the exact bug (#1835) this convention exists to prevent. Both now
  route through `_safe_build_github_template` (log-and-skip per template).
- **Creation does not read the catalog's copy.** `_get_cached_metadata` fetches
  with the **global platform** PAT, off the **default branch**, through a
  10-minute per-process cache. Creation resolves its PAT differently (per-agent
  → per-user → global, ent#162) and may target `@branch`, so
  `fetch_template_metadata_for_create(repo, pat, ref)` does a fresh, pinned,
  authenticated read — and logs a WARNING naming the repo and reason on any
  failure. A silently empty declaration on the `github:` path is precisely the
  class this feature exists to close, and the catalog dict would have
  reintroduced it one layer up.

Errors carry the **entry index, the key, and a YAML type name** — never the
`name`/`message`/`description` *value*, since the list is persisted into
`agent_compatibility_results.checks_json`, rendered in the UI, and returned in
the catalog response. The cron and timezone strings are the one echo, bounded
and printable-filtered by a local twin of `_sanitize_for_warning` (the leaf
cannot import it back without closing a cycle; the two are comment-linked).

Consumption at creation is in
[scheduling.md](scheduling.md#template-declared-schedules-at-creation-trinity-enterprise89);
the compatibility check is T-018 in
[agent-compatibility-validation.md](agent-compatibility-validation.md).

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
        # ^ the CATALOG shape: a list of names (the badge feed). The objects shape
        #   belongs to `credential_requirements` / the caller-less extractor — see
        #   "Two shapes, two owners" above (ent#128).
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
1. `validate_destination_pat(user_pat)` → login; USER-owner mismatch → 400 `FORK_DESTINATION_FORBIDDEN`. Called **before** `_resolve_template_tip` so a bad PAT reports `FORK_PAT_INVALID` even when the template is also unreachable.
2. Destination state via `inspect_or_create_destination_repo()` → `created | empty | branches`: missing → `create_repository(private=…)` (user PAT) + REST visibility poll (≤10s); exists+empty → reuse. The **reuse/refuse policy stays in this caller** — exists holding exactly the template tip (single branch, head SHA match) → idempotent reuse, skip push; else → 409 `FORK_DESTINATION_EXISTS`.
   > **ent#109**: steps 1–2 are now the *shared* half, reused verbatim by the post-creation rebind ([agent-repo-binding.md](agent-repo-binding.md)). The seam is deliberately one level below the triage: the reuse branch **is** the template-tip SHA comparison, which is meaningless to a caller whose content source is an agent's workspace volume — so the primitive **reports** `created|empty|branches` and each caller decides. Behaviour here is unchanged and pinned by the 40 tests below.
3. Bare single-branch full-history clone of the template (read auth: platform PAT or none) staged under `/data/agent-fork-tmp` (backend `/tmp` is a 100 MB tmpfs), push to destination with the **user PAT via `GIT_CONFIG_*` env** (`http.extraHeader` — token never on argv/URL), `git ls-remote` poll (≤15s) so the agent's startup clone can't race an empty repo (#1439 class). All output passes `scrub_secret` (plain + b64 forms) before logging.

**Binding guard**: destination already referenced by any `agent_git_config` row → 409 `FORK_DESTINATION_IN_USE` (agent name disclosed only when the caller can access it, #186). Re-checked **after** `reserve_and_generate_instance_id` (source-mode rows bypass the partial UNIQUE index; the whole copy sits between check and act) — concurrent losers (all but the lexicographically-first name) roll back their row deterministically.

**Ownership wiring**: `github_repo_for_agent=destination`, `GITHUB_PAT`=user PAT (SecretStr unwrapped exactly once at the crud boundary); the PAT is persisted via `db.set_agent_github_pat` (#347, AES-256-GCM) INSIDE the docker try-block — failure rolls back the reserved row + MCP key, so a fork agent can never silently fall back to the platform PAT on recreate (`get_github_pat_for_agent` resolves per-agent first). `GIT_UPSTREAM_REPO=<template>` is baked; `startup.sh` adds a credential-less `upstream` remote in both the fresh-clone and restart paths, making `git pull upstream <branch>` the documented template-update path. Fork agents get `GIT_SYNC_AUTO=true` + the auto-sync DB flag even in source mode (pushing captures to your own main is the point).

**Frontend** (`CreateAgentModal.vue`): templates with `fork_to_own === 'required'` render as featured cards (tagline subtitle) above the standard list; selecting one reveals destination/PAT/visibility fields. Private is the default; Public sits behind a loud warning. PAT hint steers to a fine-grained single-repo token (the agent can read its own git credential — see `docs/security-reports/cso-2026-07-06.md`). The PAT ref is cleared after create.

**Enforcement now fails CLOSED (trinity-enterprise#14).** This line previously read *"`fork_to_own: required` enforcement is fail-open when the template.yaml metadata fetch fails (advisory gate; empty metadata → flag unseen for that 10-min cache window)"* — an accurate description of a real defect. `_fetch_template_yaml_result` returns `({}, "HTTP 403")` on a rate limit, the catalog wrapper discarded the reason, `_build_template` emitted `fork_to_own: None`, and `crud._apply_fork_to_own`'s `== "required"` test never fired — so the agent was created bound to the **shared upstream template repo** instead of a user-owned copy, silently, for the whole cache window. The reason is now cached beside the metadata and surfaced as `metadata_unavailable`, and the non-forking creation path **refuses** (503 `TEMPLATE_METADATA_UNAVAILABLE`, retryable, naming the platform-PAT remedy) rather than treating unreadable as absent. A clean HTTP **404 stays "absent"**, so a repo that ships no `template.yaml` creates as before; a caller who IS forking is not blocked, since they get a user-owned repo whatever the template declares. Trade-off, stated: creation now depends on GitHub API reachability where it previously depended only on `git clone`. See [template-registry.md](template-registry.md), which is what made this reachable in practice.

**Other known limitations**: MCP `create_agent` deliberately does NOT accept `fork_to_own` (tool args are audit-logged — a PAT arg would persist in plaintext). Repos pre-created WITH a README are non-empty → 409. Soft-deleted agents keep their destination binding until purge (blocking is intentional — admin recovery would resurrect it). Upstream template flag for `Abilityai/cornelius` ships separately in that repo. This is the **create-time** path only; retrofitting an already-created agent onto a user-owned repo is [agent-repo-binding.md](agent-repo-binding.md) (ent#109).

Tests: `tests/unit/test_fork_to_own.py` (40 — model validation, orchestrator collision/scrub/timeout paths, crud gates, deep-slice env/PAT-persist/rollback, destination race, facade delegation, startup.sh static); `tests/unit/test_ent109_fork_to_own_extraction.py` (13 — the shared primitive's three states + error registry, and the validate-before-template ordering the split must preserve).

---

## Status
**Working** - Template processing fully functional for both GitHub and local templates

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-07-07 | **Catalog curation (#1513)**: `get_local_templates()` now excludes `hidden: true` fixtures (test/canary/demo/system) from the user-facing list while keeping them creatable by id; `_build_local_template`/`_build_template` surface a coerced-int `priority` so the router sort actually orders real starters first; fixed `scribe/template.yaml` (unquoted `usage:` broke YAML parse → silently dropped from catalog); `Templates.vue` renders a Starter (local) section matching CreateAgentModal. See "Catalog curation" note above. |
| 2026-08-04 | **ent#14 — `fork_to_own` enforcement fails closed**: the documented "advisory gate / fail-open" limitation is fixed, not restated. The per-repo fetch REASON is carried through the metadata cache as `metadata_unavailable`; an unreadable `template.yaml` refuses creation (503) instead of reading as "declares nothing". A clean 404 still means absent. Also: the GitHub half of the catalog now resolves through the remote template registry — see [template-registry.md](template-registry.md). |
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
- **Per-agent runtime companion**: [guided-credential-setup.md](guided-credential-setup.md) —
  this flow is catalog/template-time (what a template *declares*); ent#127 is the
  per-agent runtime half (what a **deployed** agent declares and which of those
  variables are actually set in its `.env`).
