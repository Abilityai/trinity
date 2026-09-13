# Skills and Playbooks

Reusable capability packages that Trinity syncs from git repositories, assigns to agents, and delivers into their containers — invocable from the Playbooks tab, from chat autocomplete, or by the agent itself.

> 📺 **Watch:** [Building Agents — Playbooks, Plugins, Deployment](https://youtu.be/MDxRZBikf70) *(Apr 2026)* · [Build an AI Recruiter Agent](https://youtu.be/K7hFWyFIf-Y) *(Jun 2026)* · [all videos](../videos.md)

## Concepts

- **Skill** — A reusable capability shipped as a whole directory: a `SKILL.md` plus optional `scripts/`, templates, and resources.
- **Skill source** — A GitHub repository Trinity syncs skills from. An installation can have several.
- **Skills library** — The merged view of every enabled source. This is what you browse and assign from.
- **Skill assignment** — An owner assigns skills to an agent, from the agent's Skills tab or from the Library. Assigning delivers the package to a running agent immediately; a stopped agent receives it on its next start.
- **Playbook** — A skill invoked from the UI. The Playbooks tab shows assigned, user-invocable skills with a **Run** button.
- **Playbook autocomplete** — Type `/` in the Chat tab to see available playbooks with argument hints.

## How It Works

### The skills library is multi-source

Trinity syncs from **one or more** GitHub repositories: a bundled public community catalog (`github.com/abilityai/trinity-skills`, shown as **Trinity Community Skills**) that is seeded on fresh installs, plus any custom repositories your admin adds.

Manage sources in **Settings → Agents → Skills Library**. The panel lists sources in resolution order — the first row is marked **wins conflicts**, the seeded catalog **bundled**, and each row shows whether it tracks a **branch** or is **pinned** to a tag, with the ref. Per source you can **Sync**, **Disable**/**Enable**, or **Remove**; **Sync all** pulls every enabled source. **+ Add a skills repository** asks for a **Name**, a **Repository URL** (github.com only), what to **Track** — *Branch (follows new commits)* or *Tag (pinned — a moved tag is refused)* — and the branch or tag name. Errors are shown verbatim under the source, so a refused tag names the tag and says what to do.

**When two sources ship the same skill name**, resolution is by priority (lower wins), then by age. Custom sources default to priority 100 and the bundled community source to 1000 — so **your own repository always wins** a name clash, and a source you add later needs no reordering.

Nothing is overwritten silently. The winning skill carries a `shadowed_by` marker naming the sources whose copy is unreachable, shown in the library listing, on the source status, and as a warning at injection time. Skill names stay bare (`pdf-export`, never `community/pdf-export`), so an agent's `/skill-name` invocation never changes because a source was added.

Deleting a source does **not** unassign its skills — they keep resolving through whatever source still provides them.

#### Repository layout

A source repository can lay its skills out in one of three ways. Trinity tries them in order and falls through on anything invalid:

1. **Declared** — a `catalog.yaml` at the repo root with a `skills_root:` key naming the directory that holds skill folders. One flat level deep.
2. **Conventional** — a `skills/` directory containing at least one `<name>/SKILL.md`.
3. **Legacy** — `.claude/skills/`, the original convention.

Existing repositories keep working with no configuration. The resolved root is reported per source in the library status, along with a `layout_conflict` flag if a repository carries more than one recognizable layout.

#### Supply-chain posture: pinned tags

Skills carry executable `scripts/`, and library automation can push them to your whole fleet unattended. So:

- The bundled **community source is pinned to a tag**, not a branch head — currently `v0.2.0` of `trinity-skills`. New upstream commits do not reach your fleet until the tag is bumped and you sync. The pin is a fresh-install seed: an existing instance keeps the source row it already has. To move it to a newer tag, an admin updates the source's ref through `PUT /api/skills/sources/{id}` (the Settings panel adds, syncs, disables, and removes sources but does not edit a ref in place) or removes the source and adds it again with the new tag. `TRINITY_DEFAULT_SKILL_SOURCE_REF` in `.env` changes the tag a fresh install is seeded with; `TRINITY_DEFAULT_SKILL_SOURCE=""` disables the seed entirely.
- **Custom sources track a branch by default**, because you control who can write to them. Choose **Tag** under **Track** to pin one instead.
- **A pinned tag that moves is refused**, not adopted. If a tag now resolves to a different commit than the last sync, the sync reports `moved_tag` and leaves your fleet alone. Moving to new content means pointing the source at a new tag name — an explicit admin action. Annotated and lightweight tags are both compared by the commit they point at, so a release tag that has not moved is never refused.

To revoke a skill from the community catalog, a new tag is cut without it.

### Browsing and assigning

Two surfaces, two starting points:

| Surface | Purpose |
|---------|---------|
| **Library** page → **Skills** tab (`/library?tab=skills`) | Start from a skill. See every skill, its contract, its source, the library's sync state, and **which agents hold it** — then assign it to one more agent or unassign it, without opening the agent. |
| Agent detail → **Skills** tab | Start from an agent. Tick the skills this agent should have, save, and see the per-skill outcome of the last delivery. |

Both surfaces write the same per-agent assignment, so there is one skill model whichever way you come at it.

#### From the agent's Skills tab

The tab shows two lists: **Assigned to this agent** (each skill's version short-SHA, description, and the outcome of its last delivery) and **Library** (everything available, with its declared dependencies). Tick or untick skills and click **Save assignments**. The save delivers straight away, and the note beside the button says what happened:

| Note | Meaning |
|------|---------|
| *Saved and delivered — available now.* | Every added skill is in the running agent. |
| *Saved — the agent is stopped, so it applies on next start.* | The assignment is recorded; the files arrive on start. |
| *Saved — still installing; the lists update when it lands.* | A large package outlived the request's wait; delivery continues in the background. |
| *Saved; some skills did not install (…). Sync now to retry.* | Named skills failed; the rest landed. |
| *Saved but not delivered: <why>. Sync now, or the agent picks it up on next start.* | The agent was mid-sync, still starting, its container state could not be read, or the install failed. The assignment is kept either way. |

**Sync now** (running agents only) is the repair action: it re-copies every assigned skill unconditionally and is highlighted only when a delivery did not land. Per-skill outcomes are shown honestly — a skill that landed but is missing a declared binary or environment variable is flagged with a warning, not reported as a clean success.

#### From the Library

Each skill card on the Library's Skills tab carries an **Assigned to N agents** line with chips linking straight to that agent's Skills tab — the first four, then **+N more**. Under it:

- **Assign to…** lists the agents you may still assign this skill to — agents you own (an admin sees every agent), minus the ones that already hold it. Pick one and click **Assign**. The delivery note appears under the control (*Assigned and delivered — available now*, *applies on next start*, or a named failure). There is no Sync button here: to retry a failed delivery, assign the same agent again.
- **×** on a chip unassigns the skill from that agent. The × appears only where you are allowed to make the change — an agent shared with you shows as a holder but carries no control.
- A refused write shows the server's reason next to the control, not in a toast.

The three states of the holder list are kept distinct rather than collapsed into a zero. If the assignment data hasn't loaded yet you see a dash; if it couldn't be read you get an explicit failure with a **retry**; only a real, successful read reports "no agents yet" (admin) or "none of your agents" (everyone else). You see only what you can access: an admin gets the whole fleet, everyone else gets their own and shared agents.

Below the library listing sits **Assigned but no longer in the library** — assignments whose skill has since been removed upstream. This matters because revocation works by cutting a new tag *without* the offending skill, after which a page keyed on the library listing would answer "who still has it?" with silence. The package stays on each agent until it is unassigned — with the × on the chip here, or from that agent's Skills tab.

#### Lists follow the assignment

Every change to an agent's assignments — assign, unassign, save, a manual sync, a background delivery finishing, a fleet re-inject — refreshes the open screens that list its skills: the **Playbooks** tab, the `/` autocomplete in **Chat**, and the `/` popup in the [Workspace](../sharing-and-access/workspace.md). Nothing needs a reload.

### Skill injection

Each assigned skill's whole package is written to `~/.claude/skills/<name>/`, and a **Platform Skills** section is written into the agent's `CLAUDE.md` listing what was injected and what is still missing. Injection runs when you assign (running agents), when the agent starts, on a manual **Sync now**, and — if enabled — as a fleet sweep after a library sync.

Injection is:

- **Versioned and idempotent** — each skill's version is its git tree SHA. A skill unchanged since the last inject is skipped on start and on assign; only changed skills transfer. A manual **Sync now** is an unconditional repair.
- **Bounded on assign** — an assign waits up to 20 seconds for delivery and then reports *still installing* while the work continues in the background. An assign that arrives while the agent is mid-sync is retried once; if the agent is still busy it is reported as not delivered and the assignment is kept.
- **Pruned by manifest diff** — files removed from a skill upstream are removed from the agent on the next injection. Only paths the platform wrote are ever touched; agent-authored files and runtime artifacts are structurally untouched.
- **Gitignored** — injected skill directories are added to the agent's `.gitignore` and untracked, so the agent's git auto-sync never commits platform packages into your repository.
- **Bounded** — 10 MiB per skill, 50 MiB per injection. An over-cap skill fails by name; the rest still inject.

On an older agent image that predates package support, a multi-file skill degrades to **SKILL.md only** with an honest warning — the skill still works.

### Unassigning removes the package

Unassigning a skill — whether you remove one skill or drop names from a bulk save — deletes the injected package from the agent, using the same manifest the injection recorded. Only paths a previous injection wrote are removed; directories left empty are cleaned up; anything else in the directory survives. The skill's `.gitignore` line is stripped too.

The unassignment itself always succeeds. If the agent is stopped, busy, or unreachable, removal is reported as deferred rather than failing the unassign — and the agent reconciles on next start, removing any platform-managed skill that is no longer assigned.

A reconcile that would remove an unusually large number of skills from one agent **refuses wholesale** and raises an operator alert instead, so a database problem cannot silently strip a fleet.

### Keeping the library current (automation)

Both settings default **OFF** — a zero-config installation behaves exactly as it always did. Configure them in **Settings → Agents → Skills Library → Automation**:

| Setting | Effect |
|---------|--------|
| **Scheduled auto-sync** | Trinity pulls every enabled source on an interval (default 1 hour; 5 minutes to 24 hours). Changing the interval applies without a restart. |
| **Re-inject across the fleet after a sync** | When a sync finds the library actually moved, running agents receive the updated packages automatically. |

Key properties:

- A no-op pull never sweeps the fleet — re-inject fires only when a commit actually changed.
- Stopped agents are skipped; they pick the change up on next start.
- The sweep runs at bounded concurrency and skips (rather than waits on) an agent that is mid-injection, reporting what it skipped.
- The sources list shows each source's last sync status and error; the **Automation** panel shows the last fleet report (updated, skipped, failed, and which agents). If ≥1 agent failed, an operator-queue alert is raised.

Sync failures are never silent, and a sync contended by another worker reports `busy` rather than claiming failure.

### Skill frontmatter and dependency checks

A skill's `SKILL.md` frontmatter can declare:

- `description:` — shown in the library and in autocomplete.
- `automation:` — the skill's intended automation level.
- `user_invocable:` — whether the skill appears as a runnable playbook (default true).
- `allowed-tools:` — the tools the skill may use.
- `requires:` with `packages`, `binaries`, and `env` lists.

At injection, Trinity runs a **declaration-only** dependency check and produces per-skill warnings (a missing binary, a missing environment variable) instead of failing. Declared package installs are surfaced but not performed. Environment checks report variable **names** only — values are never read.

A skill whose frontmatter fails to parse gets a named warning and a description falling back to its first paragraph. It is never silently dropped.

### Running playbooks

1. Open agent detail → **Playbooks** tab.
2. See assigned, user-invocable skills with descriptions.
3. Click **Run** to send the skill as a task to the agent.
4. Or, in **Chat**, type `/` to autocomplete a playbook command.

### Running skills without assignment (Skill Runner)

Two MCP tools let an agent run a **permitted** self-contained skill without assigning it:

| Tool | Description |
|------|-------------|
| `list_runnable_skills()` | List the skills this agent is permitted to run (its permitted set, decided by an operator — not the whole library) |
| `run_skill(skill_name, input?)` | Run a permitted skill and return its result |

The runner uses a **separate workspace** — it cannot see the calling agent's files. Use it for self-contained skills (call an API, generate an artifact from the `input` you pass). A skill that must operate on the caller's own files goes through assignment and injection instead.

The Skill Runner is an **entitled** surface. In a community build, `run_skill` and `list_runnable_skills` return a "disabled" result.

## For Agents

MCP tools for skills and playbooks:

| Tool | Description |
|------|-------------|
| `list_skills()` | List library skills. Each entry carries its `source` name and any `shadowed_by` sources. |
| `get_skill(name)` | Skill details and contract |
| `get_skills_library_status()` | Library sync status, including the per-source array |
| `assign_skill_to_agent(skill_name, agent_name)` | Assign one skill and deliver it. The response's `delivery` block reports `injected`, `pending_start`, `in_progress`, or `not_delivered` with a `reason` |
| `set_agent_skills(agent_name, skill_names)` | Set the full skill list. Added names are delivered, dropped names are removed; `delivery` and `removal` report each half |
| `sync_agent_skills(agent_name)` | Force re-inject into a running agent — the manual retry after a `not_delivered` |
| `get_agent_skills(agent_name)` | List an agent's assigned skills |

**REST endpoints** — see [Backend API Docs](http://localhost:8000/docs) for full schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/skills/library` | GET | Merged skill listing across sources |
| `/api/skills/library/status` | GET | Sync state + per-source array |
| `/api/skills/library/sync` | POST | Sync every enabled source (admin, human-only) |
| `/api/skills/sources` | GET/POST | List / register a source (admin, human-only) |
| `/api/skills/sources/{id}` | PUT/DELETE | Edit / remove a source (admin, human-only) |
| `/api/skills/sources/{id}/sync` | POST | Sync one source (admin, human-only) |
| `/api/settings/skills-library` | GET/PUT | Auto-sync and fleet re-inject configuration (admin) |
| `/api/skills/assignments` | GET | Which agents hold each skill, batched, plus `assignable_agents` — the agents the caller may assign to. Human-only; admins see the fleet, others their accessible agents (the response says which via `scope`) |
| `/api/agents/{name}/skills` | GET/PUT | Read / set an agent's assignments (owner). The PUT response carries `delivery` and `removal` |
| `/api/agents/{name}/skills/{skill}` | POST/DELETE | Assign (and deliver) / unassign one skill (owner). The POST response carries `delivery` |
| `/api/agents/{name}/skills/inject` | POST | Force re-inject into this agent (owner); 409 while another injection holds the agent |

Source management is **REST-only and human-only** — there is no MCP tool for it, and agent-scoped keys are rejected. Registering or syncing a source decides which repository your fleet executes code from, so it is an operator action regardless of the caller's role.

## Limitations

- Auto-sync is one library-wide timer over every enabled source, not a per-source cadence.
- Skill names share one flat namespace. Shadowed copies are not offered as separate entries because they are unreachable.
- Assignment happens per agent. There is no fleet-wide "assign to everything" action.
- The declared-`skills_root` layout supports a single flat directory, one level deep. Nested layouts require a future schema version, which current installations refuse (falling back to the probe) rather than misread.

## See Also

- [Scheduling](scheduling.md) — automate skill execution on a schedule
- [Abilities Marketplace](abilities-marketplace.md) — the plugin marketplace for building agents
- [Agent Configuration](../agents/agent-configuration.md) — per-agent settings
