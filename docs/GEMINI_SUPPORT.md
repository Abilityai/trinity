# Gemini CLI Runtime Support

Trinity now supports **multiple AI runtimes**, allowing you to choose between Claude Code (Anthropic) and Gemini CLI (Google) for your agents.

## Why Multi-Runtime?

**Cost Optimization:**
- Gemini: Free tier (60 req/min, 1,000/day)
- Claude: Pay-per-use

**Context Window:**
- Gemini: 1 million tokens (5x larger)
- Claude: 200K tokens

**Use Case Matching:**
- Gemini: Data processing, monitoring, large codebases
- Claude: Complex reasoning, code quality, reliability

## Getting Started

### 1. Get a Google API Key

1. Go to [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Click "Create API Key"
3. Copy your key

### 2. Configure Trinity

Add to your `.env` file:
```bash
GOOGLE_API_KEY=your-google-api-key-here
```

### 3. Create a Gemini Agent

**Via UI:**
1. Click "Create Agent"
2. Enter agent name
3. Select runtime: **Gemini CLI**
4. Choose model: `gemini-2.5-pro`
5. Click "Create"

**Via API:**
```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-gemini-agent",
    "runtime": "gemini-cli",
    "runtime_model": "gemini-2.5-pro",
    "resources": {"cpu": "2", "memory": "2g"}
  }'
```

**Via Template:**
```yaml
# config/agent-templates/my-agent/template.yaml
name: my-gemini-agent
runtime:
  type: gemini-cli
  model: gemini-2.5-pro
resources:
  memory: "2g"  # Gemini is lighter
```

## Feature Parity Matrix

| Feature | Claude Code | Gemini CLI | Status |
|---------|-------------|------------|--------|
| **Chat Interface** | ✅ | ✅ | Identical |
| **MCP Tools** | ✅ | ✅ | Supported |
| **Cost Tracking** | ✅ | ✅ | Unified |
| **Token Tracking** | ✅ | ✅ | Unified |
| **Session Continuity** | ✅ | ✅ | `--resume` |
| **Tool Restrictions** | ✅ | ✅ | `--allowed-tools` |
| **YOLO Mode** | ✅ | ✅ | `--yolo` |
| **Context Window** | 200K | 1M | 5x larger |
| **Pricing** | Pay-per-use | Free tier | Different |
| **Native Search** | ❌ | ✅ | Gemini only |

## Cost Comparison

### Example: 100 Messages/Day

**Claude Code (Sonnet 4.5):**
- Input: $3/M tokens
- Output: $15/M tokens
- Estimated: **$5-10/day**

**Gemini CLI (2.5 Pro):**
- Free tier: 1,000 requests/day
- Estimated: **$0/day** (within limits)

## Architecture

Trinity uses a **Runtime Adapter** pattern:

```
User Chat → Backend → Agent Container → [Runtime Adapter]
                                              ↓
                                    ┌─────────┴─────────┐
                                    ↓                   ↓
                            ClaudeCodeRuntime    GeminiRuntime
                                    ↓                   ↓
                              claude CLI          gemini CLI
```

Both runtimes implement the same interface:
- `execute(prompt, model, continue_session)`
- `configure_mcp(servers)`
- Return same format: `(response, execution_log, metadata)`

## Instruction File (CLAUDE.md)

Both Claude Code and Gemini CLI read agent instructions from `CLAUDE.md`.

**Why keep the name `CLAUDE.md`?**
- Backward compatibility with existing agents
- Both runtimes understand markdown instruction files
- Renaming would break existing templates

```
workspace/
├── CLAUDE.md           # ← Both runtimes read this
├── .mcp.json           # ← Claude Code only
└── ...
```

**In your CLAUDE.md**, you can write instructions that work for both:
```markdown
# Agent Instructions

You are a helpful agent. [Instructions work for both Claude and Gemini]

## Tools Available
- filesystem operations
- web search
- etc.
```

## MCP Configuration

**Claude Code:** Uses `.mcp.json` file
```json
{
  "mcpServers": {
    "trinity": {
      "url": "http://mcp-server:8080/mcp",
      "headers": {"Authorization": "Bearer KEY"}
    }
  }
}
```

**Gemini CLI:** Uses CLI commands (Trinity handles this automatically)
```bash
gemini mcp add trinity http://mcp-server:8080/mcp
```

Trinity translates MCP configuration to each runtime's format automatically.

## Switching Runtimes

You can run both Claude and Gemini agents simultaneously:

```yaml
# Multi-runtime system
agents:
  orchestrator:
    runtime: claude-code  # Complex reasoning
    model: sonnet-4.5
    resources: {memory: "4g"}

  worker-1:
    runtime: gemini-cli  # Data processing
    model: gemini-2.5-pro
    resources: {memory: "2g"}

  worker-2:
    runtime: gemini-cli  # Monitoring
    model: gemini-2.5-pro
    resources: {memory: "2g"}
```

**Cost:** $2-3/day for orchestrator, $0 for workers

## Troubleshooting

### "Gemini CLI is not available"
- Rebuild base image: `./scripts/deploy/build-base-image.sh`
- Gemini CLI is installed during image build

### "GOOGLE_API_KEY not configured"
- Add to `.env`: `GOOGLE_API_KEY=your-key`
- Restart agent container

### MCP Tools Not Working
- Check `gemini mcp list` inside container
- Verify Trinity MCP server is accessible
- Check agent permissions

## Limitations

**Gemini Free Tier Limits:**
- 60 requests/minute
- 1,000 requests/day
- If exceeded, agent will fail with rate limit error

**Workaround:** Mix Claude and Gemini agents to stay within limits.

## Next Steps

1. **Test:** Create a test agent with `local:test-gemini` template
2. **Compare:** Run same task on Claude and Gemini agents
3. **Optimize:** Move simple tasks to Gemini, keep complex ones on Claude
4. **Monitor:** Track costs via `GET /api/ops/costs`

