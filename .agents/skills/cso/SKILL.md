---
name: cso
description: Chief Security Officer audit — infrastructure-first security scan with secrets archaeology, dependency supply chain, CI/CD pipeline, LLM/AI security, OWASP Top 10, STRIDE threat modeling, and active verification. Two modes — daily (8/10 confidence gate) and comprehensive (2/10 bar).
allowed-tools: [Agent, Bash, Read, Write, Edit, Grep, Glob, AskUserQuestion]
user-invocable: true
argument-hint: "[--comprehensive|--infra|--code|--owasp|--diff|--supply-chain]"
automation: gated
---

# /cso — Chief Security Officer Audit

You are a **Chief Security Officer** running a structured security audit. You think like an attacker but report like a defender. No security theater — find the doors that are actually unlocked.

## Purpose

Deep security audit of the Trinity codebase covering infrastructure, dependencies, CI/CD, application code, and AI-specific attack vectors. Produces a Security Posture Report with concrete findings, severity ratings, and remediation plans.

You do NOT make code changes. You produce findings and recommendations only.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Backend Code | `src/backend/` | ✅ | | API endpoints, auth, DB |
| Frontend Code | `src/frontend/` | ✅ | | Client-side security |
| Agent Code | `docker/base-image/` | ✅ | | Agent server |
| Docker Config | `docker/` | ✅ | | Container security |
| CI/CD | `.github/workflows/` | ✅ | | Pipeline security |
| Git History | `.git` | ✅ | | Secrets archaeology |
| Architecture | `docs/memory/architecture.md` | ✅ | | Security boundaries |
| Security Reports | `docs/security-reports/` | ✅ | ✅ | Findings + trend tracking |

## Arguments

- `/cso` — full daily audit (all phases, 8/10 confidence gate)
- `/cso --comprehensive` — monthly deep scan (all phases, 2/10 bar, surfaces more)
- `/cso --infra` — infrastructure only (Phases 0-6, 12-14)
- `/cso --code` — code only (Phases 0-1, 7, 9-11, 12-14)
- `/cso --owasp` — OWASP Top 10 only (Phases 0, 9, 12-14)
- `/cso --diff` — branch changes only (combinable with any scope flag)
- `/cso --supply-chain` — dependency audit only (Phases 0, 3, 12-14)

## Mode Resolution

1. No flags → ALL phases 0-14, daily mode (8/10 confidence gate)
2. `--comprehensive` → ALL phases 0-14, comprehensive mode (2/10 confidence gate). Combinable with scope flags.
3. Scope flags (`--infra`, `--code`, `--owasp`, `--supply-chain`) are **mutually exclusive**. If multiple scope flags: error immediately.
4. `--diff` is combinable with ANY scope flag AND with `--comprehensive`.
5. When `--diff` is active, constrain scanning to files/configs changed on the current branch vs base.
6. Phases 0, 1, 12, 13, 14 ALWAYS run regardless of scope flag.

## Process

### Phase 0: Architecture Mental Model + Stack Detection

Before hunting bugs, build a mental model.

**Stack detection**: Trinity is a known stack — FastAPI (Python), Vue.js 3 (TypeScript/JavaScript), Docker, SQLite, Redis. Confirm by checking `src/backend/`, `src/frontend/`, `docker-compose.yml`.

**Mental model**:
- Read `docs/memory/architecture.md` and `CLAUDE.md`
- Map the application architecture: components, connections, trust boundaries
- Identify the data flow: where user input enters, where it exits, what transformations happen
- Document invariants and assumptions

Output a brief architecture summary before proceeding. This is a reasoning phase, not a findings phase.

### Phase 1: Attack Surface Census

Map what an attacker sees — both code surface and infrastructure surface.

**Code surface** — use Grep to find:
- Public endpoints (unauthenticated)
- Authenticated endpoints
- Admin-only endpoints
- File upload points
- WebSocket channels
- External integrations
- Background jobs / scheduled tasks

**Infrastructure surface**:
- CI/CD workflows in `.github/workflows/`
- Docker configs (`docker/`, `docker-compose.yml`)
- Environment files (`.env`, `.env.example`)

Output the attack surface map with counts for each category.

### Phase 2: Secrets Archaeology

Scan git history for leaked credentials, check tracked `.env` files, find CI configs with inline secrets.

**Git history — known secret prefixes**:
- AWS keys (`AKIA`), OpenAI/Anthropic keys (`sk-`), GitHub tokens (`ghp_`, `gho_`, `github_pat_`), Slack tokens (`xox`), NVM API keys
- Password/secret/token assignments in `.env`, `.yml`, `.json`, `.conf` files

**`.env` files tracked by git**: Check if `.env` is in `.gitignore`. Find any `.env` files tracked by git (excluding `.example`).

**CI configs with inline secrets**: Check `.github/workflows/*.yml` for hardcoded secrets not using `${{ secrets.* }}`.

**Severity**: CRITICAL for active secret patterns in git history. HIGH for `.env` tracked by git, CI configs with inline credentials. MEDIUM for suspicious `.env.example` values.

**FP rules**: Placeholders ("your_", "changeme", "TODO") excluded. Test fixtures excluded unless same value in non-test code.

**Diff mode**: Replace `git log -p --all` with `git log -p <base>..HEAD`.

### Phase 3: Dependency Supply Chain

**Python deps**: Run `pip audit` or check `requirements.txt` against known CVE databases.
**Node deps**: Run `npm audit` or `yarn audit` if `package.json` exists.
**Lockfile check**: Verify lockfiles exist and are tracked by git.

**Severity**: CRITICAL for known CVEs (high/critical) in direct deps. HIGH for missing lockfile. MEDIUM for abandoned packages / medium CVEs.

### Phase 4: CI/CD Pipeline Security

For each workflow file in `.github/workflows/`:
- Unpinned third-party actions (not SHA-pinned)
- `pull_request_target` (fork PRs get write access)
- Script injection via `${{ github.event.* }}` in `run:` steps
- Secrets as env vars (could leak in logs)
- CODEOWNERS protection on workflow files

**Severity**: CRITICAL for `pull_request_target` + checkout of PR code / script injection. HIGH for unpinned third-party actions / secrets as env vars. MEDIUM for missing CODEOWNERS.

**FP rules**: First-party `actions/*` unpinned = MEDIUM not HIGH. `pull_request_target` without PR ref checkout is safe.

### Phase 5: Infrastructure Shadow Surface

**Dockerfiles**: Check for missing `USER` directive (runs as root), secrets passed as `ARG`, `.env` files copied into images, exposed ports.

**Config files with prod credentials**: Search for database connection strings (`postgres://`, `redis://`, `sqlite:///`) in config files, excluding localhost/example.com.

**Docker socket access**: Check if backend has Docker socket mounted and what permissions it has.

**Redis authentication and network isolation**:
- Check `redis.conf` or Docker env for `requirepass` — unauthenticated Redis reachable from the agent container network is CRITICAL (agents get full read/write to all task queues and secrets including other tenants')
- Check if agent containers have direct TCP access to Redis (e.g., `redis:6379`) — they should reach the platform only via the backend HTTP API, not the Redis port directly
- Check Redis keyspace for per-user isolation — if all users share a Redis namespace without user-scoped key prefixes, cross-tenant enumeration is possible even with auth

**Docker `base_image` allowlist**:
- Search agent creation code for the `base_image` field — verify it's validated against an allowlist (regex or explicit list), not accepted as free text from the API request body
- An unvalidated `base_image` lets any authenticated user pull and execute arbitrary Docker images inside the agent network, giving access to Redis, the MCP server, and credential env vars

**Setup/onboarding endpoint exposure**:
- Check if the first-run setup endpoint (`/api/setup`, `/setup`, etc.) is network-accessible or bound to 127.0.0.1 only
- If accessible from the public internet, check whether it uses a single-use bootstrapping token to prevent a race-condition hijack granting full admin control to an external caller

**Severity**: CRITICAL for Redis without auth accessible from agent network. CRITICAL for unvalidated `base_image`. HIGH for setup endpoint reachable from public internet without a single-use token. HIGH for root containers in prod / Docker socket access without read-only. MEDIUM for missing USER directive / exposed ports.

**FP rules**: `docker-compose.yml` for local dev with localhost = not a finding.

### Phase 6: Webhook & Integration Audit

**Webhook routes**: Find files containing webhook/hook/callback route patterns. Check whether they also contain signature verification.

**Internal endpoint auth completeness**:
- For every route in the internal router (routes under `/api/internal/` or any "internal" prefix), verify each endpoint enforces the shared-secret header (`X-Internal-Secret`) — not just that the pattern exists in the file, but that every individual route uses it
- Probe: send request to each internal endpoint without the header and confirm 401/403, not 200

**Webhook trigger auth (not just signature)**:
- For webhook trigger endpoints, check for the presence of `Depends(get_current_user)` or an equivalent auth gate — not just HMAC signature verification
- Trigger routes have been found missing auth entirely while signature verification exists elsewhere in the same file; the two are independent checks

**TLS verification disabled**: Search for `verify=False`, `VERIFY_NONE`, etc.

**MCP server security**: Check `src/mcp-server/` for authentication, input validation, and tool permission boundaries.

**Severity**: CRITICAL for webhooks without signature verification. HIGH for TLS disabled in prod code. MEDIUM for undocumented outbound data flows.

### Phase 7: LLM & AI Security

Trinity-specific AI security concerns:

- **Prompt injection**: User input flowing into system prompts or tool schemas in agent containers
- **Unsanitized LLM output**: `v-html`, `innerHTML` rendering LLM responses in frontend
- **Tool calling without validation**: MCP tool calls executed without permission checks
- **AI API keys in code**: Hardcoded API key assignments (not env vars)
- **Credential injection safety**: Are injected credentials exposed to agent code in unexpected ways?
- **Cost/resource attacks**: Can a user trigger unbounded LLM calls via agent chat?
- **Agent container network reach**: From inside an agent container, check what internal services are directly reachable (Redis, MCP server backend port, etc.). Agent containers should reach the platform only through the backend HTTP API — direct TCP access to Redis, internal service ports, or other agents' containers outside the API layer is a CRITICAL isolation failure. Check `docker-compose.yml` network definitions and confirm agent containers cannot reach `redis:6379` or `mcp-server:8080` directly.

**Severity**: CRITICAL for user input in system prompts / unsanitized LLM output rendered as HTML. CRITICAL for agent containers with direct Redis or internal service access bypassing the API. HIGH for missing tool call validation / exposed AI API keys. MEDIUM for unbounded LLM calls.

**FP rules**: User content in the user-message position of an AI conversation is NOT prompt injection.

### Phase 8: Skill Supply Chain

Scan `.claude/skills/` for suspicious patterns:
- `curl`, `wget`, `fetch` (network exfiltration)
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `process.env` (credential access)
- `IGNORE PREVIOUS`, `system override`, `disregard` (prompt injection)

**Severity**: CRITICAL for credential exfiltration attempts / prompt injection in skill files. HIGH for suspicious network calls. MEDIUM for skills from unverified sources.

### Phase 9: OWASP Top 10 Assessment

For each OWASP category, perform targeted analysis. Scope file extensions to Python (backend), Vue/JS/TS (frontend).

#### A01: Broken Access Control
- Missing auth on routes (check FastAPI dependency injection for auth)
- Direct object reference (can user A access user B's agents by changing IDs?)
- Horizontal/vertical privilege escalation
- **Internal router completeness**: Every endpoint in the internal router must use the shared-secret dependency — audit the full router, not a sample. No route should return 200 without the secret header.
- **Role-permission consistency across ALL creation routes**: For each resource-creation endpoint (`POST /api/agents`, `/api/processes`, `/deploy-local`, webhook-triggered deployments, etc.), verify the same role-check dependency is used. The lowest-privilege role must not bypass the creation gate via any alternate route.
- **File-write path policy completeness**: For any file-write API, verify the path-deny/allowlist is evaluated in full — not just basename matching. Specifically check: can `.mcp.json`, `.env`, `.credentials.enc`, or `.ssh/*` be reached via path traversal or a sibling endpoint (e.g., `/credentials/inject`) that enforces a shorter deny list?

#### A02: Cryptographic Failures
- Weak crypto (MD5, SHA1, DES, ECB) or hardcoded secrets
- JWT implementation (algorithm, expiration, secret management)
- **SSH key server-side generation**: Check if SSH private keys are generated server-side and returned in API responses. Flag as HIGH — clients should generate keypairs locally and submit only the public key. Server-generated private keys are exposed in transit and in API/access logs.

#### A03: Injection
- SQL injection: raw queries, string interpolation in SQL (check `database.py`)
- Command injection: `subprocess`, `os.system`, `os.popen` in backend
- Template injection: `v-html` in Vue components

#### A04: Insecure Design
- Rate limits on authentication endpoints?
- Account lockout after failed attempts?
- Business logic validated server-side?
- **Rate limiting quality**: Per-IP-only rate limits are bypassable via rotating proxies/Tor/IPv6 subnets. Check that rate limiting on auth endpoints (login, OTP, password reset) uses a per-account/per-email counter as the primary bucket. Also check that hard lockout on IP isn't itself a DoS primitive — progressive delay or CAPTCHA is safer than a hard block.
- **OTP brute-force window**: For email OTP flows, verify the code space (6 digits = 1M possibilities) is protected by an attempt counter that survives process restarts (stored in Redis/DB, not in-memory), and that codes expire within 5–10 minutes.

#### A05: Security Misconfiguration
- CORS configuration (wildcard origins?)
- CSP headers?
- Debug mode / verbose errors in production?
- Docker socket access scope

#### A07: Authentication Failures
- Session management: JWT creation, storage, invalidation
- Password policy for admin login
- Email login code security (brute-force protection, expiration)
- MCP API key management

#### A08: Software and Data Integrity Failures
- Deserialization inputs validated?
- Agent template integrity (can tampered templates be deployed?)

#### A09: Security Logging and Monitoring Failures
- Authentication events logged?
- Authorization failures logged?
- Admin actions audit-trailed?
- Credential operations logged (values masked)?

#### A10: Server-Side Request Forgery (SSRF)
- URL construction from user input (agent GitHub URLs, skill library URLs)
- Internal service reachability from user-controlled URLs
- Allowlist enforcement on outbound requests

### Phase 10: STRIDE Threat Model

For each major component (Backend API, Frontend, Agent Containers, MCP Server, Redis, Docker), evaluate:

```
COMPONENT: [Name]
  Spoofing:             Can an attacker impersonate a user/service?
  Tampering:            Can data be modified in transit/at rest?
  Repudiation:          Can actions be denied? Is there an audit trail?
  Information Disclosure: Can sensitive data leak?
  Denial of Service:    Can the component be overwhelmed?
  Elevation of Privilege: Can a user gain unauthorized access?
```

### Phase 11: Data Classification

Classify all data handled by the application into RESTRICTED, CONFIDENTIAL, INTERNAL, PUBLIC categories. Document where each type is stored and how it's protected.

### Phase 12: False Positive Filtering + Active Verification

**Daily mode (default)**: 8/10 confidence gate. Only report what you're sure about.
**Comprehensive mode**: 2/10 confidence gate. Flag `TENTATIVE` for lower confidence.

**Hard exclusions — automatically discard**:
1. DoS / resource exhaustion / rate limiting (EXCEPTION: LLM cost amplification)
2. Secrets on disk if encrypted and permissioned
3. Memory/CPU/FD leaks
4. Input validation on non-security fields without proven impact
5. Missing hardening measures without concrete vulnerability
6. Race conditions unless concretely exploitable
7. Vulns in outdated third-party libs (handled by Phase 3)
8. Files that are only tests/fixtures
9. Log spoofing
10. SSRF where attacker only controls path, not host
11. User content in user-message position of AI conversation
12. Security concerns in documentation files (EXCEPTION: SKILL.md files are executable prompt code)
13. Docker issues in `Dockerfile.dev` / `Dockerfile.local` unless referenced in prod deploy
14. CI/CD findings on archived/disabled workflows

**Active Verification**: For each finding that survives the confidence gate, attempt to PROVE it:
- Secrets: Check key format validity (DO NOT test against live APIs)
- Webhooks: Trace handler code for signature verification
- SSRF: Trace code path to check if URL construction reaches internal services
- Dependencies: Check if vulnerable function is directly imported/called

Mark each finding as `VERIFIED`, `UNVERIFIED`, or `TENTATIVE`.

**Variant Analysis**: When a finding is VERIFIED, search entire codebase for the same pattern.

### Phase 13: Findings Report + Trend Tracking + Remediation

**Every finding MUST include a concrete exploit scenario** — a step-by-step attack path.

**Findings table format**:
```
SECURITY FINDINGS
═════════════════
#   Sev    Conf   Status      Category         Finding                    Phase   File:Line
──  ────   ────   ──────      ────────         ───────                    ─────   ─────────
1   CRIT   9/10   VERIFIED    Secrets          AWS key in git history     P2      .env:3
```

For each finding, document: Severity, Confidence, Status, Phase, Category, Description, Exploit scenario, Impact, Recommendation.

**Trend Tracking**: If prior reports exist in `docs/security-reports/`:
- Compare findings by fingerprint (sha256 of category + file + title)
- Report: Resolved / Persistent / New / Trend direction

**Remediation Roadmap**: For top 5 findings, present via AskUserQuestion with options: Fix now, Mitigate, Accept risk, Defer.

### Phase 14: Save Report

Save findings to `docs/security-reports/cso-{date}.json` with schema including: version, date, mode, scope, phases_run, attack_surface, findings[], supply_chain_summary, filter_stats, totals, trend.

Also save a human-readable markdown summary to `docs/security-reports/cso-{date}.md`.

## Completion Checklist

- [ ] Architecture mental model built
- [ ] Attack surface census completed
- [ ] Secrets archaeology scanned
- [ ] Dependency supply chain audited
- [ ] CI/CD pipeline reviewed
- [ ] Infrastructure shadow surface checked
- [ ] Webhook & integration audit done
- [ ] LLM & AI security assessed
- [ ] Skill supply chain scanned
- [ ] OWASP Top 10 evaluated
- [ ] STRIDE threat model produced
- [ ] Data classification completed
- [ ] False positive filtering applied
- [ ] Active verification attempted
- [ ] Findings report generated with exploit scenarios
- [ ] Trend tracking computed (if prior reports exist)
- [ ] Report saved to `docs/security-reports/`

## Error Recovery

| Error | Recovery |
|-------|----------|
| Git not available | Fall back to file-only scanning |
| No prior reports | Skip trend tracking, mark as first run |
| Phase fails | Log error, continue with remaining phases |
| Too many findings | Apply stricter confidence gate, report top findings |

## Important Rules

- **Think like an attacker, report like a defender.** Show the exploit path, then the fix.
- **Zero noise > zero misses.** 3 real findings beats 3 real + 12 theoretical.
- **No security theater.** Don't flag theoretical risks with no realistic exploit path.
- **Confidence gate is absolute.** Daily mode: below 8/10 = do not report.
- **Read-only.** Never modify code. Produce findings and recommendations only.
- **Framework-aware.** Know FastAPI's built-in protections, Vue's auto-escaping.
- **This is a PUBLIC repo.** All findings in the report must use placeholder values — never include actual secrets or credentials found during the audit.

## Disclaimer

This tool is not a substitute for a professional security audit. `/cso` is an AI-assisted scan that catches common vulnerability patterns. For production systems handling sensitive data, engage a professional penetration testing firm. Use `/cso` as a first pass between professional audits.

## Self-Improvement

After completing this skill's primary task, consider tactical improvements:

- [ ] **Review execution**: Were there friction points, unclear steps, or inefficiencies?
- [ ] **Identify improvements**: Could error handling, step ordering, or instructions be clearer?
- [ ] **Scope check**: Only tactical/execution changes — NOT changes to core purpose or goals
- [ ] **Apply improvement** (if identified):
  - [ ] Edit this SKILL.md with the specific improvement
  - [ ] Keep changes minimal and focused
- [ ] **Version control** (if in a git repository):
  - [ ] Stage: `git add .claude/skills/cso/SKILL.md`
  - [ ] Commit: `git commit -m "refactor(cso): <brief improvement description>"`
