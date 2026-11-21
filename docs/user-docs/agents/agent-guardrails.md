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

On an agent with [gated skills](../guides/keeping-agents-in-bounds.md), the deny-list also refuses starting a coding-agent CLI: `claude`, `codex` or `gemini` as a command, for example `claude -p "hello"` or `cd repo && codex exec "fix it"`. Naming one is fine (`ls .claude`, `grep claude notes.md`, `which codex`), and so is a command that starts no session: `--version`, `--help`, `mcp`, and `plugin` on `claude` and `codex`. Adding `-p` or `--print` to `mcp` or `plugin` starts a session, so that is refused. The rule applies to the whole agent while it has at least one gate, from the moment the gate is saved, and per-agent overrides cannot remove it. An agent with no gates is not affected. Any skill on a gated agent that runs `claude -p` for a sub-step stops working, not only the gated one; rewrite that step as described in the [Recommended Trinity Prompt](recommended-fleet-prompt.md#what-the-trinity-prompt-is).

### 2. Credential File Protection

The `PreToolUse` hook on `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` blocks modifications to sensitive paths:

- `.env`, `.env.*` -- Environment files with secrets. The one exception is `.env.example`, the names-only template every `${VAR}` an agent uses must appear in: the agent may write it. A symlinked `.env.example`, or one matched by an owner's `extra_path_deny`, is still refused
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

### 4. A Shared Person's Turn

When someone the agent is shared with is chatting with it, a `PreToolUse` hook on `Read`, `Grep`, `Glob`, `NotebookRead`, `Write`, `Edit`, `MultiEdit` and `NotebookEdit` holds the file tools to the paths that person could open in the [Files tab](agent-files.md), and the `Bash` tool is refused. The hook asks the platform whose turn is running; only an owner-tier turn (the owner, an admin, or work the owner set up) gets past an owner-tier path or the shell. If the platform cannot answer, the call is refused. What a shared person's turn may do is described in [Agent Sharing](../sharing-and-access/agent-sharing.md#what-the-agent-does-for-someone-you-share-it-with).

Gemini CLI and Codex run no Claude Code hooks, so on those runtimes the agent refuses a shared person's turn before the CLI starts, with a message saying the owner can switch the agent to Claude Code.

### 5. Turn Limits

Every Claude Code invocation enforces a maximum turn count via `--max-turns`. Gemini CLI and Codex have no turn cap, so on those runtimes the limit is logged and the execution timeout bounds the run:

| Mode | Default | Range |
|------|---------|-------|
| Chat | 200 turns | 1-500 |
| Task/Headless | 200 turns | 1-500 |

This prevents runaway loops that burn through API credits.

### How the Hooks Run

Each hook runs as a fixed command, not through a shell. It starts with an empty environment under an isolated Python interpreter that ignores user site-packages. A shell prefix or a startup file the agent sets therefore cannot run code ahead of the hook. The image build runs every registered hook exactly as it is registered, so a broken hook fails the build instead of every tool call.

The hooks are Claude Code hooks. Codex enforces read-only mode through its own sandbox, and Gemini CLI turns do not pass through these hooks — see [Agent Runtimes](agent-runtimes.md).

## Tool Access

**Settings › Guardrails › Tool access** answers "what may this agent use?" with one choice. Each profile sets read-only mode and the list of tools the agent can't use together, so the two can never disagree:

| Profile | Read-only | What the agent can't do |
|---------|-----------|-------------------------|
| **Full access** (default) | Off | Nothing beyond the platform's guardrails |
| **Workspace-only** | Off | Send messages, group messages, notifications, voice replies, calls, room posts or file links to people or channels. It still replies in its own chats and channels (text only), files reports, asks you questions and works with other agents |
| **Read-only + report** | On | The same sends, plus changing its code, instructions or config files. It can still write to `content/`, `output/`, `reports/` and `exports/`, and delivers its work as reports and text replies |
| **Strict read-only** | On | Everything Read-only + report blocks, plus filing reports. It still replies in its own chats (text only) and can ask you a question |
| **Custom** | Your choice | Exactly the tools you list, for example `Bash`, `Write`, `Edit`, `WebFetch`, `WebSearch`; a Trinity tool is written `mcp__trinity__<tool>` |

Each card states what the profile actually enforces on the agent's runtime:

- **Sends** (the seven send tools, and `report` where the profile removes it) are refused by Trinity's own backend on every runtime, not only hidden from the agent.
- **Read-only** is enforced on Claude Code by the file-tool hook (shell commands are not covered) and on Codex by its sandbox (which blocks every shell command, reads included). Gemini CLI does not enforce it.
- **Other removed tools** are taken off the tool list on Claude Code only.

Read-only mode **is** the Strict read-only profile. The **Read-only** toggle in the agent header and on the Dashboard list is its shortcut: turning it on selects Strict read-only, and turning it off returns to the profile the agent had before (Full access if none). Turning it on from a Custom list that removes tools Strict read-only allows is refused, naming those tools, so the toggle never loosens anything silently.

The read-only part applies at once; the tool list applies when the agent restarts, which the bar at the top of Settings offers. An agent created before profiles existed shows the profile that matches what it enforces, and nothing changes until you save. If what is enforced does not match the chosen profile, its card is marked **Not applied** (for a read-only agent from before profiles, **Send block not applied**) with an **Apply** button. Custom remembers its settings while another profile is selected. Every change is recorded in the audit log with the before and after state. The system agent's tool access is managed by the platform.

## Visitors and the Shell

Under **Settings › Guardrails › Bounds › Manage rules**, the **Channel and public visitors may run commands** switch decides what the agent may do for conversations from Slack, Telegram, WhatsApp, a public link, voice or a paid call:

- **On** (the default, so channel agents keep working as before): those conversations can use the shell, schedule standing work, and a Gemini or Codex agent answers them. The shell reaches everything the agent can, so turn this off if those visitors should not.
- **Off**: they get answers without running commands. On a Gemini or Codex agent, a visitor's message is refused; a public-link visitor sees *This agent isn't taking conversations from public links right now. Please contact its owner.*

The switch saves as soon as you change it and applies to the next message, with no restart. People you share the agent with never get the shell, whatever this switch says, except the agent's primary person (below).

### The Agent's Primary Person

On an instance that assigns people to agents, the person an admin seated as **primary** has the agent work in full for them: commands, its own playbooks, the tools you added, and schedules, loops, reminders and events set up for them, on every runtime and when another agent calls it during their turn. They still cannot change the agent's settings, credentials, instructions, keys or sharing, and neither can the agent during their turn.

The **The agent works in full for its primary person** switch, below the visitor switch, turns this off for one agent (on by default). Off, the primary is treated like anyone else the agent is shared with, and a refusal says the owner turned it off. It saves on change and applies to the next message, with no restart. The switch appears only where people can be assigned to agents.

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
| `disallowed_tools` | list (max 50) | Claude Code tools to disable. Normally set through a [tool access profile](#tool-access) |
| `visitor_shell` | bool | Whether channel and public-link visitors may run commands ([Visitors and the Shell](#visitors-and-the-shell)); default on |
| `primary_reach` | bool | Whether the agent works in full for its primary person ([The Agent's Primary Person](#the-agents-primary-person)); default on |

A task that stops at its turn limit fails with HTTP `422` and the error code `max_turns` (on the `X-Trinity-Error-Code` response header), and the execution's error reads *Task exceeded turn limit*. This applies to whichever cap stopped the task: the `max_turns_task` guardrail, or a tighter per-task `max_turns` sent with `POST /api/agents/{name}/task`. Retrying the same task stops at the same limit. The error text names the cap that bound and its remedy: if the guardrail stopped it, raise `max_turns_task`; if the caller's per-task `max_turns` stopped it, send a larger per-task value (it may not exceed the guardrail). Either way, splitting the work into smaller tasks also helps.

### Configure via UI

1. Open the agent detail page
2. Go to the **Settings** tab (visible to owners; it is pinned at the end of the tab strip, never under **More ▾**) and pick **Guardrails** in the rail (`?tab=settings&section=guardrails`)
3. The **Guardrail rules** row under **Bounds** summarises the current limits (*Max turns: chat N · task N*, or *default*). Click **Manage rules** to open the editor below the row, then set **Max turns (chat)** and **Max turns (task)**. Leave a field blank to inherit the platform default.
4. Click **Save**
5. **Restart the agent** — the save is listed as **Guardrail rules** in the bar at the top of Settings, with a **Restart now** button

The rules editor exposes the turn limits, the visitor switch and, where people can be assigned to agents, the primary person switch. Disallowed tools are set through [Tool access](#tool-access) above; the extra deny lists are API-only. A UI save preserves both, so it never wipes overrides set via the API. The execution timeout has its own control under **Settings › General › Execution** (see [Agent Configuration](agent-configuration.md#execution-timeout)).

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
| `/api/agents/{name}/tool-access` | GET | The agent's tool access profile, whether it is applied, and the five cards with each one's coverage line for the agent's runtime (owner tier) |
| `/api/agents/{name}/tool-access` | PUT | Select a profile — body `{profile, custom?: {read_only, disallowed_tools}}`, `profile` one of `full`, `workspace_only`, `read_only_report`, `strict_read_only`, `custom`. The owner or an admin, or another agent holding **Manage other agents** (never the agent on itself). A change that races another answers `409` |
| `/api/agents/{name}/read-only` | PUT | The read-only shortcut (on selects Strict read-only, off returns to the previous profile) |

No MCP tool reads or changes a tool access profile.

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## Limitations

- **Baseline cannot be relaxed** -- Per-agent overrides only add restrictions, never remove them.
- **Restart required** -- Guardrail changes require stopping and starting the agent; the bar at the top of Settings lists the pending save and restarts the agent for you. The rule for gated skills is the exception: it follows the agent's gates with no restart.
- **Pattern matching** -- Bash deny-list uses regex patterns; creative command reformulation may evade detection. The patterns do not parse quotes, so text inside a quoted string or heredoc that reads like a command can be refused too.
- **Claude Code only** -- The hooks and the turn limit apply to Claude Code. Codex honours read-only mode through its sandbox; Gemini CLI turns are not covered.
- **Partial UI coverage** -- Settings › Guardrails manages tool access, turn limits and the visitor switch, and Settings › General the execution timeout; the extra deny lists are configured via the API.

## See Also

- [Agent Configuration](agent-configuration.md) -- Other per-agent settings
- [Managing Agents](managing-agents.md) -- Start/stop to apply changes
- [Monitoring](../operations/monitoring.md) -- View guardrail events in logs
- [How Trinity Keeps Agents in Bounds](../guides/keeping-agents-in-bounds.md) -- The guardrails approach: where the real boundary sits, and what is available now
- [Recommended Trinity Prompt](recommended-fleet-prompt.md) -- Fleet rules for behaviour that guardrails can't block
