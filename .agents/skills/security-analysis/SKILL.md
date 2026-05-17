---
name: security-analysis
description: Perform OWASP-based security analysis of the codebase. Use when auditing code for vulnerabilities, before production deployment, or when reviewing authentication/authorization changes.
context: fork
agent: security-analyzer
allowed-tools: Read, Grep, Glob, Bash(grep *)
automation: manual
---

# Security Analysis

Perform a comprehensive security analysis of the Trinity codebase based on OWASP Top 10.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Architecture | `.claude/memory/architecture.md` | ✅ | | Security boundaries |
| Backend Code | `src/backend/` | ✅ | | API security |
| Frontend Code | `src/frontend/` | ✅ | | Client security |
| Docker Config | `docker/` | ✅ | | Container security |
| Security Report | `docs/security-reports/security-analysis-{date}.md` | | ✅ | Generated report |

## Scope

$ARGUMENTS

If no scope specified, analyze the full codebase with focus on:
- `src/backend/` - API endpoints, authentication, database access
- `src/frontend/` - Client-side security, token handling
- `docker/` - Container security, privilege escalation risks

## Analysis Process

Follow the methodology in the security-analyzer agent:

1. **Read architecture.md** to understand security boundaries
2. **Check each OWASP category** systematically
3. **Document findings** with severity levels
4. **Generate report** to `docs/security-reports/`

## OWASP Categories to Check

- A01: Broken Access Control
- A02: Cryptographic Failures
- A03: Injection
- A04: Insecure Design
- A05: Security Misconfiguration
- A07: Authentication Failures
- A09: Security Logging Failures

## Output

Save report to: `docs/security-reports/security-analysis-{date}.md`

Return a summary with:
- Critical/High findings count
- Top 3 most urgent issues
- Recommended immediate actions

## Completion Checklist

- [ ] Architecture.md reviewed for security boundaries
- [ ] OWASP categories checked systematically
- [ ] Findings documented with severity levels
- [ ] Report saved to `docs/security-reports/`
- [ ] Summary returned with critical count and top issues
