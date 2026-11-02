# Feature Flow: A Credential Handed in the Chat (trinity-enterprise#1066)

> **Status**: OSS core. No schema change — the kind rides the existing
> `operator_queue.type` TEXT column and the name rides `context.credential_name`.
> Requirements: [credentials.md §3.10](../requirements/credentials.md),
> [security.md §20.11](../requirements/security.md).

## Problem

A person who works with an agent only in the Workspace has no Credentials tab
and cannot reach the owner-tier inject route. When the agent needed a key, the
chat was the only channel left — and a value typed into the chat is persisted in
the thread, the execution log and the agent's next prompt.

## The flow

```
agent ──ask_for_credential(name, why)──▶ MCP operator_queue.ts
        POST /api/agents/{self}/operator-queue  {type: credential, context.credential_name, to: primary}
        └─ ask_service.raise_ask → _validated_ask (name policy, no options) → exactly one person (else 422 no_person)

person's Workspace thread ── PortalAsks → PortalCredentialField (masked)
        Send ─▶ store.provideCredential(id, value)
             POST /api/enterprise/client-portal/asks/{id}/credential  {value}
             └─ asks/service.provide_credential  (_owned_ask, kind, pending, expiry, divergence)
                └─ services/credential_sink.set_env_value
                     name re-check · value check · stage_secret (fail closed) · agent running
                     · per-agent lease · read .env · set one line · inject · audit chat_provide
                └─ ask_service.answer(response="provided", via_credential_sink=True)
                     └─ wake: "NAME is now set in your environment; never repeat its value"
thread row: "NAME provided"
```

## Pieces

| Layer | File | Role |
|---|---|---|
| MCP | `src/mcp-server/src/tools/operator_queue.ts` → `ask_for_credential` | Self-acting; builds the ask from declared fields only; derives `request_id` from name + turn when left out |
| Backend | `services/ask_service.py` | `credential` in `ASK_TYPES`; options refused; `no_person`; `CredentialAnswerRefused` unless `via_credential_sink` |
| Backend | `services/credential_ask_policy.py` | Name charset, ≤64, reserved names (agent-side mirror, parity-tested) |
| Backend | `client_portal/asks/router.py` → `POST /{id}/credential` | Person-only door, rate-limited, uniform 404 |
| Backend | `client_portal/asks/service.py::provide_credential` | Ownership and state checks, sink, then the ask sink |
| Backend | `services/credential_sink.py` | The one leaf that handles the value |
| Backend | `services/operator_resume_service.py` | Wake names the variable, never a value |
| Backend | `client_portal/router.py::_refuse_secret_in_message` | Paste guard on both Workspace chat routes (422 `secret_in_message`) |
| Backend | `utils/credential_sanitizer.py::secret_shape` | Detector over `SECRET_VALUE_PATTERNS` (+ `ops_`, `dop_v1_`) |
| Frontend | `components/portal/PortalCredentialField.vue` | Masked field, Show/Hide, one POST, emptied before the request |
| Frontend | `components/portal/PortalAsks.vue` | Renders the field for the kind; no Discuss |
| Frontend | `utils/operatorQueue.js` | `queueResponseKind` → `credential`; label; "You provided NAME" |
| Frontend | `components/portal/portalChatAsks.js` | History row "Credential · NAME provided" |
| Frontend | `components/operator/QueueCard.vue`, `QueueItemDetail.vue`, `views/MobileAdmin.vue` | No respond box for the kind |
| Frontend | `components/portal/PortalConversation.vue` | On `secret_in_message`: drop the bubble, no Retry, no draft, show the pointer |

## Where the value goes, and where it does not

- **Goes**: the request body of one POST; the runtime identity scrub's staged
  set (AES-GCM envelope in Redis, 24 h); one line of the agent's `.env`.
- **Never**: the component's state after Send, a URL, browser storage or the
  drafts, a log line, the audit row, a refusal message, the ask row, its answer,
  the write-back or the agent's wake prompt.

## Refusals

| Code | Status | Where | Meaning |
|---|---|---|---|
| `no_person` | 422 | raise | No single person to ask |
| `invalid_credential_name` | 422 | raise / sink | Malformed or reserved name (`reason`) |
| `invalid_options` | 422 | raise | A credential ask offers no options |
| `credential_answer_via_field` | 422 | operator respond / Workspace answer | Only the field answers the kind |
| `invalid_value` | 422 | sink | Empty, line break, NUL, too short for the scrub, over 32 KiB |
| `scrub_unavailable` | 503 | sink | The scrub could not be armed; nothing stored |
| `agent_not_running` | 409 | sink | Start the agent, then send again |
| `env_busy` | 409 | sink | Another sink write holds the agent's lease |
| `env_too_large` | 422 | sink | `.env` would exceed 256 KiB |
| `agent_unreachable` | 503 | sink | The agent's read or inject failed |
| `already_resolved` | 409 / 400 | door | Ended first; `credential_written: true` when the value landed |
| `secret_in_message` | 422 | Workspace chat | The message looked like a secret; nothing stored |

## Residuals

- The paste guard is not wired into channel adapters (Slack, Telegram, voice).
- The value is stored in the agent's `.env` only; a vault target is a second
  sink target, not a change to the ask kind or the field.
- The owner's Credentials-tab inject and the other non-sink `.env` writers are
  whole-file writes that do not take the sink's lease, so they stay
  last-writer-wins against it.
- The live stream of a turn is not scrubbed (ent#279), so a model that echoes the
  value shows it once on screen, never in history.

## Testing

- `tests/unit/test_ent1066_credential_ask_kind.py` — the kind, the name policy and parity, `no_person`, the answer refusal on both doors, the wake.
- `tests/unit/test_ent1066_credential_sink.py` — round-trip through the agent's parser, fail-closed staging, stopped agent, lease, CAS after write, value absent from logs, audit and refusals.
- `tests/unit/test_ent1066_credential_route_gates.py` — agent, system and connector keys refused; Workspace-only person admitted; stranger 404.
- `tests/unit/test_ent1066_secret_paste_guard.py` — every shape, both routes, nothing persisted, no echo.
- `src/mcp-server/src/operator_queue.test.ts` — the tool's shape, only declared fields travel, the description.
- `src/frontend/tests/unit/portalCredentialField.mount.spec.js`, `credentialAskSurfaces.mount.spec.js`, `portalSecretInMessage.mount.spec.js` — mounted.
