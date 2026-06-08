# dev-methodology Plugin

Documentation-driven development methodology for any codebase. Enforces a structured cycle: context loading, development, testing, documentation, and PR validation.

## Installation

```bash
/plugin install dev-methodology@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/dev-methodology:init` | Scaffold the methodology into your project |
| `/dev-methodology:read-docs` | Load project context at session start |
| `/dev-methodology:implement` | End-to-end feature implementation |
| `/dev-methodology:validate-pr` | Validate a PR against the methodology |
| `/dev-methodology:commit` | Create well-formatted commits |
| `/dev-methodology:add-testing` | Add tests to existing code |
| `/dev-methodology:tidy` | Clean up code |
| `/dev-methodology:refactor-audit` | Review changed code for reuse, quality, and efficiency |
| `/dev-methodology:roadmap` | Query issues for roadmap priorities |
| `/dev-methodology:security-check` | Quick security scan |
| `/dev-methodology:security-analysis` | Deep OWASP-based security analysis |
| `/dev-methodology:feature-flow-analysis` | Create or update a feature flow document |
| `/dev-methodology:sync-feature-flows` | Analyze code changes and update affected feature flows |
| `/dev-methodology:update-docs` | Update project documentation after making changes |
| `/dev-methodology:generate-user-docs` | Generate user-facing docs from source code |

## How It Works

### Initialize Methodology

```bash
/dev-methodology:init
```

Scaffolds into your project:

- `docs/memory/` — Architecture and requirements docs
- `docs/memory/feature-flows/` — Vertical slice documentation
- `.claude/skills/` — Development playbooks

### Session Start

```bash
/dev-methodology:read-docs
```

Loads project context:

- Architecture overview
- Current requirements
- Recent changes
- Active feature flows

### Feature Implementation

```bash
/dev-methodology:implement #42
```

End-to-end flow:

1. Read the issue and understand requirements
2. Plan the implementation
3. Write code with tests
4. Update documentation
5. Create a PR

### PR Validation

```bash
/dev-methodology:validate-pr 123
```

Checks:

- Code quality and style
- Test coverage
- Documentation updates
- Security considerations
- Breaking changes

## Development Cycle

The methodology enforces a 5-phase cycle:

```
1. Context     2. Plan        3. Implement    4. Document    5. Validate
read-docs      understand     write code      update-docs    validate-pr
               requirements   write tests     feature flows
```

## Security Tools

### Quick Scan

```bash
/dev-methodology:security-check
```

Fast checks for common issues:

- Hardcoded secrets
- SQL injection patterns
- XSS vulnerabilities

### Deep Analysis

```bash
/dev-methodology:security-analysis
```

Comprehensive OWASP-based review:

- Authentication flows
- Authorization checks
- Input validation
- Data exposure

## See Also

- [Abilities Overview](overview.md) — Full toolkit overview
- [GitHub: abilityai/abilities](https://github.com/abilityai/abilities) — Source repository
