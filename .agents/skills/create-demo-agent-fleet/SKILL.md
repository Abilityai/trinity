---
name: create-demo-agent-fleet
description: Create and configure a demo agent fleet to showcase Trinity's capabilities. Supports Acme Consulting (3 agents), Research Network (2 agents), or GitHub templates.
allowed-tools: [Bash, Read]
user-invocable: true
argument-hint: "[acme|research|github]"
automation: gated
---

# Create Demo Agent Fleet

Create and configure a demo agent fleet to showcase Trinity's capabilities.

> **Note**: Process Engine is dormant (see `docs/memory/architecture.md` — "OUT OF SCOPE"). The Acme Consulting option's business-process steps (create/publish/execute processes, `/api/processes*` endpoints, `/processes` UI) are non-functional. Agent deployment, shared folders, schedules, and agent-to-agent collaboration still work.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Trinity Platform | `http://localhost:8000/api` | ✅ | ✅ | Agent CRUD |
| Manifests | `config/manifests/` | ✅ | | System manifests |
| Templates | `config/agent-templates/` | ✅ | | Agent templates |
| Process Templates | `config/process-templates/` | ✅ | | Business processes |
| GitHub | GitHub API | ✅ | | Template repos |

## Quick Start (Acme Consulting)

1. Deploy agents via manifest
2. Create business processes from templates
3. Configure schedules for automation
4. Enable autonomy mode
5. Run initial tasks to build history

---

## Option A: Acme Consulting (Recommended)

A business-focused 3-agent consulting team demonstrating:
- Agent-to-agent collaboration via MCP
- File-based collaboration via shared folders
- Scheduled automation

| Agent | Template | Role |
|-------|----------|------|
| **acme-scout** | `local:scout` | Market Research Analyst - finds trends, competitors, opportunities |
| **acme-sage** | `local:sage` | Strategic Advisor - synthesizes research, develops strategy |
| **acme-scribe** | `local:scribe` | Content Writer - creates reports, proposals, deliverables |

### Business Processes

| Process | Steps | Description |
|---------|-------|-------------|
| **client-onboarding** | 4 | Research → Strategy → Human Approval → Proposal |
| **market-analysis** | 4 | Research → Synthesis → Human Review → Report |
| **weekly-brief** | 3 | Intelligence → Commentary → Executive Brief |

### Prerequisites

1. **Trinity platform running** - `./scripts/deploy/start.sh`
2. **Logged in as admin** - Get JWT token for API calls
3. No external credentials required

### Step 1: Deploy Agents

Deploy all 3 agents with permissions and shared folders via system manifest:

**Via MCP:**
```
mcp__trinity__deploy_system(manifest="""
name: acme
description: Acme Consulting AI Team - Scout (Research), Sage (Strategy), Scribe (Content)

agents:
  scout:
    template: local:scout
    folders:
      expose: true
      consume: false
    resources:
      memory: 2g
      cpu: 1.0

  sage:
    template: local:sage
    folders:
      expose: true
      consume: true
    resources:
      memory: 2g
      cpu: 1.0

  scribe:
    template: local:scribe
    folders:
      expose: true
      consume: true
    resources:
      memory: 2g
      cpu: 1.0

permissions:
  preset: full-mesh

auto_start: true

trinity_prompt: |
  You are part of the Acme Consulting AI team.
  Team members:
  - Scout (acme-scout): Market research analyst
  - Sage (acme-sage): Strategic advisor
  - Scribe (acme-scribe): Content writer
  Collaborate effectively using shared folders and Trinity MCP.
""")
```

**Or via API (manifest file exists at `config/manifests/acme-consulting.yaml`):**
```bash
# Get auth token first
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=YOUR_PASSWORD" | jq -r '.access_token')

# Deploy using existing manifest
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/manifests/deploy \
  -d @config/manifests/acme-consulting.yaml
```

**Verify agents are running:**
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents | \
  jq '.[] | select(.name | startswith("acme")) | {name, status}'
```

### Step 2: Create Business Processes

Create processes from the bundled templates. Navigate to http://localhost/processes and use "Create from Template", or use the API.

**Via UI:**
1. Go to http://localhost/processes
2. Click "Create Process" → "From Template"
3. Select each template: `weekly-brief`, `market-analysis`, `client-onboarding`
4. Publish each process after creation

**Via API:**
```bash
# Create weekly-brief process from template
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/process-templates/weekly-brief/use

# Create market-analysis process
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/process-templates/market-analysis/use

# Create client-onboarding process
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/process-templates/client-onboarding/use
```

### Step 3: Configure Schedules

Set up recurring automation for the agents:

**Scout - Weekly Trend Scan (Mondays 9am):**
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/agents/acme-scout/schedules \
  -d '{
    "name": "Weekly Trend Scan",
    "cron_expression": "0 9 * * 1",
    "message": "/trends AI and automation - scan for emerging trends in enterprise AI, automation tools, and agentic systems",
    "enabled": true,
    "timezone": "America/Los_Angeles",
    "description": "Weekly scan of AI and automation trends every Monday at 9am"
  }'
```

**Sage - Weekly Strategic Briefing (Mondays 10am):**
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/agents/acme-sage/schedules \
  -d '{
    "name": "Weekly Strategic Briefing",
    "cron_expression": "0 10 * * 1",
    "message": "/briefing AI market - Create executive briefing synthesizing recent research from Scout",
    "enabled": true,
    "timezone": "America/Los_Angeles",
    "description": "Weekly executive briefing every Monday at 10am"
  }'
```

**Scribe - Weekly Summary (Fridays 2pm):**
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/agents/acme-scribe/schedules \
  -d '{
    "name": "Weekly Summary Compilation",
    "cron_expression": "0 14 * * 5",
    "message": "/summary team outputs - Create weekly summary of all team research and strategy outputs",
    "enabled": true,
    "timezone": "America/Los_Angeles",
    "description": "Weekly summary every Friday at 2pm"
  }'
```

### Step 4: Enable Autonomy Mode (Optional)

Enable autonomy to activate all schedules for an agent:

**Via UI:**
- Go to agent detail page → Toggle "AUTO/Manual" switch in header

**Via API:**
```bash
# Enable autonomy for all demo agents
for agent in acme-scout acme-sage acme-scribe; do
  curl -X PUT -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "http://localhost:8000/api/agents/$agent/autonomy" \
    -d '{"enabled": true}'
done
```

### Step 5: Build Demo History

Run initial tasks to populate the Dashboard Timeline with activity.

### Step 6: Verify Everything Works

**Check agent status:**
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents | \
  jq '.[] | select(.name | startswith("acme")) | {name, status, autonomy_enabled}'
```

### Cleanup (Option A)

```bash
# Delete agents
for agent in acme-scout acme-sage acme-scribe; do
  curl -X DELETE -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8000/api/agents/$agent"
done
```

---

## Option B: Research Network

A simpler 2-agent system demonstrating shared folders and inter-agent collaboration.

| Agent | Template | Role |
|-------|----------|------|
| **researcher** | `local:demo-researcher` | Autonomous researcher - discovers trends, writes findings |
| **analyst** | `local:demo-analyst` | Strategic analyst - synthesizes research, generates briefings |

### Prerequisites

1. **Trinity platform running** - `./scripts/deploy/start.sh`
2. No external credentials required

### Deploy via System Manifest

```
mcp__trinity__deploy_system(manifest="""
name: research-network
description: Research and analysis demo system

agents:
  researcher:
    template: local:demo-researcher
    folders:
      expose: true
      consume: false

  analyst:
    template: local:demo-analyst
    folders:
      expose: false
      consume: true

permissions:
  preset: full-mesh
""")
```

### Cleanup (Option B)

```
mcp__trinity__delete_agent(name="research-network-researcher")
mcp__trinity__delete_agent(name="research-network-analyst")
```

---

## Option C: GitHub Templates (Cornelius, Corbin, Ruby)

Production-style agents from GitHub repositories. Demonstrates GitHub template cloning and source mode sync.

| Agent | Template | Role |
|-------|----------|------|
| **Cornelius** | `github:abilityai/agent-cornelius` | Knowledge Base Manager - Obsidian vault, insights |
| **Corbin** | `github:abilityai/agent-corbin` | Business Assistant - Google Workspace, contacts |
| **Ruby** | `github:abilityai/agent-ruby` | Content Creator - Content strategy, publishing |

### Prerequisites

1. **GitHub PAT must be configured** - Set `GITHUB_PAT` in `.env` and restart backend
2. **Trinity platform running** - `./scripts/deploy/start.sh`

### Step 1: Verify GitHub PAT

```
mcp__trinity__list_templates()
```

If GitHub templates show errors, check `.env` has `GITHUB_PAT=github_pat_xxx`.

### Step 2: Create Agents

```
mcp__trinity__create_agent(name="cornelius", template="github:abilityai/agent-cornelius")
mcp__trinity__create_agent(name="corbin", template="github:abilityai/agent-corbin")
mcp__trinity__create_agent(name="ruby", template="github:abilityai/agent-ruby")
```

### Cleanup (Option C)

```
mcp__trinity__delete_agent(name="cornelius")
mcp__trinity__delete_agent(name="corbin")
mcp__trinity__delete_agent(name="ruby")
```

---

## Quick Reference: API Endpoints

| Action | Method | Endpoint |
|--------|--------|----------|
| List agents | GET | `/api/agents` |
| Create agent | POST | `/api/agents` |
| Delete agent | DELETE | `/api/agents/{name}` |
| Get token | POST | `/api/token` |
| List schedules | GET | `/api/agents/{name}/schedules` |
| Create schedule | POST | `/api/agents/{name}/schedules` |
| Toggle autonomy | PUT | `/api/agents/{name}/autonomy` |
| Run task | POST | `/api/agents/{name}/task` |
| List processes | GET | `/api/processes` |
| Execute process | POST | `/api/executions/processes/{id}/execute` |
| Get executions | GET | `/api/executions` |
| Activity timeline | GET | `/api/activities/timeline` |

---

## Demo Checklist

After setup, verify these work:

- [ ] All agents running (Dashboard shows green status)
- [ ] Shared folders configured (agents can read each other's outputs)
- [ ] Schedules created (visible in agent Schedules tab)
- [ ] At least one process published (visible in /processes)
- [ ] Execution history exists (Tasks tab shows completed tasks)
- [ ] Activity timeline populated (Dashboard Timeline view shows events)
- [ ] Agent collaboration works (Sage can trigger Scout via MCP)

---

## Notes

- **Local templates** (`local:*`) require no external dependencies
- **GitHub templates** require a valid GitHub PAT with repo access
- **Manifest file**: Pre-configured at `config/manifests/acme-consulting.yaml`
- **Process templates**: Located in `config/process-templates/`
- **Timezone**: Schedules default to America/Los_Angeles - adjust as needed
- Run `/agent-status` to check fleet health after creation

## Completion Checklist

- [ ] Option selected (Acme/Research/GitHub)
- [ ] Prerequisites verified
- [ ] Agents deployed
- [ ] Agents running
- [ ] Processes created (Acme only)
- [ ] Schedules configured (Acme only)
- [ ] Autonomy enabled (optional)
- [ ] Demo history built
- [ ] All verification checks passed
