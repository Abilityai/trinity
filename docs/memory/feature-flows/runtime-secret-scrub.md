# Feature Flow: Runtime Secret-Scrub Seam (ent#279)

> **Status**: OSS seam shipping in the ent#279 public PR (`services/runtime_secret_scrub.py`
> + the execution-terminal persistence chokepoints + a drift-parity guard). A
> generic mechanism — mechanism-only by design, it names no consumer. **No DB
> schema change** (all state is Redis). **Behaviour-neutral** when nothing is
> staged (every scrub call is a no-op), so it is inert on an OSS build that never
> stages a value. The staging producer is an entitlement-gated module in the
> private submodule — the public half is documented here; see
> [requirements §3.7](../requirements/credentials.md) for the public-surface
> summary.

## Problem

Trinity has two credential sanitizers and **neither catches a value it was never
told about**:

- The **agent-side** vendored sanitizer builds its value registry only from
  `os.environ` + `~/.env`, behind name-pattern gates — a value that a producer
  hands to an agent at runtime (never in `.env`) enters neither.
- The **backend** sanitizer (`utils/credential_sanitizer`) is **pattern-only**:
  ~15 prefix regexes (`sk-`, `ghp_`, `xoxb-`, …) plus a `KEY=value` pass, with no
  value registry. A prefix-less secret — a customer DB password, an internal API
  token with no recognizable shape — survives verbatim.

So a producer that delivers an arbitrary secret to an agent as a plaintext tool
result (correct by design — the live result is what the agent needs) has no way
to keep that value from persisting verbatim into a **durable** backend sink: the
transcript (`schedule_executions.response`), the exec-log, the activity preview,
a `#1578` completion-event payload, a channel completion report, or — the sink
the pattern pass most obviously misses — an `idempotency_keys.response_snapshot`
that is **replayed to duplicate callers for 24h**.

## Goal

A generic OSS **identity-scrub** seam: a producer **STAGES** a known secret value
(by identity), and the execution-terminal persistence chokepoints **SCRUB** every
staged value out of the persisted copy **before** it is written. The live tool
result stays whatever the producer delivered; only the persisted copy is
scrubbed. It complements the pattern sanitizer (both run) rather than replacing
it.

## The seam — `src/backend/services/runtime_secret_scrub.py`

Sync API throughout (required: `pull_coordination_service.apply_task_result` is
sync). Redis via `redis_breaker_util`'s cached fail-open client with a **memoized
down-state** (an outage costs one failed connect per 30s window, not one per
call).

### Store (global, enveloped, per-member TTL)

```
HASH  secret_scrub:staged      field = sha256(value)  -> AES-256-GCM envelope
ZSET  secret_scrub:staged_at   member = same field    -> score = stage time
```

- **Global, not per-agent** — a staged value is *always* correct to scrub
  regardless of whose text it appears in, and cross-producer relay (A stages a
  value, hands it to B) defeats any per-agent set. Global keying also dissolves
  the rename / recycled-name / `agent_name=None` edges outright.
- **HASH keyed on `sha256(value)`** gives exact dedup — AES-GCM's random nonce
  means a *set of envelopes* never dedups (→ unbounded growth under a repeated
  fetch cadence), while one field per distinct value collapses repeats to one.
- **Values are enveloped** because Redis AOF persists them; the encryption key is
  the shared `get_credential_encryption_service()` singleton (never a per-call
  rebuild).
- The **ZSET gives per-member 24h expiry** (pruned at stage time — `staged_at <
  now − 24h`), so one busy producer can't keep the whole store alive via a
  whole-key TTL refresh. 24h was chosen to outlive the #1083 lease window + a
  same-day crash-resend (a late-`SUCCESS`-after-`LEASE_EXPIRED` CAS win provably
  outlives the earlier 7500s draft; aligns with the idempotency-key window).

### Stage side — fail **CLOSED**

`stage_secret(agent_name, value)` (`agent_name` is for the log line only — the
store is global):

- Values `len < 8` are **skipped** with a WARNING (never an error) — scrubbing a
  3-char value would shred transcripts; matches the agent-side registry's own ≥8
  floor.
- **Any** failure raises `StagingUnavailable`: Redis down, or a hard cap
  (`_HARD_CAP_FIELDS = 1000` distinct values; soft-warn at 200). A NEW value over
  the cap raises; re-staging an already-present value at the cap is a no-growth
  no-op and is allowed.
- Fail-closed is deliberate: a producer that cannot stage its secret **must
  refuse delivery**, because delivering an unstageable secret is a silent
  security downgrade.

### Scrub side — fail **OPEN**

- `get_staged_values() -> list[str]` — ONE `HGETALL` + one decrypt pass per
  applier invocation (never per-field round-trips). Empty/down → `[]` fast path
  (producers that never staged pay one cheap Redis call per terminal, zero
  decrypts). A per-member decrypt failure (rotated key, corrupt envelope) **skips
  that member** with a WARNING and scrubs the rest. A store error returns `[]` +
  one **throttled** ERROR — the terminal MUST persist, and the pattern pass still
  runs. No NEW secret is delivered during the outage (the stage side is closed),
  so the open scrub side never widens exposure beyond turns already in flight.
- `scrub_text(values, text)` — replaces every rendition of every staged value
  with the sanitizer's `REDACTION_PLACEHOLDER` (`***REDACTED***`, so operators
  and the manual verification grep one marker). **Longest-first** (a staged value
  that is a substring of a longer one must not shred the longer match). **Literal**
  `str.replace`, never regex (no regex-injection surface). **Falsy passthrough** —
  a falsy input is returned unchanged (never `None` → `""`, which would corrupt a
  NULL error column).
- Per value, **three renditions** are scrubbed: raw, once-JSON-escaped
  (`json.dumps(v)[1:-1]` — catches values embedded in already-dumped JSON), and
  base64 (the most common trivial encoding).
- `scrub_obj(values, obj)` — recursively walks dict/list **string leaves**,
  returns a NEW structure (never mutates the input), leaves non-strings alone.

## Chokepoint wiring (the persistence appliers)

**Ordering rule at every site**: scrub the **RAW** envelope/structure fields
FIRST — before the existing `sanitize_*` calls, before `json.dumps`, before any
truncation, before any log line. Post-dumps scrubbing misses any value containing
`"`, `\`, or non-ASCII; a pattern pass running first can redact a *substring* of
a staged value and break the identity match; truncation can cut a value so no
rendition matches. One `get_staged_values()` call per applier invocation, then
scrub all fields.

- `task_execution_service.apply_result` — THE single terminal applier (sync +
  #1083 async callback). Success branch scrubs `response`, `execution_log` (obj
  walk), `tool_calls`, **and `raw_response`'s string fields** (a missed durable
  sink: returned verbatim and persisted into `idempotency_keys.response_snapshot`
  by the `/task` sync paths, replayed to duplicate-key callers for 24h). Failure
  branch scrubs `error` at the top — covering the CAS write, the activity
  preview, the #1578 event payload, and the channel completion report that fan
  out from it.
- `task_execution_service._write_terminal_and_gate` — scrubs `error` before the
  terminal write (covers the CB fast-fail / timeout / backend-budget / unexpected
  Exception call sites). The backend-shutdown `CancelledError` writer bypasses
  this helper but writes a static literal — no foreign text, no scrub needed.
- `chat_execution_service` — the one divergent sync-chat applier:
  `_finalize_chat_success` (covers both the DB writes and the raw idempotency
  snapshot), `_parse_agent_http_error` (scrubbed **before** its ERROR log, else
  the Vector-captured platform log keeps what the DB row scrubbed), and
  `_finalize_budget_exhausted`.
- `pull_coordination_service.apply_task_result` (sync — hence the seam's sync
  API).
- `proactive_message_service.send_message` and `channel_history` — scrubbed at
  the TOP, **before delivery AND persist**.
- `routers/voice.py` `_save_transcript` — transcript entry text scrubbed before
  persist.
- `routers/sessions.py` needs **no** direct wiring: its assistant write persists
  the applier's already-scrubbed outputs (verified, transitively covered).

## Drift guard

`tests/unit/test_ent279_scrub_parity.py` — the chokepoint set is **discovered,
not derived** (this repo has re-learned that class three times: #45, #767, #1804
"the emit set is not the close set"). It anchors the way
`test_1804_terminal_activity_parity.py` anchors on terminal writes: every
function that persists agent-authored text into the named tables
(`schedule_executions`, `chat_messages`, `agent_session_messages`,
`public_chat_messages`, `agent_activities`, `idempotency_keys`, `agent_events`)
must call the scrub seam or appear on an explicit allowlist with a one-line
justification — so a future persistence writer that ships un-wired is a CI
failure, not a silent leak.

## Error Handling

| Case | Behavior |
|------|----------|
| Stage: Redis down / hard cap | raises `StagingUnavailable` (fail-closed; the producer refuses delivery) |
| Stage: value `< 8` chars | skipped + WARNING (never staged, never an error) |
| Scrub: Redis / `HGETALL` error at persist | fail-open → `[]` + one throttled ERROR; pattern pass still runs; terminal persists |
| Scrub: one corrupt staged member | skip that member, scrub the rest, WARN (rewrap / TTL clears it) |

## Residuals (stated honestly)

- In-container `~/.claude/projects/*.jsonl` holds tool results verbatim
  (CLI-written; a pre-existing class for **every** secret an agent handles — not
  a durable backend sink).
- The scrub window is 24h TTL-bounded — a value echoed later, or a `#1083`
  `resend_pending_results` terminal delivered after expiry, persists with the
  pattern pass only.
- Encodings beyond the three scrubbed renditions (raw / JSON-escaped / base64) are
  not chased.
- The live tool result is plaintext **by design** (locked).
- Agent-authored side channels (report payloads, `emit_event` payloads, room
  messages) are documented residuals, not chokepoints.
- Staged plaintext exists transiently in backend process memory during a scrub
  pass (outside the encryption-at-rest threat model; Redis holds only envelopes).

## Testing

- `tests/unit/test_ent279_secret_scrub.py` — seam-direct: stage/read round-trip +
  `sha256` dedup, the <8-char floor skip, per-member 24h expiry prune, hard-cap
  `StagingUnavailable`, fail-**closed** stage / fail-**open** read (Redis down,
  `HGETALL` error, corrupt-member skip, down-memo), and `scrub_text` /
  `scrub_obj` contracts (raw/JSON-escaped/base64 renditions, longest-first,
  literal-not-regex, falsy passthrough, recursive/immutable obj walk). Loaded
  standalone via `importlib` against `fakeredis` + a fake reversible enc service
  (real AES is `test_267_credential_key_rotation.py`'s job).
- The chokepoint-scrub tests (incl. the escaping-evasion case — a secret
  `p@ss"w\orld✓` fetched → appears in exec-log tool output → asserted absent from
  the persisted `execution_log` / `tool_calls` / `response_snapshot`) and the
  `:6390` cross-worker two-client propagation test land **with** the chokepoint
  wiring, alongside the parity guard.

## Related Flows

- [Guided Credential Setup](guided-credential-setup.md) — the per-agent
  credential checklist (ent#127), the sibling credential surface.
- [A2A Outbound Calls](a2a-outbound-call.md) — the open-core-seam pattern (public
  seam + private provider) this feature follows.
- [Idempotency Keys](idempotency-keys.md) — the `response_snapshot` replay sink
  the scrub covers.
