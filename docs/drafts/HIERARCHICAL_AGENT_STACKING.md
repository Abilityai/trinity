# Hierarchical Agent Stacking: Depth-Selectable Deployment

> Pre-deployment inheritance pattern for Trinity agents using CLAUDE.md folder hierarchy

**Status:** Draft/Concept
**Created:** 2025-12-20
**Author:** Fred (Agent Management Agent)

---

## Executive Summary

This document proposes a pattern where agent identity, knowledge, and capability are encoded in folder hierarchy. Each subfolder adds a layer of context. Deployment can target any depth - shallow for general capability, deep for specialized context.

**Core insight:** The folder path IS the agent's capability profile.

---

## The Mechanism

### Claude Code's Native Behavior

Claude Code reads CLAUDE.md files recursively up the directory tree:

> "Starting in the cwd, Claude Code recurses up to (but not including) the root directory `/` and reads any CLAUDE.md or CLAUDE.local.md files it finds."

This means:
- `cd agents/support/tier-3 && claude` loads: `agents/CLAUDE.md` + `agents/support/CLAUDE.md` + `agents/support/tier-3/CLAUDE.md`
- Each level adds to the agent's identity and context

### Pre-Deployment Stacking for Trinity

Since Trinity agents run in isolated containers, we apply this pattern at **build time**:

```
BUILD PHASE (local)                      DEPLOY PHASE (Trinity)
─────────────────────                    ────────────────────────
folder hierarchy          ──build──→     single CLAUDE.compiled.md
with multiple CLAUDE.md                  deployed as one agent
```

A build script walks the target path, concatenates all CLAUDE.md files in order, produces one deployable agent.

---

## Folder Structure Pattern

```
agents/
├── CLAUDE.md                           # ROOT: Core identity, values, policies
├── template.yaml                       # Shared template (optional)
│
├── [domain]/
│   ├── CLAUDE.md                       # DOMAIN: Industry/function expertise
│   │
│   └── [context]/
│       ├── CLAUDE.md                   # CONTEXT: Specific client/case/situation
│       └── template.yaml               # Deploy from here
```

**Principle:** Shallow = general, Deep = specialized

| Level | Adds |
|-------|------|
| Root | Identity, ethics, output style, global policies |
| Domain | Industry knowledge, domain tools, terminology |
| Context | Specific situation, history, preferences |

---

## Build Script Reference

```bash
#!/bin/bash
# build-agent.sh <target-path>
# Walks up from target, concatenates all CLAUDE.md files

target="$1"
output=""
current="$target"

# Walk up collecting CLAUDE.md files
while [[ "$current" != "." && "$current" != "/" ]]; do
  if [[ -f "$current/CLAUDE.md" ]]; then
    content=$(cat "$current/CLAUDE.md")
    output="$content

---

$output"
  fi
  current=$(dirname "$current")
done

# Write compiled output
echo "$output" > "$target/CLAUDE.compiled.md"
echo "Built: $target/CLAUDE.compiled.md"
```

Deploy to Trinity using the compiled CLAUDE.md:
```bash
./build-agent.sh agents/support/tier-3
# Copy CLAUDE.compiled.md to agent's CLAUDE.md before deployment
```

---

## Business Use Cases

### 1. Tiered Customer Support

```
support/
├── CLAUDE.md                        # Tone, policies, product basics
│
├── tier-1/
│   ├── CLAUDE.md                    # FAQ handling, common issues
│   │
│   ├── tier-2/
│   │   ├── CLAUDE.md                # Technical troubleshooting
│   │   │
│   │   └── tier-3/
│   │       ├── CLAUDE.md            # Engineering-level debugging
│   │       └── template.yaml
```

**Value:** Auto-escalation by redeploying deeper. Ticket unresolved at tier-1 → redeploy from tier-2 with conversation history. Same identity, more capability. No handoff friction.

**Router logic:**
```python
if ticket.priority == "critical":
    deploy("support/tier-3")
elif ticket.priority == "high":
    deploy("support/tier-2")
else:
    deploy("support/tier-1")
```

---

### 2. Account-Specific Sales Agents

```
sales/
├── CLAUDE.md                        # Company pitch, pricing, objection handling
│
├── healthcare/
│   ├── CLAUDE.md                    # HIPAA, healthcare terminology
│   │
│   ├── client-kaiser/
│   │   ├── CLAUDE.md                # Kaiser history, contacts, past deals
│   │   └── template.yaml
│   │
│   └── client-mayo/
│       ├── CLAUDE.md                # Mayo-specific context
│       └── template.yaml
│
├── fintech/
│   ├── CLAUDE.md                    # SOC2, financial regulations
│   │
│   └── client-stripe/
│       ├── CLAUDE.md                # Stripe relationship history
│       └── template.yaml
```

**Value:** When Stripe calls, deploy `sales/fintech/client-stripe`. Agent knows their history, terminology, past objections, internal champions. Relationship continuity without human memory.

---

### 3. Multi-Jurisdiction Legal/Compliance

```
legal/
├── CLAUDE.md                        # Legal reasoning framework
│
├── us/
│   ├── CLAUDE.md                    # US federal law
│   │
│   ├── california/
│   │   ├── CLAUDE.md                # CCPA, CA employment law
│   │   └── template.yaml
│   │
│   └── delaware/
│       ├── CLAUDE.md                # Corporate law
│       └── template.yaml
│
├── eu/
│   ├── CLAUDE.md                    # GDPR, EU directives
│   │
│   └── germany/
│       ├── CLAUDE.md                # German implementation
│       └── template.yaml
```

**Value:** Query routes to correct jurisdiction. "Is this GDPR compliant?" → deploy `legal/eu`. One system, jurisdiction-aware. No wrong answers from wrong legal framework.

---

### 4. Consulting Engagement Memory

```
consulting/
├── CLAUDE.md                        # Firm methodology, standards
│
├── strategy/
│   ├── CLAUDE.md                    # Strategy frameworks
│   │
│   └── engagement-acme-2024/
│       ├── CLAUDE.md                # ACME context, stakeholders
│       │
│       ├── phase-1-discovery/
│       │   ├── CLAUDE.md            # Interview notes, hypotheses
│       │   └── template.yaml
│       │
│       └── phase-2-recommendations/
│           ├── CLAUDE.md            # Refined insights
│           └── template.yaml
```

**Value:** Engagement agent accumulates knowledge through phases. New team member joins phase-2 → deploy from that level → instant context. Knowledge doesn't leave when consultants rotate.

---

### 5. Multi-Tenant SaaS Support

```
product/
├── CLAUDE.md                        # Core product knowledge
│
├── tenant-shopify/
│   ├── CLAUDE.md                    # Shopify's config, integrations
│   │
│   └── user-john/
│       ├── CLAUDE.md                # John's preferences, history
│       └── template.yaml
│
├── tenant-stripe/
│   ├── CLAUDE.md                    # Stripe's setup
│   └── template.yaml
```

**Value:** Each tenant gets "their" agent. Power users get personalized agents. Support feels bespoke without custom engineering.

---

### 6. Progressive Disclosure Training

```
onboarding/
├── CLAUDE.md                        # Company culture, basics
│
├── week-1/
│   ├── CLAUDE.md                    # Orientation, tools
│   │
│   ├── week-2/
│   │   ├── CLAUDE.md                # Deeper processes
│   │   │
│   │   └── week-4/
│       │   ├── CLAUDE.md            # Full context
│       │   └── template.yaml
```

**Value:** New hire gets week-1 agent. Agent grows with them. Adaptive onboarding - agent meets employee where they are.

---

## The Depth Dimension

| Dimension | Shallow | Deep |
|-----------|---------|------|
| Knowledge | General | Specific |
| Context | None | Full history |
| Capability | Basic | Advanced |
| Risk | Low | High (more autonomy) |
| Cost | Cheaper (smaller context) | More expensive |

**Deployment depth chosen based on:**
- Ticket severity / priority
- Client importance / tier
- Task complexity
- User experience level
- Risk tolerance
- Cost constraints

---

## Integration with Trinity

### Option A: Build-Time Compilation

1. Developer maintains folder hierarchy locally
2. Build script compiles CLAUDE.md chain
3. Deploy compiled agent to Trinity
4. Trinity treats it as normal single agent

**Pros:** Simple, works today
**Cons:** Manual build step, no runtime switching

### Option B: Trinity-Native Hierarchy

Trinity could natively support:

```yaml
# template.yaml
name: support-tier-3
inherits_from:
  - support/tier-2
  - support/tier-1
  - support
```

At deployment, Trinity walks the inheritance chain and compiles CLAUDE.md files.

**Pros:** No build step, runtime-aware
**Cons:** Requires Trinity feature development

### Option C: Router Agent Pattern

A meta-agent that:
1. Receives requests
2. Analyzes required depth
3. Deploys appropriate agent from hierarchy
4. Passes conversation to deployed agent

```python
# Router agent logic
def route_request(request):
    depth = analyze_complexity(request)
    agent_path = determine_path(request.domain, depth)
    agent = deploy_from_path(agent_path)
    return agent.handle(request)
```

**Pros:** Dynamic, intelligent routing
**Cons:** Additional latency, complexity

---

## Advanced Pattern: Memory Archaeology

Beyond role specialization, hierarchy can model **temporal/experiential depth**:

```
agent-sophia/
├── CLAUDE.md                    # Core identity
│
├── year-1/
│   ├── CLAUDE.md                # First experiences
│   │
│   └── after-crisis/
│       ├── CLAUDE.md            # Post-crisis wisdom
│       └── template.yaml
│
├── year-1-alt/                  # BRANCH: alternate timeline
│   └── CLAUDE.md                # What if crisis never happened?
```

**Enables:**
- Query agent at any point in its "life"
- Compare responses before/after key events
- Branching worldviews (sibling folders)
- Character development for fiction/simulation

---

## Implementation Recommendations

### Phase 1: Manual Build Script
- Create build script (see reference above)
- Document folder structure standards
- Test with 2-3 use cases

### Phase 2: Trinity CLI Integration
- Add `trinity build <path>` command
- Auto-detect hierarchy, compile CLAUDE.md
- Validate template.yaml at each level

### Phase 3: Native Inheritance
- Add `inherits_from` to template.yaml spec
- Trinity resolves at deployment time
- Support runtime depth switching

### Phase 4: Router Agent Template
- Provide standard router agent template
- Configurable routing rules
- Depth-aware deployment automation

---

## Open Questions

1. **MCP inheritance:** Should .mcp.json also stack? Or only at leaf level?
2. **Credential scoping:** How do credentials work across hierarchy levels?
3. **Version control:** How to version individual layers vs. compiled output?
4. **Cache invalidation:** When parent CLAUDE.md changes, how to rebuild affected children?
5. **Metrics aggregation:** How to roll up metrics from different depth deployments?

---

## Conclusion

Hierarchical agent stacking transforms folder structure into capability architecture. One codebase, one identity, variable depth. The pattern enables:

- **Reuse:** Base layers shared across many agents
- **Consistency:** All agents share core identity
- **Customization:** Deep layers add specificity
- **Versioning:** Each layer is diffable, auditable
- **Dynamic deployment:** Choose depth at runtime

This is agent design as **geological formation** - each layer adds sediment to the agent's knowledge and capability.

---

*Draft document - feedback welcome*
