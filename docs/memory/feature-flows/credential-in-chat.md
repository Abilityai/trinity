# Feature Flow: A Credential Handed in the Chat (trinity-enterprise#1066)

> **Status**: OSS core. No schema change — the kind rides the existing
> `operator_queue.type` TEXT column, the name rides `context.credential_name`
> and, since trinity-enterprise#1109, a destination rides
> `context.credential_destination`.
> Requirements: [credentials.md §3.10](../requirements/credentials.md) and
> [§3.11](../requirements/credentials.md) (the destination),
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
        └─ ask_service.raise_ask → _validated_ask (name policy, no options, to: primary only) → exactly one person (else 422 no_person)
file channel ── _clamp_ingested_item: a credential entry not addressed to the agent's primary person is ingested unaddressed

person's Workspace thread ── PortalAsks → PortalCredentialField (masked)
        Send ─▶ store.provideCredential(id, value)
             POST /api/enterprise/client-portal/asks/{id}/credential  {value}
             └─ asks/service.provide_credential  (_owned_ask, kind, pending, expiry, divergence)
                └─ services/credential_sink.set_env_value
                     name re-check · value check · stage_secret (fail closed) · agent running
                     · per-agent lease · read .env (only a file the agent reports `missing` is empty)
                     · set one line · inject · audit chat_provide
                     (a refusal before the inject unstages what this call staged)
                └─ ask_service.answer(response="provided", via_credential_sink=True)
                     └─ wake: "NAME is now set in your environment; never repeat its value"
thread row: "NAME provided"
```

### With a destination (trinity-enterprise#1109)

```
agent ──ask_for_credential(name, why, destination={instance_url, agent_name, key_env})──▶ MCP
        context.credential_destination ── _validated_ask → credential_destination.validate
             (URL: validate_a2a_endpoint_url — HTTPS + public, or an admin-trusted network; no path;
              agent: a slug; key_env: a variable NAME the policy allows) → stored normalised
        receipt + Workspace projection: credential_destination {agent_name, host} — never key_env

person's field: "Sent to your agent lopez-digest on your Trinity at member.example.com …"
        Send ─▶ provide_credential  (destination read from the STORED row, never the request)
             └─ credential_sink.set_env_value(..., destination)
                  stage_secret (fail closed)
                  · _deliver_remote: the ASKING agent running → its .env read → the key by NAME
                    (destination_key_missing) · URL validated again, connection pinned
                    (a2a_client transport, Host + SNI = the registered host)
                  · POST {instance}/api/agents/{agent}/credentials/inject
                    {mode: "merge", env: {NAME: value}}  Authorization: Bearer <key>
                  · 2xx → unstage (nothing rests here) → audit credential_delivered {host, agent_name}
                  · refusals: destination_unreachable (503; connect → unstaged, timeout → stays staged,
                    may_have_written), destination_key_refused (401/403), destination_agent_missing (404),
                    destination_refused (other) — all 409, ask pending, value in no log/audit/message
             └─ ask_service.answer(response="provided", via_credential_sink=True)
                  └─ wake: "NAME was delivered to your agent X on your Trinity at host; it is not in
                     your environment"
receiving instance: POST /api/agents/{name}/credentials/inject mode "merge"
        require_person (a user-scoped key is a person) → credential_sink.merge_env_values
        → the same read-patch-inject leaf, every other byte kept → audit inject {mode: merge, names}
```

## Pieces

| Layer | File | Role |
|---|---|---|
| MCP | `src/mcp-server/src/tools/operator_queue.ts` → `ask_for_credential` | Self-acting; builds the ask from declared fields only; derives `request_id` from name + turn when left out |
| Backend | `services/ask_service.py` | `credential` in `ASK_TYPES`; options refused; `to` other than `primary` refused (`invalid_to`); `no_person`; `CredentialAnswerRefused` unless `via_credential_sink` |
| Backend | `services/operator_queue_service::_clamp_ingested_item` | File channel: a credential entry keeps its addressee only when that is the agent's single primary person |
| Backend | `services/credential_ask_policy.py` | Name charset, ≤64, reserved names (agent-side mirror, parity-tested) |
| Backend | `client_portal/asks/router.py` → `POST /{id}/credential` | Person-only door, rate-limited, uniform 404 |
| Backend | `client_portal/asks/service.py::provide_credential` | Ownership and state checks, sink, then the ask sink |
| Backend | `services/credential_sink.py` | The one leaf that handles the value; `_deliver_remote` (destination branch) and `merge_env_values` (the inject route's `mode: "merge"`) since ent#1109 |
| Backend | `services/credential_destination.py` | ent#1109: the destination's shape, URL gate (`validate_a2a_endpoint_url`), `destination_of` at answer time, `public_view` / `plain_words` (agent + host, never `key_env`) |
| Backend | `routers/credentials.py` → `POST /agents/{name}/credentials/inject` | `mode: "merge"` + `env` (ent#1109): one line set in place through the sink's leaf; `mode: "replace"` (default) is the whole-file write, unchanged |
| Backend | `services/system_service.py` → `parse_manifest` | ent#1109: `credentials: [NAME, ...]` read as variable names (validated, previewed); the older map form stays an unknown key, warned and dropped |
| Agent | `agent_server/routers/credentials.py` → `GET /api/credentials/read` | Lists a path in `missing` only when the agent proved it absent; a path in none of `files` / `files_b64` / `missing` was not read |
| Backend | `services/operator_resume_service.py` | Wake names the variable, never a value |
| Backend | `client_portal/router.py::_refuse_secret_in_message` | Paste guard on both Workspace chat routes (422 `secret_in_message`) |
| Backend | `utils/credential_sanitizer.py::secret_shape` | Detector over `SECRET_VALUE_PATTERNS` (+ `ops_eyJ…`, `dop_v1_`) |
| Frontend | `components/portal/PortalCredentialField.vue` | Masked field, Show/Hide, one POST, emptied before the request |
| Frontend | `components/portal/PortalAsks.vue` | Renders the field for the kind; always names the asking agent and quotes its reason as its words; no Discuss |
| Frontend | `utils/operatorQueue.js` | `queueResponseKind` → `credential`; label; "You provided NAME" |
| Frontend | `components/portal/portalChatAsks.js` | History row "Credential · NAME provided" |
| Frontend | `components/operator/QueueCard.vue`, `QueueItemDetail.vue`, `views/MobileAdmin.vue` | No respond box for the kind |
| Frontend | `components/portal/PortalConversation.vue` | On `secret_in_message`: drop the bubble, no Retry, no draft, show the pointer |

## Where the value goes, and where it does not

- **Goes**: the request body of one POST; the runtime identity scrub's staged
  set (AES-GCM envelope in Redis, 24 h); one line of the agent's `.env`. A
  refusal before the inject removes the value this call staged, so "nothing was
  stored" holds for the scrub too; a failed inject keeps it staged (the agent
  may have written) and says the value may or may not be stored.
- **Never**: the component's state after Send, a URL, browser storage or the
  drafts, a log line, the audit row, a refusal message, the ask row, its answer,
  the write-back or the agent's wake prompt.

## Refusals

| Code | Status | Where | Meaning |
|---|---|---|---|
| `invalid_to` | 422 | raise | A credential ask is addressed to `primary` only |
| `no_person` | 422 | raise | No single person to ask |
| `invalid_credential_name` | 422 | raise / sink | Malformed or reserved name (`reason`) |
| `invalid_options` | 422 | raise | A credential ask offers no options |
| `credential_answer_via_field` | 422 | operator respond / Workspace answer | Only the field answers the kind |
| `invalid_value` | 422 | sink | Empty, line break, NUL, too short for the scrub, over 32 KiB |
| `scrub_unavailable` | 503 | sink | The scrub could not be armed; nothing stored |
| `agent_not_running` | 409 | sink | Start the agent, then send again |
| `env_busy` | 409 | sink | Another sink write holds the agent's lease |
| `env_unreadable` | 409 | sink | The agent did not hand `.env` back as text (`reason`: `not_utf8`, or `unconfirmed` — a read error or an image too old to report `missing`); the file is left as it is |
| `env_too_large` | 422 | sink | `.env` would exceed 256 KiB |
| `agent_unreachable` | 503 | sink | The agent's read (nothing stored) or inject (may or may not be stored) failed |
| `already_resolved` | 409 / 400 | door | Ended first; `credential_written: true` when the value landed |
| `secret_in_message` | 422 | Workspace chat | The message looked like a secret; nothing stored |
| `destination_invalid` / `destination_url_invalid` / `destination_agent_invalid` / `destination_key_env_invalid` | 422 | raise (ent#1109) | The destination's shape, URL (`reason`: `endpoint_not_https`, `endpoint_private_address`, `endpoint_dns_failure`, `endpoint_invalid`, `path`, …), agent name or key variable name |
| `destination_on_non_credential` | 422 | raise | Only a credential ask carries a destination |
| `destination_key_missing` | 409 | sink | The asking agent holds no key under `key_env`; nothing stored |
| `destination_url_invalid` | 422 | sink | The URL no longer passes the gate at delivery time; nothing stored |
| `destination_unreachable` | 503 | sink | Connect failure (nothing stored, unstaged) or a timeout after the request left (`may_have_written: true`, stays staged) |
| `destination_key_refused` | 409 | sink | The destination answered 401/403 to the key; nothing stored |
| `destination_agent_missing` | 409 | sink | The destination answered 404 — no such agent, or one the key cannot reach |
| `destination_refused` | 409 | sink | Any other non-2xx (`status`); nothing stored here |
| `merge_takes_env` | 400 | inject route | `mode: "merge"` with `files`, or without `env` |

## Residuals

- The paste guard is not wired into channel adapters (Slack, Telegram, voice).
- The value is stored in the agent's `.env` only; a vault target is a second
  sink target, not a change to the ask kind or the field. A destination
  (ent#1109) is such a second target: another Trinity's agent, over the key
  the asking agent holds for it.
- The platform schedules no retry of a failed delivery: the person sends the
  value again. The asking agent's own wording (where the secret lives, who
  reads it, how to rotate it) and the "ask for each missing manifest
  `credentials:` name before apply" prompting are the agent's skill.
- The owner's Credentials-tab inject and the other non-sink `.env` writers are
  whole-file writes that do not take the sink's lease, so they stay
  last-writer-wins against it.
- A credential ask raised from a schedule or another agent's turn reaches the
  agent's owner; any variable name the policy allows can be asked for, and the
  card (who asks, in its own words) is the person's defence.
- The live stream of a turn is not scrubbed (ent#279), so a model that echoes the
  value shows it once on screen, never in history.

## Testing

- `tests/unit/test_ent1066_credential_ask_kind.py` — the kind, the name policy and parity, primary-only on the REST door and the file channel, `no_person`, the answer refusal on both doors, the wake.
- `tests/unit/test_ent1066_credential_sink.py` — round-trip through the agent's parser, the agent image's real read/inject over a tmp home (non-UTF-8 and unreadable `.env` keep every byte), fail-closed staging and unstaging on refusal, stopped agent, lease, CAS after write, value absent from logs, audit and refusals.
- `tests/unit/test_ent1066_credential_route_gates.py` — agent, system and connector keys refused; Workspace-only person admitted; stranger 404.
- `tests/unit/test_ent1109_credential_handover.py` — the destination: validated and stored at raise, said in plain words on the receipt and the projection, the key read by name and never shown, the pinned merge inject on the destination, nothing written here and nothing staged after, every refusal named and unstaged when nothing could have been written, the wake, `mode: "merge"` on the inject route, manifest `credentials:` names (and the legacy map form kept as an unknown key).
- `tests/unit/test_ent1066_secret_paste_guard.py` — every shape, both routes, nothing persisted, no echo; `ops_` snake_case identifiers neither refused nor redacted.
- `src/mcp-server/src/operator_queue.test.ts` — the tool's shape, only declared fields travel, the description.
- `src/frontend/tests/unit/portalCredentialField.mount.spec.js`, `credentialAskSurfaces.mount.spec.js`, `portalSecretInMessage.mount.spec.js` — mounted.
