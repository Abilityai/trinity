# Read-Only Mode for Agents (CFG-007)

> **Status**: Not Started
> **Priority**: HIGH
> **Created**: 2026-02-17

## Overview

Enable a per-agent "Read-Only Mode" that prevents agents from modifying source code, instructions, or configuration files while still allowing them to execute tasks, produce outputs, and read the codebase.

**Use Case**: Deploy agents to clients where you want them to follow instructions and produce deliverables, but NOT modify the agent's own code, CLAUDE.md, skills, or configuration.

## User Stories

| ID | Story |
|----|-------|
| ROM-001 | As an agent owner, I want to enable read-only mode for an agent so that it cannot modify its own instructions or source code |
| ROM-002 | As an agent owner, I want to configure which paths are protected (blocklist) so that I can customize restrictions per agent |
| ROM-003 | As an agent owner, I want certain paths to remain writable (allowlist) so that agents can still produce outputs in designated folders |

## Solution: PreToolUse Hook Injection

Use Claude Code's `PreToolUse` hook mechanism to intercept `Write` and `Edit` tool calls and block them based on path patterns.

### How It Works

1. Owner enables "Read-Only Mode" toggle in Agent Detail UI
2. Platform stores setting in database (`read_only_mode` column)
3. On agent start, platform injects `.claude/settings.local.json` with PreToolUse rules
4. Claude Code's hook system blocks Write/Edit to protected paths
5. Agent can still write to allowed paths (e.g., `content/`, `output/`, `reports/`)

### Hook Configuration

Injected to agent's `~/.claude/settings.local.json` (merged with existing settings):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/developer/.trinity/hooks/read-only-guard.py"
          }
        ]
      }
    ]
  }
}
```

The guard script receives tool input as **JSON via stdin** and communicates via:
- **Exit 0** - Allow the write (optionally print JSON for structured control)
- **Exit 2** - Block with stderr message shown to Claude as feedback

---

## Database Schema

Add column to `agent_ownership` table:

```sql
ALTER TABLE agent_ownership ADD COLUMN read_only_mode INTEGER DEFAULT 0;
ALTER TABLE agent_ownership ADD COLUMN read_only_config TEXT;  -- JSON for custom rules
```

**read_only_config JSON Schema**:
```json
{
  "blocked_patterns": [
    "*.py", "*.js", "*.ts", "*.vue",
    "CLAUDE.md", ".claude/*", ".env",
    "template.yaml", "*.sh"
  ],
  "allowed_patterns": [
    "content/*", "output/*", "reports/*", "*.log"
  ]
}
```

---

## API Endpoints

### GET /api/agents/{name}/read-only
Get read-only mode status and configuration.

**Response**:
```json
{
  "enabled": true,
  "config": {
    "blocked_patterns": ["*.py", "*.js", "CLAUDE.md", ".claude/*"],
    "allowed_patterns": ["content/*", "output/*"]
  }
}
```

### PUT /api/agents/{name}/read-only
Set read-only mode status and configuration.

**Request**:
```json
{
  "enabled": true,
  "config": {
    "blocked_patterns": ["*.py", "*.vue", "CLAUDE.md"],
    "allowed_patterns": ["content/*", "reports/*"]
  }
}
```

**Authorization**: Owner only (uses `can_user_share_agent` check, same as autonomy)

---

## Default Configuration

When read-only mode is enabled without custom config, use sensible defaults:

**Blocked by Default** (source code and configuration):
```
*.py, *.js, *.ts, *.jsx, *.tsx, *.vue, *.svelte
*.go, *.rs, *.rb, *.java, *.c, *.cpp, *.h
*.sh, *.bash, Makefile, Dockerfile
CLAUDE.md, README.md, .claude/*, .env, .env.*
template.yaml, *.yaml, *.yml, *.json, *.toml
```

**Allowed by Default** (output directories):
```
content/*, output/*, reports/*, exports/*
*.log, *.txt (in workspace root)
```

---

## Implementation Plan

### Phase 1: Backend Core
1. Add database columns (`read_only_mode`, `read_only_config`)
2. Add database operations in `db/agents.py`
3. Create service module `services/agent_service/read_only.py`
4. Add API endpoints to `routers/agents.py`
5. Create hook guard script template in `config/hooks/read-only-guard.py`

### Phase 2: Injection on Start
1. Modify `start_agent_internal()` in `lifecycle.py`
2. Add `inject_read_only_hooks()` function (after skill injection)
3. Use `AgentClient.write_file()` to inject:
   - `~/.trinity/read-only-config.json` - Configuration file
   - `~/.trinity/hooks/read-only-guard.py` - Guard script
   - `~/.claude/settings.local.json` - Hook registration (merge with existing)
4. **Important**: Claude Code captures a snapshot of hooks at startup, so hooks are injected before Claude Code starts in the container

### Phase 3: Frontend UI
1. Add "Read-Only Mode" toggle to AgentHeader (next to Autonomy toggle)
2. Add "Read-Only Settings" section in Settings tab
3. Pattern editor for blocked/allowed paths
4. Presets: "Strict" (block all code), "Standard" (block code + config), "Custom"

### Phase 4: Testing
1. Unit tests for guard script path matching
2. Integration tests for hook injection
3. E2E test: enable read-only, try to edit file, verify blocked

---

## Guard Script Implementation

**File**: `config/hooks/read-only-guard.py`

```python
#!/usr/bin/env python3
"""
Read-Only Mode Guard - PreToolUse hook for Trinity agents.
Blocks Write/Edit operations to protected paths.

Input: JSON via stdin with tool_input.file_path
Output: Exit 0 to allow, Exit 2 with stderr message to block

Reference: https://docs.anthropic.com/en/docs/claude-code/hooks
"""
import os
import sys
import json
import fnmatch

# Load config from file (written by platform on agent start)
CONFIG_PATH = os.path.expanduser("~/.trinity/read-only-config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    # Defaults if no config
    return {
        "blocked_patterns": ["*.py", "*.js", "CLAUDE.md", ".claude/*"],
        "allowed_patterns": ["content/*", "output/*", "reports/*"]
    }

def matches_any(path: str, patterns: list) -> bool:
    """Check if path matches any of the glob patterns."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Also check basename for patterns like "*.py"
        if fnmatch.fnmatch(os.path.basename(path), pattern):
            return True
    return False

def main():
    # Read JSON input from stdin (Claude Code hook protocol)
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Can't parse input, allow by default
        sys.exit(0)

    # Extract file_path from tool_input
    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        # No file path in input, allow
        sys.exit(0)

    # Normalize path (remove /home/developer prefix if present)
    if file_path.startswith("/home/developer/"):
        file_path = file_path[len("/home/developer/"):]

    config = load_config()
    blocked = config.get("blocked_patterns", [])
    allowed = config.get("allowed_patterns", [])

    # Allowed patterns take precedence
    if matches_any(file_path, allowed):
        sys.exit(0)  # Allow

    # Check blocked patterns
    if matches_any(file_path, blocked):
        # Exit 2 with stderr = block with feedback to Claude
        print(f"Read-only mode: Cannot modify '{file_path}' (protected path)", file=sys.stderr)
        sys.exit(2)

    # Default: allow
    sys.exit(0)

if __name__ == "__main__":
    main()
```

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `src/backend/db/agents.py` | Modify | Add `get_read_only_mode()`, `set_read_only_mode()` |
| `src/backend/database.py` | Modify | Add migration for new columns |
| `src/backend/services/agent_service/read_only.py` | Create | Service logic for read-only mode |
| `src/backend/services/agent_service/lifecycle.py` | Modify | Add `inject_read_only_hooks()` to startup |
| `src/backend/routers/agents.py` | Modify | Add read-only endpoints |
| `config/hooks/read-only-guard.py` | Create | Guard script template (copied to agent on start) |
| `src/frontend/src/components/ReadOnlyToggle.vue` | Create | Reusable toggle component |
| `src/frontend/src/components/AgentHeader.vue` | Modify | Add read-only toggle |
| `docs/memory/feature-flows/read-only-mode.md` | Create | Feature flow documentation |

**Files injected into agent container on start:**

| Agent Path | Source | Description |
|------------|--------|-------------|
| `~/.trinity/hooks/read-only-guard.py` | `config/hooks/read-only-guard.py` | Guard script |
| `~/.trinity/read-only-config.json` | Generated from DB | Blocked/allowed patterns |
| `~/.claude/settings.local.json` | Generated | Hook registration (merged with existing) |

---

## Security Considerations

1. **Hook Snapshot on Startup**: Claude Code captures a snapshot of hooks at startup and uses it throughout the session. This prevents malicious or accidental hook modifications from taking effect mid-session. Hooks must be injected **before** Claude Code starts in the container.

2. **Hook Persistence**: Hooks are injected on container start. If container restarts mid-session without going through `start_agent_internal()`, hooks might not be present. Mitigation: Store hooks in persistent volume.

3. **Hook Bypass**: User could potentially delete `.claude/settings.local.json` via terminal. **Superseded by #887 + ent#345** — the read-only hook is no longer registered there (it is baked into the root-owned `/etc/claude-code/managed-settings.json`, and read-only mode writes only a *config* file the hook reads), so deleting a user settings file disables nothing. Original mitigation:
   - Make hooks file read-only via file permissions
   - Claude Code warns users and requires review in `/hooks` menu before external changes apply
   - Consider using managed policy settings for stricter enforcement

4. **Pattern Escaping**: Ensure patterns are validated to prevent injection attacks in fnmatch.

5. **Owner-Only**: Only agent owner can toggle read-only mode (same auth as autonomy).

6. **Full User Permissions**: Hook scripts run with the container user's full permissions. The guard script should be minimal and well-audited.

---

## UI Mockup

```
┌─────────────────────────────────────────────────────────────┐
│  Agent: ruby-assistant                        [Running ●]   │
│                                                             │
│  [Toggle: Running]  [Toggle: AUTO]  [Toggle: Read-Only 🔒]  │
│                                                             │
└─────────────────────────────────────────────────────────────┘

Settings Tab:
┌─────────────────────────────────────────────────────────────┐
│  Read-Only Mode                                    [ON/OFF] │
│                                                             │
│  Preset: [Standard ▼]                                       │
│                                                             │
│  Blocked Paths:                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ *.py, *.js, *.ts, *.vue                             │   │
│  │ CLAUDE.md, .claude/*, .env                          │   │
│  │ template.yaml                                        │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  Allowed Paths (can still write here):                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ content/*, output/*, reports/*                      │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  [Save Configuration]                                       │
└─────────────────────────────────────────────────────────────┘
```

---

## Related Features

- **Autonomy Mode** (`autonomy-mode.md`): Similar per-agent toggle pattern
- **Skill Injection** (`skill-injection.md`): Similar injection-on-start pattern
- **Container Capabilities** (`container-capabilities.md`): Another per-agent security setting

---

## Alternative Approaches (Considered)

### Prompt-Based Hook (type: "prompt")
Instead of a shell script, use an LLM to evaluate each write:
```json
{
  "type": "prompt",
  "prompt": "Evaluate if this file write should be allowed. Protected paths: *.py, CLAUDE.md. $ARGUMENTS"
}
```
**Pros**: More flexible, can handle complex decisions
**Cons**: Slower (LLM call per write), costs tokens, non-deterministic
**Decision**: Use command hook for deterministic, fast enforcement

### Agent-Based Hook (type: "agent")
Spawn a subagent with tool access to verify writes:
**Pros**: Can inspect file contents, complex verification
**Cons**: Much slower, higher cost, overkill for path matching
**Decision**: Not needed for simple path-based restrictions

---

## Out of Scope (Future)

- **Bash Command Filtering**: Block specific bash commands (rm, mv, etc.)
- **MCP Tool Restrictions**: Block specific MCP tools (e.g., `mcp__filesystem__*`)
- **Time-Based Restrictions**: Read-only during certain hours
- **Approval Workflow**: Require human approval for blocked writes (use `permissionDecision: "ask"`)

---

## Testing

### Unit Tests (Guard Script)
```bash
# Test blocked path
echo '{"tool_input":{"file_path":"/home/developer/main.py"}}' | python3 read-only-guard.py
# Expected: Exit 2, stderr: "Read-only mode: Cannot modify 'main.py' (protected path)"

# Test allowed path
echo '{"tool_input":{"file_path":"/home/developer/content/output.txt"}}' | python3 read-only-guard.py
# Expected: Exit 0, no output

# Test empty input
echo '{}' | python3 read-only-guard.py
# Expected: Exit 0, no output
```

### Integration Tests
1. Create agent with read-only mode enabled
2. Start agent, verify hooks injected to `~/.claude/settings.local.json`
3. Ask agent to modify a protected file (e.g., "edit main.py")
4. Verify agent receives feedback and cannot modify
5. Ask agent to write to allowed path (e.g., "create content/report.txt")
6. Verify write succeeds

### Debug Mode
Run Claude Code with `--debug` flag to see hook execution:
```
[DEBUG] Executing hooks for PreToolUse:Write
[DEBUG] Found 1 hook matchers in settings
[DEBUG] Hook command completed with status 2: Read-only mode: Cannot modify...
```

---

## Acceptance Criteria

- [ ] Owner can enable/disable read-only mode via UI toggle
- [ ] Hooks are injected on agent start when mode is enabled
- [ ] Write/Edit to blocked paths shows clear error message to Claude
- [ ] Write/Edit to allowed paths (content/, output/) still works
- [ ] Custom path patterns can be configured
- [ ] Setting persists across agent restarts
- [ ] Feature documented in feature-flows
