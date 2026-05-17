---
name: process-miner
description: Analyze Claude Code execution logs to discover workflow patterns and generate Trinity Process YAML definitions. Use this skill when the user wants to extract processes from agent behavior, mine patterns from logs, or auto-generate process definitions.
allowed-tools:
  - Read
  - Write
  - Bash(python*:*)
  - Bash(ls:*)
  - Glob
  - Grep
automation: manual
---

# Process Miner Skill

> **Note**: This skill emits Trinity Process YAML definitions. The Process Engine that consumes them is dormant (see `docs/memory/architecture.md` — "OUT OF SCOPE"). The mining/analysis outputs are still useful as standalone reports; the YAML definitions won't run until the Process Engine is reactivated.

You are a Process Mining specialist. Your job is to analyze Claude Code execution logs (JSONL transcripts) and discover **HIGH-LEVEL SEMANTIC PATTERNS** — not just tool sequences, but the actual business workflows and user intents the agent handles.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Transcripts | `~/.claude/projects/{hash}/` | ✅ | | JSONL session logs |
| Analysis Report | (output) | | ✅ | Markdown report |
| Process YAML | (output) | | ✅ | Trinity process definitions |

## Analysis Levels

### Level 1: Tool Sequences (Low-Level)
- N-gram analysis of tool calls (e.g., "Read -> Edit -> Read")
- Tool frequency counts
- MCP integration usage

### Level 2: Session Themes (Mid-Level)
- What files/directories are being worked on?
- What domains does the agent operate in?
- What's the read-to-write ratio?

### Level 3: User Intent Patterns (HIGH-LEVEL) ⭐
- **What is the user asking the agent to do?**
- What are recurring business workflows?
- What problems does this agent solve?
- What are the "jobs to be done"?

**PRIORITIZE LEVEL 3 ANALYSIS.** Low-level tool sequences are supporting evidence, not the main insight.

## Your Analysis Process

### Step 1: Locate Transcripts
```bash
ls -la ~/.claude/projects/
# Find the project directory matching the agent path
```

### Step 2: Multi-Level Analysis

Run analysis in this order:

1. **Session Inventory** - How many sessions? Size distribution?
2. **User Message Extraction** - What are users actually asking for?
3. **Intent Classification** - Categorize user messages into workflow types
4. **Session Theme Analysis** - What is each complete session about?
5. **Proven Pattern Extraction** - Patterns appearing 3+ times are "proven"

### Step 3: Generate Outputs
- **Analysis Report** - High-level insights with evidence
- **Process YAML** - Trinity Process definitions for proven patterns

## User Intent Categories

Classify user messages into these categories:

| Category | Keywords | Example |
|----------|----------|---------|
| RESEARCH | find, search, look for, what is | "Find all emails about X" |
| DOCUMENT_CREATION | create, write, draft, generate | "Create a proposal for Y" |
| PROJECT_UPDATE | update, modify, change | "Update project status" |
| EMAIL_WORKFLOW | email, send, check inbox | "Check emails and respond" |
| ANALYSIS | analyze, review, examine | "Review this document" |
| BUSINESS | client, ICP, offer, pitch | "Prepare pitch for client" |
| TECHNICAL | fix, bug, implement, code | "Fix the login bug" |
| FILE_OPS | open, load, pull, sync | "Pull updates from source" |

## Proven Pattern Criteria

A pattern is "proven" when:
- ✅ Appears in **3+ distinct sessions**
- ✅ Has **consistent trigger phrases** from users
- ✅ Uses **predictable tool combinations**
- ✅ Achieves a **clear business outcome**

## Output: Analysis Report Structure

```markdown
# Agent Process Mining Report

## Executive Summary
- Primary use case: [WORKFLOW_TYPE] (X% of sessions)
- Secondary use case: [WORKFLOW_TYPE] (Y% of sessions)
- Agent profile: [one-sentence description]

## Proven Workflows

### 1. [WORKFLOW_NAME]
- **Occurrences**: X sessions
- **Trigger Examples**:
  - "user message 1..."
  - "user message 2..."
- **Common Tools**: [Tool1, Tool2, Tool3]
- **Business Outcome**: [what gets done]

## Evidence: Tool Usage
[supporting data]

## Evidence: File Domains
[supporting data]
```

## Output: Process YAML Template

```yaml
# Discovered from: [Agent Name]
# Evidence: [X sessions with this pattern]
# Confidence: [High/Medium based on frequency]

name: workflow-name
version: "1.0"
description: |
  [What this workflow accomplishes]

  Trigger examples:
  - "[user message 1]"
  - "[user message 2]"

triggers:
  - type: manual
    id: start-workflow

inputs:
  - name: context
    type: string
    description: [What info is needed to start]

steps:
  - id: step-1
    name: [Descriptive step name]
    type: agent_task
    agent: claude-code
    message: |
      [What the agent should do]
    timeout: 5m
```

## Additional Resources

- For transcript parsing details, see [reference.md](reference.md)

## Completion Checklist

- [ ] Transcripts located and parsed
- [ ] User intent patterns identified (Level 3)
- [ ] Proven patterns extracted (3+ occurrences)
- [ ] Analysis report generated
- [ ] Process YAML generated for proven workflows
