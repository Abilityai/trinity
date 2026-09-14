# Feature: Skills Library Sync

## Overview
Synchronizes a skills library from a GitHub repository to the local filesystem using git clone/pull operations. Enables platform administrators to maintain a centralized collection of reusable agent skills that can be assigned to agents.

## User Story
As a platform administrator, I want to configure and sync a GitHub repository containing skill definitions so that I can manage a centralized skills library and assign skills to agents.

---

## Entry Points

| UI Location | API Endpoint | Purpose |
|-------------|--------------|---------|
| `src/frontend/src/views/Settings.vue:432-527` | `GET /api/skills/library/status` | Check library sync status |
| `src/frontend/src/views/Settings.vue:501-512` | `POST /api/skills/library/sync` | Trigger library sync |
| `src/frontend/src/views/Settings.vue:514-524` | `PUT /api/settings/{key}` | Save URL/branch settings |
| Settings → Skills Library → Automation | `GET/PUT /api/settings/skills-library` | Auto-sync + fleet re-inject config, sync status, last fleet report (ent#236) |
| — (background) | `services/skills_sync_service.py` | Scheduled sync loop (ent#236) |

---

## Scheduled Auto-Sync + Fleet Re-Inject (ent#236)

Both default OFF; a zero-config install behaves exactly as before.

```
skills_sync_service loop (every worker, self-gating, config re-read per cycle)
   │  skills:sync:leader  (SET NX, TTL 3×interval, own-lease refresh, fail-open)
   ▼  leader only
sync_library()  ── failure ─▶ persist last_status/last_error → Settings panel
   │                            (no operator alarm: a repeating item for an
   │                             unreachable GitHub is the muted-alert failure mode)
   ├─ commit unchanged ─▶ stop.   A no-op pull must not sweep the fleet.
   └─ commit changed + auto_reinject_enabled
          ▼
      run_fleet_reinject: running, non-ghost agents · force=False (tree-SHA skip
      makes unchanged skills free) · Semaphore(SKILLS_FLEET_INJECT_CONCURRENCY=5)
      · SkillInjectionBusy ⇒ skip-and-report, never wait
          ▼
      report → system_settings['skills_fleet_reinject_last_run'] (Settings panel)
      + operator alarm on `_skills-sync` iff ≥1 agent failed
```

**Why the backend, not the standalone scheduler.** The scheduler container is on
`trinity-platform` only and deliberately never talks to agents; the fleet sweep
must. Splitting the timer from the sweep across two processes buys nothing, so
both live in the backend behind a Redis leader lease.

**Durable status.** `_last_sync` / `_last_commit_sha` are per-process fields and
the backend runs `--workers 2`, so the worker answering `/status` was usually not
the worker that synced — a stale timestamp, and a sync error that could never be
displayed at all. Both are now mirrored into `system_settings`
(`skills_library_last_sync` / `_last_status` / `_last_error` / `_last_commit`),
written on **both** the success and failure branches. `_last_commit` is also what
"did the commit change?" compares against: reading the in-memory field would make
every backend restart look like a change and sweep the whole fleet.

**Manual sync** (`POST /api/skills/library/sync`) spawns the same sweep in the
background when the flag is on and the commit moved — the operator clicked "Sync
Library", not "block until every agent is updated"; the sweep's own report is the
honest surface.

---

## Frontend Layer

### Component (`src/frontend/src/views/Settings.vue`)

**Skills Library Section (lines 432-527)**

The Settings page includes a "Skills Library" section with:
1. Repository URL input field (line 448-459)
2. Branch input field (line 462-476)
3. Status display showing skill count, commit SHA, last sync time (lines 479-496)
4. "Sync Library" and "Save Settings" buttons (lines 499-525)

```vue
<!-- Repository URL Input (line 448-459) -->
<input
  type="text"
  id="skills-library-url"
  v-model="skillsLibraryUrl"
  placeholder="github.com/owner/skills-library"
  class="..."
/>

<!-- Branch Input (line 468-475) -->
<input
  type="text"
  id="skills-library-branch"
  v-model="skillsLibraryBranch"
  placeholder="main"
  class="..."
/>

<!-- Status Display (line 479-496) -->
<div v-if="skillsLibraryStatus.cloned" class="...">
  <span>{{ skillsLibraryStatus.skill_count }} skills available</span>
  <span v-if="skillsLibraryStatus.commit_sha">
    Commit: <code>{{ skillsLibraryStatus.commit_sha }}</code>
  </span>
  <span v-if="skillsLibraryStatus.last_sync">
    Last synced: {{ formatDate(skillsLibraryStatus.last_sync) }}
  </span>
</div>
```

### State Management (`src/frontend/src/views/Settings.vue:642-653`)

```javascript
// Skills Library state
const skillsLibraryUrl = ref('')
const skillsLibraryBranch = ref('main')
const skillsLibraryStatus = ref({
  configured: false,
  cloned: false,
  skill_count: 0,
  commit_sha: null,
  last_sync: null
})
const syncingSkillsLibrary = ref(false)
const savingSkillsLibrary = ref(false)
```

### Load Settings (`src/frontend/src/views/Settings.vue:968-979`)

```javascript
async function loadSkillsLibrarySettings() {
  try {
    const response = await axios.get('/api/skills/library/status', {
      headers: authStore.authHeader
    })
    skillsLibraryStatus.value = response.data
    skillsLibraryUrl.value = response.data.url || ''
    skillsLibraryBranch.value = response.data.branch || 'main'
  } catch (e) {
    console.error('Failed to load skills library status:', e)
  }
}
```

### Save Settings (`src/frontend/src/views/Settings.vue:981-1009`)

```javascript
async function saveSkillsLibrarySettings() {
  savingSkillsLibrary.value = true
  error.value = null

  try {
    // Save URL setting
    if (skillsLibraryUrl.value.trim()) {
      await settingsStore.updateSetting('skills_library_url', skillsLibraryUrl.value.trim())
    } else {
      await settingsStore.deleteSetting('skills_library_url')
    }

    // Save branch setting
    if (skillsLibraryBranch.value.trim() && skillsLibraryBranch.value !== 'main') {
      await settingsStore.updateSetting('skills_library_branch', skillsLibraryBranch.value.trim())
    } else {
      await settingsStore.deleteSetting('skills_library_branch')
    }

    showSuccess.value = true
    setTimeout(() => { showSuccess.value = false }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to save skills library settings'
  } finally {
    savingSkillsLibrary.value = false
  }
}
```

### Sync Library (`src/frontend/src/views/Settings.vue:1011-1036`)

```javascript
async function syncSkillsLibrary() {
  syncingSkillsLibrary.value = true
  error.value = null

  try {
    // Save settings first
    await saveSkillsLibrarySettings()

    // Then sync
    const response = await axios.post('/api/skills/library/sync', {}, {
      headers: authStore.authHeader
    })

    // Reload status
    await loadSkillsLibrarySettings()

    showSuccess.value = true
    setTimeout(() => { showSuccess.value = false }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to sync skills library'
  } finally {
    syncingSkillsLibrary.value = false
  }
}
```

### Store (`src/frontend/src/stores/settings.js:66-80`)

```javascript
async updateSetting(key, value) {
  this.saving = true
  this.error = null

  try {
    const response = await axios.put(`/api/settings/${key}`, { value })
    this.settings[key] = response.data.value
    return response.data
  } catch (error) {
    console.error(`Failed to update setting ${key}:`, error)
    this.error = error.response?.data?.detail || 'Failed to update setting'
    throw error
  } finally {
    this.saving = false
  }
}
```

---

## Backend Layer

### Router (`src/backend/routers/skills.py`)

**Status Endpoint (lines 49-57)**

```python
@router.get("/skills/library/status", response_model=SkillsLibraryStatus)
async def get_library_status(current_user: User = Depends(get_current_user)):
    """
    Get the current status of the skills library.

    Returns configuration status, sync info, and skill count.
    """
    return skill_service.get_library_status()
```

> **`response_model` here is a security boundary, not documentation (ent#334).**
> The route stays open to every authenticated caller — the per-agent Skills tab
> and the MCP `get_skills_library_status` tool both read it — but the same
> service dict is *also* served by `GET /skills/sources`, which is
> `require_admin` + `reject_agent_principal` precisely because source repo URLs
> are sensitive. Returning the dict raw handed the admin-gated value to the
> callers that gate excludes. `SkillsLibraryStatus` is an allow-list: the flat
> `url`, the per-source `url`, and per-source `last_error` (git failure text
> echoes the PAT-spliced clone URL — ent#347) are withheld; everything the
> frontend actually derives state from is kept. Widening it re-opens the leak.

**Sync Endpoint (lines 73-87)**

```python
@router.post("/skills/library/sync")
async def sync_library(admin_user: User = Depends(require_admin)):
    """
    Sync the skills library from GitHub.

    Admin-only. Clones or pulls the configured repository.
    """
    result = skill_service.sync_library()
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Sync failed")
        )
    return result
```

### Service (`src/backend/services/skill_service.py`)

**Sync Library Method (lines 51-114)**

```python
def sync_library(self) -> Dict[str, Any]:
    """
    Sync the skills library from GitHub.

    Clones the repository if it doesn't exist, or pulls latest changes.
    Uses GitHub PAT for private repository access.

    Returns:
        Dict with sync status, commit info, and skill count
    """
    url = get_skills_library_url()
    if not url:
        return {
            "success": False,
            "error": "Skills library URL not configured",
            "hint": "Configure skills_library_url in Settings"
        }

    branch = get_skills_library_branch()
    github_pat = get_github_pat()

    # Construct authenticated URL for private repos
    if github_pat and "github.com" in url:
        # Handle various URL formats
        if url.startswith("https://"):
            auth_url = url.replace("https://", f"https://{github_pat}@")
        elif url.startswith("github.com"):
            auth_url = f"https://{github_pat}@{url}"
        else:
            auth_url = f"https://{github_pat}@github.com/{url}"
    else:
        # Public repo or no PAT
        if not url.startswith("https://"):
            auth_url = f"https://github.com/{url}"
        else:
            auth_url = url

    # Log without exposing PAT
    safe_url = re.sub(r'https://[^@]+@', 'https://***@', auth_url)
    logger.info(f"Syncing skills library from {safe_url} (branch: {branch})")

    try:
        if self.library_path.exists():
            # Pull latest changes
            result = self._git_pull(branch)
        else:
            # Clone repository
            result = self._git_clone(auth_url, branch)

        if result["success"]:
            self._last_sync = datetime.utcnow()
            self._last_commit_sha = self._get_current_commit()
            result["commit_sha"] = self._last_commit_sha
            result["skill_count"] = len(self.list_skills())
            result["last_sync"] = self._last_sync.isoformat()

        return result

    except Exception as e:
        logger.error(f"Failed to sync skills library: {e}")
        return {
            "success": False,
            "error": str(e)
        }
```

**Git Clone Method (lines 116-132)**

```python
def _git_clone(self, url: str, branch: str) -> Dict[str, Any]:
    """Clone the skills library repository."""
    self.library_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--branch", branch, "--depth", "1", url, str(self.library_path)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        logger.info(f"Cloned skills library to {self.library_path}")
        return {"success": True, "action": "cloned"}
    except subprocess.CalledProcessError as e:
        # Sanitize error output to remove PAT
        error_msg = re.sub(r'https://[^@]+@', 'https://***@', e.stderr or str(e))
        logger.error(f"Git clone failed: {error_msg}")
        return {"success": False, "error": f"Clone failed: {error_msg}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Clone timed out after 120 seconds"}
```

**Git Pull Method (lines 134-161)**

```python
def _git_pull(self, branch: str) -> Dict[str, Any]:
    """Pull latest changes from the skills library."""
    try:
        # Fetch and reset to remote branch
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=self.library_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=self.library_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        logger.info(f"Pulled latest skills library changes")
        return {"success": True, "action": "pulled"}
    except subprocess.CalledProcessError as e:
        error_msg = re.sub(r'https://[^@]+@', 'https://***@', e.stderr or str(e))
        logger.error(f"Git pull failed: {error_msg}")
        return {"success": False, "error": f"Pull failed: {error_msg}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Pull timed out"}
```

**Get Library Status (lines 271-294)**

> **Stale snippet — pre-ent#237.** The block below shows the single-library
> builder (one `url`, `self.library_path`). ent#237 replaced it with the
> multi-source version that iterates `skill_sources` and emits a `sources[]`
> array, and ent#334 wrapped both URL emitters in `strip_url_credentials`.
> Read `skill_service.get_library_status()` for current truth; this is kept
> only for the shape of the pre-multi-source flow. Rewriting it belongs with
> the ent#237 doc sync, not with a security fix.

```python
def get_library_status(self) -> Dict[str, Any]:
    """
    Get the current status of the skills library.

    Returns:
        Dict with configuration status, sync info, and skill count
    """
    url = get_skills_library_url()
    branch = get_skills_library_branch()

    status = {
        "configured": bool(url),
        "url": url,
        "branch": branch,
        "cloned": self.library_path.exists(),
        "last_sync": self._last_sync.isoformat() if self._last_sync else None,
        "commit_sha": self._last_commit_sha or self._get_current_commit(),
        "skill_count": 0
    }

    if self.library_path.exists():
        status["skill_count"] = len(self.list_skills())

    return status
```

### Settings Service (`src/backend/services/settings_service.py`)

**Skills Library Settings (lines 141-163)**

```python
def get_skills_library_url() -> Optional[str]:
    """
    Get the skills library GitHub repository URL.

    Returns None if not configured (feature disabled).

    Example: "github.com/Abilityai/skills-library-41"
    """
    return settings_service.get_setting('skills_library_url')


def get_skills_library_branch() -> str:
    """
    Get the skills library branch to use.

    Default: "main"
    """
    return settings_service.get_setting('skills_library_branch', 'main')
```

**GitHub PAT Retrieval (lines 71-76)**

```python
def get_github_pat(self) -> str:
    """Get GitHub PAT from settings, fallback to env var."""
    key = self.get_setting('github_pat')
    if key:
        return key
    return os.getenv('GITHUB_PAT', '')
```

---

## Data Flow

### 1. User Configures Settings
```
User enters URL/branch in Settings.vue
    -> saveSkillsLibrarySettings()
    -> PUT /api/settings/skills_library_url
    -> PUT /api/settings/skills_library_branch
    -> system_settings table updated
```

### 2. User Clicks Sync
```
User clicks "Sync Library" button
    -> syncSkillsLibrary()
    -> POST /api/skills/library/sync (admin-only)
    -> skill_service.sync_library()
    -> get_skills_library_url() (from settings_service)
    -> get_skills_library_branch() (from settings_service)
    -> get_github_pat() (for private repos)
    -> _git_clone() or _git_pull()
    -> _get_current_commit()
    -> list_skills()
    -> Return sync result
```

### 3. Status Display
```
Page loads or sync completes
    -> loadSkillsLibrarySettings()
    -> GET /api/skills/library/status
    -> skill_service.get_library_status()
    -> UI updates with skill count, commit SHA, last sync time
```

---

## File System Structure

### Local Storage Path

```
/data/skills-library/           <- SKILLS_LIBRARY_PATH constant
├── .git/                       <- Git repository data (HEAD is the injection
│                                  source: `git archive` + per-skill tree SHAs,
│                                  ent#183 — so injection is atomic vs a
│                                  concurrent `git reset --hard` here)
├── .claude/
│   └── skills/
│       ├── skill-name-1/
│       │   ├── SKILL.md        <- Skill definition + frontmatter contract
│       │   ├── scripts/        <- Full directory ships at injection (ent#183)
│       │   └── ...resources
│       ├── skill-name-2/
│       │   └── SKILL.md
│       └── ...
└── README.md                   <- Optional repository docs
```

Since ent#183, `list_skills`/`get_skill` also parse the frontmatter **contract**
(`automation`, `user_invocable`, `requires: {packages, binaries, env}`,
`allowed-tools`) plus package metadata (`multi_file`, `file_count`,
`size_bytes`, `version` = git tree SHA); `get_skill` adds a `files` list. The
list metadata is cached per library commit SHA and invalidated by
`sync_library()`. `GET /api/skills/library/status` additionally reports
`multi_file_count`. See [skill-injection.md](skill-injection.md) for the
contract details and injection pipeline.

### Skills Discovery (`src/backend/services/skill_service.py:182-205`)

```python
def list_skills(self) -> List[Dict[str, Any]]:
    """
    List all available skills from the library.

    Scans .claude/skills/*/SKILL.md files.
    """
    skills = []
    skills_dir = self.library_path / ".claude" / "skills"

    if not skills_dir.exists():
        logger.debug(f"Skills directory not found: {skills_dir}")
        return skills

    for skill_path in skills_dir.iterdir():
        if skill_path.is_dir():
            skill_file = skill_path / "SKILL.md"
            if skill_file.exists():
                skill_info = self._parse_skill_info(skill_path.name, skill_file)
                skills.append(skill_info)

    return sorted(skills, key=lambda s: s["name"])
```

---

## Database Schema

### system_settings Table

```sql
-- Settings stored via PUT /api/settings/{key}
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)

-- Relevant keys:
-- 'skills_library_url'    -> "github.com/owner/repo"
-- 'skills_library_branch' -> "main"
-- 'github_pat'            -> "ghp_..." (for private repos)
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| URL not configured | 400 | "Skills library URL not configured" |
| Invalid URL (SSRF) | 400 | "Skills library URL must point to github.com" |
| Non-HTTPS URL | 400 | "Skills library URL must use HTTPS" |
| URL resolves to internal IP | 400 | "Skills library URL resolved to internal address" |
| Clone failed | 400 | "Clone failed: {error}" |
| Pull failed | 400 | "Pull failed: {error}" |
| Clone timeout | 400 | "Clone timed out after 120 seconds" |
| Pull timeout | 400 | "Pull timed out" |
| Non-admin user | 403 | "Admin access required" |
| Invalid branch | 400 | Git error message (branch not found) |

---

## Security Considerations

1. **Admin-Only Sync**: The `POST /api/skills/library/sync` endpoint requires `require_admin` dependency
2. **PAT Protection**: GitHub PAT is never logged - sanitized with `re.sub(r'https://[^@]+@', 'https://***@', ...)`
3. **Shallow Clone**: Uses `--depth 1` to minimize data transfer and avoid cloning full history
4. **Timeout Protection**: Clone has 120s timeout, pull has 60s timeout to prevent hanging
5. **Command Injection**: Uses subprocess with list arguments, not shell=True
6. **SSRF Prevention (SEC-179)**: URL validated at two layers to prevent Server-Side Request Forgery:
   - **Write-time** (`routers/settings.py`): When `skills_library_url` is saved via `PUT /api/settings/skills_library_url`, `validate_skills_library_url()` rejects non-github.com URLs with HTTP 400
   - **Sync-time** (`services/skill_service.py`): Before git operations, URL is validated again as defense-in-depth
   - **Validation rules** (`utils/url_validation.py`): HTTPS required, hostname must be exactly `github.com` or `www.github.com`, DNS resolution checked against private/loopback/reserved IP ranges, non-http schemes rejected

---

## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost:3000
- [ ] Logged in as admin user
- [ ] GitHub repository with `.claude/skills/*/SKILL.md` structure

### Test Steps

#### 1. Configure Skills Library
**Action**:
- Navigate to http://localhost/settings
- Scroll to "Skills Library" section
- Enter repository URL: `github.com/your-org/skills-library`
- Enter branch: `main`
- Click "Save Settings"

**Verify**:
- [ ] Success message appears
- [ ] GET `/api/settings/skills_library_url` returns the URL
- [ ] GET `/api/settings/skills_library_branch` returns "main"

#### 2. Initial Sync (Clone)
**Action**:
- Click "Sync Library" button
- Wait for sync to complete

**Verify**:
- [ ] Loading spinner shows during sync
- [ ] Success message appears
- [ ] Status shows skill count > 0
- [ ] Status shows commit SHA (12 characters)
- [ ] Status shows "Last synced: Today" or similar
- [ ] `/data/skills-library/.claude/skills/` contains SKILL.md files

#### 3. Subsequent Sync (Pull)
**Action**:
- Click "Sync Library" again

**Verify**:
- [ ] Sync completes successfully
- [ ] Uses git pull (not clone)
- [ ] Skill count updates if repository changed
- [ ] Commit SHA updates if new commits
- [ ] A **tag-pinned** source (the bundled community catalog, `ref_type: tag`) reports `action: pinned` with the **same** commit SHA and no `moved_tag` — including for an *annotated* tag, which every `trinity-skills` release tag is (#2550: the update path compares the tag peeled to its commit; a bare rev-parse of an annotated tag is the tag object, and read an unmoved tag as moved)

#### 4. Private Repository
**Action**:
- Configure GitHub PAT in Settings -> API Keys
- Configure a private repository URL
- Click "Sync Library"

**Verify**:
- [ ] Clone succeeds with authentication
- [ ] PAT is NOT visible in logs or error messages
- [ ] Skills are loaded from private repo

#### 5. Error Cases
**Action**: Test various error scenarios

**Verify**:
- [ ] No URL configured -> "Skills library URL not configured"
- [ ] Invalid URL -> "Clone failed: ..." with sanitized error
- [ ] Invalid branch -> Git error message
- [ ] Non-admin user -> 403 Forbidden

---

## Related Flows

- **Upstream**: [platform-settings.md](platform-settings.md) - GitHub PAT configuration
- **Downstream**: [skill-injection.md](skill-injection.md) - Injecting skills into agents
- **Downstream**: [agent-skill-assignment.md](agent-skill-assignment.md) - Assigning skills to agents
- **Related**: [skills-crud.md](skills-crud.md) - Creating/managing skills via UI

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-09-06 | **#2545 + #2550**: bundled community-catalog pin bumped to `trinity-skills` **v0.2.0** (fresh-install seed only — adds the `project-management` category, 14 skills; `.env.example` documents the same value, `tests/unit/test_2545_skill_source_pin.py` keeps them in step). Update-path tag pin now compares the tag **peeled** (`refs/tags/<ref>^{commit}`): an annotated tag's bare rev-parse is the tag object, so the unmoved bundled source was refused as `moved_tag` on every sync after the first (`tests/unit/test_2550_annotated_tag_pin.py`, both tag kinds). |
| 2026-07-29 | **ent#236 lifecycle automation**: scheduled leader-locked auto-sync, commit-gated fleet-wide re-inject with an honest per-agent report, durable sync status (`--workers 2` gap), and the dedicated range-validated `GET/PUT /api/settings/skills-library` route. Removal-on-unassign is documented in [skill-injection.md](skill-injection.md). |
| 2026-07-19 | **ent#183 skill packages**: skills are full directory packages; list/get surface the frontmatter contract + package metadata (commit-SHA-cached); status adds `multi_file_count`; HEAD of the clone is the atomic injection source. |
| 2026-03-27 | **SEC-179 SSRF prevention**: Added URL validation at write-time and sync-time. New `utils/url_validation.py` module. Updated error handling and security considerations. |
| 2026-01-25 | **Initial document creation**: Complete vertical slice from Settings.vue through skill_service.py git operations. Documented URL formats, shallow clone, PAT handling, status endpoint, error cases. |

---

**Last Updated**: 2026-09-06
**Status**: Verified - Updated for #2545 pin bump + #2550 annotated-tag pin fix
