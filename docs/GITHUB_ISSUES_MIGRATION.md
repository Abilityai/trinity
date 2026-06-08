# GitHub Issues Migration Plan

> **Status**: Migration complete. Roadmap is now tracked via GitHub Issues and the **Trinity Roadmap** project board. The old `docs/memory/roadmap.md` has been archived to `docs/memory/roadmap-archive.md`. See `CONTRIBUTING.md` for the current SDLC.
>
> Original purpose: Migrate from `docs/memory/roadmap.md` to GitHub Issues for roadmap tracking.

## Labels Created

| Label | Color | Description |
|-------|-------|-------------|
| `priority-p0` | Red | Blocking/urgent |
| `priority-p1` | Orange | Critical path |
| `priority-p2` | Yellow | Important |
| `priority-p3` | Green | Nice-to-have |
| `type-feature` | Blue | New functionality |
| `type-bug` | Red | Bug fix |
| `type-refactor` | Purple | Code improvement |
| `type-docs` | Blue | Documentation |
| `status-in-progress` | Yellow | Currently being worked on |
| `status-blocked` | Gray | Has dependencies |

## Issues to Create

### Phase 9: Deep Agent Core Infrastructure (In Progress)

| Title | Labels | Description |
|-------|--------|-------------|
| Task Actions UI | `priority-p3`, `type-feature` | Manual complete/fail/re-run buttons for tasks |
| Failure Recovery | `priority-p2`, `type-feature` | Plan restructuring when steps fail (add debug nodes) |
| Sentinel/Watchdog Agents | `priority-p2`, `type-feature` | Low-cost monitors detecting infinite loops, human escalation |
| Thinking Budget | `priority-p2`, `type-feature` | Cost limits per reasoning task ($X max spend) |

### Phase 10: Memory & Observability (In Progress)

| Title | Labels | Description |
|-------|--------|-------------|
| Memory Folding | `priority-p1`, `type-feature` | Periodic context compression to summary files |
| Reasoning Chain Logs | `priority-p2`, `type-feature` | Capture "why" decisions, not just tool calls |
| Cognitive Rollback | `priority-p2`, `type-feature` | Git-based agent state restoration |
| Vectorized Episodic Memory | `priority-p3`, `type-feature` | Auto-store conversations for "Have I solved this before?" |

### Phase 11: Ecosystem & Enterprise (In Progress)

| Title | Labels | Description |
|-------|--------|-------------|
| Audit Trail System (SEC-001) | `priority-p0`, `type-feature` | Comprehensive audit logging for user/agent actions. Append-only audit_log table, MCP tool call tracking, hash chain for tamper evidence. |
| System Manifest UI | `priority-p2`, `type-feature` | Upload YAML, view deployment results, group agents by system prefix |
| A2A Protocol Support | `priority-p3`, `type-feature` | Agent discovery and negotiation across boundaries |
| Agent collaboration execution tracking | `priority-p3`, `type-feature` | Extend schedule_executions |
| Automated sync modes | `priority-p3`, `type-feature` | GitOps enhancement (scheduled, on-stop) |
| Automated secret rotation | `priority-p3`, `type-feature` | Security enhancement |

### Phase 11.5: Content Management (In Progress)

| Title | Labels | Description |
|-------|--------|-------------|
| Create Folder in File Manager | `priority-p2`, `type-feature` | Create new directories in agent workspace |

### Phase 12: Agent Perception & Attention

| Title | Labels | Description |
|-------|--------|-------------|
| Event Bus Infrastructure | `priority-p1`, `type-feature` | Platform-wide pub/sub system for agent event broadcasting and subscription |
| Event Types & Schema | `priority-p1`, `type-feature` | Define standard event types (task_completed, anomaly_detected, etc.) |
| Agent Event Subscriptions | `priority-p2`, `type-feature` | Agents declare interest in event types via template.yaml or runtime API |
| Event Persistence & Replay | `priority-p2`, `type-feature` | Store events for late-joining agents and debugging |
| Attention Amplification Pattern | `priority-p1`, `type-feature` | Cognitive pattern: agents select salient events and amplify to others |
| Salience Scoring | `priority-p2`, `type-feature` | Agents assign importance scores to perceived events |
| Broadcast Amplification | `priority-p2`, `type-feature` | High-salience events re-broadcast with amplification metadata |
| Attention Cascade Detection | `priority-p3`, `type-feature` | Platform detects when multiple agents amplify same event |
| Attention Dashboard | `priority-p3`, `type-feature` | Visualize event flow and amplification patterns |

### Phase 13: Agent Scalability

| Title | Labels | Description |
|-------|--------|-------------|
| Horizontal Agent Scalability | `priority-p1`, `type-feature` | Agent pools with N instances, load balancing, auto-scaling |
| Event Handlers & Reactions | `priority-p1`, `type-feature` | Configure automatic agent reactions to events from permitted agents |

### Phase 16: Agent-as-a-Service (In Progress)

| Title | Labels | Description |
|-------|--------|-------------|
| Client/Viewer User Role (AUTH-002) | `priority-p0`, `type-feature` | Simplified user role. Sees only basic UI. Enables sharing agents with clients. |
| Subscription Management (SUB-001) | `priority-p0`, `type-feature` | Centralized Claude Max/Pro subscription registry. MCP workflow. |
| Unified Executions Dashboard (EXEC-022) | `priority-p0`, `type-feature` | Combined view of all executions across all agents. Filter, real-time updates. |
| MCP Execution Query Tools (MCP-007) | `priority-p0`, `type-feature` | list_recent_executions, get_execution_result, get_agent_activity_summary |
| Quick Instance Deploy | `priority-p2`, `type-feature` | One-command agent creation from template with credentials |
| Execution Notifications | `priority-p2`, `type-feature` | Notify on task completion (success/failure). Webhook + UI toast. |
| Client Usage Dashboard | `priority-p2`, `type-feature` | Per-client view: executions, costs, outcomes |
| Credential Usage Audit | `priority-p2`, `type-feature` | Log when credentials read, log external API calls |
| Monorepo Workspace Deploy | `priority-p2`, `type-feature` | Deploy entire directory of agents as cohesive system |
| Platform Simplification Mode | `priority-p3`, `type-feature` | Hide advanced features behind toggle or role |

### Backlog Items

| Title | Labels | Description |
|-------|--------|-------------|
| Agent Notification Endpoint | `priority-p2`, `type-feature` | POST /api/agents/{name}/notify - explicit push notifications |
| Trinity Connect Skill Template | `priority-p3`, `type-feature` | Packaged skill for local Claude Code with listener script |
| Agent Skills Spec Alignment | `priority-p1`, `type-feature` | Align SKILL.md format with https://agentskills.io |
| Platform Memory Primitives | `priority-p1`, `type-feature` | 3-tier memory (Episodic→Semantic→Procedural) |
| Code Execution Mode | `priority-p2`, `type-feature` | Agents write code to interact with MCP servers (98% token reduction) |
| Smart Model Routing | `priority-p2`, `type-feature` | Route queries by complexity: haiku→sonnet→opus |
| Orphaned Execution Recovery | `priority-p1`, `type-feature` | On scheduler startup, mark all "running" executions as failed |
| MEMORY.md Convention | `priority-p2`, `type-feature` | Agents maintain curated long-term memory file |
| Telegram Channel Integration | `priority-p2`, `type-feature` | Mobile-first interface via Telegram bot |
| Create Schedule from Task | `priority-p3`, `type-feature` | Add icon in Tasks tab to create schedule from execution |
| Process Designer UI | `priority-p2`, `type-feature` | Visual drag-and-drop process builder |
| Claude Code Hooks for State Persistence | `priority-p1`, `type-feature` | Use .claude/hooks/ to persist agent work state |
| Convoy-like Work Bundles | `priority-p2`, `type-feature` | Group related tasks for multi-agent coordination |
| Ephemeral Agent Spawning | `priority-p2`, `type-feature` | Let System Agent spawn temporary worker agents on-demand |
| Ready Work Discovery MCP Tool | `priority-p3`, `type-feature` | find_ready_work() tool to discover unblocked tasks |
| Git Worktrees for Task Isolation | `priority-p1`, `type-feature` | Each execution gets its own git worktree/branch |
| MCP as Primary Integration Point | `priority-p1`, `type-feature` | Local agents connect via MCP, pull skills, minimal setup |
| Agent Org Structures (Hierarchy) | `priority-p2`, `type-feature` | Parent-child agent relationships |
| Any Repo / Empty Agent Creation | `priority-p2`, `type-feature` | Create agent from any GitHub repo or empty |
| Empty Agent → GitHub Safety | `priority-p2`, `type-feature` | Block "Initialize GitHub" when workspace is empty |
| Centralized MCP Server Management | `priority-p2`, `type-feature` | UI and MCP tools to manage MCP server connections |
| Agent History in GitHub | `priority-p3`, `type-feature` | Store agent history in git repo |
| Dashboard Quick Chat/Terminal | `priority-p2`, `type-feature` | Popup terminal or chat modal from agent card |

## Migration Commands

Run these commands to create all issues:

```bash
# Phase 9 items
gh issue create --repo abilityai/trinity --title "Task Actions UI" --label "priority-p3,type-feature" --body "Manual complete/fail/re-run buttons for tasks in the Plans UI."

gh issue create --repo abilityai/trinity --title "Failure Recovery" --label "priority-p2,type-feature" --body "Plan restructuring when steps fail. Add debug nodes, recovery strategies."

gh issue create --repo abilityai/trinity --title "Sentinel/Watchdog Agents" --label "priority-p2,type-feature" --body "Low-cost monitors detecting infinite loops, human escalation."

gh issue create --repo abilityai/trinity --title "Thinking Budget" --label "priority-p2,type-feature" --body "Cost limits per reasoning task (\$X max spend)."

# Phase 16 P0 items (highest priority)
gh issue create --repo abilityai/trinity --title "Client/Viewer User Role (AUTH-002)" --label "priority-p0,type-feature" --body "Simplified user role using existing auth. Sees only basic UI (agent list, terminal, files). Hides advanced features. Enables sharing agents with paying clients. Spec: docs/requirements/CLIENT_VIEWER_USER_ROLE.md"

gh issue create --repo abilityai/trinity --title "Subscription Management (SUB-001)" --label "priority-p0,type-feature" --body "Centralized Claude Max/Pro subscription registry. MCP workflow: authenticate locally, register via register_subscription, assign to agents. Spec: docs/requirements/SUBSCRIPTION_MANAGEMENT.md"

gh issue create --repo abilityai/trinity --title "Unified Executions Dashboard (EXEC-022)" --label "priority-p0,type-feature" --body "Combined view of all executions across all agents. Filter by agent, time, trigger type. Real-time WebSocket updates. Click through to details. Spec: docs/requirements/UNIFIED_EXECUTIONS_DASHBOARD.md"

gh issue create --repo abilityai/trinity --title "MCP Execution Query Tools (MCP-007)" --label "priority-p0,type-feature" --body "list_recent_executions - query what happened across fleet. get_execution_result - fetch specific output. get_agent_activity_summary - high-level status. Spec: docs/requirements/MCP_EXECUTION_QUERY_TOOLS.md"

gh issue create --repo abilityai/trinity --title "Audit Trail System (SEC-001)" --label "priority-p0,type-feature" --body "Comprehensive audit logging for user/agent actions with full actor attribution. Append-only audit_log table, MCP tool call tracking, hash chain for tamper evidence. Spec: docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md"
```

## After Migration

1. **Keep roadmap.md as historical reference** - rename to `roadmap-archive.md`
2. **Update CLAUDE.md** - change roadmap reference to use `/roadmap` skill
3. **Create GitHub Project** (optional) - for visual Kanban/roadmap view
4. **Set up milestones** (optional) - for release planning

## New Workflow

| Old Way | New Way |
|---------|---------|
| Read `docs/memory/roadmap.md` | Run `/roadmap` |
| Edit roadmap.md to add item | Run `/roadmap create <title>` |
| Mark item complete in file | Close issue on GitHub |
| Check priorities | Run `/roadmap` (shows P0/P1) |

---

## 2026-06-03 — Project board (#6) archived; issues-only SDLC (#1042)

The interim step above ("Create GitHub Project — for visual Kanban/roadmap
view") was tried and then **retired**. GitHub Project #6 ("Trinity
Roadmap") was **archived** on 2026-06-03. Prioritization metadata is now
carried entirely on **labels + native sub-issues** — there is no project
board. Full rationale and migration plan:
`docs/planning/PROJECT_BOARD_DEPRECATION_2026-05.md`.

**What changed:**

| Board field (retired) | Replacement |
|-----------------------|-------------|
| Theme (single-select) | `theme-*` labels (`theme-reliability`, `theme-ui-ux`, `theme-security`, `theme-channels`, `theme-devex`, `theme-monetization`, `theme-infrastructure`) |
| Epic (single-select) | Issues labeled `type-epic` + native GitHub **sub-issue** links (automatic rollup) |
| Complexity (number) | `complexity-low` / `complexity-medium` / `complexity-high` labels |
| Tier (P1a/P1b/P1c) | **Dropped** — fake precision, ~0% adoption |
| Rank (number) | **Dropped** — replaced by a written ordering rule |

**Ordering rule** (replaces numeric Rank): P0 first; then P1 with
`type-bug` before `type-feature` and, within a type, **newest issue number
first**; then P2/P3. Theme focus (`CLAUDE.md` → "Current Product Focus") is
a tiebreaker filter, not a sort key.

**Legacy labels removed:** `P1b` (stray Tier label), `status-review` (the
SDLC dropped the "Review" stage in favor of "In Dev").

Historical Rank/Tier/Epic/Theme values remain readable in the **archived**
Project #6 (archive preserves data; it was not deleted). Six skills
(`roadmap`, `groom`, `create-issue`, `metrics`, `read-docs`, `sprint`) and
`.claude/DEVELOPMENT_WORKFLOW.md` were rewritten to operate on labels +
sub-issues only.
