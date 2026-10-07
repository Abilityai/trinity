# GitHub Sync

Keep agents in sync with GitHub (or self-hosted Git) repositories using two modes: Source mode (pull-only) and Working Branch mode (bidirectional). Which one a new agent gets depends on whose repository it is.

> 📺 **Watch:** [Why Every AI Agent Needs a GitHub Repo](https://youtu.be/R4nNHf6ywEs) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

- **Source Mode**: Pull-only. The agent pulls from the repo but never pushes. Used for deploying agent code from a canonical source, and for any repository that is not yours.
- **Working Branch Mode**: Bidirectional. The agent has its own branch (e.g. `trinity/<agent>/<id>`) and can push changes back. Used for agents that modify their own code.
- **Agent vs. deployment**: At creation you say what you are creating. An **agent** (the default) owns its repository and keeps its work there. A **deployment** runs a codebase and only pulls.
- **Pull sync**: The agent's container fetches its repository on its own, on a timer, so commits people and other agents push reach the agent without anyone pressing **Pull**. Separate from auto-sync, which pushes.
- **Branch Selection**: Specify a branch via URL syntax `github:owner/repo@branch` during creation, or via the `source_branch` parameter in MCP.
- **Branch Owner**: Each working branch is owned by a single agent instance. Ownership is enforced at the database layer to prevent concurrent pushes from clobbering each other.
- **Parallel History**: The agent's branch and its upstream have no shared commit ancestor — each side evolved independently. Requires an explicit resolution choice.

## How It Works

### Creating an agent with sync

Agents created from a Git template automatically get sync configured. Trinity picks the mode when it creates the agent.

An agent created as an **agent** (the default) gets a working branch, auto-sync on, and **Pause schedules while sync is failing** on — but only when all of these hold:

- The repository is not a shared catalog template.
- The repository's owner is the GitHub account of the token being used (compared case-insensitively). An organization's repository does not qualify, even when your token can write to it.
- The token is your own personal token or the agent's own token. The platform-wide token set by an admin never qualifies.
- A push check confirms the token can actually push to the repository.

Anything else creates the agent pull-only (Source mode). A **deployment** (`kind: "deployment"`), a disposable agent, and a create with no token are always pull-only. A fork-to-own agent owns its fork and auto-syncs to it. An explicit `source_mode` in the request always sets the mode.

The create response carries `git_mode` — `kind`, `source_mode`, `pushes`, and a `reason` — so you can see why an agent came out pull-only. For example: *owned by your-org, not your GitHub account: pull-only. Fork it to your own repository to keep this agent's work in git.*

`pushes` says whether the agent saves its work to git. Read it rather than `source_mode`: a fork-to-own agent is in source mode and still pushes, to its own fork.

When the agent will auto-push, Trinity also checks push access before it creates anything. A token that can read the repository but not push to it (for example a fine-grained token with **Contents: Read-only**) fails the create with a `400` that names the fix: give the token write access, or create the agent in source mode.

Existing agents keep their mode. The `trinity deploy --repo` CLI command creates a deployment. A [system manifest](../collaboration/system-manifest.md) takes `kind` per agent, and exporting a system writes `kind: deployment` for a pull-only member, so a deployment stays pull-only when you redeploy the export. Operator runbook: [Agent working-branch default](../../migrations/AGENT_WORKING_BRANCH_DEFAULT_2026-09.md).

### "What is this repository?" in the create dialog

When you create an agent from a custom GitHub repository with the **clone** option, the **Create Agent** dialog asks **What is this repository?**:

- **An agent** (default) — the repository is the agent: its memory, skills and state. It saves its work to its own branch. This needs a repository you own and your own GitHub token (set in **Settings**); otherwise the agent is created pull-only. For someone else's template, choose **Fork** instead.
- **A deployment of a codebase** — the repository is a product the agent runs. It only pulls updates and never pushes.

The dialog asks only where the answer can change the outcome. It does not ask for blank or local agents, copy or fork, a fork-to-own template, or a GitHub template picked from the list. Every list template is a shared catalog entry, so it is always created pull-only; fork it to keep the agent's work in git. When the question is not asked, no `kind` is sent.

After a GitHub create, the dialog shows what Trinity chose and why. *Created pull-only*, highlighted as a warning, means you asked for an agent and it came out pull-only; the reason line below says why. The agent's **Git** tab then shows the binding as a badge: **Agent · own branch**, **Agent · own repo** (a fork-to-own agent saving to its own repository), or **Pull-only** (auto-sync does not push this agent's work).

### How git authenticates inside the agent

The agent's `origin` remote carries no token. On every fetch, pull, and push, git asks Trinity's credential helper for the agent's GitHub token, so the token stays out of `.git/config` and out of the container's process list and logs. When you set or rotate a token, it is written to the agent's workspace `.env`, which the helper reads first, so the next git operation uses it without a restart. On a self-hosted git server the helper answers only for `TRINITY_GIT_BASE_URL`. For what the agent can still read, and what upgrading does to existing agents, see [GitHub PAT Setup → How git gets the token](github-pat-setup.md#how-git-gets-the-token).

### Turning auto-sync on or off

Auto-sync commits and pushes the agent's changes every 15 minutes. Turn it on or off in the agent's **Settings** tab, under **Git sync**. The same section holds **Pull changes from GitHub on every sync cycle** (see [Pulling changes from GitHub](#pulling-changes-from-github)) and **Pause schedules while sync is failing**. All three switches take effect on the agent's next cycle, with no restart: the agent reads each setting at the start of every cycle. An agent with no GitHub repository shows a note pointing you to the **Git** tab instead.

Each cycle fetches the agent's branch first. If someone else pushed to it, the agent rebases its own commits on top and pushes. The push only succeeds if the branch has not moved again since the fetch, so another writer's commits are never overwritten. When the rebase conflicts, the cycle aborts the rebase, leaves the repository as it was, and records `diverged: rebase conflict on <branch>` as a sync failure. Trinity never resolves the conflict for you.

A source-mode agent sitting on the repository's default branch refuses to auto-push and records `refused: source-mode on <branch>` instead. Its commits would otherwise land straight on a shared `main`. Fork-to-own agents are exempt, because they own their fork. A cycle with nothing staged makes no commit.

### Pulling changes from GitHub

The agent's container pulls its repository on its own. Without it, edits that people and other agents pushed to GitHub reached the agent only when someone pressed **Pull**.

- **Where it's on.** New agents created from GitHub have it on, source-mode agents included. Agents created before this feature have it on only if auto-sync was already on. Everyone else has it off until you turn on **Pull changes from GitHub on every sync cycle** under **Settings → Git sync**.
- **How often.** On the same interval as auto-sync (15 minutes by default), offset by half an interval so the push and the pull do not reach the repository together. When one is running, the other waits for it (up to two minutes) instead of skipping.
- **What a cycle does.** It fetches the branch the agent has checked out. With no local commits it fast-forwards; with local commits it rebases them on top. On a working branch (`trinity/<agent>/…`) it also merges `main` in, so commits people push to `main` reach the agent. A conflict is aborted and recorded, for example `diverged: merge conflict with main (<files>)`. Trinity never resolves it for you.
- **When it waits.** A pull never starts while the agent is running a turn or has one queued, and never over a repository with unresolved merge conflicts.
- **Uncommitted edits.** The pull sets the agent's uncommitted edits aside and puts them back afterwards. If the incoming commits collide with them, the pull is undone and the edits are put back, and the cycle records `local edits conflict with incoming changes on <branch>`. If an edit cannot be put back, the error says it is kept in `git stash`.

The pull outcome appears in the agent's sync state: `last_pull_at`, `last_pull_status` (`success`, `failed` or `skipped`), `last_pull_error`, `behind_after_pull`, `last_successful_pull_at`, `consecutive_pull_failures` and `consecutive_pull_skips` (`GET /api/agents/{name}/git/sync-state`, `get_git_sync_state`). A failed pull does not count toward the push failures that trip the schedule pause.

> **Edits made outside agent turns are not protected.** The pull only protects work done inside the agent's own turns. A file uploaded through the Files tab, or edited in the web terminal or with `docker exec`, while a pull is integrating can be lost. A turn that starts during that window (up to about two minutes) can also see files change under it.

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

### What happens to `.claude/settings.json`

`.claude/settings.json` is not gitignored, so a template's project settings (for example the hooks a marketplace plugin registers) commit and travel with the repository. Trinity checks the file's **content** each time it commits — on the auto-sync cycle, on **Sync**/Push, on the preserve-state reset, and when it initializes a repository. It keeps the file out of that commit when the staged copy:

- registers hooks that run from `/opt/trinity/` (paths that exist only inside the container, so they would break any other clone),
- carries a non-empty `env`, `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `gcpAuthRefresh`, or `otelHeadersHelper` key (the settings that hold a credential or name the command that produces one), or
- is not a valid JSON object, so it cannot be checked.

When that happens, Trinity keeps the last committed copy in the commit. If there is no committed copy, or the committed copy itself registers `/opt/trinity/` hooks, it untracks the file instead. The file on the agent's disk is never touched, so the running agent keeps whatever it registers. The agent log names the reason and the keys, never a value.

### Sync health polling

Trinity reads the git status of every git-enabled agent once a minute. Each read runs a `git fetch` inside the agent, so on a large fleet you can poll less often by setting `SYNC_HEALTH_POLL_INTERVAL_SECONDS` in the backend's `.env` (default `60`; see [Single-Server Deployment → `.env` reference](../guides/deploying/single-server.md#observability)). When an agent's consecutive sync failures reach three, an alert lands in the [Operating Room](../operations/operating-room.md#sync-health-alerts). When the failures are refused pushes, the alert is titled **Git token can't push**: the token can read the repository but not write to it, so the failures will not stop on their own. Give the token write access (fine-grained: **Contents: Read and write**; classic: the `repo` scope) or set a per-agent token that has it, and the next sync pushes everything that is waiting.

### What the sync state means

Sync health measures whether the agent and its repository **agree**, not only whether the last push worked. Trinity computes one state per agent and every surface shows the same one: the dashboard dot, the sync chip on the Fleet tile and the **Sync** line on Agent Detail → Overview (`↑7 ↓0 · 12 dirty · pushed 3h ago` in the state colour; hover for the reason), `GET /api/agents/sync-health`, `GET /api/agents/{name}/git/sync-state`, fleet health (`GET /api/monitoring/status` and `get_fleet_health`), the fleet sync audit (`GET /api/fleet/sync-audit` and `get_fleet_sync_audit`), and the `get_git_sync_state` MCP tool. The reason never includes the raw git error text; the per-agent sync-state read still carries it as `last_error_summary`.

| State | When |
|-------|------|
| **Red** | The last sync failed; or the agent has been ahead of or behind its repository for more than 24 hours; or it has had uncommitted changes for more than 24 hours; or auto-sync is on but no sync has run for 7 days |
| **Yellow** | The agent has been ahead or behind for 24 hours or less |
| **Green** | The agent and its repository agree |
| **Gray** | Trinity has not observed the agent yet |

Ahead and behind are counted against the agent's **own branch** on GitHub, not against `main`, whatever that branch is named. A branch that was never pushed counts the commits no remote holds. A count Trinity cannot compute is reported as unknown (`null`), never as `0`. Every entry carries a `reason` (for example `diverged 31 behind / 0 ahead for 26h`) and, when there is an obvious fix, a `recommendation` — `credential is read-only`, `push via git_sync strategy=pull_first`, `enable auto-sync`, `pull via git_pull`. Hover the dot to read both.

An agent that tracks a shared branch with auto-sync off — a **deployment** of a codebase rather than an agent that owns its branch — is never red for being behind: being behind is normal for a deployment. It shows yellow instead.

The ages start counting at the first check after the upgrade that introduced them, so an agent that was already out of step shows yellow for its first day and red after that.

### Pausing schedules when sync is unhealthy

With **freeze schedules if sync failing** on (`PUT /api/agents/{name}/git/freeze-schedules-if-failing`), the scheduler skips the agent's scheduled runs when either:

- its git sync has failed on three consecutive checks, or
- it is an agent that owns its branch (a working branch, or auto-sync on) and has been ahead of or behind its repository for more than 24 hours.

Each skipped run appears in the execution history with the reason, for example *Git sync frozen: diverged 0 behind / 7 ahead for 26h*. A divergence pause also files one **Agent diverged from GitHub — schedules paused** notice in the Operating Room per episode. Schedules resume by themselves on the first scheduled run after the agent is back in step — push its work, or pull what it is missing. The divergence pause needs a recent check (within 15 minutes): if Trinity cannot reach the agent, it does not keep it paused on old information. Manual runs are never paused.

The read is built to stay out of the agent's way:

- **It takes no git lock.** The status read never takes the repository's `.git/index.lock`, so the agent's own `git add` or `git commit` cannot fail because Trinity was looking.
- **Callers share one read.** The background poll, the Git tab, and the `get_git_status` MCP tool share a single in-flight computation instead of stacking parallel fetches. The response's `computed_at` says when that snapshot was taken, so a result can be up to one fetch old.
- **Stuck locks are reported, not deleted.** Trinity never removes a lock inside a running container, because deleting a lock that a live git process still holds can corrupt the index. When the same `index.lock` stays unchanged across at least three status reads spanning 15 minutes or more, the status response carries `index_lock_stuck` and the backend logs a warning once.
- **A restart clears stale locks.** At container start no git process can be running, so the startup script removes leftover git locks — including those of submodules and linked worktrees — and logs each one. The status response then records the cleanup under `lock_recovery`, and the backend logs it once.

### Initializing sync for existing agents

Agents created without a Git repository can be connected after the fact:

- Use the Git repo initialization flow in the UI. The new repository is always created at the agent's home directory (`/home/developer`), even when the template shipped files into `workspace/`. Trinity then asks the agent itself whether it sees the repository; if it does not, initialization fails with that reason and the configuration is rolled back, rather than reporting success while Sync and the Git tab say "not enabled".
- Via MCP: `initialize_github_sync(agent_name, repo_owner, repo_name)` — creates the repository if it does not exist (private by default; `create_repo`, `private` and `description` are optional)

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
| `/api/agents/{name}/git/status` | GET | Git sync status including `ahead`, `behind`, `common_ancestor_sha`, `pull_branch`, plus `computed_at`, `lock_recovery`, and `index_lock_stuck` (see [Sync health polling](#sync-health-polling)). Each changed file's `path` is its real path on disk; for a rename it is the new path, with the old one in `orig_path` |
| `/api/agents/{name}/git/sync` | POST | Trigger sync; the response carries `removed_paths`, `unignored_paths`, and `shadowed_negations` from the `.gitignore` sweep |
| `/api/agents/{name}/git/log` | GET | Recent commits |
| `/api/agents/{name}/git/pull` | POST | Pull from remote |
| `/api/agents/{name}/git/config` | GET | The agent's stored git configuration (repo, mode, branch) |
| `/api/agents/{name}/git/initialize` | POST | Connect an agent created without a repository to GitHub |
| `/api/agents/{name}/github-pat` | GET / PUT / DELETE | Per-agent PAT override: status only, set, or clear (back to the platform PAT) |
| `/api/agents/{name}/git/reset-to-main-preserve-state` | POST | Adopt `origin/main` as the new baseline while keeping the agent's persisted state |
| `/api/agents/{name}/git/auto-sync` | GET / PUT | The 15-minute auto-sync heartbeat for this agent. A change applies on the agent's next cycle, with no restart |
| `/api/agents/{name}/git/pull-sync` | GET / PUT | Whether the container pulls origin on its own. The agent reads it every cycle; turning it on or off takes a person — a browser session or your own user key; agent keys are refused |
| `/api/agents/{name}/git/freeze-schedules-if-failing` | GET / PUT | Pause scheduled executions while sync is failing |
| `/api/agents/{name}/git/sync-state` | GET | The persisted sync-state row for this agent, including the pull-cycle fields |
| `/api/agents/{name}/git/bind-to-own-repo` | POST | Bind to a repository you own (owner-only, human-only) |
| `/api/agents/{name}/git/bind-to-own-repo/status` | GET | Reconcile a binding whose response was lost |
| `/api/agents/sync-health` | GET | Per-agent sync health for the fleet |
| `/api/fleet/sync-audit` | GET | Fleet sync audit, including duplicate repository bindings, ahead/behind, dirty files, last push and the sync state per agent |

`POST /api/agents` and the MCP `create_agent` tool take an optional `kind` — `agent` (default) or `deployment` — on a `github:` create, and the response's `git_mode` says which mode was chosen and why (see [Creating an agent with sync](#creating-an-agent-with-sync)).

MCP tools: `initialize_github_sync`, `get_git_status`, `git_sync`, `get_git_log`, `git_pull`, `get_git_sync_state`, `reset_to_main_preserve_state`, and fleet-wide `get_fleet_sync_audit` (scoped to the agents you can access; an agent key sees its owner's). Mutating tools are owner-only; a shared key gets read and pull.

There is no MCP tool for binding to your own repository — it requires your personal token. The auto-sync, pull-sync and schedule-pause switches have no MCP tools either; use the **Settings** tab or the endpoints above.

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## Limitations

- Binding to your own repository requires the agent to be running and is not available in Working Branch mode.
- The agent-vs-deployment choice applies at creation only. There is no endpoint yet to switch an existing agent from Source mode to Working Branch mode; create a new agent from your repository or use **Bind to your own repo**.
- An agent created from a repository owned by an organization is pull-only by default, even when your token can push to it. Pass `source_mode: false` explicitly to ask for a working branch.
- A **copy**-imported agent has no Git configuration at all. Use **Initialize GitHub Sync** rather than bind-to-own-repo.
- Repository maintenance (repack/gc) runs on the agent's own home repository. Sub-repositories cloned into the workspace get no automatic maintenance.
- A stuck `index.lock` is reported but never removed while the agent runs. Restart the agent to clear it.
- The pull cycle protects edits made inside agent turns only. Uploads, web-terminal and `docker exec` edits made while a pull integrates can be lost.
- A list template is always created pull-only, whatever you choose. Fork it to keep the agent's work in git.

## See Also

- [GitHub PAT Setup](github-pat-setup.md) — Configure a Personal Access Token before using sync
- [Creating Agents](../agents/creating-agents.md) — Creating agents from Git templates
- [Monitoring](../operations/monitoring.md) — The Health tab and agent heartbeats
- [Operating Room](../operations/operating-room.md) — Where sync-health alerts and a push's `.gitignore` sweep notice land
- [Git remote token scrub](../../migrations/GIT_REMOTE_TOKEN_SCRUB_2026-09.md) — Operator runbook: what upgrading does to existing agents' remotes
