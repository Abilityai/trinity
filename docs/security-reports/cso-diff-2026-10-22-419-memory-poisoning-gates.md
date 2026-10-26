# CSO diff audit — 2026-10-22 — trinity-enterprise#419 memory poisoning gates

**Scope:** `feature/419-memory-poisoning-gates` vs `dev` (`f049d6731`), daily mode (8/10 gate), cso v1.2.3.

**Result:** 0 findings at or above the gate. 2 coverage gaps. No hypotheses.

## What changed on the attack surface
- `POST /api/agents/{name}/user-memory`: same principal rules. It adds the one-to-one channel rule (ent#549 stamp) and the write-boundary screen. Both are 422s after the existing 403/404 checks.
- `POST /api/agents/{name}/decisions`: the same one-to-one rule. `decided`, `criterion` and `reversal` pass the screen's marker, instruction, MCP-tool-id and slotted-URL checks.
- MEM-001 memory block: notes and summary sit inside a nonce'd, margined envelope with a framing paragraph and a trailing reminder. Decisions follow with their own framing line, one line per field.
- Summarizer: each chat message is clamped to 4,000 characters and margined under its role label. The output is screened and tripping lines are dropped.
- Telegram group history: delimiter copies are defanged in one pass, not deleted.

## Checks run (diff-scoped)
| Check | Result |
|---|---|
| Who can set `source_channel_client` with a channel trigger | Server-stamped only: message_router, portal, rooms (`room`), delegation (`agent`/`mcp`), skill-gate replay (frozen), scheduler (internal secret). No caller path |
| ReDoS on agent-supplied 8k input (request path) | Worst case 38.6 ms over 17 crafted shapes. Summarizer-scale `margin_lines` is linear (time-budget tests) |
| Secrets / PII / IPs in the diff | None |
| Invisible Unicode in shipped text (prompts, MCP description) | None |
| Failure directions (A10) | Screen fails closed in the router. A failed envelope drops the memory (logged). An exception in the summarizer screen stores nothing. An absent stamp is a refusal. All stated in the code |
| Enterprise-docs guard over added doc lines | Clean |
| Tests | 168 new unit tests; 21/21 mutants killed; 2,161 neighbouring tests and the MCP suite green |

## Coverage gaps
1. **No live end-to-end probe** of `POST /user-memory` or of a rendered prompt on a running stack. The operator asked for no live tests on localhost. A sibling-stack `/verify-local --skip-agent` follows.
2. **No dependency scanner run**: the diff changes no pin, lockfile or image install.

## Trend
The prior full runs (2026-09-29, 2026-09-30) list `write_user_memory` only in the ASI06 ingress map, not as a finding. The next full run should record that ingress as screened + enveloped.

## Stated residual limits (by design, not findings)
- The screen is pattern-based. Paraphrase, other languages and homoglyphs get past it; the envelope is the layer that does not depend on spelling (stance: trinity-enterprise#409).
- The summarizer's writes are screened but not recorded in history, and there is no owner/admin memory view. Both are trinity-enterprise#962.
