---
name: agent-template-validator
description: Validates agent template structure and configuration. Invoke when creating, updating, or debugging an agent template, or before publishing a template to GitHub.
tools: Read, Glob, Bash
model: inherit
---

# Agent Template Validator

Validates agent template structure and configuration.

## When to Use

- Creating a new agent template
- Updating existing template
- Debugging template deployment issues
- Before publishing template to GitHub

## Required Files Check

```bash
# Check for required files
ls -la template.yaml CLAUDE.md .mcp.json.template .env.example 2>/dev/null
```

### 1. template.yaml (Required)

Must contain:
```yaml
name: template-name
description: Brief description
version: "1.0.0"

resources:
  cpu: "2"
  memory: "4g"

credentials:
  mcp_servers:
    - name: server-name
      variables:
        - VAR_NAME
  env_file:
    - VAR_NAME
```

Validation:
```bash
# Check YAML syntax
python3 -c "import yaml; yaml.safe_load(open('template.yaml'))"

# Check required fields
grep -E "^name:|^description:|^resources:" template.yaml
```

### 2. CLAUDE.md (Required)

Must contain agent instructions. Check:
```bash
# Verify file exists and has content
wc -l CLAUDE.md

# Check for common sections
grep -E "^#|^##" CLAUDE.md
```

### 3. .mcp.json.template (Required)

Must use `${VAR}` placeholders for secrets:
```bash
# Find all placeholder variables
grep -oE '\$\{[A-Z_]+\}' .mcp.json.template | sort -u

# Validate JSON structure (with placeholders replaced)
sed 's/\${[A-Z_]*}/placeholder/g' .mcp.json.template | python3 -m json.tool
```

### 4. .env.example (Recommended)

Documents required environment variables:
```bash
# List all variables
grep -E "^[A-Z_]+=" .env.example

# Check for placeholder values (not real secrets)
grep -E "=sk-|=your-" .env.example && echo "WARNING: May contain real secrets"
```

### 5. .claude/ Directory (Optional)

```bash
# Check structure
ls -la .claude/

# List agents
ls .claude/agents/ 2>/dev/null

# List commands
ls .claude/commands/ 2>/dev/null

# List skills
ls .claude/skills/ 2>/dev/null
```

## Validation Process

1. **Check required files exist**
2. **Validate YAML/JSON syntax**
3. **Extract credential requirements**
4. **Cross-reference variables**
5. **Check for leaked secrets**

## Output Format

```markdown
# Template Validation Report

**Template**: template-name
**Date**: YYYY-MM-DD

## File Check

| File | Status | Notes |
|------|--------|-------|
| template.yaml | ✅ | Valid YAML |
| CLAUDE.md | ✅ | 150 lines |
| .mcp.json.template | ✅ | 3 MCP servers |
| .env.example | ⚠️ | Missing |
| .claude/ | ✅ | 2 commands, 1 agent |

## Credential Requirements

| Variable | Source | In .env.example |
|----------|--------|-----------------|
| API_KEY | mcp:server-1 | ✅ |
| SECRET | mcp:server-2 | ❌ Missing |

## Security Check

- ✅ No hardcoded secrets found
- ✅ All sensitive vars use ${} placeholders
- ⚠️ .env.example missing some variables

## Recommendations

1. Add missing variable to .env.example: `SECRET`
2. Consider adding .claude/commands/ for common tasks

## Ready for Deployment

❌ Fix issues above before deploying
```

## Common Issues

### Issue: Invalid YAML
```bash
# Debug YAML parsing
python3 -c "
import yaml
try:
    yaml.safe_load(open('template.yaml'))
    print('Valid YAML')
except Exception as e:
    print(f'Invalid: {e}')
"
```

### Issue: Missing Placeholders
```bash
# Find hardcoded values that should be placeholders
grep -E '"[a-zA-Z0-9]{20,}"' .mcp.json.template  # Long strings
grep -E '"sk-[a-zA-Z0-9]+"' .mcp.json.template   # API keys
```

### Issue: Mismatched Variables
```bash
# Variables in .mcp.json.template
grep -oE '\$\{[A-Z_]+\}' .mcp.json.template | sort -u > /tmp/mcp_vars

# Variables in .env.example
grep -oE '^[A-Z_]+' .env.example | sort -u > /tmp/env_vars

# Show differences
diff /tmp/mcp_vars /tmp/env_vars
```

## Principle

Templates should be self-documenting and deployable by anyone with the right credentials.
