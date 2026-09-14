# Feature: Agent Compatibility Validation (#668)

> **Type**: feature · P1 · `theme-devex` · Epic #1045 (Agent Infrastructure)
>
> **One-line**: Advisory, non-blocking server-side validation of a **running** agent's workspace against 88 best-practice checks (12 categories), surfaced in the Agent Detail Overview tab with one-click auto-fix for the 9 gitignore checks, plus an MCP tool.

## Overview

Agents deployed to Trinity that don't follow Trinity conventions (no `template.yaml`, `.claude/` gitignored away, secrets committed, no playbooks) fail silently at runtime. This feature runs a deterministic + AI-assisted compatibility check against the agent's live workspace and surfaces HARD / SOFT / INFO findings — **without blocking deployment**.

The canonical check list is **`docs/agent-validation-spec.md`** (88 checks), the single source of truth kept in lockstep with `services/compatibility/spec.py` by `tests/unit/test_compatibility_checks.py::TestSpecDocSync` — which asserts the id set **and** the Severity column, over a `[A-Z]{1,2}-\d{3}` id regex (#2137).

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

- **Collector**: one `docker exec` runs a base64-injected in-container Python script that walks a FIXED path allowlist and emits ONE JSON snapshot — per-file `{exists, size, binary, truncated, content}` with 256 KB/file + 2 MB/total caps. Secret-bearing files (`.env`, generated `.mcp.json`) are **existence-only** (content never leaves the container). Backend `json.loads` once → `unavailable` on any failure (never 500). Stopped container detected via `docker_service` **before** exec → degraded report showing the last persisted result. Scan root from `git_service._detect_git_dir` — git's own `rev-parse --show-toplevel` since #2075, so an agent that keeps a populated non-git `workspace/` data directory is no longer scanned (and auto-fixed) at the wrong root, which used to fail `F-001 template.yaml exists` and thereby skip the 23 `_with_template` checks behind it.
- **Static checks**: pure functions over the snapshot, registered in `STATIC_CHECKS` (consistency-tested against `spec.STATIC_IDS`). HARD checks are all STATIC. P-006 (autonomous approval-gate scan) and S-003/S-009 (secret pattern scan) are implemented STATIC; the report cites secret **location/pattern**, never the value.
- **Self-explaining D-block messages (#2110).** D-002 and D-003 (HARD) carry their specifics in `message`: the Overview panel renders `c.message` only and `_check_dict` copies the string verbatim, so one string reaches the UI row, the `CompatibilityReport` JSON / MCP `get_agent_compatibility_report`, and persisted `checks_json` with zero frontend/MCP/schema change. D-002 shape: `unsupported dashboard widget type(s): 'chart' ×5 — not rendered; supported: metric, status, progress, text, markdown, table, list, link, image, divider, spacer` (plus a trend hint only when chart is among the bad types) — quoted names with counts, alphabetical, ≤5 named then `+N more`, each clipped to 40 printable chars (`_clip`) *before* it becomes the counting key so message and `detail` agree; D-003 appends `'list' needs items, 'text' needs content` after its unchanged prefix. Hardening: non-string `type`/`color` (`5` / `true` / `{a: 1}`) no longer raise into "check could not be evaluated" (D-003's raise was a spurious HARD), and YAML `date` values in a progress `label` / status `color` are stringified at the origin — at HEAD they reached `detail` raw and `json.dumps(checks)` in `upsert_result` raised, failing persistence of the WHOLE report. D-004/D-005 message text unchanged. `upsert_result` now serialises with `default=str` as the belt. `_WIDGET_TYPES` is a tuple in the agent-server order; it, the agent server's `valid_types` (the gate that strips unknown widgets), the `DashboardPanel.vue` render chain and five contract docs are pinned by `tests/unit/test_2110_widget_type_parity.py`.
- **A detector must never read narrower than the mechanism it audits (ent#128).** Both credential HARD gates were two expressions of that one root cause. **K-001** compared `.mcp.json.template`'s `${VAR}`s against an UPPERCASE-ONLY view of `.env.example`, but Trinity's substitution engines impose no charset (a `str.replace` and an `env_val[2:-1]` slice), so `${my_var}` IS substituted at runtime and a template documenting `my_var=` was HARD-failed for a gap that did not exist. **K-002/T-015** compared the same references against `set(credentials.keys())` — the *section* names — so the documented structured form (`credentials.mcp_servers.<s>.env_vars`) satisfied nothing while `${env_file}` and `${mcp_servers}` PASSED; that admitted set was "whichever sections this template happens to use", making the blind spot template-dependent. Both now read the declaration via `template_service.declared_credential_names()`, and the four detector patterns share `services/credential_charset.py` — which carries an explicit **NON-MEMBERS** list, because `mcp_validator._ENV_VAR_REF_RE` is a *fail-closed gate* paired with a deliberately widest finder and widening it would ADMIT input, not fix a false positive.
- **A HARD gate must not be able to go dark (ent#128).** `run_static` caught `Exception` → `skipped` and `_counts` counted only `status == "fail"`, so a raise inside a HARD check DROPPED `hard_count` and could flip `overall_status` from `issues` to `compatible` on an agent with a real problem. Four lines of untrusted `template.yaml` were the trigger (`env_vars: [{K: v}]` → `TypeError: unhashable type: 'dict'`), and because `c_k002` delegates to `c_t015` one raise took BOTH gates dark together — indistinguishable from a clean pass in the counts. `template.yaml` is read from the agent's own workspace, so it is a self-attestation bypass on the surface that polices it. Three layers now: `c_t015` wraps ONLY its new term and degrades to the narrower set (which makes `missing` LARGER — fail-closed by direction, never `skipped`); `run_static` returns **FAIL** for a raising check (*a check that could not evaluate is not a check that passed*); and `_counts` also counts `skipped` + `skip_reason == "check_error"` as a finding, at the sink (#1525), so the property survives a future path reintroducing the skip. A benign precondition skip (`no_template`, `ai_not_run`) still counts as nothing.
- **Verdict changes (release-noted, not monotone).** The blanket "strictly monotone, `fail→pass` only" claim is false. The complete set: `fail→pass` for K-001/K-002/T-015 (the fix); `pass→fail` for K-002 on `${env_file}`/`${mcp_servers}`/`${config_files}` (a closed false negative, deliberate); `pass→fail` for **K-003 (SOFT)** on a lowercase-only comment-free `.env.example` (collateral of widening `_env_example_vars`, which is K-003's precondition for *demanding* comments — the verdict is correct but it is a `pass→fail`); and `pass→pass` for S-010, safe only because its `generic` blocklist is uppercase-exact — asserted, not assumed. The claim that survives: **no agent gains a HARD failure.**
- **AI checks**: category-batched calls to Anthropic `/v1/messages` (`claude-haiku-4-5`) via `settings_service.get_anthropic_api_key` + httpx, tool-use structured output. **Iterate-expected** (an omitted check becomes `skipped`, never vanishes), per-item validation, concurrent via `asyncio.gather`, fail-open (no key / API error → `skipped` with reason). **AI severity is capped at SOFT** — an LLM verdict never drives the HARD count. Secret-bearing files are never sent; a redaction pass runs over every file before egress.
- **Runtime-aware**: Claude-only checks (`CLAUDE.md`, `.claude/` skills) are omitted for non-Claude runtimes (Codex/Gemini, #1187).
- **Fixes**: the 9 gitignore checks (G-002 retired in #2137); reuses `git_service._GITIGNORE_PATTERNS`; per-agent Redis lock (`compat_fix:{name}`, ownership-checked via the shared `SingleFlightLock` #1920 — a former constant-"1" + unconditional-delete twin of system_seed's bug); atomic base64 write-back (`… | base64 -d > .gitignore.tmp && mv`); G-001 removes a blanket `.claude/` line by exact-line match (never substring, CRLF-normalized). **No auto-commit** — uncommitted until the agent's next git sync. `check_id` validated against the spec-derived whitelist (400 otherwise; 409 on a concurrent fix).

### T-018 and the fail-open class (trinity-enterprise#89)

`T-018` (soft, static) reports the `template.yaml` `schedules:` block's
**structure** — presence/type of `name`/`cron`/`message`, entry shape, block
shape, bounds, cap — via the same `services/template_schedules.py` reader the
creation-time materializer uses, so the report cannot drift from what creation
actually does. It exists because Trinity now *acts* on that block: a malformed
one silently costs the agent its recurring tasks.

**Cron is A-002's, not T-018's.** A-002 already shipped as "cron expressions are
valid"; two checks disagreeing about one field is worse than either alone. (The
reader *does* gate cron strictly, but that is a materialization decision — drop
the entry — not a report verdict.) A-002's own validator was replaced in the
same change: `_valid_cron` was a per-field `^[\d*/,\-]+$` regex, wrong in **both
directions** — it rejected `0 9 * * MON` (valid; the scheduler translates named
days) and accepted `99 99 * * *` (invalid; no range check). It now delegates to
`schedule_validation.validate_cron_expression`, the parser the dedicated
scheduler registers jobs with (#1472). *Validate a config with the same parser
the executor uses.*

**T-018 is the one check that fails closed, and the reason generalizes.**
`run_static` catches a raising check and records `_skip(..., "check_error")`;
`_counts` counts only `status == "fail"`. So a raising **soft** check drops
`soft_count` 1→0, and because `overall` is a bare
`(hard_count + soft_count) > 0` test, the whole report flips `issues` →
`compatible` exactly when that check's finding was the only failure — which is
the entire population T-018 serves. It is also **durable**: `build_report`
persists `checks_json`, and `_report_from_persisted` recomputes counts from that
snapshot, so one transient raise is replayed as a clean bill of health on every
stopped-agent read. T-018 therefore catches its own `Exception` and returns
`_fail`, carrying `type(e).__name__` **only** — `str(e)` can embed untrusted
template content into a persisted, UI-rendered blob.

This was not hypothetical. **`c_p006` — a HARD check — was failing open in the
field**: it iterated `data.get("schedules") or []` with no
`isinstance(..., list)` guard, unlike all four sibling readers of that field, so
`schedules: 5` raised `TypeError` and the check silently vanished from
`hard_count`. Fixed in the same change, which is what makes the fail-closed
design evidence-backed rather than theoretical.

`run_static`'s swallow now **logs** (`logger.error`, previously silent for all
~100 checks). Converting that swallow to `fail` platform-wide is deliberately
*not* done here — it would flip an unmeasured number of installs to `issues` in
one step; the log line is the instrument for measuring first.

Regression coverage runs every malformed fixture through **all** of
`spec.STATIC_IDS` asserting no check lands at `skip_reason == "check_error"` — a
T-018-only assertion would never have caught `c_p006` — plus a `build_report`
test pinning the *direction* (a raising reader must still yield
`overall_status == "issues"`).

### Catalog alignment (#2137)

The catalog had drifted from both what the platform implements and what the
public `create-agent` wizards generate. A freshly-scaffolded agent landed with
**9 findings (6 SOFT + 3 INFO)** that were not the author's fault and could not
be acted on — which trains operators to ignore the panel — while the one HARD
check guarding autonomous runs from hanging (**P-006**) was silently inert.
101 checks → **88**; a wizard agent now reports **0 HARD, 0 SOFT, 5 INFO**
(optional `template.yaml` metadata + the A-001 determinism suggestion).

- **Gating on fields nothing reads.** `template.yaml`'s `git:` block
  (`commit_paths` / `ignore_paths` / `push_enabled`) has no backend reader
  anywhere and no bundled template declares it — yet **T-017 was HARD**. Same for
  `metrics:` (D-006; `dashboard.yaml` is the read surface) and `.trinity/post-check`
  (I-005), whose only other mention was a `git_service` comment pointing back at
  I-005. All retired, and `TRINITY_COMPATIBLE_AGENT_GUIDE.md` now marks the `git:`
  block inert so authors stop writing it. **The `.trinity/post-check` PATH stays in
  `_TRINITY_AUTHORED_PATHS`** — #2070 derives the `!` re-includes from that tuple
  and 14 bundled templates ship `!.trinity/post-check`, so removing it would
  untrack an authored hook on the next push.
- **A retired duplicate is not always a severity duplicate.** `c_k002` was
  literally `return c_t015(snap)` — but K-002 was declared **HARD** and T-015
  **SOFT**. Retiring K-002 as a plain duplicate would have silently downgraded the
  credential-declaration gate and undone ent#128's deliberate choice. **T-015 was
  promoted to HARD**; `test_ent128b1_compat_gates.py` (which asserts
  `hard_count >= 1` on a hostile declaration) is what caught it.
- **P-006 had never fired.** `_slash_command()` anchored at position 0
  (`^\s*/`), but the marketplace's own generated schedules read
  `"Run /pipeline-tick"`, `"Run /project-steward"`, `"Run /weekly-report and post
  the summary"`. The scheduled-command set came back empty, so the HARD check
  returned *"no scheduled/autonomous skills declared"* on precisely the agents it
  exists to guard. The matcher now finds a slash command anywhere a token starts
  (`(?<![^\s(\[])/…`), so a POSIX path or URL is still not mistaken for one.
  P-006 also honours an **`automation: gated|manual`** frontmatter opt-out — the
  convention already used across `abilityai/abilities`; an intentional human pause
  is a design decision, not a defect.
- **X-007 was blind to the only layout the wizards produce.** `_command_names()`
  globbed `.claude/commands/` only, so a skill-based agent naming a real skill was
  reported as referencing a missing command. Both layouts now resolve.
- **The catalog contradicted itself.** P-009 tells authors to split reference
  material out of `SKILL.md`; P-004 then flagged the resulting `reference.md`,
  because `_skill_files()` walks every `.md` under `.claude/skills/`. P-004 is
  scoped to `SKILL.md` (matching P-002), so the two are now a ladder: split at
  ~200, fail at 500.
- **Unactionable SOFTs downgraded to INFO** — `T-007`/`T-008` (metadata),
  `T-010`/`T-011` (only `_build_local_template()` surfaces these; the
  GitHub-sourced builder, now the default path, never reads them), `K-003`,
  `C-012`, and `A-001` (prose schedule messages are valid Trinity input, and the
  wizards generate prose for all 13 of their scheduled tasks). `F-004` became
  conditional on the agent actually declaring credentials.
- **The anti-drift test did not hold.** `test_ids_match_doc` matched
  `^\|\s*([A-Z]-\d{3})\s*\|` — a **single** letter — so the doc's
  `DP-001`…`DP-005` never participated: five checks were documented, indexed, and
  entirely unimplemented while the test reported the two files in sync. The regex
  is now `[A-Z]{1,2}-\d{3}`, and a **new `test_severities_match_doc`** asserts the
  Severity column too (an id-only test passes happily while the doc claims HARD for
  a check the catalog emits as INFO — and that column is what an operator reads).
  `DP-001`..`DP-004` are now implemented (`data_paths` IS a real field: #1169
  `materialize_data_paths`); `DP-005` was retired because `.trinity/pre-snapshot`
  has no executor. DP-001 shares `git_service._is_safe_data_path` with the
  materializer — the A-002 discipline of validating with the parser the executor
  uses — and adds the containment check the materializer deliberately omits.
- **Retired ids are never reissued**, so persisted `checks_json` rows written
  before the retirement stay interpretable. The full table is in
  `docs/agent-validation-spec.md` § Retired checks.

## Persistence

`agent_compatibility_results` (latest-snapshot-per-agent, upserted by `agent_name`). **Departs from the issue's original "no DB table" note** (see `docs/memory/requirements/lifecycle-observability.md` §42.1): AI verdicts aren't cheaply recomputable, so persistence lets them show on every Overview load without re-spending tokens and unlocks fleet aggregation. STATIC recomputes live each read; persisted AI verdicts merge in until a re-run. Dual-track migration (SQLite `db/migrations.py` + Alembic `migrations/versions/0003_*`); cascade/rename via the `AGENT_REFS` registry. Creates no execution, so Invariant #18 (idempotency) doesn't apply.

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
| Charset | `services/credential_charset.py` (shared detector charset + NON-MEMBERS list, ent#128) |
| Tests | `tests/unit/test_compatibility_checks.py`, `tests/unit/test_ent128b1_compat_gates.py` |

## Testing

`tests/unit/test_compatibility_checks.py` (fixture-driven, no Docker): spec consistency + spec↔doc sync, STATIC checks over good/bad/empty snapshots, gitignore fix transforms (CRLF/dup/comment/`.claude/projects/` survival/idempotent), AI batching (no-key skip, omitted→skipped, redaction), `build_report` orchestration (assemble + tmp-DB persistence, codex runtime omits claude_only, stopped→unavailable), and the collector script executed against a temp ROOT.

`tests/unit/test_ent128b1_compat_gates.py` (39 tests, ent#128) covers the credential
gates specifically: the complete transition set above, 9 hostile-declaration shapes
that must still HARD-fail, `run_static` returning FAIL for a raising check, the
`_counts` belt, and the charset NON-MEMBERSHIP assertions that catch an
"align all the regexes" refactor. Verified against a **49-fixture synthetic
adversarial corpus** diffed per check on `(status, skip_reason)` — `hard_count` alone
cannot distinguish `fail→pass` from `fail→skipped`, which is exactly how a dark gate
hides. The BUNDLED templates cannot prove any of it: with 0 `.mcp.json.template` and
0 `.env.example` files in the bundle, every changed check short-circuits before
reaching changed code (0 verdict diffs, normalizer invoked 0 times), so a green diff
there is green-because-vacuous.

`tests/unit/test_2110_widget_type_parity.py` (16 tests, #2110): Tier A code parity incl. order across `_WIDGET_TYPES` / the agent server's `valid_types` / `DashboardPanel.vue`, Tier B the five contract docs, Tier C the regression signature (no backticked fictional type on a "widget type(s)" line), plus planted-violation meta-tests proving each extractor bites. The D-block cases (+13, 98 → 111) live in `tests/unit/test_compatibility_checks.py`: message shape/ordering/cap/clip, non-string `type`/`color`, YAML-date `label`/`color`, `build_report` persisting the message, and the `upsert_result` `default=str` belt.

## Boundaries / fast-follow

Validates **running** agents only — a stopped/failing-to-boot container can't be exec'd (boot-triage is a separate follow-up). AI-verdict trend history is deferred (latest-only). Forward-looking template checks (#927 replica-safety, #1084 side-effect profile) tracked as spec follow-ups.

## See Also

- [agent-detail-overview.md](agent-detail-overview.md) (if present) — the Overview tab host
- `docs/memory/architecture.md` → Agent Compatibility Validation (#668)
- `docs/memory/requirements/lifecycle-observability.md` §42.1
