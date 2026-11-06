# CLI Tool (CLI-001)

> **Status**: Phase 2 — deploy + MCP provisioning
> **Issues**: #231, #259
> **Docs**: `docs/CLI.md`

## Overview

Python Click CLI (`trinity`) that provides shell-level access to the Trinity platform. Thin HTTP client to the existing FastAPI backend — same API surface as the MCP server but invoked via Bash instead of MCP tool calls.

```
User/Agent/Script → trinity CLI → HTTP → FastAPI Backend (:8000)
```

## Multi-Instance Profiles

The CLI supports named profiles for managing multiple Trinity instances (local dev, staging, production).

### Config Format

```json
{
  "current_profile": "localhost",
  "profiles": {
    "localhost": {
      "instance_url": "http://localhost:8000",
      "token": "eyJ...",
      "mcp_api_key": "trinity_mcp_...",
      "user": {"email": "admin@example.com"}
    },
    "trinity.example.com": {
      "instance_url": "https://trinity.example.com",
      "token": "eyJ...",
      "user": {"email": "user@example.com"}
    }
  }
}
```

### Profile Commands

| Command | Description |
|---------|-------------|
| `trinity profile list` | Show all profiles with active indicator |
| `trinity profile use <name>` | Switch active profile |
| `trinity profile remove <name>` | Delete a profile |

### Profile Resolution Priority

1. `TRINITY_URL` / `TRINITY_API_KEY` env vars (always win)
2. `--profile <name>` global flag
3. `TRINITY_PROFILE` env var
4. `current_profile` in config file

### Backwards Compatibility

Legacy flat configs (`{"instance_url": "...", "token": "..."}`) are auto-migrated to a `default` profile on first access.

## Authentication Flow

### `trinity init` (onboarding)

```
User runs `trinity init [--profile name]`
  → Prompt: instance URL
  → Normalize URL (bare domain → https://, strip trailing slash)
  → GET /api/auth/mode (verify reachable)
  → If unreachable: prompt for new URL (up to 3 attempts)
  → Derive profile name from hostname (or use --profile)
  → Prompt: email
  → POST /api/access/request {email}     ← self-signup (403 unless operator enabled it; ent#10)
  → POST /api/auth/email/request {email} ← existing email auth
  → Prompt: 6-digit code
  → POST /api/auth/email/verify {email, code}
  → Store JWT + user in profile within ~/.trinity/config.json (0600)
  → Set as active profile
  → POST /api/mcp/keys/ensure-default (auto-provision MCP key)
  → Store mcp_api_key in profile
  → Write .mcp.json with Trinity MCP server config (merge if exists)
  → Add .mcp.json to .gitignore (if git repo)
  → Ready
```

URL normalization (`_normalize_url`): bare domains like `trinity.ability.ai` get `https://` prepended. Both `init` and `login` retry up to 3 times on connection failure.

### `trinity login` (returning user)

```
User runs `trinity login [--profile name]`
  → Uses stored instance URL from profile (or --instance flag)
  → POST /api/auth/email/request
  → POST /api/auth/email/verify
  → Update stored JWT in profile
  → POST /api/mcp/keys/ensure-default (auto-provision MCP key if needed)
```

### Token Resolution

Priority order:
1. `TRINITY_API_KEY` env var
2. Active profile's token in `~/.trinity/config.json`
3. Error: "Run trinity init"

Instance URL:
1. `TRINITY_URL` env var
2. Active profile's instance_url in `~/.trinity/config.json`
3. Error: "Run trinity init"

## Backend: Access Request Endpoint

**File**: `src/backend/routers/auth.py`
**Endpoint**: `POST /api/access/request`
**Auth**: None (public)

```
Request:  {"email": "user@example.com"}
Response: {"success": true, "message": "Email added to the access whitelist", "already_registered": false}
```

- **Disabled by default** (trinity-enterprise#10, secure default): returns **403** unless the operator opts in via `PUBLIC_ACCESS_REQUESTS_ENABLED=true` (env) or the `public_access_requests_enabled` system setting. When off it does NOT whitelist — the email whitelist stays authoritative against self-enrollment. `trinity:connect` frictionless onboarding requires the operator to enable this flag.
- When enabled, auto-adds email to whitelist via `db.add_to_whitelist(email, added_by="admin", source="cli", default_role="user")` — #314 defaults public self-signup to `user`; owners promote via `PUT /api/users/{username}/role`.
- Idempotent: returns `already_registered: true` if exists
- Rate limited: reuses `check_login_rate_limit(client_ip)` — 5 req / 10 min per IP
- Requires: setup completed, email auth enabled, **and** self-signup flag enabled
- Login-code requests for already-whitelisted emails (`POST /api/auth/email/request`) are unaffected by the flag.

## HTTP Client

**File**: `src/cli/trinity_cli/client.py`

`TrinityClient` wraps httpx with:
- Auto-inject `Authorization: Bearer <token>` header
- 401 → "Run trinity login" message
- HTTP errors → `TrinityAPIError` with status + detail
- Unauthenticated variants for login flow (`post_unauthenticated`, `get_unauthenticated`)

## Output Formatting

**File**: `src/cli/trinity_cli/output.py`

- `--format table` (default): Rich table rendering
  - Lists → column headers from dict keys
  - Dicts → key/value two-column table
  - Every cell is a `rich.text.Text`, never markup: values are server data, and a plain string with `[/]` (a library author's description or successor) raised `MarkupError` and took the command down (ent#736)
- `trinity skills list` builds its table rows in `trinity_cli/skill_rows.py` (stdlib only, so the backend unit job can run it by path): name, description (60 chars), category, and `deprecated` — `yes`, `→ <successor>` (40 chars) or blank (ent#736); `--format json` is the listing unchanged
- `--format json`: `json.dumps(data, indent=2)` for piping/scripting

## Command Map

| CLI Command | HTTP Method | Backend Endpoint |
|-------------|-------------|------------------|
| `trinity deploy .` | POST | `/api/agents/deploy-local` |
| `trinity deploy --repo user/repo` | POST | `/api/agents` |
| `trinity profile list` | — | local config |
| `trinity profile use <name>` | — | local config |
| `trinity profile remove <name>` | — | local config |
| `trinity agents list` | GET | `/api/agents` |
| `trinity agents get <name>` | GET | `/api/agents/{name}` |
| `trinity agents create <name>` | POST | `/api/agents` |
| `trinity agents delete <name>` | DELETE | `/api/agents/{name}` |
| `trinity agents start <name>` | POST | `/api/agents/{name}/start` |
| `trinity agents stop <name>` | POST | `/api/agents/{name}/stop` |
| `trinity agents rename <old> <new>` | PUT | `/api/agents/{name}/rename` |
| `trinity chat <agent> "msg"` | POST | `/api/agents/{name}/chat` |
| `trinity history <agent>` | GET | `/api/agents/{name}/chat/history` |
| `trinity logs <agent>` | GET | `/api/agents/{name}/logs` |
| `trinity health fleet` | GET | `/api/monitoring/status` |
| `trinity health agent <name>` | GET | `/api/monitoring/agents/{name}` |
| `trinity skills list` | GET | `/api/skills/library` |
| `trinity skills get <name>` | GET | `/api/skills/library/{name}` |
| `trinity schedules list <agent>` | GET | `/api/agents/{name}/schedules` |
| `trinity schedules trigger <agent> <id>` | POST | `/api/agents/{name}/schedules/{id}/trigger` |
| `trinity tags list` | GET | `/api/tags` |
| `trinity tags get <agent>` | GET | `/api/agents/{name}/tags` |

## Installation

```bash
# PyPI (recommended)
pip install trinity-cli

# Homebrew (macOS/Linux)
brew install abilityai/tap/trinity-cli

# From source (development)
pip install -e src/cli/
```

Published via GitHub Actions (`publish-cli.yml`) triggered by `cli-v*` tags. Uses PyPI Trusted Publishing (OIDC, no API tokens).

### Distribution Channels

| Channel | Command | Source |
|---------|---------|--------|
| PyPI | `pip install trinity-cli` | `src/cli/` built on tag push |
| Homebrew | `brew install abilityai/tap/trinity-cli` | Formula in `abilityai/homebrew-tap` |
| Source | `pip install -e src/cli/` | Local development |

## Architecture

```
src/cli/
├── pyproject.toml              # Package definition, console_scripts entry
├── trinity_cli/
│   ├── __init__.py             # Version
│   ├── main.py                 # Click group, --profile global option
│   ├── client.py               # TrinityClient (httpx wrapper, profile-aware)
│   ├── config.py               # Profile-based config, legacy migration
│   ├── output.py               # JSON/table formatting (Rich)
│   └── commands/
│       ├── auth.py             # init, login, logout, status (+ MCP key provisioning)
│       ├── deploy.py           # deploy . (file-based), deploy --repo (GitHub-based)
│       ├── profiles.py         # list, use, remove
│       ├── agents.py           # list, get, create, delete, start, stop, rename
│       ├── chat.py             # chat, history, logs
│       ├── health.py           # fleet, agent
│       ├── skills.py           # list, get
│       ├── schedules.py        # list, trigger
│       └── tags.py             # list, get
```

## Deploy Command (CLI-006)

**File**: `src/cli/trinity_cli/commands/deploy.py`

### File-based deploy: `trinity deploy .`

```
User runs `trinity deploy [path] [--name NAME]`
  → Load .trinity-remote.yaml if exists (redeploy detection)
  → If instance mismatch: warn and confirm
  → Resolve agent name: --name flag > tracking file > template.yaml > directory name
  → Archive directory (git ls-files if git repo, else walk + exclude)
  → Exclude: .git, node_modules, __pycache__, .venv, .env files
  → Base64 encode archive
  → POST /api/agents/deploy-local {archive, name}
  → Write .trinity-remote.yaml (auto-added to .gitignore)
```

### GitHub-based deploy: `trinity deploy --repo user/repo`

```
User runs `trinity deploy --repo user/repo [--name NAME]`
  → POST /api/agents {name, template: "github:user/repo"}
```

### Tracking file: `.trinity-remote.yaml`

```yaml
# Auto-generated by trinity deploy — do not edit
instance: https://trinity.example.com
agent: my-agent
profile: production
deployed_at: 2026-04-03T15:45:00Z
```

Enables idempotent redeploys — subsequent `trinity deploy .` updates the same agent.

## Agent Quota Enforcement (QUOTA-001)

**Files**: `src/backend/services/agent_service/crud.py`, `src/backend/services/agent_service/deploy.py`

- Default limit: 3 agents per user (configurable via `max_agents_per_user` setting)
- Enforced in `create_agent_internal()` and `deploy_local_agent_logic()`
- System agents (`is_system=True`) excluded from count
- Redeploys of existing user-owned agents bypass quota
- Returns HTTP 429 on exceed

## Release Process

Tag-driven auto-versioning. Version is extracted from the git tag at build time.

```
git tag cli-v0.3.0 && git push --tags
```

This triggers `.github/workflows/publish-cli.yml`:

1. **PyPI publish**: Extracts version from tag (`cli-v0.3.0` → `0.3.0`), injects into `pyproject.toml`, builds, publishes
2. **Homebrew update**: Downloads published sdist, computes sha256, pushes updated formula to `abilityai/homebrew-tap`

**Version at runtime**: `trinity --version` reads from installed package metadata via `importlib.metadata.version("trinity-cli")`. Source files contain placeholder `0.0.0`.

**Requires**: `HOMEBREW_TAP_TOKEN` repo secret (fine-grained PAT with Contents read/write on `abilityai/homebrew-tap`).

## Future Phases

- Phase 3: Remaining ~47 MCP tool equivalents (credentials, events, executions, systems, subscriptions, notifications, nevermined)
- Phase 3: Shell completions (Click supports bash/zsh/fish completion generation)
