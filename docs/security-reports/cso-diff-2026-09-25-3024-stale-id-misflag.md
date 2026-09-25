# CSO Diff Audit — 2026-09-25 — abilityai/trinity#3024 (stale_id flags the entry awaiting its flip)

**Mode**: diff (daily, 8/10 confidence gate) · **Branch**: `feature/3024-stale-id-misflag` → `dev` · **Skill**: cso v1.1
**Diff**: 2 backend files (`db/operator_queue.py`, `services/operator_queue_service.py`), 1 test file, the test registry, 1 feature flow

## Attack Surface Delta

| Category | Count |
|---|---|
| New endpoints | 0 |
| New WebSocket channels | 0 |
| New MCP tools | 0 |
| New public surfaces | 0 |
| New background jobs | 0 |
| Migrations | 0 |

The change narrows when the leader-locked poller records `stale_id`, and adds one heal to `confirmed`. The terminal sync index gains two selected columns, `delivery_state` and `delivery_detail`, read only by the poller.

## Findings

**None.**

## Checked

- **Secrets (P2).** The diff was scanned for key prefixes and secret assignments: no hits. No `.env`, workflow, dependency or Docker files changed.
- **Integrity (STRIDE: Tampering).** An agent cannot use the narrower rule to hide a reuse that matters.
  - A pending entry the write-back is about to flip is still never re-admitted: the terminal branch `continue`s before the create path.
  - That same cycle still writes the row's ending into the entry.
  - The flag is an operator-visibility signal, not an authorization or dispatch control. The same holds for reuses of acknowledged rows, delivered flips and missing entries.
- **The heal.** It fires only when the agent's entry already reads the row's own terminal status, which means the file and the row agree. An agent can trigger it only by agreeing with the platform. It audits `reconciled`, the existing accountability transition.
- **One rule, two spellings.** `awaits_terminal_flip` (Python) mirrors the SQL that selects the write-back's rows, `get_terminal_items_for_agent`. A real-DB test pins the two against each other, so neither can drift into flagging or skipping rows unseen.
- **Disclosure.** The index change is internal to the poller. No API response or `/ws` payload changes.

## Posture Delta — neutral

Operator-visible sync state becomes truthful again: an ended ask no longer reads "Re-used id". Access, data and exposure are unchanged.
