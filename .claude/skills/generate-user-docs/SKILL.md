---
name: generate-user-docs
description: Generate and update user documentation from code, feature flows, and recent changes into docs/user-docs/
allowed-tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
  - Agent
user-invocable: true
---

# Generate User Docs

Generate and maintain `docs/user-docs/` — the authoritative user and agent documentation for Trinity, derived from code as the single source of truth.

## Purpose

Read backend routers, frontend views, feature flows, and recent changes to produce clear, non-redundant, MECE documentation organized for two audiences: human users (UI workflows) and agent users (API and programmatic usage).

## State Dependencies

| Source | Location | Read | Write |
|--------|----------|------|-------|
| Backend routers | `src/backend/routers/*.py` | Yes | No |
| Frontend views | `src/frontend/src/views/*.vue` | Yes | No |
| Feature flows | `docs/memory/feature-flows/*.md` | Yes | No |
| Feature flow index | `docs/memory/feature-flows.md` | Yes | No |
| Requirements | `docs/memory/requirements.md` | Yes | No |
| Architecture | `docs/memory/architecture.md` | Yes | No |
| Deployment config | `.env.example`, `scripts/deploy/*.sh`, `docker-compose.yml`, `docker-compose.prod.yml`, `deploy.config.example` | Yes | No |
| Trinity Docs site | `../trinity-docs/app/getting-started/*.tsx` | Yes | No |
| Abilities repo | `github.com/abilityai/abilities` (README) | Yes | No |
| Ops agent repo (public) | `github.com/abilityai/trinity-ops-public` (README, CLAUDE.md) | Yes | No |
| Ops runbook (private, pattern source for ops content) | `../ops-runbook/playbooks/*.md`, `../ops-runbook/instances/_template/scripts/*.sh`, `../ops-runbook/instances/_template/CLAUDE.md` | Yes | No |
| Git history | `git log --since` | Yes | No |
| Existing user docs | `docs/user-docs/**/*.md` | Yes | Yes |

## Prerequisites

- Repository checked out with `docs/memory/` populated
- No build or runtime dependencies required

## Maintained Guides

These tutorial-style guides walk users through end-to-end tasks. Keep them in sync with their source.

| Guide | Source | Purpose |
|-------|--------|---------|
| `guides/deploying-trinity.md` | `trinity-docs/app/getting-started/deploying-trinity/page.tsx` + this skill's deploy hub rules | Hub: cloud vs self-hosted decision, prerequisites, links into spokes |
| `guides/deploying/local-development.md` | `docker-compose.yml`, `scripts/deploy/start.sh`, `.env.example` | Docker Desktop, dev compose, hot reload, base image build |
| `guides/deploying/single-server.md` | `docker-compose.prod.yml`, `deploy.config.example`, `.env.example` | VPS/bare-metal: prod compose, env, base image, email, Redis password, host paths |
| `guides/deploying/public-access.md` | `docker-compose.prod.yml` (cloudflared profile), `.env.example` (TUNNEL_TOKEN, PUBLIC_CHAT_URL) | Cloudflare Tunnel, public webhook surface, DNS |
| `guides/deploying/upgrading.md` | This skill's operational template + ops runbook patterns | Pre-flight → backup → pull → rebuild platform services → restart → verify → rollback |
| `guides/deploying/backup-and-restore.md` | This skill's operational template + ops runbook patterns | Volume-mounted alpine `cp` pattern, retention, daily cron template |
| `guides/deploying/monitoring.md` | This skill's operational template + ops runbook patterns + `/api/ops/fleet/health` router | Six health probes, fleet-health API, resource thresholds table, common-recovery patterns |
| `guides/using-trinity.md` | `trinity-docs/app/getting-started/using-trinity/page.tsx` | UI tour: dashboard, agents, monitoring |
| `guides/building-agents.md` | `trinity-docs/app/getting-started/building-agents/page.tsx` + abilities repo README | Create, develop, deploy with abilities; includes abilities plugin marketplace (`/plugin marketplace add abilityai/abilities`), all 5 plugins (create-agent, agent-dev, trinity, dev-methodology, utilities), and agent onboarding via `/trinity:onboard` |
| `guides/deploying/ops-agent.md` | trinity-ops-public README + CLAUDE.md | Trinity Ops Agent: what it is, how to connect it to an instance via `/trinity:deploy`, day-to-day operations (`/status`, `/diagnose`), and when to use it vs. raw Docker |

**Sync rule**: When the trinity-docs source changes, update the corresponding guide to match. Convert TSX to markdown, preserving structure and content. **Code wins on conflict** — if trinity-docs disagrees with `.env.example`, `scripts/deploy/*.sh`, or `docker-compose.yml`, fix the local guide to match observed repo behavior and note the divergence for upstream.

**Ops-runbook rule**: Pages under `guides/deploying/` (especially `upgrading.md`, `backup-and-restore.md`, `monitoring.md`) draw their *patterns* from the local private ops runbook. Treat that as a pattern library, not a paste source. See Step 2h for what's safe to import vs. what must stay private.

### Deployment config reading rules

When reading scripts and compose files to produce deployment docs:

1. Read `scripts/deploy/start.sh` literally — document what it **actually does**, not what comments say it does
2. Check `docker-compose.yml` environment blocks for which vars are **actually forwarded** to each service
3. For vars in `.env.example` marked `[PROD]` or `[OVERLAY]`, note their scope clearly — do not present them as universally available in dev compose
4. Never describe a feature as "auto" unless the script code proves it is
5. `verify-platform.sh` is the canonical six-probe checklist — reference it by name, don't duplicate it inline

### Operational guide template

Pages under `guides/deploying/` that cover operations (upgrade, backup, monitoring) follow this structure:

```markdown
## [Operation Name]

[One sentence: what this operation does and when to run it]

### Pre-flight

[What to check before starting. Backup steps. Smoke tests.]

### Steps

[Numbered, concrete commands. No vague instructions.]

### Verify

[How to confirm success. Commands to run. What to look for.]

### Rollback

[How to undo if something went wrong. Commands to restore prior state.]
```

## Target Structure

```
docs/user-docs/
├── README.md                          # Index + navigation
├── guides/                            # Tutorial-style walkthroughs
│   ├── deploying-trinity.md          # Hub: cloud vs self-hosted decision, links into spokes
│   ├── deploying/                    # Self-hosted deployment spokes (per-scenario + ops)
│   │   ├── local-development.md      # Docker Desktop, dev compose, hot reload, base image
│   │   ├── single-server.md          # VPS/bare-metal: prod compose, env, email, backups
│   │   ├── public-access.md          # Cloudflare Tunnel, PUBLIC_CHAT_URL, webhook surface
│   │   ├── upgrading.md              # Pre-flight → backup → rebuild → restart → verify → rollback
│   │   ├── backup-and-restore.md     # Volume-mounted alpine cp, retention, daily cron
│   │   ├── monitoring.md             # Six health probes, /api/ops/fleet/health, thresholds
│   │   └── ops-agent.md              # Trinity Ops Agent: connect, deploy, day-to-day ops
│   ├── using-trinity.md              # UI tour: dashboard, agents, monitoring
│   └── building-agents.md            # Create, develop, deploy with abilities
├── getting-started/
│   ├── overview.md                    # What is Trinity, key concepts
│   ├── setup.md                       # First-time setup, login, admin config
│   └── quick-start.md                # Create first agent in 5 minutes
├── agents/
│   ├── creating-agents.md            # Templates, GitHub repos, manual
│   ├── managing-agents.md            # Start/stop, rename, delete, health
│   ├── agent-chat.md                 # Chat interface, streaming, history
│   ├── agent-terminal.md             # Web terminal, SSH access
│   ├── agent-files.md                # File browser, virtual filesystem
│   ├── agent-logs.md                 # Log viewing, telemetry
│   └── agent-configuration.md        # Config tab, environment, runtime
├── credentials/
│   ├── credential-management.md      # Adding, editing, hot-reload
│   ├── oauth-credentials.md          # OAuth2 flow, Google setup
│   └── subscription-credentials.md   # Shared credentials across agents
├── collaboration/
│   ├── agent-network.md              # Multi-agent systems, DAGs
│   ├── agent-permissions.md          # Who can call whom
│   ├── event-subscriptions.md        # Pub/sub between agents
│   └── system-manifest.md           # System-wide configuration
├── automation/
│   ├── scheduling.md                 # Cron schedules, execution queue
│   ├── skills-and-playbooks.md       # Skills library, assignment, playbooks
│   ├── abilities-marketplace.md      # Plugin marketplace: install abilities, available plugins, onboarding
│   └── approvals.md                  # Human-in-the-loop approval gates
├── operations/
│   ├── dashboard.md                  # Main dashboard, timeline view
│   ├── operating-room.md             # Events, costs, system alerts
│   ├── monitoring.md                 # Health checks, system metrics
│   └── executions.md                # Execution list, detail, logs
├── sharing-and-access/
│   ├── agent-sharing.md              # Share with users, read-only mode
│   ├── public-links.md              # Public chat links, anonymous access
│   ├── tags-and-organization.md      # Tags, filtering, system views
│   └── mobile-admin.md              # Mobile PWA at /m
├── integrations/
│   ├── github-sync.md               # Git sync, branch support
│   ├── slack-integration.md          # Slack channels, routing
│   ├── mcp-server.md                # MCP tools, API keys
│   └── nevermined-payments.md        # x402 payments, monetization
├── advanced/
│   ├── voice-chat.md                 # Voice via Gemini Live API
│   ├── image-generation.md           # Platform image generation
│   ├── agent-avatars.md              # AI-generated avatars
│   └── dynamic-dashboards.md         # Custom agent dashboards
└── api-reference/
    ├── authentication.md             # JWT tokens, API keys, auth flow
    ├── agent-api.md                  # Agent CRUD, lifecycle endpoints
    ├── chat-api.md                   # Chat, voice, streaming endpoints
    └── webhook-triggers.md           # Remote triggers, event webhooks
```

## Process

### Step 1: Inventory Current State

Read what already exists in `docs/user-docs/` and build a checklist of files that need creating or updating.

```bash
find docs/user-docs -name "*.md" -type f 2>/dev/null | sort
```

### Step 2: Read Source Material

Read these sources to extract current feature state. Use parallel agents where possible.

**2a. Feature flows** — Read `docs/memory/feature-flows.md` (the index) to know which flows exist, then read individual flows as needed per section.

**2b. Requirements** — Read `docs/memory/requirements.md` for the canonical feature list and acceptance criteria.

**2c. Architecture** — Read `docs/memory/architecture.md` for system design context, component relationships, and data flow.

**2d. Recent changes** — Get recent changes from git history:
```bash
git log --oneline --since="2 weeks ago" | head -30
```
This identifies what has changed recently and which docs may need updating.

**2e. Deployment config (for deploying/setup guides)** — Read `.env.example`, `scripts/deploy/start.sh`, `scripts/deploy/stop.sh`, `scripts/deploy/build-base-image.sh`, `docker-compose.yml`, `docker-compose.prod.yml`, `docker-compose.gitea.yml`, and `deploy.config.example`. Extract: required env vars, auto-generated vs user-set secrets, default ports and override vars (e.g., `FRONTEND_PORT`), first-boot behavior, and what the start/stop scripts actually print and do. **Cross-check `.env.example` keys against the `environment:` blocks in each compose file** — flag any key present in `.env.example` but not forwarded by a given compose as "prod-only / overlay-only / requires compose edit," because docs should not promise behavior the chosen compose can't deliver. This is the authoritative source for deploy/setup docs.

**2f. Backend routers** — Glob `src/backend/routers/*.py` and read router files relevant to the section being written. Extract:
- Endpoint paths and HTTP methods
- Request/response patterns
- Business logic and validation rules

**2g. Frontend views** — Glob `src/frontend/src/views/*.vue` and read views relevant to the section being written. Extract:
- UI layout and tab structure
- User-facing labels and actions
- State management patterns

**2h. Ops-runbook patterns (for `guides/deploying/upgrading.md`, `backup-and-restore.md`, `monitoring.md`)** — Read the local private ops runbook: `playbooks/upgrade-instance.md`, `playbooks/monitoring-instance.md`, `instances/_template/CLAUDE.md`, and the helper scripts in `instances/_template/scripts/` (`update.sh`, `health-check.sh`, `restart.sh`, `status.sh`). Treat as a **pattern library**, not a paste source.

**Safe to import (the rules and shapes that make production work):**
- The "use `docker compose restart`, never `down/up`" rule and *why* (preserves agent containers and `trinity-agent-network`).
- The "rebuild only platform services, not the base image" rule (`docker compose build --no-cache backend frontend mcp-server scheduler`).
- The pre-flight → backup → pull → rebuild → restart → verify → rollback sequence shape.
- The six-probe verification list (backend `/health`, scheduler `:8001/health`, frontend HTTP 200, redis PONG, MCP `/health`, Vector `:8686/health`).
- The fleet-status API surface (`/api/ops/fleet/health`, `/api/ops/fleet/status`).
- The resource-thresholds table (warning/critical bands for context %, CPU, memory, disk, error rate, container restarts, DB size, log size).
- The volume-mounted alpine `cp` pattern for SQLite backup/restore.
- Common-recovery patterns (agent network not found, agent context >90%, database locked).
- The daily-DB-backup + 14-day-retention cron template.

**Forbidden to import (private detail; would leak in a public repo):**
- Any host name, IP address, Tailscale identity, or tailnet name.
- Any `sshpass`, `ssh -i`, or `ssh user@host`-prefixed command — local-host examples only.
- Any reference to specific instance directories (`instances/dgx/` etc.) or instance-specific scripts.
- Any password, token, or API-key value (even masked) from ops `.env` files.
- Any private repo name or internal path that reveals the ops repo's structure.
- Multi-instance management workflows (`source .env && ./scripts/run.sh ...`) — that's an operator-fleet pattern, not a single-instance user pattern.

When in doubt, rewrite the *idea* in localhost form. A production-ops `sshpass -p $PW ssh user@host "sudo docker logs trinity-backend"` becomes the public `docker logs trinity-backend`. The rule survives; the access pattern stays private.

**2i. Abilities repo (for `guides/building-agents.md` and `automation/abilities-marketplace.md`)** — Fetch the README from `github.com/abilityai/abilities`. Extract: what the abilities plugin is, the 5 plugins and what each covers (create-agent, agent-dev, trinity, dev-methodology, utilities), the installation command (`/plugin marketplace add abilityai/abilities`), and agent onboarding flow (`/trinity:onboard`). This is the canonical source for abilities marketplace docs — cover it explicitly in both the building-agents guide and a dedicated `automation/abilities-marketplace.md` page.

**2j. Ops agent repo (for `guides/deploying/ops-agent.md`)** — Fetch the README and CLAUDE.md from `github.com/abilityai/trinity-ops-public`. Extract: what the ops agent does (health checks, log tailing, restarts, updates, rollbacks, diagnostics, agent management), how to connect it to an instance (one `.env` pointed at any server), the `/trinity:deploy` wizard flow, and day-to-day command examples (`/status`, `/diagnose`). Document it as the recommended way to operate a Trinity instance post-deploy, contrasting it with raw Docker commands.

### Step 3: Generate/Update Documentation

For each section in the target structure, produce or update the markdown file following these rules:

#### Writing Rules

1. **MECE structure** — Each section covers a mutually exclusive, collectively exhaustive slice of functionality. No concept is explained in two places. If a concept spans sections, explain it once and cross-reference.

2. **Dual audience format** — Each doc follows this template:

```markdown
# [Feature Name]

[1-2 sentence summary of what this feature does and why it matters]

## Concepts

[Define key terms specific to this feature. Only terms not defined elsewhere.]

## How It Works

[Step-by-step explanation for human users. Describe UI workflows with
screen locations ("click the **Agents** tab in the sidebar"). Include
what the user sees at each step.]

## For Agents

[Programmatic usage. API endpoints with methods and paths. Link to
API docs at `/docs` (Swagger) for full request/response schemas.
Include example cURL or SDK snippets only when the pattern is
non-obvious.]

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/...` | GET | ... |

## Limitations

[Known constraints, edge cases, or things that don't work yet.
Only include if meaningful.]

## See Also

[Cross-references to related docs in this folder. Use relative links.]
```

**The template above is the minimum, not the maximum.** Add domain-specific sections between **How It Works** and **Limitations** when the content doesn't fit the default flow. Common extensions:

- **Decision tables** — when the feature exposes a meaningful user choice (e.g., classic vs. fine-grained PAT, source mode vs. working branch mode). Lead with the tradeoff, not the mechanics.
- **Workflow / pattern sections** — when the feature unlocks a multi-party workflow worth documenting explicitly (e.g., "Hosting Agents from External Contributors" for PATs, "Thread Routing" for Slack). These don't map cleanly to "How It Works" because they cross features or involve external actors.
- **Maintenance guidance** — when post-setup ongoing operations are non-obvious (e.g., "Updating the Token Later" for PATs, "Rotating Keys" for MCP). Covers what's editable in-place vs. what requires recreation.

Use judgment: if the same content keeps needing to sit awkwardly inside "How It Works" or "Limitations," promote it to its own `##` section.

#### Operational guide template (use for `guides/deploying/upgrading.md`, `backup-and-restore.md`, `monitoring.md`)

The dual-audience template above is for *features*. Operational guides are *procedures with checkpoints* — different shape. Use this instead:

```markdown
# [Procedure Name]

[1-2 sentence summary: what this procedure achieves and when an operator runs it.]

## When to Run This

[Trigger conditions. E.g. "Before every Trinity update," "Daily as a cron job,"
"When the dashboard sync-health dot goes red." Concrete, not aspirational.]

## Pre-flight

- [ ] [Check 1 — e.g., backup target has space]
- [ ] [Check 2 — e.g., no scheduled tasks running in the next N minutes]
- [ ] [Check 3 — e.g., on the right branch / version]

## Procedure

### Step 1: [name]

[What and why. Then the command in a code block. Then "expected output: ..."]

### Step 2: [name]
...

## Verify

After the procedure, confirm each of these returns the expected result. If any
fails, do NOT proceed — go to **Rollback** or **Recovery** below.

| Check | Command | Expected |
|-------|---------|----------|
| Backend | `curl -s http://localhost:8000/health` | `{"status":"healthy",...}` |
| ... | ... | ... |

## Rollback / Recovery

[For procedures with destructive or risky steps, include the inverse procedure.
For monitoring guides, include common-issue resolution table instead.]

## Automation

[Optional: cron template, CI hook, or scheduling guidance if the procedure is
intended to run regularly.]

## See Also
```

**Why this shape:** operators care about (1) when to run, (2) safety pre-checks, (3) numbered steps, (4) explicit verification, (5) rollback. The feature template's "How It Works / For Agents / Limitations" doesn't map to any of those.

#### Reusable snippets for operational guides

The following are canonical snippets. Use verbatim across the deploy spokes so the same rule reads identically in every place. Do **not** paraphrase; consistency is the point.

**The "use restart, never down/up" rule:**

> **Use `docker compose restart`, not `down/up`.** `docker compose down` removes the `trinity-agent-network`, which orphans every running agent container — they keep running but lose their network and have to be removed and recreated. `restart` preserves both the agents and the network. The only times to use `down` are: (1) intentional full teardown, (2) recovering from a corrupted compose state.

**The "rebuild platform services only" rule:**

> When updating Trinity code, rebuild the platform images only:
> ```bash
> docker compose build --no-cache backend frontend mcp-server scheduler
> ```
> The `trinity-agent-base` image is **not** rebuilt by this command. It changes much less often, and rebuilding it forces every agent to be re-deployed. Rebuild it only when `docker/base-image/Dockerfile` itself changes, via `./scripts/deploy/build-base-image.sh`.

**The six-probe verification list:**

```bash
# 1. Backend
curl -s http://localhost:8000/health
# Expected: {"status":"healthy",...}

# 2. Scheduler
curl -s http://localhost:8001/health
# Expected: {"status":"healthy","active_schedules":N}

# 3. Frontend (HTTP 200)
curl -s -o /dev/null -w '%{http_code}' http://localhost
# Expected: 200

# 4. Redis
docker exec trinity-redis redis-cli ping
# Expected: PONG

# 5. MCP Server
curl -s http://localhost:8080/health
# Expected: 200 OK

# 6. Vector (log aggregation)
docker exec trinity-vector wget -q -O - http://localhost:8686/health
# Expected: non-empty response
```

**Resource thresholds table:**

| Metric | Warning | Critical | Action |
|---|---|---|---|
| Backend `/health` | — | not 200 | Restart `trinity-backend` |
| Scheduler `/health` | — | not 200 | Restart `trinity-scheduler` |
| Agent context usage | >75% | >90% | Reset agent context or restart agent container |
| Host CPU | >80% | >95% | Investigate runaway processes |
| Host memory | >85% | >95% | Check container memory limits |
| Disk free | <20% | <5% | Prune Docker, archive logs |
| Error rate (per hour) | >10 | >50 | Inspect platform.json log |
| Container restarts | any | repeated | `docker logs <container>` |
| `trinity.db` size | >1 GB | >5 GB | Archive old data |
| Vector log size | >5 GB | >10 GB | Trigger archival rotation |

**SQLite backup pattern (volume-mounted alpine):**

```bash
# Backup (run on the host, with services running)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /data/trinity.db /backup/trinity-$(date +%Y%m%d-%H%M%S).db

# Restore (services stopped first)
docker compose stop backend scheduler
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /backup/trinity-YYYYMMDD-HHMMSS.db /data/trinity.db
docker compose start backend scheduler
```

3. **No redundancy** — Do not repeat information from other docs. Cross-reference instead. The `Concepts` section in `getting-started/overview.md` is the canonical glossary; other docs reference it rather than re-defining terms.

4. **Code-derived accuracy** — Every claim must trace to code or a feature flow. Do not invent features. If a feature flow says "planned" or a router has TODO comments, note it as upcoming rather than documenting it as available.

5. **Clear, direct tone** — Active voice. Short sentences. No filler ("In order to", "It should be noted that"). Say what happens, not what "can" happen.

6. **Placeholder values** — Use `your-domain.com`, `your-api-key`, `user@example.com` in all examples. Never include real credentials or internal URLs.

7. **External references for integration docs** — Docs under `integrations/*.md` document features that sit on top of third-party services (GitHub, Slack, Telegram, Twilio, Nevermined, etc.). For each, the `See Also` section must include an `**External references:**` subsection with:

   - **The authoritative vendor doc** — the canonical page from the vendor describing the underlying primitive (e.g., [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) for GitHub PATs). Prefer the evergreen docs URL over blog posts.
   - **A broader-context vendor doc** if useful (e.g., "About authentication to GitHub" for the full menu of auth methods).
   - **One clear third-party explainer** when the vendor's own docs are dense or skip the "why." Check the article is current (within ~2 years) and pedagogically clean before linking. Skip this if the vendor docs are already accessible.

   Split `See Also` into **Trinity docs:** and **External references:** subsections so the distinction is visible.

### Step 4: Generate README.md Index

Create `docs/user-docs/README.md` as the entry point:

```markdown
# Trinity User Documentation

> Auto-generated from source code. Run `/generate-user-docs` to update.

## Getting Started
- [Overview](getting-started/overview.md) — What is Trinity
- [Setup](getting-started/setup.md) — Installation and first login
- [Quick Start](getting-started/quick-start.md) — Create your first agent

## Agents
- [Creating Agents](agents/creating-agents.md)
- [Managing Agents](agents/managing-agents.md)
...
```

List every file with a one-line description. Group by section folder.

### Step 5: Diff Review (Approval Gate)

**STOP and present changes to the user before writing.**

Show:
- Files created (new)
- Files updated (with summary of what changed)
- Files unchanged (skipped)

Ask: "Write these changes?" Only proceed after confirmation.

### Step 6: Write Files

Create directories and write all approved files:

```bash
mkdir -p docs/user-docs/{getting-started,agents,credentials,collaboration,automation,operations,sharing-and-access,integrations,advanced,api-reference}
mkdir -p docs/user-docs/guides/deploying
```

Write each markdown file using the Write or Edit tool.

### Step 7: Verify

**Count files written:**

```bash
find docs/user-docs -name "*.md" -type f | wc -l
```

**Public-safety scan** — every page generated under `guides/deploying/` must pass these greps with zero hits. If any hit, the page is leaking ops-internal detail and must be rewritten:

```bash
# Tokens that indicate private-repo or operator-fleet patterns
grep -rE 'sshpass|tailnet|ts\.net|ssh -i [^ ]+ [^ ]+@' docs/user-docs/guides/deploying/

# Real IPs (allow only loopback and the documented agent subnet 172.28.0.0/16)
grep -rE '\b(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]+\.[0-9]+' docs/user-docs/guides/deploying/ \
  | grep -vE '172\.28\.0\.[0-9]+/16'

# Instance directory references that hint at private-repo structure
grep -rE 'instances/[a-z0-9_-]+/' docs/user-docs/guides/deploying/
```

Confirm the expected number of files were created/updated. Report the final count.

## Completion Checklist

- [ ] All sections in target structure have corresponding files (including `guides/deploying/` spokes)
- [ ] Every feature doc follows the dual-audience template (How It Works + For Agents)
- [ ] Every operational doc under `guides/deploying/` follows the operational template (When to Run → Pre-flight → Procedure → Verify → Rollback)
- [ ] No redundant explanations across docs (MECE verified)
- [ ] All API references link to Swagger (`/docs`) rather than duplicating schemas
- [ ] Every key in `.env.example` is annotated in the relevant deploy spoke as: dev-compose / prod-compose-only / overlay-only / requires-compose-edit (no key promised to work in a compose that doesn't forward it)
- [ ] Reusable snippets (the "use restart" rule, "rebuild platform services only" rule, six-probe verification, thresholds table, alpine `cp` backup pattern) are used verbatim, not paraphrased, across deploy spokes
- [ ] No real credentials, internal URLs, host IPs, or PII in any doc
- [ ] Public-safety greps from Step 7 return zero hits across `guides/deploying/`
- [ ] README.md index is complete and links are valid
- [ ] Changes reviewed by user before writing
- [ ] `automation/abilities-marketplace.md` covers the plugin marketplace install command, all 5 abilities plugins, and the `/trinity:onboard` agent setup flow
- [ ] `guides/deploying/ops-agent.md` covers what the ops agent is, how to connect it via `/trinity:deploy`, and the day-to-day operational commands

## Error Recovery

| Error | Recovery |
|-------|----------|
| Feature flow missing for a section | Write doc from router code + view code; note "No feature flow available" |
| Router has no docstrings | Read function bodies and URL patterns to infer behavior |
| Conflicting info between requirements and code | Trust code (source of truth); note discrepancy |
| Existing doc is manually edited | Preserve manual edits; append auto-generated sections below a separator |
| Section has no corresponding code yet | Mark as "Planned" with brief description from requirements.md |

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/generate-user-docs/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(generate-user-docs): <brief improvement description>"`
