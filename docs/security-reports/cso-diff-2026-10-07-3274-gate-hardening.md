# /cso --diff — trinity#3274 gated-skill hardening (2026-10-07)

**Mode**: diff (branch `feature/3274-gate-hardening` vs `a51549772`), daily confidence gate (8/10). cso v1.2.3.
**Scope**: the skill gate's inputs, the approved-run replay, the requester's notice and pending answer, the MCP gate result, the six UI run senders, and the public status route. No new endpoint, dependency, workflow, Dockerfile, compose or migration.

## Verdict

**0 findings** at the 8/10 gate. One defect was found during this audit and fixed on the branch before the report (below). Coverage gaps: none for the diff's scope.

## Architecture delta

- `skill_gate_service.enforce` gains `context_text`. It is scanned apart from the request, and its sources are the schedule name, MCP key name and requester email the executor's prompt renders, each raw and rendered. It goes on the card only when it alone matched, and is never replayed. Callers: the `execute_task` backstop, `/task`, `/chat`.
- The approved run's message and system prompt are frozen apart (`frozen_replay`) at `/task` and fan-out. They are stored sanitised in the record's `dispatch` JSON, which no route exposes.
- `outcome_delivery` (one rule, shared with `_notify`) rides the 202. The requester's notice names the decider by display name (platform-principal requests only) or "the agent's approver", never by email.
- `GET /api/public/executions/{token}/{id}/status` returns `error` for a `skipped` row whose `triggered_by` is `public`.
- MCP: `inlineConnectorChat` reads the gate result before its own 403 / non-2xx handling. `parseGateResult` promises delivery only for `agent_task` / `inbox`.

## Stack CVE quicklist

No pin in the quicklist changed in this diff (Starlette, Vite, Vitest, axios, Redis, Claude Code, runc, Node CLIs). Every row is outside the diff: **not reachable by this change**.

## Attack-surface delta

| Surface | Change | Assessment |
|---|---|---|
| Public status route (link-token auth) | `error` for `skipped` public rows | Same access check as before (link token + row on the link's agent). It exposes the gate notice (skill name, agent, request id), never a person or role. Workspace rows also carry `triggered_by = public`; reading one needs its unguessable execution id, and those rows' success responses were already readable this way. Below the gate, no new class. |
| Agent-context ingress | none new | The gate reads MORE (quote line, context). The Telegram quote is pre-neutralised by `reply_quote_line` (brackets and quotes rewritten) and already reached the executor in the composed prompt. |
| Approver card | context lines when they alone matched | Sanitised, one line per value, 200-char clamp, fixed labels; counted toward the 6,000-byte bound. Same forging class as the existing one-line schedule label. |
| Requester notice | decider label | Removes the email disclosure (item 8). The label never contains `@`, and a lookup failure falls to the generic label (fail toward hiding, stated in the docstring). |
| MCP inline tier | gate result first | An access 403 carries no `X-Trinity-Error-Code` with a matching `refused` body, so it still reads "no access" (pinned in `chat-gate.test.ts`). |

## Phase notes (diff scope)

- **P2 secrets:** the added lines contain no credential shapes. One test fixture originally used a vendor-prefixed fake key. It was replaced before this report with a `Bearer` value the sanitiser redacts, so the public repo carries no key-shaped string. Emails are `example.com`.
- **P3/P4/P5/P6:** no dependency, lockfile, workflow, Dockerfile, compose, webhook or internal-route change.
- **P7 (ASI01/03/09):** the gate only widens what it holds, in the fail-closed direction. Self-approval is unchanged: the `is_person` rule and the event-loopback flag, now pinned by `test_3274_gate_wiring.py`. The notice change narrows disclosure.
- **P8:** no skill, template, plugin or hook content changed. Invisible-Unicode scan over every changed and new file: the only hit is a pre-existing ⚠️ (U+FE0F) in a `fan_out_service.py` docstring, which agents do not load. The matcher test fixtures spell every invisible as an escape.
- **P9 A10 (failure direction):** `outcome_delivery` defaults to `none` (an answer that does not know never promises). An older backend's missing field reads as `none` in the MCP layer. `_held_notice` returns nothing for an absent `triggered_by`. `_decider_label` fails to the generic label. Each is stated in code.

## Found and fixed during this audit

- **Matcher: an invisible before the slash plus one inside the name evaded both normalisations** (fixed on the branch; would have been HIGH-adjacent bypass of the dispatch-time check on Codex/Gemini). `run­/pay‍-invoice`: removing both joins `run` to the slash (a path); spacing both splits the name. Fix: on the removed form, a run of invisibles right before a slash reads as a space (`utils/skill_invocation.py::_INVISIBLE_BEFORE_SLASH`). Row in `test_ent751_skill_invocation.py`; mutation-proved (removing the rule turns it red). Learnings fragment `2026-10-07-a-matcher-must-read-every-form-the-reader-sees.md`.

## Hypotheses

None at this gate.

## Coverage gaps

None for the diff's scope (no Docker-image, dependency or CI surface changed).

## Trend

Follows `cso-diff-2026-10-02-ent751-gated-skills` (both findings fixed in #3208) and `cso-diff-2026-10-05-ent752-gated-skill-hook`. This diff closes the seven gaps the #3208 merge-train validation listed. No persistent finding is reopened.
