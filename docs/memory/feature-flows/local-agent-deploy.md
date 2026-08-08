# Feature: Local Agent Deployment via MCP

> **Updated**: 2026-08-08 - #2060 integrity contract: embedded `.trinity-manifest.json` verified post-extract AND post-copy (fail-closed `MANIFEST_DRIFT`), symlink preservation contract (`copytree(symlinks=True)`, `extractall(filter='tar')`), observed-vs-limit caps + decompressed-size cap + AppleDouble skip, evidence-bearing response (`verified`/counts/`compatibility_hard_count`), `Idempotency-Key` + per-base-name deploy lock, residue/compensation fixes. See requirements `core-agent.md` §4.1.2.

## Overview

Deploy Trinity-compatible local Claude Code agents to a remote Trinity platform with a single MCP command. The **local agent** (Claude Code on your machine) packages the directory into a tar.gz archive and sends it to the remote Trinity backend for deployment.

**Key Architecture Point**: The MCP server runs remotely and cannot access your local filesystem. Therefore, the **calling agent** must package the archive locally before invoking the MCP tool.

## User Story

As a developer working with a Trinity-compatible local agent, I want to deploy it to a remote Trinity instance with one command so I can run it on the platform without manual file transfer.

## Entry Points

- **CLI**: `trinity deploy .` — packages and uploads local directory (`src/cli/trinity_cli/commands/deploy.py`)
- **MCP Tool**: `deploy_local_agent` via Trinity MCP server
- **API**: `POST /api/agents/deploy-local`

---

## Architecture

```
+-------------------------------------+                     +-----------------------------+
|  Your Local Machine                 |                     |  Remote Trinity Server      |
|                                     |                     |                             |
|  Claude Code (local agent)          |     HTTP POST       |  MCP Server                 |
|  1. tar -czf archive.tar.gz ...     |  ---------------->  |  deploy_local_agent tool    |
|  2. base64 archive.tar.gz           |   archive           |         |                   |
|  3. Call deploy_local_agent         |                     |         v                   |
|                                     |                     |  Backend API                |
|                                     |                     |  /api/agents/deploy-local   |
|  /home/you/my-agent/                |                     |         |                   |
|  |-- template.yaml                  |                     |         v                   |
|  |-- CLAUDE.md                      |                     |  Extract, validate, deploy  |
|  +-- .env                           |                     |  Agent container created    |
+-------------------------------------+                     +-----------------------------+
```

---

## MCP Tool Layer

### Tool: `deploy_local_agent`

**Location**: `src/mcp-server/src/tools/agents.ts:744+` (`deployLocalAgent`)

**Parameters**:
```typescript
{
  archive: string,                    // Base64-encoded tar.gz archive (REQUIRED, manifest embedded)
  name?: string                       // Override agent name (optional)
}
```

The archive should include all files needed by the agent — `.env`, `.mcp.json`, `CLAUDE.md`, etc. **The MCP tool exposes only `archive` and `name`** and forwards those two fields **plus `require_manifest: true`** to the backend. `require_manifest` is set in tool CODE, not a model-controlled parameter (#2060) — the MCP surface gets the integrity contract unconditionally, at the cost of one loud `MANIFEST_REQUIRED` round-trip for a naive caller. The underlying API and `trinity deploy` CLI additionally accept an optional `credentials` map that is merged into the deployed `.env` (see Request Model + step 9 below) — that path is what surfaces the MCP credential-gap `warnings[]`.

**Idempotency (#2060)**: `execute()` derives a deterministic `Idempotency-Key` over `[userId, "deploy_local_agent", name, archive]` and sends it via `extraHeaders`. Same args ⇒ same key, so a transport retry of a slow deploy replays the original response instead of minting ANOTHER version (versioning made an un-keyed retry fork twice: `my-agent-2` AND `my-agent-3`). Deliberately same-args-only: a re-run packaging pipeline produces new gzip bytes (gzip mtime) ⇒ a new key ⇒ a visible new version — keying on content would false-replay an intentional identical-content redeploy.

**Validation**:
- Checks archive is provided and non-empty
- Validates base64 format with regex: `/^[A-Za-z0-9+/=]+$/`

**Description contract (#2060)**: the description carries the manifest-generation snippet (same four excludes as the tar command, writes `.trinity-manifest.json` INTO the agent dir so the tar carries it), the `COPYFILE_DISABLE=1` macOS note, the caps, and the **honest token ceiling**: the archive rides the tool call's arguments, which are token-bound (~100–200 KB of base64 per model turn in practice) — larger agents must deploy via the turn-bypassing transports that already exist (`trinity` CLI / `curl` from bash; MCP keys are valid Bearer tokens). The integrated direct-upload channel is the FU-1 follow-up. The tool does NOT access the local filesystem — that's the calling agent's responsibility.

**Tests**: `src/mcp-server/src/tools/agents.deploy.test.ts` pins `require_manifest` in the body, key determinism (identical args only), and the description contract.

---

## Calling Agent Workflow

The local Claude Code agent must perform these steps before calling `deploy_local_agent`:

### Step 0: Write the Integrity Manifest (#2060)

Run **from the agent directory**, with the same excludes as the tar command —
the manifest is computed from the FULL disk tree and ships INSIDE the archive,
so a tar-level `--exclude` cannot prune the manifest's content and every
accidental truncation/exclusion class drifts loudly at deploy time:

```bash
python3 - <<'PY'
import hashlib, json, os
# MUST match the tar command's excludes below — a manifest/tar exclude
# mismatch IS drift and will be refused.
EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv'}
EXCLUDE_FILES = {'.DS_Store'}
entries = []
for dirpath, dirnames, filenames in os.walk('.'):
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
    for d in list(dirnames):
        p = os.path.join(dirpath, d)
        if os.path.islink(p):
            entries.append({'path': os.path.relpath(p, '.'), 'link_target': os.readlink(p)})
            dirnames.remove(d)
    for f in filenames:
        p = os.path.join(dirpath, f)
        rel = os.path.relpath(p, '.')
        if rel == '.trinity-manifest.json' or f.startswith('._') \
                or f in EXCLUDE_FILES or f.endswith('.pyc'):
            continue
        if os.path.islink(p):
            entries.append({'path': rel, 'link_target': os.readlink(p)})
        elif os.path.isfile(p):
            entries.append({'path': rel, 'sha256': hashlib.sha256(open(p, 'rb').read()).hexdigest()})
with open('.trinity-manifest.json', 'w') as fh:
    json.dump(entries, fh)
PY
```

Files carry `sha256`, symlinks carry `link_target`, directories are omitted;
the manifest never lists itself (it cannot self-hash — its transport integrity
is the gzip's). The `trinity deploy` CLI computes and injects the manifest
in-memory automatically (never mutating the source dir).

### Step 1: Create tar.gz Archive

```bash
# Package the agent directory, including .env and all credential files.
# COPYFILE_DISABLE=1 prevents macOS AppleDouble ._* pollution (#2060 — such
# members are skipped server-side with a warning either way).
COPYFILE_DISABLE=1 tar -czf /tmp/agent-deploy.tar.gz \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  -C /path/to/parent agent-directory-name
```

### Step 2: Base64 Encode

```bash
# macOS
base64 -i /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64

# Linux
base64 /tmp/agent-deploy.tar.gz > /tmp/agent-deploy.b64
```

### Step 3: Call MCP Tool

The agent then calls `deploy_local_agent` with:
- `archive`: Contents of the base64 file
- `name`: Optional name override

---

## Backend Layer

### Architecture (Service Layer)

The local agent deployment uses a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py` | Endpoint definition |
| Service | `src/backend/services/agent_service/deploy.py` | Deployment business logic |

### Endpoint: POST /api/agents/deploy-local

**Router**: `src/backend/routers/agents.py:537+`

Deploy requires the **creator** role or above (`require_role("creator")`),
consistent with `create_agent` — see [role-model.md](role-model.md).

**Idempotency (#2060, Invariant #18)**: the endpoint accepts an optional
`Idempotency-Key` header, scope `agent_deploy:{current_user.id}` — mirroring
the create endpoint **including** the #2040-F3 staleness branch: a completed
replay is honored only while `db.is_agent_live(snapshot.versioning.new_version)`;
stale → `discard_stale_replay` + re-begin (a genuinely fresh deploy). An
in-flight duplicate answers 409 `DEPLOY_IN_FLIGHT`; a failed deploy releases
the claim (`idempotency_service.fail`, fail-open). Without a key, versioning
made a client-timeout retry fork twice (`my-agent-2` AND `my-agent-3`).

**Request Model** (`src/backend/models.py`):
```python
class DeployManifestEntry(BaseModel):
    """One entry of the embedded deploy integrity manifest (#2060)."""
    path: str
    sha256: Optional[str] = None        # regular files
    link_target: Optional[str] = None   # symlinks (exactly one of the two)

class DeployLocalRequest(BaseModel):
    """Request to deploy a local agent."""
    archive: str  # Base64-encoded tar.gz (manifest embedded as a member)
    name: Optional[str] = None  # Override name from template.yaml
    credentials: Optional[Dict[str, str]] = None  # Optional {KEY: value} merged into .env
    require_manifest: Optional[bool] = False  # #2060: refuse manifest-less archives

# Maximum credentials allowed per deploy-local request
MAX_DEPLOY_CREDENTIALS = 100
```

`credentials` is capped at `MAX_DEPLOY_CREDENTIALS` (100); exceeding it returns
HTTP 400. The MCP `deploy_local_agent` tool does not pass this field — it is
used by the API and `trinity deploy` CLI to fold operator-supplied secrets into
the archive's `.env` at deploy time. `require_manifest` defaults to False on the
raw HTTP API so manifest-less legacy deploys (previously-shipped PyPI CLI,
abilities plugin) keep working — they answer `status: "success"` with
`verified: false` + a warning (flipping `status` would make every legacy deploy
*report* failure after succeeding; the shipped CLI hard-fails on
`status != "success"`). The MCP tool and the in-repo CLI both set it true.

**Response Model** (`src/backend/models.py`):
```python
class DeployLocalResponse(BaseModel):
    """Response from local agent deployment."""
    status: str  # "success" or "error"
    agent: Optional[AgentStatus] = None
    versioning: Optional[VersioningInfo] = None
    credentials_imported: Optional[Dict[str, str]] = None
    credentials_injected: Optional[int] = None
    warnings: List[str] = []  # Advisory deploy-time warnings (e.g. MCP credential gaps)
    error: Optional[str] = None
    code: Optional[str] = None  # Error code for machine-readable errors
    # #2060 evidence fields
    verified: bool = False                       # manifest present AND both verifications passed
    files_expected: Optional[int] = None         # manifest file entries
    files_deployed: Optional[int] = None         # regular files at dest (manifest member excluded)
    symlinks_deployed: Optional[int] = None
    compatibility_hard_count: Optional[int] = None  # post-deploy #668 STATIC; None = unavailable
```

`warnings` carries non-fatal advisories — MCP servers whose `${VAR}` references
have no matching credential after the request `credentials` are merged into
`.env` (see step 9 below), plus the #2060 additions: skipped AppleDouble
member counts, preserved dangling symlinks (`dangling symlink preserved:
{path} -> {target}`), the manifest-less "NOT integrity-verified" advisory, and
a compat-gate-unavailable note. The MCP `deploy_local_agent` tool
`JSON.stringify`s the whole response, so warnings reach `/trinity:onboard`
automatically.

### Deployment Flow (`deploy.py`)

1. **Decode & Validate Archive**
   - Decode base64 archive
   - Check size limit (50MB max; error carries `observed` + `limit`, #2060)
   - Reject `body.credentials` exceeding `MAX_DEPLOY_CREDENTIALS` (100) → HTTP 400

2. **Extract Archive**
   - Extract to temp directory using `_safe_extract_tar()`
   - Security: Full path validation via `_validate_tar_member()` — strictly
     PRE-extraction (the #2060 layering rule); AppleDouble `._*` members
     skipped (counted into a warning); decompressed-size cap; `filter='tar'`

3. **Find Root Directory**
   - Handle nested extraction (single directory case)

3a. **Manifest Load + Post-Extract Verification (#2060)** — before ANY side
    effect (no version computed, no previous agent stopped, nothing
    persisted — the same ordering rationale as the #2006 `.mcp.json` gate):
   - `_load_manifest(extract_root)` parses + bound-checks
     `.trinity-manifest.json` (400 `MANIFEST_INVALID` on oversize/shape/
     dup/traversal/self-listing)
   - `require_manifest` set + no manifest → 400 `MANIFEST_REQUIRED`
     (detail carries the generation snippet)
   - `_verify_manifest(extract_root, entries, check_extras=True)` — every
     entry present with matching kind/sha256/link_target, every extracted
     entry present in the manifest (**extras are drift** — a stale committed
     manifest fails honestly). Drift → 400 `MANIFEST_DRIFT` with capped
     `missing`/`altered`/`extra`/`link_mismatch` lists + full counts; the
     recovery text directs at rebuild-without-excludes / the CLI, never at
     removing entries from the manifest
   - Dangling in-root symlinks collected → `dangling symlink preserved:`
     warnings (preserved, not rejected — links to runtime-created dirs like
     `content/` are legitimate; a pruned *target* still listed in the
     manifest is refused as `missing`)

4. **Trinity-Compatible Validation**
   - `is_trinity_compatible()` in `services/template_service.py`
   - Requires template.yaml with `name` and `resources` fields
   - Requires a non-empty, UTF-8-readable `CLAUDE.md` — missing / empty /
     whitespace-only / non-UTF-8 → HTTP 400 `NOT_TRINITY_COMPATIBLE` (#950).
     **Behavior change**: agents that previously deployed without a CLAUDE.md
     (a warning, not an error) are now rejected at deploy time. A redeploy of
     a CLAUDE.md-less local agent will 400 until a CLAUDE.md is added.

5. **Determine Agent Name**
   - Use body.name override or template.yaml name
   - Sanitize with `sanitize_agent_name()`

6. **Agent Quota Enforcement** (added in #259)
   - Check if existing versions are owned by current user (`get_agents_by_prefix` + `get_agents_by_owner`)
   - Skip quota for redeploys of user-owned agents
   - For new agents: enforce `max_agents_per_user` setting (default: 3)
   - System agents excluded from count
   - Returns HTTP 429 with `QUOTA_EXCEEDED` code on limit

6c. **Per-Base-Name Deploy Lock (#2060)** — `agent:deploy_op:{base_name}`
    (Redis SETNX, 10-min TTL, released in `finally`; registered in
    `agent_runtime_state.EXEMPT_KEYSPACES`). Held from before the first side
    effect; contention → 409 `DEPLOY_IN_PROGRESS`. **Fail-open** on Redis
    down — the prepop attached-volume 409 is the destructive-collision
    backstop.

7. **Version Handling**
   - `get_next_version_name()` finds next available version
   - Pattern: `my-agent` -> `my-agent-2` -> `my-agent-3`
   - Stops previous version if running; the stopped name is remembered for
     compensation (#2060 — a FAILED deploy restarts what it stopped,
     including when `create_agent_fn` raised; success keeps it stopped)

8. **Persist Template** (#950, #2060)
   - Write the validated archive contents to `/data/deployed-templates/{version_name}/` (`dest_path`) for inspection and future `template.yaml` lookups. On write failure: HTTP 500 with `code=DEPLOYED_TEMPLATES_DIR_UNWRITABLE` (fail-fast, no silent fallback).
   - `dest_created` is assigned **before** the rmtree/`copytree` pair (#2060
     Crux #4, the #2006 class) so a mid-copy failure is cleaned by
     `_remove_partial_deploy` instead of leaving an addressable
     `local:<version>` partial dir.
   - `shutil.copytree(..., symlinks=True)` — in-root symlinks preserved (the
     default dereferenced them); dangling links copied as links instead of
     crashing into an opaque 500. A copy failure is a named 500
     `TEMPLATE_COPY_FAILED` summarizing the `shutil.Error` paths.
   - Curated catalog at `/agent-configs/templates` stays read-only (operators' source of truth).

8a. **Post-Copy Manifest Re-Verification (#2060)** — `_verify_manifest(dest_path,
    entries, check_extras=False)` immediately after the copy and **before**
    the step-9 credentials merge mutates `.env` (ordering load-bearing — the
    merge would otherwise false-drift a manifest-listed `.env`). Evidence
    counts (`files_deployed`/`symlinks_deployed`) captured here, manifest
    member excluded. Post-`put_archive` verification is deliberately skipped:
    the volume copy is a local `tar.add` from the just-verified `dest_path`,
    and `put_archive` failures already raise.

9. **Merge Credentials + MCP Credential-Gap Warnings** (#950)
   - If `body.credentials` is provided, merge the `{KEY: value}` pairs into the persisted `.env` at `dest_path/.env` (returned `credentials_injected` count; `credentials_imported` records `.env`/`.mcp.json` provenance: `from_archive` / `merged` / `created`).
   - `collect_mcp_credential_warnings(dest_path)` then scans `.mcp.json.template` (falling back to `.mcp.json`) for `${VAR}` references whose key is neither present in the **post-merge** `.env` nor platform-injected, and returns them as advisory `warnings[]` — non-fatal. The platform-injected allowlist (`_PLATFORM_INJECTED_EXACT` + `TRINITY_`/`GIT_`/`OTEL_`/`CLAUDE_CODE_` prefixes) is a deliberate static mirror of the env vars Trinity sets at create time (`crud.py`), so those don't produce false-positive gaps. The MCP server name (an arbitrary, operator-supplied JSON key) is passed through `_sanitize_for_warning()` before interpolation — non-printable characters (ANSI escapes, newlines, C0/C1 controls) are stripped and the length is bounded — so a hostile template can't smuggle terminal-escape sequences into the operator-facing warning rendered by `/trinity:onboard` (CSO L1).

   - **Since #1900** the subsequent agent-create staging resolves this template's `.mcp.json` from the deploy-local directory itself (`crud._resolve_local_template_dir`), not from the template.yaml's `name:` field, so a hostile `name:` can no longer read another tenant's `.mcp.json` into the new agent. **Behaviour delta, stated precisely** (the old lookup searched the *curated* catalog and always missed, so this file was never generated for a deploy-local template before): the staged file now **wins** over the archive's raw copy — `startup.sh` copies `/generated-creds/.mcp.json` after the template-copy block and unconditionally — and `_stage_config_files` calls `generate_credential_files` with an **empty** credential map (CRED-002: real values arrive later, via injection), so every `${VAR}` in the archive's `.mcp.json` is rewritten to `""`, not to the value merged into `.env` above. Hardcoded (non-`${VAR}`) entries survive verbatim. The convention-following record of which variables the server needs is `.mcp.json.template`, which is pre-populated untouched. See [template-processing.md](template-processing.md).

10. **Workspace Volume Pre-population** (#950, #2060)
    - **Pre-populate the agent's workspace volume directly** via `put_archive` into an ephemeral `alpine:3.20` container that mounts `agent-{version_name}-workspace`. Includes a `.trinity-initialized` marker so the agent's `startup.sh` skips its `/template` → `/home/developer` copy on boot. On failure: HTTP 500 with `code=WORKSPACE_PREPOP_FAILED`.
    - **Stale-volume hygiene (#2060)**: a pre-existing volume under the new version name is a failed/concurrent deploy's leftover (no ownership row exists yet by construction). Unattached → removed-and-recreated (`put_archive` overlays, never prunes — reuse would let stale files from the failed attempt survive into this deploy). Attached, or attachment unknowable → 409 `WORKSPACE_VOLUME_IN_USE` (never `put_archive` into a mounted volume — it means a concurrent/zombie deploy; fail-closed per the #1664 lesson).
    - The prepop `tar.add` preserves symlinks (tarfile default `dereference=False`), so the #2060 symlink contract holds end to end into the workspace.
    - **Why no bind-mount transport for deploy-local**: dev compose uses a docker-managed named volume for `/data` while prod uses a host bind. Any host-path math in `crud.py` was right on prod and wrong on dev, producing empty agents on dev. Pre-populating the workspace volume directly is uniform across both.

11. **Agent Creation**
    - Extract runtime config from template
    - Call `create_agent_fn()` (injected `create_agent_internal`) with local template
    - Agent container starts with all files from the archive (including `.env`)

11a. **Post-Deploy Compatibility Evidence (#2060 / #668)** — STATIC-only
     `build_report(version_name, include_ai=False)` → `compatibility_hard_count`
     in the response. **Fail-open**: an unavailable/raising report yields
     `None` + a warning, never a failed deploy (absent evidence is not zero
     findings — an `unavailable` report maps to `None`, not `0`).

12. **Return Response**
    - Return DeployLocalResponse with agent status, versioning info, `credentials_injected`/`credentials_imported`, advisory `warnings`, and the #2060 evidence fields (`verified`, `files_expected`, `files_deployed`, `symlinks_deployed`, `compatibility_hard_count`)

**Failure compensation (#2060 S6)** — both except handlers run, in order:
`_remove_partial_deploy(dest_created)` (#2006), `_cleanup_deploy_volume(volume_created)`
(label + unattached double-guarded, best-effort — `volume_created` deliberately
stays set ACROSS `create_agent_fn`, because after a create failure crud
rollback + ent#313 reclaim remove the failed container, making the volume
provably unattached; while anything mounts it the guard refuses and the #1581
orphan sweep backstops), and `_restart_stopped_previous(previous_stopped_name)`
(best-effort, log-only on failure, never masks the original error). The final
catch-all 500 carries `code: "DEPLOY_FAILED"`. The deploy lock is released in
`finally`.

### Safe Tar Extraction (`deploy.py` — `_is_path_within` / `_validate_tar_member` / `_safe_extract_tar`)

The extraction uses comprehensive security validation:

**Path Validation** (`_is_path_within()`):
- Uses `Path.resolve()` to normalize paths
- Checks target stays within base directory

**Member Validation** (`_validate_tar_member()`):
- Rejects absolute paths
- Rejects path traversal (`..` in paths)
- Validates destination stays within base_dir
- Rejects device files (chr, blk) and FIFOs
- Validates symlink targets stay within base_dir (each hop of a chain is
  itself a member and refused individually; refusals name path + target)
- Validates hardlink targets stay within base_dir

**Safe Extraction** (`_safe_extract_tar()`):
- Skips macOS AppleDouble `._*` members (returned count → warning, #2060)
- Checks file count (10000 max; error carries `observed` + `limit`)
- Checks decompressed size from member headers (500 MB max — gzip-bomb
  guard, #2060) → 400 `ARCHIVE_EXTRACTED_TOO_LARGE`
- Validates all members BEFORE extraction (**layering rule #2060**: security
  validation is pre-extraction by contract; only manifest drift verification
  runs post-extract — moving containment checks post-extract would reopen
  the tar-slip class)
- Extracts with `filter='tar'` pinned (Py3.14 flips the unpinned default to
  `'data'`, changing link/metadata semantics under us; `'tar'` is
  behavior-stable, strips setuid/setgid/sticky as defense-in-depth, and
  leaves `_validate_tar_member` the single authoritative link barrier)

---

## Template Validation

**Location**: `src/backend/services/template_service.py:608-728` (`is_trinity_compatible` + `collect_mcp_credential_warnings`)

```python
def is_trinity_compatible(path: Path) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Check if a directory contains a Trinity-compatible agent.

    A Trinity-compatible agent must have:
    1. template.yaml file
    2. name field in template.yaml
    3. resources field in template.yaml
    4. a non-empty CLAUDE.md (agent instructions)
    """
```

**Validation Checks**:
1. `template.yaml` exists
2. File is valid YAML
3. File is not empty
4. `name` field present
5. `resources` field present and is a dictionary
6. `CLAUDE.md` present, readable as UTF-8, and non-empty after `.strip()`
   (blocking — #950; a binary/non-UTF-8 CLAUDE.md is rejected with a clean
   400 rather than crashing the generic handler with a 500)

---

## Error Codes

| Code | HTTP | Description |
|------|------|-------------|
| `NOT_TRINITY_COMPATIBLE` | 400 | Missing/invalid template.yaml, or missing/empty/non-UTF-8 CLAUDE.md (#950) |
| `ARCHIVE_TOO_LARGE` | 400 | Exceeds 50MB limit (carries `observed` + `limit`, #2060) |
| `ARCHIVE_EXTRACTED_TOO_LARGE` | 400 | Decompressed size (member headers) exceeds 500 MB — gzip-bomb guard (#2060) |
| `INVALID_ARCHIVE` | 400 | Not valid tar.gz, bad base64, or path traversal (member refusals name path + target) |
| `TOO_MANY_FILES` | 400 | Exceeds 10000 file limit (carries `observed` + `limit`, #2060) |
| `MANIFEST_REQUIRED` | 400 | `require_manifest` set, no `.trinity-manifest.json` in archive; carries the generation snippet (#2060) |
| `MANIFEST_INVALID` | 400 | Manifest oversize (>5 MB) / not JSON / bad shape / dup / traversal / self-listed (#2060) |
| `MANIFEST_DRIFT` | 400 | Extracted or copied tree diverges from the manifest — capped `missing`/`altered`/`extra`/`link_mismatch` lists + counts, `stage: post-extract\|post-copy` (#2060) |
| `MISSING_NAME` | 400 | No name specified and template.yaml has no name |
| `DEPLOY_IN_PROGRESS` | 409 | Per-base-name deploy lock contention (#2060) |
| `DEPLOY_IN_FLIGHT` | 409 | Idempotency-Key duplicate still being processed (#2060) |
| `WORKSPACE_VOLUME_IN_USE` | 409 | Stale workspace volume attached (or attachment unknowable) — never overlaid (#2060) |
| `QUOTA_EXCEEDED` | 429 | Agent quota reached (skipped for redeploys) |
| `DEPLOYED_TEMPLATES_DIR_UNWRITABLE` | 500 | `/data/deployed-templates` could not be created — fail-fast, no silent fallback (#950/#971) |
| `TEMPLATE_COPY_FAILED` | 500 | `copytree` into the deploy store failed — named, paths summarized (was an opaque 500) (#2060) |
| `WORKSPACE_PREPOP_FAILED` | 500 | Workspace volume pre-population (`put_archive`/chown) failed (#950) |
| `DEPLOY_FAILED` | 500 | Catch-all (now carries the code; was a bare string) (#2060) |

---

## Size Limits

| Limit | Value | Constant Location |
|-------|-------|-------------------|
| Archive size (compressed) | 50 MB | `deploy.py` MAX_ARCHIVE_SIZE (unchanged — ~67 MB as base64 JSON body; raising it belongs to the FU-1 upload channel) |
| Extracted size (member headers) | 500 MB | `deploy.py` MAX_EXTRACTED_SIZE (#2060) |
| File count | 10000 | `deploy.py` MAX_FILES (#2060: was 1000 — a Cornelius-class KB agent exceeds it) |
| Manifest read | 5 MB | `deploy.py` MAX_MANIFEST_BYTES (#2060) |

All cap rejections carry `observed` + `limit` (#2060).

---

## Security Considerations

1. **Path Traversal Prevention**: Archive paths validated via `_validate_tar_member()`:
   - No `..` in paths
   - No absolute paths
   - Symlinks/hardlinks validated to stay within extraction dir
   - Device files and FIFOs rejected

2. **Temp Cleanup**: Temp directory always cleaned up in finally block (lines 430-436)

3. **Self-Contained Archives**: Credentials (`.env`) travel inside the archive. The optional `credentials` map is an additive merge into that `.env` at deploy time (step 9), not an out-of-band injection into a running container.

4. **Auth Required**: JWT authentication plus the **creator** role gate (`require_role("creator")`, which wraps `get_current_user`)

5. **Write Permission Check**: Templates directory write-tested before use

6. **Integrity vs Security Layering (#2060)**: two distinct layers, ordering
   load-bearing — *security validation* (containment, link targets, member
   types) runs pre-extraction in `_validate_tar_member`; *drift verification*
   (manifest matching) runs post-extract. Never merge the loops.

7. **Honesty note (#2060)**: manifest verification is **accident-proof, not
   adversary-proof**. The manifest is computed from the full disk tree, so
   every accidental truncation/exclusion class drifts and is refused; a
   passing incomplete deploy requires consistently editing both the tar and
   the manifest commands — deliberate evasion, visible in the calling
   transcript. On the pure-MCP path the archive is also token-bound
   (~100–200 KB base64/turn); large agents use the `trinity` CLI or curl from
   bash. The direct-upload channel that removes the payload from the model
   turn entirely is the FU-1 follow-up.

---

## Testing

### Prerequisites
- Trinity backend running (local or remote)
- MCP server running and accessible
- Valid MCP API key configured in Claude Code
- Local agent directory with valid template.yaml

### Test Steps

#### 1. Create Test Agent Directory
```bash
mkdir -p /tmp/test-deploy-agent
cat > /tmp/test-deploy-agent/template.yaml << 'EOF'
name: test-deploy
display_name: Test Deploy Agent
description: Testing local agent deployment
resources:
  cpu: "2"
  memory: "4g"
EOF

echo "# Test Deploy Agent" > /tmp/test-deploy-agent/CLAUDE.md
echo "TEST_API_KEY=test-value-123" > /tmp/test-deploy-agent/.env
```

#### 2. Package and Deploy via Claude Code

In Claude Code with Trinity MCP configured, ask:

```
Package and deploy my local agent at /tmp/test-deploy-agent to Trinity.
```

**Expected**: Claude Code will:
1. Run `tar` command to create archive (including .env)
2. Run `base64` to encode it
3. Call the MCP tool with the archive

**Verify**:
- Agent "test-deploy" created in Trinity
- Agent has .env file from archive

#### 3. Deploy Again (Versioning Test)
```
Deploy my local agent at /tmp/test-deploy-agent to Trinity again
```

**Expected**:
- New agent "test-deploy-2" created
- Previous "test-deploy" stopped

#### 4. Test Invalid Archive
```
Call deploy_local_agent with archive="not-valid-base64!"
```

**Expected**: Error "Invalid archive format"

#### 5. Test Missing Template
```bash
rm /tmp/test-deploy-agent/template.yaml
# Then try to deploy
```

**Expected**: Error "NOT_TRINITY_COMPATIBLE"

### Edge Cases
- [ ] Archive larger than 50MB -> ARCHIVE_TOO_LARGE
- [ ] More than 1000 files -> TOO_MANY_FILES
- [ ] Path traversal in archive -> INVALID_ARCHIVE

### Cleanup
```bash
rm -rf /tmp/test-deploy-agent
rm -f /tmp/agent-deploy.tar.gz /tmp/agent-deploy.b64
```

---

## Example: Full Deployment Script

For reference, here's a complete bash script a local agent might execute:

```bash
#!/bin/bash
# deploy-to-trinity.sh - Package and prepare for MCP deployment

AGENT_DIR="$1"
if [ -z "$AGENT_DIR" ]; then
  echo "Usage: deploy-to-trinity.sh /path/to/agent"
  exit 1
fi

# Validate template.yaml exists
if [ ! -f "$AGENT_DIR/template.yaml" ]; then
  echo "Error: Not Trinity-compatible - missing template.yaml"
  exit 1
fi

# Create archive (includes .env and all credential files)
ARCHIVE="/tmp/trinity-deploy-$$.tar.gz"
tar -czf "$ARCHIVE" \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  -C "$(dirname "$AGENT_DIR")" "$(basename "$AGENT_DIR")"

# Base64 encode
ARCHIVE_B64=$(base64 -i "$ARCHIVE" 2>/dev/null || base64 "$ARCHIVE")

echo "Archive size: $(wc -c < "$ARCHIVE") bytes"
echo "Ready for deploy_local_agent MCP call"

# Cleanup
rm -f "$ARCHIVE"
```

---

## Related Documentation

- [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md) - Required template.yaml structure
- [credential-injection.md](credential-injection.md) - Credential management
- [agent-lifecycle.md](agent-lifecycle.md) - Agent creation flow

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-08-08 | **#2060 (integrity contract)**: embedded `.trinity-manifest.json` verified post-extract + post-copy, fail-closed `MANIFEST_REQUIRED`/`MANIFEST_INVALID`/`MANIFEST_DRIFT`; symlink contract (`copytree(symlinks=True)`, dangling preserved + warned, `extractall(filter='tar')` pinned); `MAX_FILES` 1000→10000, new `MAX_EXTRACTED_SIZE` 500 MB, observed+limit on cap errors, AppleDouble `._*` skip; evidence response fields (`verified`/`files_expected`/`files_deployed`/`symlinks_deployed`/`compatibility_hard_count` via fail-open #668 STATIC); `Idempotency-Key` (scope `agent_deploy:{user_id}`, #2040-F3 staleness branch) + `agent:deploy_op:{base}` lock; S6 fixes: `dest_created` before copy (named `TEMPLATE_COPY_FAILED`), workspace-volume cleanup + `WORKSPACE_VOLUME_IN_USE` 409, previous-version restart on any failure. MCP tool sets `require_manifest: true` in code + derives the idempotency key; CLI embeds the manifest in-memory + prints evidence. Requirements: `core-agent.md` §4.1.2. Follow-ups FU-1 (direct-upload transport, AC 1) / FU-2 (redeploy-in-place, AC 7). |
| 2026-05-29 | **#950 (deferred hardening)**: `is_trinity_compatible()` now requires a non-empty, UTF-8 `CLAUDE.md` (blocking 400, was a non-fatal warning). New `collect_mcp_credential_warnings()` surfaces MCP servers with unsatisfied `${VAR}` refs as advisory `DeployLocalResponse.warnings[]` (also added to the MCP tool response type). Documented the `credentials` request field (reinstated after #251) + `MAX_DEPLOY_CREDENTIALS`, the credential-merge step, and the `DEPLOYED_TEMPLATES_DIR_UNWRITABLE`/`WORKSPACE_PREPOP_FAILED` error codes. Refreshed router snippet (`require_role("creator")`, `agents.py:418-430`). |
| 2026-04-03 | **#251**: Removed `credentials` parameter from the deploy flow. Archive is self-contained — `.env` and credential files included in tar.gz. Removed credential injection block that caused hangs. *(Note: a `credentials` map was later reinstated as an optional API/CLI field — see 2026-05-29.)* |
| 2026-02-05 | **CRED-002**: Removed `credential_manager` parameter from deploy flow. |
| 2026-01-23 | Verified all line numbers. Updated deploy.py references (now 437 lines). Added safe tar extraction details. Updated router line numbers (212-225). Added template validation location. |
| 2025-12-30 | Verified line numbers |
| 2025-12-27 | Service layer refactoring: Deploy logic moved to `services/agent_service/deploy.py` |
| 2025-12-24 | Changed from local path to archive-based deployment |
| 2025-12-21 | Initial implementation |

**Status**: Working
