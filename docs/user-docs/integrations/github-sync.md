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

### What a Push does to `.gitignore`

Before every push, Trinity rebuilds the agent's `.gitignore` around its own rules and then untracks anything the rules now cover:

```
# >>> Trinity default ignore rules — managed; your own rules go BELOW and win >>>
   (caches, virtualenvs, local databases, generated content, …)
# <<< Trinity default ignore rules <<<
   (the agent's own rules, in their original order)
# >>> Trinity protected rules — managed; NOT overridable >>>
   (credential files and the agent's .trinity/ state)
# <<< Trinity protected rules <<<
```

The order is the point. Git is last-match-wins, so the managed defaults sit **above** the agent's own rules and a `!negation` the agent wrote keeps winning — a file the agent chose to keep is never silently untracked by the platform's defaults. The protected floor at the bottom cannot be overridden, so a stray rule can never re-track a credential file or drop the agent's `.trinity/` hooks. The rebuild is idempotent: an unchanged file is left untouched, so the auto-sync loop has nothing to re-commit.

A Push then reports what the sweep changed. The sync response and the `git_sync` MCP result carry `removed_paths` (tracked → untracked by this push), `unignored_paths` (newly un-ignored and committed by this same push), and `shadowed_negations` (a `!rule` of yours that a managed pattern still overrides — the deciding pattern is named); the Git tab's toast and the commit message state the untracked and un-ignored counts and paths. When a push actually changed what is tracked, Trinity also files an operator-queue notice — *Push untracked files that now match .gitignore*, *Push committed files that were previously gitignored*, or *Push changed which files are tracked (.gitignore sweep)* — naming the paths, so an unattended scheduled sync cannot untrack files for weeks without anyone noticing. Find it on the [Operations page](../operations/operating-room.md). A newly un-ignored path that was a secret is already in the remote's history: rotate it and remove the rule.

One limit, reported rather than fixed: many default patterns are directory-form (`node_modules/`, `content/`), and git never descends into an excluded directory, so a negation *beneath* one is inert wherever it sits. Such rules appear under `shadowed_negations`.

### Initializing sync for existing agents

Agents created without a Git repository can be connected after the fact:

- Use the Git repo initialization flow in the UI.
- Via MCP: `initialize_github_sync(agent_name, repo_url)`

### Binding an agent to a repository you own

An agent created from a **public** template clones with no token — it works, it learns, it accumulates a workspace, but it cannot push anywhere. **Bind to your own repo** on the agent's **Git** tab is how you take ownership of it in place, without recreating the agent.

What it does, in order:

1. Creates the destination repository under your account if it doesn't exist (**private by default**), using a GitHub token you supply in the form.
2. Pushes the agent's **current workspace history** into it — not the template's, the agent's.
3. Repoints the agent's `origin` at the new repository.
4. Saves your token as the agent's per-agent token, so restarts never fall back to a shared platform token.
5. Rebuilds the container so the change survives a restart.

The agent keeps its name, its identity, its 180-day name reservation, its data volumes, and its history. Nothing is re-provisioned.

This is a **rebind, not a fork** — an agent that already has a writable repository can be rebound too, which is what you want after a typo'd destination, the wrong token account, or an org migration.

Requirements and refusals:

- The agent must be **running** (the operation reads its live workspace).
- Owner-only and **human-only** — there is deliberately no MCP tool, because binding requires putting your personal token in the request.
- Working-Branch-mode agents are refused (they need a branch re-reservation), as are agents with no Git configuration at all, including local-template agents and the system agent.
- A destination that already contains unrelated commits, or that another agent is already bound to, is refused rather than overwritten.
- A retry after a partial failure is safe. Re-submitting the same destination resumes; **Bind status** on the Git tab reports what the database and the live container each believe, so you can tell whether a lost response actually completed.

After binding, the agent pushes normally and appears on all git-sync surfaces.

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
| `/api/agents/{name}/git/sync` | POST | Trigger sync; the response carries `removed_paths`, `unignored_paths`, and `shadowed_negations` from the `.gitignore` sweep |
| `/api/agents/{name}/git/log` | GET | Recent commits |
| `/api/agents/{name}/git/pull` | POST | Pull from remote |
| `/api/agents/{name}/git/bind-to-own-repo` | POST | Bind to a repository you own (owner-only, human-only) |
| `/api/agents/{name}/git/bind-to-own-repo/status` | GET | Reconcile a binding whose response was lost |
| `/api/agents/sync-health` | GET | Per-agent sync health for the fleet |
| `/api/fleet/sync-audit` | GET | Fleet sync audit, including duplicate repository bindings |

MCP tools: `initialize_github_sync`, `get_git_status`, `git_sync`, `get_git_log`, `git_pull`, `get_git_sync_state`, `reset_to_main_preserve_state`. Mutating tools are owner-only; a shared key gets read and pull.

There is no MCP tool for binding to your own repository — it requires your personal token.

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## Limitations

- Binding to your own repository requires the agent to be running and is not available in Working Branch mode.
- A **copy**-imported agent has no Git configuration at all. Use **Initialize GitHub Sync** rather than bind-to-own-repo.
- Repository maintenance (repack/gc) runs on the agent's own home repository. Sub-repositories cloned into the workspace get no automatic maintenance.

## See Also

- [GitHub PAT Setup](github-pat-setup.md) — Configure a Personal Access Token before using sync
- [Creating Agents](../agents/creating-agents.md) — Creating agents from Git templates
- [Monitoring](../operations/monitoring.md) — Sync-health alerts and repository-bloat warnings
- [Operating Room](../operations/operating-room.md) — Where a push's `.gitignore` sweep notice lands
