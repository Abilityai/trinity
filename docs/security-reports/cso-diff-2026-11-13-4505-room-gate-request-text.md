# /cso --diff — trinity#4505 room gate request text (2026-11-13)

**Mode:** diff, daily (8/10 gate). **Scope:** `shared_sessions/service.py`, `services/skill_gate_service.py`, two unit tests, `feature-flows/skill-gate.md`. **Run:** PARTIAL (Docker down; no image-internal checks needed for this diff).

## Change under audit
- Room wake passes the skill gate a card text (`gate_replay`): the shown posts that each invoke a gated skill and the requester's latest post, each line prefixed with its sender, plus the ask answer. The scan text (`request_text`) is unchanged: the whole shown slice plus the ask answer.
- In a room (`triggered_by == "room"`), `enforce` drops a replay that misses any skill the request scan matched and cards the scanned text. `/task` and fan-out replays are untouched.
- Too-long refusals in a room name other authors of the gated posts.
- Room requesters display as "A room conversation" and each card line names its sender; `kind` stays `KIND_CHANNEL`.

## Quicklist
No dependency, Dockerfile, compose or workflow change in the diff: every row `not reachable (no diff overlap)`.

## Attack surface
New endpoints 0. New inputs 0. New MCP tools 0. New DB columns 0. Agent-context ingress unchanged (the room transcript the agent sees is untouched).

## Checks
| Area | What was checked | Result |
|---|---|---|
| Secrets (P2) | added lines grepped for key prefixes, emails, passwords | none |
| ASI03 identity | requester `kind`, `key`, `is_person`, `email` unchanged; label only feeds `Requester.display()` | no self-approval or cap change |
| ASI09 approver trust | card text == `REPLAY_MESSAGE` == what `_dispatch_approved` sends (test pins it) | approver reads exactly what runs |
| Gate bypass via narrowing | scan still reads full slice; per-post matcher cannot match across the joining newline; replay missing a matched name falls back to scanned text (test pins it) | no bypass found |
| A10 failure direction | `_room_gate_card` unreadable/empty map → no replay → full-text card (prior behaviour, stated in docstring); backstop's own read still refuses `gate_unavailable` | fails toward over-gating |
| Disclosure | too-long notice names sender identities (usernames / agent names) already shown as sender labels to the same room | no new disclosure |
| `/task`, fan-out | fallback is room-only; a `/task` whose gated name is only in `user_message` keeps message and system prompt apart (test pins it) | unchanged |
| A05 injection | no SQL, subprocess, YAML, `v-html`, `request.url.path` in diff | none |

## Findings
None at the 8/10 gate.

## Hypotheses
None (daily mode).

## Coverage gaps
- Docker pass not run (Docker down). Not needed for this diff: no image, dependency or runtime-config change.
- Pre-existing: approved room runs post no reply to the room (#4549). Functional gap, not a security finding.
