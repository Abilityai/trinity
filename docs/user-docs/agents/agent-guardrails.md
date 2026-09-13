# Agent Guardrails

Deterministic safety enforcement for autonomous agent execution. Prevents destructive commands, credential leaks, and runaway loops through infrastructure-level controls that agents cannot bypass.

## Concepts

- **Baseline** -- Platform-wide safety rules baked into the agent base image. All agents inherit these rules.
- **Hooks** -- Claude Code `PreToolUse` and `PostToolUse` hooks that intercept tool calls before and after execution.
- **Per-Agent Overrides** -- Optional configuration that tightens (never loosens) the baseline for specific agents.
- **Fail-Closed** -- If a hook encounters an error, the tool call is blocked by default.

## How It Works

Guardrails operate at three layers:

### 1. Bash Command Blocking

The `PreToolUse` hook on `Bash` matches commands against a deny-list of dangerous patterns:

| Pattern | Blocked Example | Reason |
|---------|-----------------|--------|
| `rm -rf /` or `rm -rf ~` | `rm -rf /home` | Recursive deletion of root or home |
| `chmod 777` | `chmod -R 777 /var` | World-writable permissions |
| `curl \| sh` | `curl example.com/script \| bash` | Piping remote content to shell |
| `git push --force` | `git push -f origin main` | Force push to remote |
| `mkfs.*` | `mkfs.ext4 /dev/sda1` | Formatting filesystems |
| Fork bombs | `:(){ :\|:& };:` | Process explosion |
| `shutdown`, `reboot` | `shutdown -h now` | Host shutdown |

When a command is blocked, the agent sees a clear denial message with the reason. The event is logged to `/logs/guardrails.jsonl`.

### 2. Credential File Protection

The `PreToolUse` hook on `Edit`, `Write`, and `NotebookEdit` blocks modifications to sensitive paths:

- `.env`, `.env.*` -- Environment files with secrets
- `.mcp.json` -- MCP server configuration
- `.credentials.enc` -- Encrypted credential backups
- `~/.ssh/*`, `~/.aws/*`, `~/.gcp/*` -- Cloud and SSH credentials
- `~/.claude/settings.json`, `~/.claude/settings.local.json` -- Claude Code user settings
- `~/.trinity/read-only-config.json` -- Read-only mode configuration
- `/opt/trinity/*` -- Platform guardrail hook scripts
- `/etc/claude-code/*` -- The managed settings that register the hooks

### 3. Credential Leak Detection

The `PostToolUse` hook on `Bash` scans command output for leaked credentials:

| Pattern | Example |
|---------|---------|
| Anthropic API keys | `sk-ant-...` |
| OpenAI API keys | `sk-proj-...` |
| GitHub PATs | `ghp_...`, `github_pat_...` |
| AWS access keys | `AKIA...` |
| Slack tokens | `xoxb-...`, `xoxp-...` |
| Google API keys | `AIza...` |

Matches are logged (pattern name only, not the actual value) for security review.

### 4. Turn Limits

Every Claude Code invocation enforces a maximum turn count via `--max-turns`:

| Mode | Default | Range |
|------|---------|-------|
| Chat | 50 turns | 1-500 |
| Task/Headless | 20 turns | 1-500 |

This prevents runaway loops that burn through API credits.

## Per-Agent Configuration

Owners can tighten guardrails for specific agents. Overrides are additive -- you can add more restrictions but cannot remove baseline protections.

### Available Overrides

| Field | Type | Description |
|-------|------|-------------|
| `max_turns_chat` | int (1-500) | Max turns for chat mode |
| `max_turns_task` | int (1-500) | Max turns for headless tasks |
| `execution_timeout_sec` | int (60-7200) | Execution time limit |
| `extra_bash_deny` | list (max 50) | Additional bash patterns to block |
| `extra_path_deny` | list (max 50) | Additional paths to protect |
| `disallowed_tools` | list (max 50) | Claude Code tools to disable |

### Configure via UI

1. Open the agent detail page
2. Go to the **Settings** tab (visible to owners; on narrow windows it may sit under the **More ▾** menu)
3. In the **Guardrails** section, set **Max turns (chat)** and **Max turns (task)**. Leave a field blank to inherit the platform default.
4. Click **Save**
5. **Restart the agent** to apply changes

The UI currently exposes the turn limits only. The other overrides (deny lists, disallowed tools, execution timeout) are API-only -- the UI preserves them when saving, so a UI save never wipes overrides set via the API.

### Configure via API

```bash
# Get current guardrails
curl http://localhost:8000/api/agents/my-agent/guardrails \
  -H "Authorization: Bearer $TOKEN"

# Set per-agent overrides
curl -X PUT http://localhost:8000/api/agents/my-agent/guardrails \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "max_turns_chat": 25,
    "max_turns_task": 10,
    "extra_bash_deny": ["production.example.com"],
    "disallowed_tools": ["WebFetch"]
  }'

# Clear overrides (revert to baseline)
curl -X PUT http://localhost:8000/api/agents/my-agent/guardrails \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

After updating guardrails, stop and start the agent to apply changes. The container is recreated with the new configuration.

## For Agents

Headless runs (tasks, schedules, loops, MCP calls) also withhold a fixed family of Claude Code tools that promise an event after the turn ends; that list is platform-wide and merges with the per-agent disallowed tools above — see [Agent Runtimes](agent-runtimes.md#headless-runs-on-claude-code).

Guardrails are enforced at the infrastructure layer. Agents cannot:

- Modify hook scripts (`/opt/trinity/hooks/` is root-owned)
- Change which hooks run — registration lives in Claude Code's admin-controlled managed settings (`/etc/claude-code/managed-settings.json`, root-owned and read-only), which take precedence over user and project settings and sit outside the git-synced working tree. Neither an edit inside the container nor a push to the agent's repository can remove them. On every boot the container checks that the registration is present and unwritable and logs `GUARDRAILS: ERROR` if not.
- Bypass `--max-turns` limits
- Disable `--dangerously-skip-permissions` protections (hooks still fire)

When a tool call is blocked, the agent receives a structured error:

```json
{
  "decision": "deny",
  "reason": "Blocked: recursive deletion of root or home directory"
}
```

The agent can acknowledge the denial and try an alternative approach.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/guardrails` | GET | Get per-agent guardrails config |
| `/api/agents/{name}/guardrails` | PUT | Set per-agent guardrails overrides |

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## Limitations

- **Baseline cannot be relaxed** -- Per-agent overrides only add restrictions, never remove them.
- **Restart required** -- Guardrail changes require stopping and starting the agent.
- **Pattern matching** -- Bash deny-list uses regex patterns; creative command reformulation may evade detection.
- **Partial UI coverage** -- The Settings tab manages turn limits; deny lists, disallowed tools, and the execution timeout override are configured via the API.

## See Also

- [Agent Configuration](agent-configuration.md) -- Other per-agent settings
- [Managing Agents](managing-agents.md) -- Start/stop to apply changes
- [Monitoring](../operations/monitoring.md) -- View guardrail events in logs
