# CSO diff audit — abilityai/trinity#2788 / trinity-enterprise#614 (`feature/614-source-agent-attribution`)

`cso v1.1` · mode `--diff` · confidence gate 8/10 · 2026-09-14

## Verdict

**Clean at the gate.** The diff is itself a trust-boundary repair: `X-Source-Agent` is honoured only for a principal that can prove it names itself (agent-scoped key, or the backend-minted event loopback), every OSS reader is routed through one helper, and an AST guard pins the reader set. It additionally fences the EVT-001 loopback JWT, which was an unrestricted five-minute admin bearer, to `POST /api/agents/{name}/task`. No secrets, dependency, workflow, container, or webhook changes. One latent audit-integrity hazard is recorded in the appendix; it is not reachable on the shipped path.

## Attack surface touched by the diff

| Category | Detail |
|---|---|
| New endpoints | 0 |
| Changed endpoints | 8 (chat, task, fan-out, loops, reminders, schedule trigger, both emit-event routes) — all tighten |
| New auth primitives | `resolve_source_agent`; `event_loopback` JWT scope + route fence; `User.vouched_source_agent` |
| MCP server | `tools/schedules.ts` sends the header only for `scope=agent` (matches `chat.ts`) |
| Dependencies / CI / Docker / compose | 0 |
| New test files | 1 (`test_ent614_source_agent_attribution.py`, 92 tests incl. AST guard) |

## Findings

None at the 8/10 gate.

## Appendix

**A1 · LOW · latent (verified by fresh-context subagent, PARTIAL).** The agent-branch audit sites in `dispatch_admission_service.py` and `routers/fan_out.py` now pass `actor_email=current_user.email` unconditionally, on the premise that it is "the key owner". For the event-loopback principal (`sub='admin'`) that premise is false: a loopback dispatch reaching one of those sites would write an `actor_type='agent'` row joined to the admin human. **Not reachable today**: the loopback is fenced to `/task`, `_audit_chat_started` is `/chat`-only, and the `/task` replay row requires an `Idempotency-Key` the loopback never sends. It becomes live the day `trigger_subscription` gains an idempotency key (Invariant #18 pushes it that way). Fix: `actor_email=None` (or the vouched agent's owner) when `vouched_source_agent` supplied the identity, plus one loopback audit-row test. Follow-up, not a merge blocker.

## Verification performed

- **P2 secrets**: 0 secret-shaped strings in non-test added lines; no tracked `.env`, workflow, requirements, lockfile, Dockerfile or compose changes.
- **P6 loopback**: scope claim is `SECRET_KEY`-signed (backend-only, unlike `INTERNAL_API_SECRET`); fence runs in `get_current_user` before revocation and user lookup, fail-closed. Vouching is derived only from agent-originated events (`emit_event` on an agent key; `emit_event_for_agent` only when the emitter is the path agent; #1578 system terminals).
- **P9 A01**: allowlist over identity; falsy check before `getattr`; unprivileged default; identical 403 for existing and non-existent targets (no enumeration). **A09**: refusals logged with endpoint, truncated header, principal id and scope.
- **Coverage**: six OSS readers all routed (enterprise submodule has none); four MCP client senders gated on `scope=agent`; frontend never sends the header.
- **Tests**: 404 passed locally at PR head across the new file, five adapted files, and the 1310/293/186 auth guards.
