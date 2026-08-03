# AI Venture Capital Due Diligence System

> **"9 analysts. One orchestrator. Complete due diligence."**

A multi-agent system where a Deal Lead orchestrates 9 specialist agents to perform comprehensive startup due diligence, then synthesizes findings into an investment recommendation.

**Fully local. No external services required.** All results stored in shared folders within Trinity.

## The Demo

**The Hook**: Provide a company name or pitch deck link. Watch `dd-lead` orchestrate 9 AI analysts via agent-to-agent calls. See arrows appear on the Dashboard Timeline as each specialist is called.

**The Experience**: Watch the orchestration unfold — dd-lead calls each specialist sequentially, arrows appear one-by-one, each specialist researches their domain and returns findings.

**The Viral Moment**: The Dashboard Timeline showing arrows between agents — a living AI investment firm where you can see the delegation happening in real-time.

**The Deliverable**: All analysis saved to `dd-lead`'s shared folder — accessible via Trinity File Manager.

---

## Quick Start

### 1. Clean Up (if redeploying)

If you have existing DD agents, clean them up first:

```bash
# Delete existing DD agents
TOKEN="YOUR_TOKEN"
for agent in $(curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agents" | jq -r '.[].name' | grep vc-due-diligence); do
  echo "Deleting: $agent"
  curl -s -X DELETE -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agents/$agent"
done
```

### 2. Deploy the Agent Fleet

> **The manifest moved (#1931).** The canonical, permission-wired manifest is
> now **[`config/manifests/vc-due-diligence.yaml`](../../../config/manifests/vc-due-diligence.yaml)**,
> alongside the other bundled manifests and covered by CI. It deploys the same
> eleven agents under the same names, and additionally wires the `dd-lead` →
> specialist permissions, so **step 3 below is no longer required** when you
> deploy it. The copy in this directory is kept for reference only — read its
> header before using it.
>
> The eleven `dd-*` templates are `hidden: true` since #1931 (a demo, not a
> starter), so they no longer appear in the Library. They remain fully
> creatable by id (`local:dd-lead`) and deployable as a set by this manifest.

```bash
TOKEN="YOUR_TOKEN"
MANIFEST=$(cat config/manifests/vc-due-diligence.yaml)

curl -s -X POST "http://localhost:8000/api/systems/deploy" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg m "$MANIFEST" '{manifest: $m, dry_run: false}')"
```

This deploys 11 agents (all auto-started):
- `vc-due-diligence-dd-intake` - Pitch deck parser
- `vc-due-diligence-dd-founder` - Founder background checks
- `vc-due-diligence-dd-market` - Market research & validation
- `vc-due-diligence-dd-competitor` - Competitive intelligence
- `vc-due-diligence-dd-tech` - Technical due diligence
- `vc-due-diligence-dd-bizmodel` - Business model analysis
- `vc-due-diligence-dd-traction` - Traction & financials
- `vc-due-diligence-dd-compliance` - Regulatory compliance
- `vc-due-diligence-dd-captable` - Cap table analysis
- `vc-due-diligence-dd-legal` - Legal review
- `vc-due-diligence-dd-lead` - **Deal lead (orchestrator)**

### 3. Configure Permissions (only if you deployed the legacy copy)

**Skip this step** if you deployed `config/manifests/vc-due-diligence.yaml` (step 2) — it declares the permissions itself.

**This step is critical for agent-to-agent communication.** Without permissions, dd-lead cannot call other agents.

Note the difference in scope: the canonical manifest grants **nine** edges — `dd-lead` → each of the nine specialists — because that is the only delegation any template's prompt actually performs (`dd-lead/CLAUDE.md` is the sole file containing `chat_with_agent`; the other ten contain none). The wider set below (`dd-intake` → specialists, `dd-legal` → `dd-lead`) is aspirational: no prompt exercises it.

```bash
TOKEN="YOUR_TOKEN"
PREFIX="vc-due-diligence"

# dd-lead can orchestrate ALL agents
curl -s -X PUT "http://localhost:8000/api/agents/$PREFIX-dd-lead/permissions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"permitted_agents\": [
    \"$PREFIX-dd-intake\", \"$PREFIX-dd-founder\", \"$PREFIX-dd-market\",
    \"$PREFIX-dd-competitor\", \"$PREFIX-dd-tech\", \"$PREFIX-dd-bizmodel\",
    \"$PREFIX-dd-traction\", \"$PREFIX-dd-compliance\", \"$PREFIX-dd-captable\",
    \"$PREFIX-dd-legal\"
  ]}"

# dd-intake can distribute to specialists
curl -s -X PUT "http://localhost:8000/api/agents/$PREFIX-dd-intake/permissions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"permitted_agents\": [
    \"$PREFIX-dd-founder\", \"$PREFIX-dd-market\", \"$PREFIX-dd-competitor\",
    \"$PREFIX-dd-tech\", \"$PREFIX-dd-bizmodel\", \"$PREFIX-dd-traction\",
    \"$PREFIX-dd-compliance\", \"$PREFIX-dd-captable\", \"$PREFIX-dd-legal\"
  ]}"

# dd-legal can call dd-lead (for term sheet coordination)
curl -s -X PUT "http://localhost:8000/api/agents/$PREFIX-dd-legal/permissions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"permitted_agents\": [\"$PREFIX-dd-lead\"]}"

echo "Permissions configured!"
```

Or via UI: **Agents → {agent} → Permissions tab → Add permitted agents**

### 4. Run Due Diligence (The Demo!)

Send a task to `dd-lead` — it will orchestrate the other agents sequentially.

**Via UI (recommended for demo):**
1. Go to **Agents** → **vc-due-diligence-dd-lead** → **Terminal**
2. Enter a task like:

```
Perform due diligence on [COMPANY NAME].

Use mcp__trinity__chat_with_agent to delegate to your specialist team. Call each with parallel=true and timeout_seconds=600 for longer research tasks:

1. vc-due-diligence-dd-founder: Research the founding team and leadership
2. vc-due-diligence-dd-market: Analyze the market size and opportunity
3. vc-due-diligence-dd-competitor: Identify competitors and competitive positioning
4. vc-due-diligence-dd-tech: Assess the technology and technical moat
5. vc-due-diligence-dd-bizmodel: Evaluate the business model
6. vc-due-diligence-dd-traction: Research revenue and growth metrics
7. vc-due-diligence-dd-compliance: Check regulatory considerations
8. vc-due-diligence-dd-captable: Research investors and funding history
9. vc-due-diligence-dd-legal: Note any legal issues

After receiving all reports, synthesize findings into an investment recommendation.
Save your final analysis to ~/shared-out/[company]-analysis.md
```

**Via API:**
```bash
TOKEN="YOUR_TOKEN"

curl -s -X POST "http://localhost:8000/api/agents/vc-due-diligence-dd-lead/task" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Perform due diligence on Anthropic. Use mcp__trinity__chat_with_agent (with parallel=true, timeout_seconds=600) to delegate to your specialist team: vc-due-diligence-dd-founder, dd-market, dd-competitor, dd-tech, dd-bizmodel, dd-traction, dd-compliance, dd-captable, dd-legal. After all reports are received, synthesize into ~/shared-out/anthropic-analysis.md",
    "max_turns": 30,
    "timeout_seconds": 1800
  }'
```

*Note: With 9 specialists at ~2 min each, expect ~20 minutes total. Use `timeout_seconds=1800` (30 min) to be safe.*

### 5. Watch the Dashboard

Open **http://localhost** (Dashboard view) and watch:
1. **Arrows appear** as dd-lead calls specialist agents
2. **Agent nodes light up** as they receive and process tasks
3. **Timeline shows** real-time agent collaboration events
4. **Results converge** as dd-lead synthesizes findings

### 6. Retrieve Results

All deliverables are stored in **dd-lead's shared folder**:

```
vc-due-diligence-dd-lead/shared-out/
├── [company]-analysis.md     # Full investment analysis
└── investment-recommendation/
    └── investment_recommendation.json  # Structured data
```

**Access via Trinity UI**:
- **Agents** → **vc-due-diligence-dd-lead** → **Files** tab → **shared-out/**
- Or **File Manager** → Select **vc-due-diligence-dd-lead**

---

## How Orchestration Works

The key to this demo is **agent-to-agent communication** via the Trinity MCP tools:

1. **dd-lead** receives your task
2. dd-lead uses `mcp__trinity__chat_with_agent` to call each specialist **sequentially**
3. Each call creates an **arrow on the Dashboard** (collaboration event)
4. Specialist researches and returns results to dd-lead
5. dd-lead calls the next specialist, and so on
6. After all specialists complete, dd-lead synthesizes findings into final recommendation

### Sequential Execution

Claude Code executes tool calls one at a time, so dd-lead calls specialists sequentially:
```
dd-lead → dd-founder (waits for result)
dd-lead → dd-market (waits for result)
dd-lead → dd-competitor (waits for result)
... and so on
```

Each arrow appears on the Dashboard as the call happens. Total time depends on how long each specialist takes.

### chat_with_agent Modes

| Mode | Parameter | Endpoint | Timeout | Use Case |
|------|-----------|----------|---------|----------|
| Sequential | `parallel=false` (default) | `/api/chat` | **Hardcoded 300s** | Multi-turn conversations |
| Parallel | `parallel=true` | `/api/task` | **Configurable** | Independent tasks, longer timeouts |

**For orchestration, use `parallel=true`** to get configurable timeouts (the 300s default may not be enough for research tasks).

**Without permissions configured**, dd-lead cannot call other agents and the orchestration fails silently.

---

## Architecture

```
                              ┌─────────────────────────┐
                              │   USER TASK             │
                              │   "Analyze Company X"   │
                              └───────────┬─────────────┘
                                          │
                              ┌───────────▼─────────────┐
                              │   DD-LEAD (Orchestrator)│
                              │   Calls specialists     │
                              │   one-by-one            │
                              └───────────┬─────────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                     │
                    ▼                     ▼                     ▼
              ┌───────────┐         ┌───────────┐         ┌───────────┐
              │ 1. FOUNDER│ ──────► │ 2. MARKET │ ──────► │ 3. COMPET.│
              └───────────┘         └───────────┘         └───────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                     │
                    ▼                     ▼                     ▼
              ┌───────────┐         ┌───────────┐         ┌───────────┐
              │ 4. TECH   │ ──────► │ 5. BIZMOD │ ──────► │ 6. TRACT. │
              └───────────┘         └───────────┘         └───────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                     │
                    ▼                     ▼                     ▼
              ┌───────────┐         ┌───────────┐         ┌───────────┐
              │7. COMPLNC │ ──────► │ 8. CAPTAB │ ──────► │ 9. LEGAL  │
              └───────────┘         └───────────┘         └───────────┘
                                          │
                              ┌───────────▼─────────────┐
                              │   DD-LEAD               │
                              │   Synthesizes findings  │
                              │   Creates recommendation│
                              └───────────┬─────────────┘
                                          │
                              ┌───────────▼─────────────┐
                              │   shared-out/           │
                              │   [company]-analysis.md │
                              └─────────────────────────┘

  Dashboard shows 9 ARROWS appearing sequentially as dd-lead calls each specialist
```

---

## Agent Fleet Details

| Agent | Full Name | Purpose | Resources |
|-------|-----------|---------|-----------|
| dd-lead | `vc-due-diligence-dd-lead` | **Orchestrator** - delegates tasks, synthesizes results | 8GB / 4 CPU |
| dd-intake | `vc-due-diligence-dd-intake` | Parse pitch decks, extract structured data | 2GB / 1 CPU |
| dd-founder | `vc-due-diligence-dd-founder` | Background checks, track record verification | 4GB / 2 CPU |
| dd-market | `vc-due-diligence-dd-market` | TAM/SAM/SOM validation, growth rates | 4GB / 2 CPU |
| dd-competitor | `vc-due-diligence-dd-competitor` | Competitive landscape, market share | 4GB / 2 CPU |
| dd-tech | `vc-due-diligence-dd-tech` | Technology assessment, IP evaluation | 4GB / 2 CPU |
| dd-bizmodel | `vc-due-diligence-dd-bizmodel` | Revenue model, unit economics | 2GB / 1 CPU |
| dd-traction | `vc-due-diligence-dd-traction` | Growth metrics, financial health | 4GB / 2 CPU |
| dd-compliance | `vc-due-diligence-dd-compliance` | Regulatory landscape, compliance | 2GB / 1 CPU |
| dd-captable | `vc-due-diligence-dd-captable` | Equity structure, investor analysis | 2GB / 1 CPU |
| dd-legal | `vc-due-diligence-dd-legal` | Corporate structure, IP ownership | 4GB / 2 CPU |

**Total Resources**: ~40GB RAM, 20 CPUs

---

## Troubleshooting

### No arrows on Dashboard?

1. **Check permissions are configured**:
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/api/agents/vc-due-diligence-dd-lead/permissions"
   ```
   Should show 10 permitted agents.

2. **Refresh the Dashboard** (Cmd+R / F5)

3. **Check collaboration events exist**:
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:8000/api/activities/timeline?activity_types=agent_collaboration&limit=10"
   ```

### Agents not calling each other?

- dd-lead must use `mcp__trinity__chat_with_agent` tool
- Include agent names in your prompt to dd-lead
- Check permissions are set (see step 3)

### Tasks timing out?

**Symptom:** Execution shows "HTTP error: ReadTimeout" after ~300 seconds, but agent logs show it actually completed.

**Root cause:** The default `parallel=false` mode has a hardcoded 300s backend timeout. Research tasks often take longer.

**Fix:** Use `parallel=true` with a longer `timeout_seconds`:
```
mcp__trinity__chat_with_agent(
  agent_name="vc-due-diligence-dd-founder",
  message="Research founders...",
  parallel=true,           # <-- Enables configurable timeout
  timeout_seconds=600      # <-- 10 minutes instead of 5
)
```

Also increase the main task timeout when calling dd-lead via API:
```json
{"timeout_seconds": 1800}  // 30 minutes for full orchestration
```

### Results not appearing?

- Check `shared-out/` folder in each specialist
- dd-lead reads from `shared-in/` (mounted from specialists)
- Verify the task completed (check execution status in UI)

---

## Demo Script

**[0:00]** "Watch an AI VC firm analyze a startup."

**[0:05]** Open dd-lead terminal, paste task with company name

**[0:10]** Switch to Dashboard - first arrow appears:
- dd-lead → dd-founder (researching founders)

**[1-2 min per specialist]** Watch arrows appear sequentially:
- dd-lead → dd-market
- dd-lead → dd-competitor
- dd-lead → dd-tech
- ... (9 specialists total)

**[~15-20 min]** All specialists complete:
- 9 arrows on Dashboard (all green/completed)
- dd-lead synthesizes all findings

**[Final]** Results appear:
- Open dd-lead → Files → shared-out/
- Full investment analysis with recommendation

**"What takes VCs 2 weeks. 20 minutes. 9 agents. Watch the orchestration."**

*Note: For a faster demo, reduce the number of specialists or give simpler tasks.*

---

## Optional: Skills Assignment

For enhanced methodology, assign skills to agents:

```bash
TOKEN="YOUR_TOKEN"
PREFIX="vc-due-diligence"

# Assign skills to all specialists
for agent in dd-intake dd-founder dd-market dd-competitor dd-tech dd-bizmodel dd-traction dd-compliance dd-captable dd-legal; do
  curl -s -X PUT "http://localhost:8000/api/agents/$PREFIX-$agent/skills" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"skills": ["dd-verification", "dd-sourcing", "dd-report-format"]}'
done

# Additional skills for dd-lead
curl -s -X PUT "http://localhost:8000/api/agents/$PREFIX-dd-lead/skills" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"skills": ["dd-verification", "dd-sourcing", "dd-report-format", "dd-risk-scoring", "dd-synthesis"]}'
```

Skills are in `docs/demos/vc-due-diligence/skills/`.

---

## Files

```
docs/demos/vc-due-diligence/
├── README.md                    # This file
├── system-manifest.yaml         # Legacy copy — canonical is config/manifests/vc-due-diligence.yaml (#1931)
└── skills/                      # Optional methodology skills
    ├── dd-verification/
    ├── dd-sourcing/
    ├── dd-report-format/
    ├── dd-risk-scoring/
    └── dd-synthesis/
```

---

*"Your startup's fate. Decided by agents. In 60 seconds. Watch the arrows."*
