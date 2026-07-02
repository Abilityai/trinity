# Trinity - Autonomous Agent Orchestration Platform - Requirements

> **SINGLE SOURCE OF TRUTH** — all development must trace back to these documents.
> Update the relevant area file **BEFORE** implementing any new feature.

This document is the **index** for Trinity's requirements. The requirements
themselves live in per-area files under [`requirements/`](requirements/) — the
monolith was split for navigability and to remove merge-conflict pressure
(#1406). This file stays the stable entry point, so existing links to
`requirements.md` still resolve.

Start with [`requirements/overview.md`](requirements/overview.md) for the
platform Vision, Status Labels, Non-Functional Requirements, and Out-of-Scope
list.

## Area files

| Area file | Covers |
|-----------|--------|
| [overview.md](requirements/overview.md) | Vision, Status Labels, Non-Functional Requirements, Out of Scope |
| [core-agent.md](requirements/core-agent.md) | Core agent management, template system, chat & terminal, activity monitoring, agent collaboration |
| [auth.md](requirements/auth.md) | Authentication & authorization (email/admin login, roles, JWT) |
| [credentials.md](requirements/credentials.md) | Credential management (file injection, encrypted exports) |
| [mcp.md](requirements/mcp.md) | MCP server, A2A discoverability, per-agent MCP exposure |
| [scheduling.md](requirements/scheduling.md) | Scheduling & autonomy, agent-defined pipelines, schedule timeout validation, MCP chat timeout recovery, sequential agent loops |
| [github.md](requirements/github.md) | GitHub integration (sync, PAT, recovery) |
| [infrastructure.md](requirements/infrastructure.md) | Infrastructure, platform operations, CLI tool, canary invariant harness, enterprise edition architecture, build info surface |
| [content-files.md](requirements/content-files.md) | Content & file management, image generation, avatars, agent runtime data |
| [runtimes.md](requirements/runtimes.md) | Multi-runtime support, OpenAI Codex, voice chat, VoIP telephony |
| [public-access.md](requirements/public-access.md) | Public access, Nevermined payments, mobile admin PWA |
| [security.md](requirements/security.md) | Security & compliance, operator queue / Operating Room, agent guardrails |
| [skills.md](requirements/skills.md) | Skills management (GitHub-based), playbooks tab |
| [lifecycle-observability.md](requirements/lifecycle-observability.md) | Agent soft-delete & retention, compatibility validation, first-run operator profile, agent-reported structured reports |
| [roadmap.md](requirements/roadmap.md) | Advanced features, planned features, process engine, future vision |

## How requirements are organized

- **One area per file.** Each file groups a cross-cutting product area. A single
  requirement lives in exactly one area file.
- **Where does a new requirement go?** Add it to the area file that owns its
  product surface (use the table above). If a new requirement spans areas, put
  it in the file matching its **primary** surface and cross-link the others with
  a relative link (e.g. `[MCP exposure](mcp.md)`). If nothing fits, it belongs in
  `roadmap.md` until it earns its own area — do **not** re-grow this index into a
  monolith.
- **Identify by feature tag, not ordinal.** Existing sections keep their
  historical `## N.` numbers (verbatim, no renumbering), but that global ordinal
  sequence is **retired** — it already collided (two `## 35.`). New requirements
  get a short heading with their issue/feature tag (e.g.
  `## Fleet Snapshots (#1234)` or a `FOO-001`-style ID), **not** the next number.
- **This index is thin by contract.** It carries no requirement text — only the
  map and these rules. Keep it that way (a CI guard enforces the size cap and
  flags dangling `requirements/` links).
