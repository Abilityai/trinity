# Unit-test fixtures

## `issue_1870_captured_tail.jsonl`

A **real, captured** Claude Code session transcript tail (CLI `2.1.220`), redacted
for this public repo. It is the reproduction fixture for
[#1870](https://github.com/abilityai/trinity/issues/1870) — *completed turn
discarded on `error_during_execution`*.

**Why captured and not typed.** The #1870 plan shipped one wrong premise that had
been typed from the issue's rendered summary table. Measured over 1,075 real
transcripts:

| Record | Measured content type |
|---|---|
| `<task-notification>` | **`str`** (999 str / 41 list) |
| `[Request interrupted by user…]` | **`list`** (261 list / 2 str) — *not* a string |

Fixtures for this bug are therefore built from a captured file, never from prose.

**What the tail contains** (record indices as committed):

| idx | Record | Why it matters |
|---|---|---|
| 0 | `user`, **string** content | the turn's opening prompt — the backward-walk boundary |
| 1–5 | `attachment`, `file-history-snapshot`, `last-prompt`, `ai-title`, `mode` | non-message records the scanner must skip |
| 6–8 | `assistant` group, `stop_reason=tool_use` | an earlier message; its text block carries `INTERMEDIATE-NARRATION-REDACTED` — the narration a `(boundary, marker]` *window* rule would wrongly fold into the answer |
| 9 | `user`, **list** content (`tool_result`) | must not be mistaken for a user-input boundary |
| 13 | `assistant`, `end_turn`, **`thinking`** block | **the E1 shape** — a thinking-enabled final message is *two* records sharing one `message.id`, and **both** carry `stop_reason: end_turn`. 40.6% of real markers are thinking-only |
| 14 | `assistant`, `end_turn`, **`text`** block, same `message.id` | **the answer** |
| 15–18 | `system`, `queue-operation` | more records the scanner must skip |
| 19 | `user`, **string** `<task-notification>` | **the #1870 trap** — a string-content user record *after* the completed answer, so the pre-fix backward walk anchors here and the forward scan returns nothing |
| 20 | `user`, **list** `[Request interrupted by user…]` | the trailing interrupt; measured shape is a list |

**Redaction policy.** Record *structure* is preserved exactly as Claude Code wrote
it — every key, every nesting level, every content-block shape, and the real record
interleaving. Only free-text *values* that could carry third-party data are
replaced: `tool_use.input`, `toolUseResult`, `tool_result` bodies, thinking
signatures, attachment payloads, and the `<task-notification>` result table are all
substituted; UUIDs and `msg_`/`toolu_`/`req_` ids are deterministically remapped;
paths are rewritten to `/home/developer`. The single assistant text block at idx 14
is kept verbatim — it is PII-free and it is the artifact under test.

Note that the kept answer is *itself* a checkpoint ("…when it reports back I'll
ledger each created draft…"), not a finished deliverable. That is not incidental:
it is precisely the partial-recovery risk the `#1870` recovery notice
(`_RECOVERY_NOTICE`) exists to flag to an operator.
