# CSO Diff Audit — 2026-10-05 — abilityai/trinity-enterprise#752 (gated skills, the in-container hook)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/752-gated-skill-hook` → `dev` · **Skill**: cso v1.2.3 · **Audited**: the uncommitted worktree diff against `09f9d0868` plus untracked files (51 files: 36 changed +897/−69, 15 new ~3,900 lines). Not a full run. No Docker operation was run (operator constraint for this session: other sessions share the local stack); image-internal checks are listed as coverage gaps.

## Architecture (Phase 0)

#751 checks a request before dispatch. It reads only what the requester typed, so a request that names a gated skill only in prose reached the executor, and the agent's own `Skill` call loaded the skill. This change adds the in-container half, for Claude Code only:

- **The hook.** `/opt/trinity/hooks/skill-gate.py` (bootstrap) + `_skill_gate.py` (decision), stdlib only. It is registered for `PreToolUse` on `Skill|Agent|Task` by its own managed-settings drop-in, `/etc/claude-code/managed-settings.d/50-skill-gate.json` (root:root 0444, dir 0755). The drop-in uses exec form, `/usr/bin/env -i /usr/local/bin/python3 -I -S …`, and pins `LD_PRELOAD` / `LD_LIBRARY_PATH` / `LD_AUDIT` to empty through managed `env`.
- **What the hook asks.** It sends the names the call loads (a `Skill` call, or a subagent definition's `skills:` preloads) to `POST /api/skill-gate/check`. It reads its identity from `/proc` only: the run from `/proc/<parent>/environ`, the URL and key from `/proc/1/environ`. Every outcome is exit 0 or 2. With no verdict, a root-owned marker decides.
- **The check.** `routers/skill_gate.py` → `skill_gate_service.check_invocation`. The agent is taken from the key (`get_self_agent`). A gated skill is allowed only when the gate record that dispatched the run, or a new `self_approved` record, clears that live run of this agent.
- **The marker.** `/opt/trinity/skill-gates-active`, written or removed by a root `docker exec` with a constant argv, at start/recreate (gated agents only) and from a rate-limited self-heal.
- **Isolated turns.** A self-approved `/chat` turn runs outside the agent's shared chat session (`isolated_session`).

Trust boundaries crossed: agent → backend (a new agent-callable route), backend → agent container as root (marker exec), platform text → executor context (the refusal, a tool result), and the claude process's environment → the hook (read from `/proc`).

## Stack CVE quicklist

| Component | Verdict |
|---|---|
| Starlette BadHost | Not reachable through this diff. The new route gates on `get_self_agent` (key-derived), not on a path. Every fence in `dependencies.py` reads `request.scope["path"]` (lines 671, 736, 757, 899, 1077 on this tree). |
| Claude Code in the base image | Patched. The pin is `2.1.281`, at/after the 2.0.65 settings-hook fixes. The diff adds a ratchet: the hook's `SKILL_GATE_CLI_VERIFIED` must equal the Dockerfile pin (`test_ent752_cli_pin.py`). Exec form was verified in the 2.1.281 bundle: with `args`, the hook is spawned `Ane(Bt[0], Bt[1], {env, cwd, …})` with no `shell` option, and only the shell-form string takes `CLAUDE_CODE_SHELL_PREFIX`. A configured sandbox wrapper re-quotes exec form into a shell command; Trinity agents run unsandboxed (recorded in the hook's re-audit list). |
| runc / Docker host | Operator host check (INFO), unchanged in kind. The diff adds `docker exec` into agent containers only for agents with gates (rate-limited heal). |
| Node CLIs in the base image | The row's premise is stale: `@google/gemini-cli@0.62.0` and `@openai/codex@0.147.0` are both pinned (#2971). Not in the diff. |
| Vite, Vitest, axios, Redis | Not touched by the diff. |

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | One new: `POST /api/skill-gate/check`. It is agent-callable, the agent comes from the key, and it has an `AGENT_CALLABLE` census entry. The ephemeral fence is not widened. |
| Unauthenticated | Agent `/health` gains `skill_gate_hook`, an enum only (`ok` / `missing` / `not_root_owned` / `writable` / `unsupported_runtime`). |
| In-container process | The hook runs as `developer` on every `Skill` / `Agent` / `Task` call. |
| Privileged exec | A root `docker exec` with a constant argv writes or removes the marker. The path rides as a positional argument; no agent or skill name enters it. |
| Agent-context ingress | One new: the refusal text, a tool result the executor reads. It is platform-authored. It echoes the matched gate keys (from the gate map) and, for a preload, the agent's own `subagent` value, collapsed to one line and capped at 128 characters. |
| Backend → agent | `/api/chat` payload gains `isolated_session` through the existing `agent_post_with_retry` (per-agent token, #1159). |
| Schema | None. `self_approved` is a new value of `skill_gate_requests.state`, inserted through an `on_conflict_do_nothing` + UNIQUE `dispatched_execution_id`. |

## Checks run (evidence)

- **Phase 2, secrets.** Added non-test lines were scanned for `sk-`, `ghp_`, `gho_`, `github_pat_`, `xox*-`, `AKIA`, `re_`, `trinity_mcp_` shapes: none. The test harness fixture `trinity_mcp_agentkey_752_do_not_log` sits under the blanket `tests/` gitleaks path allowlist. No `system_settings` writer was added. The hook logs names, via and outcome, never the key: `test_each_outcome_is_logged_without_the_key` asserts the key is absent from the log file. The key travels only in an `Authorization` header, never argv. The hook's own environment is empty (`env -i` + an in-process clear, `test_the_hook_empties_its_own_environment_before_deciding`).
- **Phase 3 / 4.** No dependency manifest, lockfile, install line or workflow is in the diff. The hook is stdlib-only, and its build smoke runs under the shipped interpreter and flags.
- **Phase 5.** The image still ends as `USER developer` (`test_the_image_still_ends_as_the_agent_user`). The new files are root-owned 0555/0444, and the drop-in directory is 0755. `managed-settings.json` is unedited (`test_the_main_registration_file_is_left_alone`), so startup's `cmp -s` legacy cleanup still matches.
- **Phase 6 / ASI03, the self boundary.** Six non-self principals get the same 403 `agent_identity_required`: a person, a user key, a connector, an ops key, a portal delegate, and a system key naming an agent. A key whose agent is gone gets 404. A sibling key answers only about itself, and a body naming an agent is a 422 (`test_ent752_check_endpoint.py`). Guards: `test_293_admin_gate_rejects_agent_keys`, `test_2996_human_only_routes`, `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_ent614_source_agent_attribution`, `test_agent_auth_header_guard`, `test_292_cmdline_credential_leak`, `test_2036_claude_settings_leak`: 259 passed. The runtime census (`test_2996_route_census_runtime`) needs `opentelemetry.exporter`, which this host venv lacks. Run through a scratch interpreter that stubs only that package, the live app registers `POST /api/skill-gate/check` and the census passes (3/3). CI runs it natively.
- **Clearance binding (A01).** An unknown run, another agent's run, a record on another agent, a finished run, and a failed self-approved run all get byte-identical answers from the same two reads (`test_every_run_id_costs_the_same_two_reads`). A record that ended `unknown` / `stale` / `not_run` clears nothing, and widening the state filter turns 3 tests red.
- **Phase 7, ASI05, the hook's integrity.** Repo settings `env` cannot reach the hook: no shell form, empty env, `-I -S`, and the loader variables pinned empty by policy scope. Its identity is read from `/proc`. It follows no proxy or redirect (`http.client`, `test_the_hooks_own_environment_is_ignored`). It cannot exit 1: the bootstrap catch-all falls back to the marker, and crash injection holds for an import failure, a raise, `SystemExit(1)`, a garbage return, and a logger that cannot start. It cannot hang: a 10 s daemon-thread deadline, non-blocking opens checked with `fstat`, FIFO traps tested.
- **Phase 7, ASI06 / ASI07 / ASI08.** The refusal hands the request back to be raised through #751's gate. No new dispatch path is created, so the #2806 depth cap is untouched. The copy names no person and no role (`test_the_refusal_names_no_person_and_no_role`).
- **Phase 8.** The invisible / bidi / tag-block Unicode scan over all 51 changed and new files found 0 hits. Two literal U+FEFF characters introduced during development were replaced with `﻿` escapes before this scan.
- **Phase 9, A09.** Refusals are audited `skill_gate_refused`: once per run and skill, and at most 20 per agent, per 10 minutes. The run id is caller-supplied, so the per-agent cap is what bounds rows in the append-only log (`test_an_agent_cannot_fill_the_audit_log_by_varying_the_run_id`).
- **Phase 9, A10 (failure direction).** Every new guard states it:
  - the hook fails closed only where the marker says gates exist, and open elsewhere;
  - the check returns every verdict, and only an unreadable map is a 503;
  - a `None` gate map is unreadable, never "nothing gated";
  - a failed self-approval write lets the run proceed and the hook then refuses inside it;
  - a failed marker exec changes nothing;
  - the refusal audit is best effort;
  - marker syncs of one agent are serialised per worker.

  No discriminator is read through a privileged default. `get_self_agent` fails closed for any scope other than `agent` / `system`.

## Findings

None at or above the 8/10 gate.

## Considered and dismissed

- **The heal lets an agent trigger a root exec on its own container.** The argv is constant, the effect is derived from the gate map (never from the report), and it runs at most once per agent per 5 minutes. No privilege gain.
- **The check tells an agent whether a skill name is gated on itself.** This is by design: #751 contract thread, point 4 ("an agent may read its own gates, read-only, own gates only"). It never discloses the approver or the role.
- **The hook reads files the agent can read.** It runs as the agent's own user, parses only names, and sends names that are never persisted (only the matched gate keys reach the log line and the audit).
- **The empty loader pins.** glibc ignores an empty `LD_LIBRARY_PATH` / `LD_PRELOAD` / `LD_AUDIT` (it processes them only when non-empty), so the pins cannot introduce a current-directory search path. The base image and `.env` (`PROTECTED_KEYS`) never set them.
- **Agent-authored names in `/logs/guardrails.jsonl`.** A skill or subagent name the agent wrote can appear in its own guardrail log line, as Bash command prefixes already do (`bash-guardrail.py`). This is agent-authored output, not a platform credential entering a log (the ent#292 class), so it is not counted.
- **A stated limit was understated.** Corrected in this branch: a nested `claude` started from Bash does not need a forged execution id. A `/<skill>` prompt expands there with no tool call at all. The stated limits in §26.13, the flow and the hook docstring now say so. The boundary remains credential confinement (R23, trinity-enterprise#558).

## Hypotheses

(daily mode — none reported)

## Coverage gaps

- **No base-image build in this run** (operator constraint: no local Docker operations this session). The `--self-test` build smoke, the drop-in's ownership and modes in a built image, runtime UIDs, and the `.pth` census of the base image were not exercised. Neither was the CLI loading the drop-in and running the hook inside a real agent container. Pending a `/verify-local` run with the agent (a `skill_gate_allow` line after a real `Skill` call) and the localhost eyeball.
- **Phase 3 scanners.** Not run, and not needed: the diff changes no dependency.

## Data classification (diff)

| Data | Class | Where | Protection |
|---|---|---|---|
| `self_approved` record (requester email, sanitised and bounded request text, skills, run id) | CONFIDENTIAL | `skill_gate_requests` | Already a person-identity table (`gate-` rows are in `_ABOUT_A_PERSON_ID_PREFIXES`). The new rows are never read by a machine key; CASCADE on agent delete. |
| `skill_gate_refused` audit (agent, skills, caller-supplied run id, reason, key id) | INTERNAL | `audit_log` (append-only) | Throttled and capped per agent. |
| The agent's MCP key in the hook | RESTRICTED | process memory only | Read from `/proc/1/environ`, sent as a header to the configured backend over the internal network, never logged, never in argv. |
| Marker | PUBLIC | container writable layer | Holds no data. |

## STRIDE — the hook + the check

- **Spoofing.** An agent can only ask about itself: the identity is the key, and the run is the claude process's launch environment. Forging the run needs a nested `claude` started by the agent, a stated limit.
- **Tampering.** The registration, scripts and marker are root-owned. Changing them needs `sudo`, a stated limit. The settings-env and loader side doors are closed by exec form, `env -i`, `-I -S` and the policy pins.
- **Repudiation.** Each refusal produces an audit row (capped) and a log line with its reason. Each self-approval produces a durable record plus the #751 audit row.
- **Information disclosure.** Answers are uniform for runs that are not the caller's; no approver or role is named; `/health` is enum only.
- **Denial of service.** Out of scope (the exclusion list), apart from fleet impact. An agent with no gates is never refused, even when the backend is down: the marker is absent.
- **Elevation of privilege.** No grant is added. The check is a use, and the root exec is constant and map-derived.

## Trend

The previous audit of this feature family, `cso-diff-2026-10-02-ent751-gated-skills`, had 2 findings, both fixed in that PR. Its open note — prose-only requests, and invocations already in a resumed session's context, are "the in-container hook's lane" — is now **closed for Claude Code**: a `Skill` call loading a gated skill in an uncleared run is refused. It remains open for Codex and Gemini (stated limit, D4). This audit adds no fingerprints.

## Remediation

Nothing to remediate. Remaining before merge: the image-level proof listed under coverage gaps.
