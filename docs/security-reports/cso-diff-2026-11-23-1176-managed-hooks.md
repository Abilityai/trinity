# /cso --diff — 2026-11-23 — trinity-enterprise#1176 (managed hooks not in effect)

**Mode:** diff, daily confidence gate (8/10). **Skill:** cso v1.2.3. **Base:** `dev` at `d481847cc`.
**Verdict:** no finding at or above the gate. Two coverage gaps.

## What the change does

- The agent server reads Claude Code's cached server-managed settings to tell when Claude Code is not applying Trinity's file-based managed hooks (`agent_server/services/managed_hooks.py`).
- In that state, a Claude turn whose rule lives in a hook is refused before the CLI starts: a shared person's turn, a visitor's turn with `visitor_shell` off, and a schedule run rated below the owner. On an agent with skill gates, the owner's turns run without the `Skill`, `Agent` and `Task` tools.
- `/health` reports `skipped` on `guardrails_registration` and `skill_gate_hook`.
- The backend names every such agent in one Operations alert (`managed_hooks_check_service.py`, run inside the leased fleet-image cycle).
- A typed `turn_refused` 403 no longer counts as a credential failure for the subscription walk (`execution_classification.classify_switch_failure`).

## Stack CVE quicklist (diff scope)

| Component | Verdict |
|---|---|
| Claude Code in the base image | patched: pin 2.1.322, at or after every listed fix; the pin is unchanged by this diff |
| Starlette, Vite, Vitest, axios, Redis, runc, Node CLIs | not in the diff (no dependency, image or compose change) |

## Attack-surface delta

- **Endpoints:** none added or removed. The route census, enumeration uniformity, admin-gate allowlist and auth-wiring guards pass.
- **`/health` (unauthenticated, agent network):** a new enum value on two existing fields. No key name or value from the cached settings is ever returned or logged; a test pins this.
- **Agent → backend:** the existing turn-tier route, now also asked for Claude turns, and only in the skipped state, with the agent's own key (the `turn_scope` precedent).
- **Backend → agent:** one `/health` read per running agent every 10 min, through `services/agent_auth.agent_httpx_client`. `test_agent_auth_header_guard` passes.
- **Operator queue:** a new alert kind under the newly reserved prefix `managed-hooks-skipped-`, so an agent cannot pre-create or suppress the row.
- **Agent-context ingress:** none new. The refusal texts are platform-authored.

## Checks by phase

- **P2 Secrets:** added lines scanned for known secret shapes. One false positive (`re_` inside `failure_classifier`). No `.env`, no credential in argv or logs. Settings-sink and cmdline-credential guards pass.
- **P3 Supply chain, P4 CI/CD, P5 Docker/compose:** not in the diff.
- **P6 Integrations:** no webhook or channel change. The backend → agent read uses the per-agent token.
- **P7 Agentic:**
  - ASI03: no route, scope or grant change.
  - ASI05: the change exists because the managed hooks can be out of force. Restricted turns now fail closed in that state; the owner's turns run with an alert (product decision).
  - ASI09: the alert and refusal texts are platform-authored; the agent names in the alert are platform identifiers.
- **P8 Agent-shipped content:** none changed. No invisible or bidi Unicode in any changed file.
- **P9 A10 failure directions (each stated in code):**
  - The detector fails closed: an unreadable, non-object or oversized cache, and any internal error, count as skipped.
  - The refusal fails closed: "no answer" is refused with 502 (never 503, which reads as an auth failure).
  - The fleet alert's "could not check" leaves the alert as it is.
  - The classifier reads only the typed body; anything else keeps the old `auth` classification.
  - No `getattr(..., None)` discriminator.

## STRIDE (changed components)

- **Agent server:**
  - Spoofing: the turn's tier comes from the platform, keyed by execution id.
  - Tampering: the detector's input is writable by the agent user. The hooks are documented as not a boundary against that user (passwordless `sudo`), and the gap is a stated limit.
  - Information disclosure: enum only.
  - Elevation of privilege: restricted turns no longer run without their hooks in this state.
- **Backend:**
  - Repudiation: the refusal is logged with the execution id.
  - Denial of service: a bounded `/health` read per agent per 10 min; no new LLM cost.

## Data classification

The cached organization settings are CONFIDENTIAL (an organization's policy). They are read on the agent and never stored, logged or returned. The alert carries agent names only (INTERNAL).

## Hypotheses

None at the daily gate.

## Coverage gaps

1. **Image not rebuilt.** The agent-server change was exercised by unit tests on the host, not inside a rebuilt base image. The CI image build or one `/verify-local` run covers this.
2. **No live organization.** Not run against a real organization with a server-managed policy key on the pinned 2.1.322. Checked on 2.1.322 through a local stub and on a subscription login with no organization settings.

## Trend

No prior finding at the changed files. Related earlier report: `cso-diff-2026-10-02-ent751-gated-skills.md` (the in-container gate's stated limits, which this change acts on).
