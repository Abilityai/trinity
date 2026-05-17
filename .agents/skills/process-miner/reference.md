# Process Miner Reference

Detailed technical reference for parsing and analyzing Claude Code transcripts.

## Transcript Format

Claude Code stores execution logs as JSONL files at `~/.claude/projects/PROJECT_PATH/*.jsonl`.

Each line is a JSON object with fields:
- `type`: "assistant", "user", or "system"
- `timestamp`: ISO 8601 timestamp
- `uuid`: Unique message identifier
- `parentUuid`: Parent message for threading
- `message.content`: Array of content blocks

## Parsing Tool Calls

Extract `tool_use` entries from assistant messages:

```python
import json
from collections import defaultdict

def parse_transcript(jsonl_path):
    """Extract tool_use sequences from a Claude Code transcript."""
    tool_calls = []

    with open(jsonl_path) as f:
        for line in f:
            entry = json.loads(line)
            if entry.get('type') == 'assistant' and 'message' in entry:
                content = entry['message'].get('content', [])
                for item in content:
                    if item.get('type') == 'tool_use':
                        tool_calls.append({
                            'timestamp': entry['timestamp'],
                            'tool': item['name'],
                            'input': item['input'],
                            'parent': entry.get('parentUuid'),
                            'uuid': entry['uuid']
                        })
    return tool_calls
```

## Pattern Discovery

Look for:
- **Frequent Sequences**: Tool A -> Tool B -> Tool C appearing repeatedly
- **Intent Clusters**: User messages that trigger similar tool patterns
- **Branch Points**: Where tool sequences diverge based on results
- **Common Idioms**: Read -> Grep -> Edit patterns, Glob -> Read patterns

## Output YAML Format

Generated Trinity Process definitions follow this structure:

```yaml
# Auto-generated from execution log analysis
# Source: ~/.claude/projects/PROJECT_PATH/session-id.jsonl
# Discovered pattern: [description of observed behavior]

name: discovered-pattern-name
version: "1.0"
description: |
  Auto-generated process discovered from Claude Code execution logs.
  Original intent: [inferred user goal]
  Confidence: [high/medium/low based on pattern frequency]

triggers:
  - type: manual
    id: manual-start

steps:
  - id: step-1
    name: [Descriptive name]
    type: agent_task
    agent: claude-code
    message: |
      [Inferred task description based on tool usage]
    timeout: 5m

  - id: step-2
    name: [Descriptive name]
    type: agent_task
    agent: claude-code
    message: |
      [Next inferred task]
    depends_on: [step-1]
    timeout: 5m

outputs:
  - name: result
    source: "{{steps.final-step.output.response}}"
```

## Semantic Analysis

The script categorizes tools into workflow types:

| Tool | Action Type | Description |
|------|-------------|-------------|
| Read | read_file | Reading file content |
| Write | write_file | Creating new files |
| Edit | edit_file | Modifying existing files |
| Grep | search_pattern | Searching for patterns |
| Glob | find_files | Finding files by pattern |
| Bash | execute_* | Running shell commands |
| Task | delegate | Delegating to subagents |
| WebFetch | fetch_url | Fetching web content |
| WebSearch | web_search | Searching the web |

## Workflow Type Inference

Based on tool combinations:

- **read-modify workflow**: Read + Edit/Write
- **search-and-review workflow**: Grep + Read
- **file discovery workflow**: Glob-heavy patterns
- **multi-agent delegation workflow**: Task tool usage
- **research workflow**: WebSearch/WebFetch
- **command execution workflow**: Bash-heavy patterns
