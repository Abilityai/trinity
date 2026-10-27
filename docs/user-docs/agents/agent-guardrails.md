# Agent Guardrails

Deterministic safety enforcement inside the agent's container. Catches destructive commands, credential leaks, and runaway loops before they happen. These hooks are defence in depth against accidents, not the security boundary: the agent user has passwordless `sudo` in its own container, so a determined agent could get around them. The boundary that holds is outside the container: the credentials the agent was given, the platform calls its key may make, and the settings only a person can change. See [How Trinity Keeps Agents in Bounds](../guides/keeping-agents-in-bounds.md).

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
| `dd of=/dev/sd*` | `dd if=image of=/dev/sda` | Writing to a raw block device |
| `kill -9 1` | `kill -9 1` | Killing the init process |
| Fork bombs | `:(){ :\|:& };:` | Process explosion |
| `shutdown`, `reboot` | `shutdown -h now` | Host shutdown |
| Writing `.trinity/read-only-config.json` | `echo '{}' > ~/.trinity/read-only-config.json` | Changing read-only mode's configuration |
| `sudo` naming `/etc/claude-code`, `/opt/trinity`, `/etc/sudoers`; `visudo` | `sudo tee /etc/claude-code/managed-settings.json` | Rewriting the guardrail registration, hooks or sudoers |
| Root shells | `sudo -i`, `sudo su`, `su -` | Interactive root shell |

When a command is blocked, the agent sees a clear denial message with the reason. The event is logged to `/logs/guardrails.jsonl`.

### 2. Credential File Protection

The `PreToolUse` hook on `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` blocks modifications to sensitive paths:

- `.env`, `.env.*` -- Environment files with secrets
- `.mcp.json` -- MCP server configuration
- `.credentials.enc` -- Encrypted credential backups
- `~/.ssh/*`, `~/.aws/*`, `~/.gcp/*` -- Cloud and SSH credentials
- `~/.claude/settings.json`, `~/.claude/settings.local.json` -- Claude Code user settings
- `~/.claude.json`, `~/.claude/.credentials.json` -- Claude Code's configuration and login
- `~/.gemini/settings.json` -- Gemini runtime configuration
- `~/.tmp/codex/*` -- Codex runtime login and MCP configuration
- `~/.trinity/read-only-config.json` -- Read-only mode configuration fallback, used while the root-owned copy under `/opt/trinity/` is missing
- `/opt/trinity/*` -- Platform guardrail hook scripts
- `/etc/claude-code/*` -- The managed settings that register the hooks

### 3. Credential Leak Detection

The `PostToolUse` hook on `Bash` scans command output for leaked credentials:

| Pattern | Example |
|---------|---------|
| Anthropic API keys and OAuth tokens | `sk-ant-...`, `sk-ant-oat01-...` |
| OpenAI API keys | `sk-proj-...` |
| GitHub PATs | `ghp_...`, `github_pat_...` |
| AWS access keys | `AKIA...` |
| Slack tokens | `xoxb-...`, `xoxp-...` |
| Google API keys | `AIza...` |

Matches are logged (pattern name only, not the actual value) for security review.

### 4. Turn Limits

Every Claude Code invocation enforces a maximum turn count via `--max-turns`. Gemini CLI and Codex have no turn cap, so on those runtimes the limit is logged and the execution timeout bounds the run:

| Mode | Default | Range |
|------|---------|-------|
| Chat | 200 turns | 1-500 |
| Task/Headless | 200 turns | 1-500 |

This prevents runaway loops that burn through API credits.

### How the Hooks Run

Each hook runs as a fixed command, not through a shell. It starts with an empty environment under an isolated Python interpreter that ignores user site-packages. A shell prefix or a startup file the agent sets therefore cannot run code ahead of the hook. The image build runs every registered hook exactly as it is registered, so a broken hook fails the build instead of every tool call.

The hooks are Claude Code hooks. Codex enforces read-only mode through its own sandbox, and Gemini CLI turns do not pass through these hooks — see [Agent Runtimes](agent-runtimes.md).

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

A task that stops at `max_turns_task` fails with HTTP `422` and the error code `max_turns` (on the `X-Trinity-Error-Code` response header), and the execution's error reads *Task exceeded turn limit*. Retrying the same task stops at the same limit: raise `max_turns_task` or split the work into smaller tasks.

### Configure via UI

1. Open the agent detail page
2. Go to the **Settings** tab (visible to owners; it is pinned at the end of the tab strip, never under **More ▾**) and pick **Guardrails** in the rail (`?tab=settings&section=guardrails`)
3. The **Guardrail rules** row under **Bounds** summarises the current limits (*Max turns: chat N · task N*, or *default*). Click **Manage rules** to open the editor below the row, then set **Max turns (chat)** and **Max turns (task)**. Leave a field blank to inherit the platform default.
4. Click **Save**
5. **Restart the agent** — the save is listed as **Guardrail rules** in the bar at the top of Settings, with a **Restart now** button

The UI exposes the turn limits only. Deny lists and disallowed tools are API-only -- the UI preserves them when saving, so a UI save never wipes overrides set via the API. The execution timeout has its own control under **Settings › General › Execution** (see [Agent Configuration](agent-configuration.md#execution-timeout)).

The same pane reads everything else that bounds the agent: [permissions to change itself](agent-configuration.md#permissions-to-change-itself), how many of its skills need approval (**Open Skills** to change them), the autonomy switch (read-only here; the chip in the header is the control), cross-model validation when the instance has the matching entitlement, and the [dispatch circuit breaker](agent-configuration.md#dispatch-circuit-breaker).

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
- Change which hooks run — registration lives in Claude Code's admin-controlled managed settings (`/etc/claude-code/managed-settings.json`, root-owned and read-only), which take precedence over user and project settings and sit outside the git-synced working tree. Neither an edit inside the container nor a push to the agent's repository can remove them. The agent user has passwordless `sudo`, so a determined agent can still rewrite the file; the Bash deny-list refuses the obvious `sudo` commands against it. On every boot the container checks that the registration is present and unwritable and logs `GUARDRAILS: ERROR` if not, and the agent's `/health` reports `guardrails_registration` on every request.
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
- **Restart required** -- Guardrail changes require stopping and starting the agent; the bar at the top of Settings lists the pending save and restarts the agent for you.
- **Pattern matching** -- Bash deny-list uses regex patterns; creative command reformulation may evade detection.
- **Claude Code only** -- The hooks and the turn limit apply to Claude Code. Codex honours read-only mode through its sandbox; Gemini CLI turns are not covered.
- **Partial UI coverage** -- Settings › Guardrails manages turn limits and Settings › General the execution timeout; deny lists and disallowed tools are configured via the API.

## See Also

- [Agent Configuration](agent-configuration.md) -- Other per-agent settings
- [Managing Agents](managing-agents.md) -- Start/stop to apply changes
- [Monitoring](../operations/monitoring.md) -- View guardrail events in logs
- [How Trinity Keeps Agents in Bounds](../guides/keeping-agents-in-bounds.md) -- The guardrails approach: where the real boundary sits, and what is available now
- [Recommended Trinity Prompt](recommended-fleet-prompt.md) -- Fleet rules for behaviour that guardrails can't block
