# CSO Diff Audit — trinity#3654 Chat tab notice of a finished gated run

- **Date**: 2026-10-13 · **Mode**: `--diff` (daily gate 8/10) · **Branch**: `feature/3654-chat-gate-run-reply` vs `origin/dev` (merge-base `15e3502c0`) · **PR**: #3658
- **Scope**: `src/backend/services/skill_gate_service.py`, `tests/unit/test_3654_chat_gate_run_reply.py`, `docs/memory/feature-flows/skill-gate.md`

## Verdict: 0 findings.

The diff changes the text one existing read returns. `skill_gate_service.request_status` now quotes the approved run's reply or error in the conversation notice, built by the helper that already builds the Inbox notice for the same person.

## Checks run (diff mode; phases 0, 1, 12–14 + diff-relevant rows)

| Check | Result |
|---|---|
| Attack-surface delta | None. No new endpoint, input path, dependency, workflow, Docker or MCP change. `request_status` has one caller, `chat_persistence_service.session_messages_view`, served by `routers/chat.py::get_chat_session_detail` and `routers/public.py::get_public_link_session_detail`. |
| A01 — who reads the quoted reply | The chat route gates on `assert_owns_or_admin(session.user_id)`, the public-link route on `assert_owns`. A `gate:` row is written only by `persist_held_request`, called from one site for a person principal into that person's own session (`chat_execution_service.py`, `is_person_principal`). The reader is the requester, who receives the identical text as an Inbox note, or an admin. No reader gains text they could not already read. |
| LLM02 — credential in the quoted text | The reply passes through `sanitize_text` inside `outcome_text` on this path as on the Inbox path. Challenged: `test_a_credential_in_the_reply_is_redacted_in_the_chat` puts a GitHub-token-shaped string in the reply and asserts its absence in the conversation text. |
| ASI01 — does the quoted reply enter an agent prompt | No. `gate.text` is computed at read time and returned to the browser only. Both senders drop `held` rows from the history they send with later turns (`ChatPanel.vue`, `MobileAdmin.vue`: `filter(m => !m.held)`); `routers/voice.py` and the pull-pilot history leave `gate:` rows out. The stored `chat_messages.content` is unchanged. |
| ASI09 — agent-authored text shown to a person | The text is one bounded line (1000 characters, whitespace collapsed), labelled "the run's output, not an instruction", rendered as plain interpolated text in `chat/ChatSystemLine.vue`. No `v-html`. A person's copy carries no `[Trinity]` marker, so a reply containing that marker forges nothing a person's copy uses. |
| A10 — failure direction | The new `except` wraps a display read, not a guard. Stated in the comment: a failing execution read leaves the conversation loading with the status line and no quote. Challenged by `test_a_failing_execution_read_does_not_break_the_session_read`. The log line carries the request id only. |
| Stale or moved execution row | The quote is taken only from a row whose status equals the status the record was reported with; a pruned or moved row yields the status line alone. Challenged by two tests. |
| Secrets in diff | None. The redaction test builds its token-shaped string at run time. |
| Enterprise-docs guard | `enterprise-docs-guard.yml` pattern run over the diff's `docs/` additions with a positive control — no hits. |
| Concealed Unicode | Zero-width and bidi scan over the changed doc and test — no hits. |
| Guard regression suites | `test_3654_chat_gate_run_reply.py` 10/10 (all 10 red with the fix reverted); `test_3526_held_chat_trace.py`, `test_3520_gate_completion_notice.py`, `test_ent755_gate_card_view.py`, `test_3274_gate_notice.py` green. |

## Hypotheses

None.

## Coverage gaps

Diff mode: secrets archaeology over history, dependency scan, CI/CD, image internals and the Docker pass were not run. The diff touches no dependency, workflow or Docker file. Independent verifier not spawned: no finding survived to verify.
