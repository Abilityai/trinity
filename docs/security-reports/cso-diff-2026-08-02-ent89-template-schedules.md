# CSO Diff Audit — trinity-enterprise#89 template-declared schedules

**Date:** 2026-08-02 · **Mode:** daily (8/10 gate) · **Scope:** `--diff`, `feature/ent89-template-schedules` vs merge-base `c4c83f4a` (dev)
**Verdict:** ⚠️ **PASS with 1 MEDIUM** — LLM cost amplification via declared schedules meeting the pre-existing autonomy-toggle clobber. No CRITICAL, no HIGH.

## Diff surface

21 files (+2581/−28): 1 new leaf service (`services/template_schedules.py`), `template_service.py` (tolerant reader surface + a create-path GitHub fetch + the GitHub list-path fence), `agent_service/crud.py` (creation materializer), `system_service.py` (manifest dedupe), `compatibility/{spec,static_checks}.py` (T-018 + 3 corrections), 8 docs, 4 test files.

**Zero new endpoints. Zero new routes. Zero new inputs from an unauthenticated caller.** No dependency, CI/CD, Dockerfile, or compose changes. No schema change, no migration.

## What was checked (and the evidence)

- **Secrets (P2):** pattern grep over every added line across the 12 branch commits (AWS/OpenAI/GitHub/Slack/`whsec_`/`re_`/private-key/password assignments) — clean. The only matches are the *identifier* `github_pat_for_agent`, not a value. Enterprise-docs guard: the 3 token hits in changed docs (`Public Access & Monetization` headers) are pre-existing context lines, not additions.
- **Credential handling (A02):** the new `fetch_template_metadata_for_create` places the resolved PAT in an `Authorization: Bearer` header only (`template_service.py:53-54`); it never reaches the URL, the query string, or any log line. The failure WARNING (`:105-113`) emits repo + ref + an exception *type-and-message* string; httpx exception text carries the request URL, never headers.
- **Auth boundaries (A01):** no new endpoint, so no new gate. The materializer threads `owner_username` from `current_user.username` (`crud.py:2375`) rather than re-resolving, and `db.create_schedule`'s three authorization gates are untouched and now *checked* rather than assumed — `user exists` / `can_user_access_agent` / `is_agent_live` (#1445 no-orphan), `db/schedules/crud.py:105-125`. Its `None` return is explicitly counted as `failed` instead of being read as success.
- **Injection (A03):** no raw SQL, no subprocess, no `eval`, no deserialization added (grep over added lines, empty). Schedule writes go through SQLAlchemy `insert()` with bound parameters.
- **Enumeration uniformity (#186):** unchanged — no agent-scoped handler added or modified.
- **Agent-key self-boundaries (#307/#1083/#918):** no endpoint accepting an agent's own key was added or touched.
- **XSS (A03/H-005):** T-018's `detail.errors` is persisted to `agent_compatibility_results.checks_json`, but `CompatibilityPanel.vue` renders only `c.explanation` through `renderMarkdown` (DOMPurify); `detail.errors` has no render path today. Independently, the error strings are constructed so they can never echo `name`/`message`/`description` values (`template_schedules.py:91-118`, locked by `test_errors_never_echo_name_message_or_description`); the only echoed values are cron/timezone, printable-filtered and length-bounded by `_safe_echo` (`:65-88`).
- **Untrusted-input totality (A08):** `template.yaml` is untrusted on all three sources (bundled, arbitrary `github:` repo, user-uploaded `local:`). The reader is a total function — proven by a Hypothesis property test over arbitrary YAML shapes (`test_property_reader_is_total_over_arbitrary_yaml`), not just an example matrix.
- **Cross-tenant disclosure:** the new `schedules` / `schedule_errors` catalog keys are safe. `get_local_templates()` scans only the read-only curated mount (`_local_templates_dir()`, `template_service.py:528-539`) — **not** `/data/deployed-templates`, so one user's uploaded template's `message:` bodies never surface in another user's catalog.
- **No surface in diff:** dependencies, CI/CD, Docker/infra, Redis ACL + two-network invariant (#589), skills, webhooks, internal endpoints, vendored-parity files (Invariant #5), OAuth scopes, MCP tool descriptions (#846), voice-token config-lock.

## Findings

| # | Sev | Conf | Status | Category | Finding | File:Line |
|---|-----|------|--------|----------|---------|-----------|
| 1 | MEDIUM | 8/10 | VERIFIED | LLM cost amplification | A template can arm 20 every-minute autonomous schedules that one autonomy toggle enables at once | `template_schedules.py:35` · `agent_service/autonomy.py:60` |

### [1] MEDIUM — declared schedules × the `set_autonomy_status` clobber

**Exploit path** (traced end to end, executed not inferred):
1. An attacker-authored (or merely careless) public `github:` template declares 20 entries with `cron: "* * * * *"` and `enabled: true`. Verified accepted: `MAX_DECLARED_SCHEDULES: 20 | materialized: 20 | enabled: 20`, cron `'* * * * *'` passes `validate_cron_expression`.
2. A `creator` — or an agent-scoped MCP key holding `create_agent` — creates an agent from it. All 20 rows are written.
3. Nothing fires yet: `agent_ownership.autonomy_enabled` is `INTEGER DEFAULT 0` and the scheduler gates on it (`src/scheduler/service.py:844`). **This is the control that holds, and it is why honoring `enabled: true` is the correct call.**
4. The operator later enables autonomy for any unrelated reason. `set_autonomy_status_logic` force-enables **every** schedule on the agent with no filter (`services/agent_service/autonomy.py:60-61`) — arming all 20 at once: ~28,800 LLM turns/day on the operator's own API key or subscription.

**Why it is MEDIUM and not HIGH:** the amplification *capability* is pre-existing — there has never been a per-agent schedule cap, so 20 deliberate API calls always achieved the same thing. What #89 changes is friction: one template choice plus one unrelated toggle now does it. Concurrency is still bounded by `max_parallel_tasks` (default 3), the fan-out by `MAX_DECLARED_SCHEDULES = 20`, ghosts are skipped, and ent#69 refuses ephemeral callers.

**Assessment:** correctly analysed and honestly documented by the author at `docs/memory/requirements/scheduling.md:115-119`, including the key insight that forcing `enabled: False` at creation would prevent *nothing* because the first toggle erases per-schedule intent either way. The gap was not the code — it was that the "separate P2 follow-up" that document promised did not exist in either tracker.

**Resolution:** filed as **[#1945](https://github.com/Abilityai/trinity/issues/1945)** (P2, `type-bug`, `theme-reliability`) during this audit — make `set_autonomy_status` stop clobbering per-schedule `enabled` intent. That single fix closes the amplification at its root and is the real control for both this and the pre-existing manual path. The requirements doc now cites the issue number. **No change to this PR.**

## Candidates considered and discarded (3)

1. **Log injection via the unsanitized `reason` argument** (`template_service.py:113`) — `reason` is interpolated raw while its `repo`/`ref` neighbours are `_sanitize_for_warning`-wrapped, and a repo string with no `/` skips `_GITHUB_REPO_PATH_RE`, so control bytes can reach the log line through an httpx `InvalidURL` message. **Discarded per hard exclusion #9 (log spoofing).** Reported instead as a code-consistency item in the `/review` pass — it is a hygiene inconsistency inside one call, not a security finding.
2. **Prompt injection via `schedules[].message`** — a template now supplies recurring prompt text executed by the agent. **Discarded: no new trust boundary.** The same `template.yaml` already ships `CLAUDE.md` (the agent's own system instructions) and its skills; a template that can inject via a schedule message could already inject far more directly. Correctly reasoned in `requirements/scheduling.md:168-170`.
3. **SSRF via `repo` in the GitHub API URL** (`template_service.py:58`) — `repo` is caller-controlled and reaches an f-string URL. **Discarded per hard exclusion #10:** the host is a hard-coded literal (`api.github.com`); the attacker controls path only. The `?ref=` value is separately constrained upstream (`_parse_github_ref` nulls any branch failing its charset check) and httpx percent-encodes it. Pattern is unchanged from the pre-#89 `_fetch_template_yaml`.

## Trend

Prior diff audits: 2026-07-30 (#1880) 0 findings · 2026-07-31 (#1860) 0 · 2026-07-31 (#1919) 0 · **2026-08-02 (ent#89) 1 MEDIUM**. New finding, not a persistent one; it is an amplification of a pre-existing mechanism rather than a newly introduced defect.

## Verification note

Independent finding verification was performed **in-context** rather than via fresh-context subagents (subagent dispatch was disabled for this session). Finding 1's exploit path was verified by direct execution of the reader against a 50-entry every-minute block, plus source confirmation of the autonomy gate and the clobber, rather than by pattern inference.
