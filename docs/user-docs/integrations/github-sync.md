# GitHub Sync

Keep agents in sync with GitHub (or self-hosted Git) repositories using two modes: Source mode (pull-only, default) and Working Branch mode (bidirectional).

> 📺 **Watch:** [Why Every AI Agent Needs a GitHub Repo](https://youtu.be/R4nNHf6ywEs) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

- **Source Mode** (default): Pull-only. The agent pulls from the repo but never pushes. Used for deploying agent code from a canonical source.
- **Working Branch Mode**: Bidirectional. The agent has its own branch (e.g. `trinity/<agent>/<id>`) and can push changes back. Used for agents that modify their own code.
- **Branch Selection**: Specify a branch via URL syntax `github:owner/repo@branch` during creation, or via the `source_branch` parameter in MCP.
- **Branch Owner**: Each working branch is owned by a single agent instance. Ownership is enforced at the database layer to prevent concurrent pushes from clobbering each other.
- **Parallel History**: The agent's branch and its upstream have no shared commit ancestor — each side evolved independently. Requires an explicit resolution choice.

## How It Works

### Creating an agent with sync

Agents created from a Git template automatically get sync configured. The default mode is Source (pull-only).

### Using sync in the UI

1. Open the agent detail page to see Git status (branch, last sync, pending changes, `ahead`/`behind` counts).
2. Click **Pull** to fetch the latest commits from the remote.
3. Click **Sync** to run a full sync operation (pull-only in Source mode; pull + push in Working Branch mode).
4. View the git log to inspect recent commits.

### Initializing sync for existing agents

Agents created without a Git repository can be connected after the fact:

- Use the Git repo initialization flow in the UI.
- Via MCP: `initialize_github_sync(agent_name, repo_url)`

## Conflict Resolution

When sync can't complete cleanly, Trinity opens the **Git Conflict Modal** with a plain-English explanation and operator-readable resolution options. The modal classifies the conflict into one of six cases:

| Class | What happened | Typical resolution |
|-------|---------------|--------------------|
| `AHEAD_ONLY` | Local has commits the remote doesn't; remote is unchanged | Push |
| `BEHIND_ONLY` | Remote has new commits; local is unchanged | Pull |
| `PARALLEL_HISTORY` | Both sides have commits and share no common ancestor | Adopt upstream *or* Force Push (see below) |
| `UNCOMMITTED_LOCAL` | Uncommitted working-tree changes block the sync | Commit, stash, or discard |
| `AUTH_FAILURE` | Git could not authenticate with the remote | Update the agent's GitHub PAT |
| `WORKING_BRANCH_EXTERNAL_WRITE` | Someone else pushed to this agent's working branch | Adopt upstream *or* Force Push |

Each class renders a title, bullet-point explanation, and recommendation. Raw `git stderr` is hidden inside a collapsible `<details>` for developers.

### Parallel history

Trinity detects parallel history at modal open by computing the common ancestor (`git merge-base HEAD origin/<branch>`). When no shared ancestor exists **and** the upstream is ahead, the modal replaces the standard Pull First / Force Push buttons with a clearer choice:

- **Adopt latest upstream (preserve my state)** — reset to the upstream tip while preserving workspace state flagged for persistence. *(Preserve-state execution ships in a follow-up; the adopt primitive resets to upstream today.)*
- **Force Push** — destructive. Overwrites the upstream with the agent's branch. Use only if you're certain no one else depends on the remote state.

### Branch ownership enforcement

Working branches are ownership-locked at the database layer. If two agent instances try to push to the same branch simultaneously, the losing push fails with a "stale info — remote SHA has moved" error. Retry the sync; the winner's state is already upstream.

This is enforced silently — you only see the error if you trigger parallel syncs (e.g. UI + scheduled job at the same moment).

## Self-Hosted Git

Trinity supports Gitea, GitHub Enterprise Server, and other Git hosts via two environment variables on the backend:

| Variable | Example | Purpose |
|----------|---------|---------|
| `TRINITY_GIT_BASE_URL` | `https://gitea.example.com` | Clone/push base URL |
| `TRINITY_GIT_API_BASE` | `https://gitea.example.com/api/v1` | REST API base for repo creation/validation |

Trailing slashes are stripped automatically. Defaults target `github.com` and `https://api.github.com` — existing deployments need no changes. Agent creation and sync flows work identically regardless of the underlying host.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/git/status` | GET | Git sync status including `ahead`, `behind`, `common_ancestor_sha`, `pull_branch` |
| `/api/agents/{name}/git/sync` | POST | Trigger sync |
| `/api/agents/{name}/git/log` | GET | Recent commits |
| `/api/agents/{name}/git/pull` | POST | Pull from remote |

MCP tool: `initialize_github_sync(agent_name, repo_url)`

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## See Also

- [GitHub PAT Setup](github-pat-setup.md) — Configure a Personal Access Token before using sync
- [Creating Agents](../agents/creating-agents.md) — Creating agents from Git templates
