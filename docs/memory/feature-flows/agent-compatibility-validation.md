# Feature: Agent Compatibility Validation (#668)

> **Type**: feature · P1 · `theme-devex` · Epic #1045 (Agent Infrastructure)
>
> **One-line**: Advisory, non-blocking server-side validation of a **running** agent's workspace against ~100 best-practice checks (11 categories), surfaced in the Agent Detail Overview tab with one-click auto-fix for the 10 gitignore checks, plus an MCP tool.

## Overview

Agents deployed to Trinity that don't follow Trinity conventions (no `template.yaml`, `.claude/` gitignored away, secrets committed, no playbooks) fail silently at runtime. This feature runs a deterministic + AI-assisted compatibility check against the agent's live workspace and surfaces HARD / SOFT / INFO findings — **without blocking deployment**.

The canonical check list is **`docs/agent-validation-spec.md`** (100 checks), the single source of truth kept in lockstep with `services/compatibility/spec.py` by `tests/unit/test_compatibility_checks.py::TestSpecDocSync`.

## End-to-end flow

```
OverviewPanel.vue ─mounts─> CompatibilityPanel.vue
   │  (two-phase fetch)
   │   1. getCompatibility(name)                → STATIC live + persisted AI (instant paint)
   │   2. getCompatibility(name, includeAi)     → fresh AI (only if never run / on "Re-run")
   ▼ (stores/agents.js)
GET /api/agents/{name}/compatibility?include_ai=     (AuthorizedAgentByName — read)
POST /api/agents/{name}/compatibility/fix {check_id} (OwnedAgentByName — owner/admin)
   ▼ routers/compatibility.py
services/compatibility/__init__.build_report / apply_fix
   ├─ collector.collect()        ── ONE docker exec → in-container python3 → 1 JSON snapshot
   ├─ static_checks.run_static() ── pure (snapshot)->[Check], driven by spec.py
   ├─ ai_checks.run_ai()         ── category-batched Anthropic (Haiku), iterate-expected, fail-open
   ├─ fixes.apply_fix()          ── gitignore mutate, per-agent Redis lock, atomic write
   └─ db.upsert/get_compatibility_result()  (agent_compatibility_results — latest snapshot)
MCP get_agent_compatibility_report ── proxies the GET (Invariant #13)
```

## Frontend

- **`components/CompatibilityPanel.vue`** (rendered inside `OverviewPanel.vue`, section 2b) reuses the Overview "needs attention" idiom: a compact summary line (tone = danger/warning/success/gray) that expands to the full checklist grouped by category. Per-check **Fix** button for auto-fixable failures; **Re-run analysis** forces fresh AI; AI staleness stamp (`ai_ran_at`). Explanations render via `utils/markdown.js` `renderMarkdown` (DOMPurify).
- **`stores/agents.js`**: `getCompatibility(name, {includeAi})` + `fixCompatibilityIssue(name, checkId)`.
- **Two-phase fetch**: STATIC-only first (fast first paint of the default tab), then AI async — cache-backed by the results table so AI findings show on every visit without re-spending tokens.

## Backend (`services/compatibility/`)

Mirrors the deterministic `canary/` library: `spec.py` (single source of truth), `collector.py`, `static_checks.py`, `ai_checks.py`, `fixes.py`, `__init__.py`.

- **Collector**: one `docker exec` runs a base64-injected in-container Python script that walks a FIXED path allowlist and emits ONE JSON snapshot — per-file `{exists, size, binary, truncated, content}` with 256 KB/file + 2 MB/total caps. Secret-bearing files (`.env`, generated `.mcp.json`) are **existence-only** (content never leaves the container). Backend `json.loads` once → `unavailable` on any failure (never 500). Stopped container detected via `docker_service` **before** exec → degraded report showing the last persisted result. Legacy `workspace/` dir via `git_service._detect_git_dir`.
- **Static checks**: pure functions over the snapshot, registered in `STATIC_CHECKS` (consistency-tested against `spec.STATIC_IDS`). HARD checks are all STATIC. P-006 (autonomous approval-gate scan) and S-003/S-009 (secret pattern scan) are implemented STATIC; the report cites secret **location/pattern**, never the value.
- **AI checks**: category-batched calls to Anthropic `/v1/messages` (`claude-haiku-4-5`) via `settings_service.get_anthropic_api_key` + httpx, tool-use structured output. **Iterate-expected** (an omitted check becomes `skipped`, never vanishes), per-item validation, concurrent via `asyncio.gather`, fail-open (no key / API error → `skipped` with reason). **AI severity is capped at SOFT** — an LLM verdict never drives the HARD count. Secret-bearing files are never sent; a redaction pass runs over every file before egress.
- **Runtime-aware**: Claude-only checks (`CLAUDE.md`, `.claude/` skills) are omitted for non-Claude runtimes (Codex/Gemini, #1187).
- **Fixes**: the 10 gitignore checks; reuses `git_service._GITIGNORE_PATTERNS`; per-agent Redis lock (`compat_fix:{name}`); atomic base64 write-back (`… | base64 -d > .gitignore.tmp && mv`); G-001 removes a blanket `.claude/` line by exact-line match (never substring, CRLF-normalized). **No auto-commit** — uncommitted until the agent's next git sync. `check_id` validated against the spec-derived whitelist (400 otherwise; 409 on a concurrent fix).

## Persistence

`agent_compatibility_results` (latest-snapshot-per-agent, upserted by `agent_name`). **Departs from the issue's original "no DB table" note** (see requirements §41): AI verdicts aren't cheaply recomputable, so persistence lets them show on every Overview load without re-spending tokens and unlocks fleet aggregation. STATIC recomputes live each read; persisted AI verdicts merge in until a re-run. Dual-track migration (SQLite `db/migrations.py` + Alembic `migrations/versions/0003_*`); cascade/rename via the `AGENT_REFS` registry. Creates no execution, so Invariant #18 (idempotency) doesn't apply.

## MCP

`get_agent_compatibility_report(agent_name, include_ai?)` in `src/mcp-server/src/tools/agents.ts` (agent-scoped access control mirrors `get_agent_info`), `client.getAgentCompatibilityReport()`, `CompatibilityReport` type. Three surfaces in sync (Invariant #13).

## Key files

| Layer | File |
|-------|------|
| Spec | `docs/agent-validation-spec.md` (canonical), `services/compatibility/spec.py` |
| Service | `services/compatibility/{__init__,collector,static_checks,ai_checks,fixes}.py` |
| Router | `routers/compatibility.py` |
| DB | `db/compatibility.py`, `db/schema.py`, `db/tables.py`, `db/migrations.py`, `migrations/versions/0003_agent_compatibility_results.py` |
| Models | `models.py` (`CompatibilityCheck`, `CompatibilityReport`, `CompatibilityFix*`) |
| Frontend | `components/CompatibilityPanel.vue`, `components/OverviewPanel.vue`, `stores/agents.js` |
| MCP | `mcp-server/src/tools/agents.ts`, `client.ts`, `types.ts` |
| Tests | `tests/unit/test_compatibility_checks.py` |

## Testing

`tests/unit/test_compatibility_checks.py` (fixture-driven, no Docker): spec consistency + spec↔doc sync, STATIC checks over good/bad/empty snapshots, gitignore fix transforms (CRLF/dup/comment/`.claude/projects/` survival/idempotent), AI batching (no-key skip, omitted→skipped, redaction), `build_report` orchestration (assemble + tmp-DB persistence, codex runtime omits claude_only, stopped→unavailable), and the collector script executed against a temp ROOT.

## Boundaries / fast-follow

Validates **running** agents only — a stopped/failing-to-boot container can't be exec'd (boot-triage is a separate follow-up). AI-verdict trend history is deferred (latest-only). Forward-looking template checks (#927 replica-safety, #1084 side-effect profile) tracked as spec follow-ups.

## See Also

- [agent-detail-overview.md](agent-detail-overview.md) (if present) — the Overview tab host
- `docs/memory/architecture.md` → Agent Compatibility Validation (#668)
- `docs/memory/requirements.md` §41
