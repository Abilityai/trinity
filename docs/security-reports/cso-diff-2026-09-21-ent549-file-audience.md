# CSO Diff Audit — 2026-09-21 — abilityai/trinity-enterprise#549 (a shared file is for the person the turn was for)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/549-file-audience` → `dev` · **Skill**: cso v1.1 · **Audited**: the uncommitted working tree against merge-base `0c470c76c`, plus an independent fresh-context adversarial review of the same diff.

## Architecture (Phase 0)
The change closes a disclosure: `agent_shared_files` was scoped by agent alone, so the Workspace Files tab listed every active share of an agent — download token included — to everyone on its roster. A share now has an addressee (three nullable columns, both migration tracks). `services/turn_audience.py` decides who a turn was for from platform-written columns on the execution row; `create_share` resolves it (or takes a roster-checked `audience_email`); `client_portal.portal_documents` narrows in the query; the owner's list route withholds the addressee from key-authenticated callers; WhatsApp media and voice notes are addressed by the platform code that already holds the recipient.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 · WebSocket: 0 · Docker/compose: unchanged · Dependencies: unchanged · CI: unchanged · Migrations: 1 (3 nullable TEXT columns; SQLite + Alembic)
- New agent-controlled input: 1 — `audience_email` on `share_file` (shape-validated, then checked against the agent's OWN roster with the reader's predicate; named 400 / 503; nothing stored on refusal)
- Changed response surface: the owner's list route gains `addressed_to`, `addressed_to_channel` (both withheld unless `is_interactive_principal`) and `audience_source` (a closed enum); `share_file` returns `visible_to_requester` / `visibility_note` (static text) and echoes `addressed_to` only when the agent supplied it
- New platform-written field: `source_channel_client` is now also stamped by the channel router, only for a verified speaker in a one-to-one chat; its one existing reader (the portal leg of completion reports) ignores non-portal rows
- Docs changed: architecture area files, requirements, four feature flows, one user doc, the learnings ledger — enterprise-docs guard replayed (PCRE via Python `re`): 0 hits

## Findings (Phases 2–12)
**None open.** Three defects were found in the diff as first written and fixed before commit; each now has a test that goes red with the fix reverted.

1. **MEDIUM — group turn addressed to the unlocker (found by this audit).** On a channel turn `source_user_email` is `verified_email OR the channel-native id`, and in a group the verified email is the unlocker's, set once per group. The resolver trusted it, so a file another group member asked for would be listed in the unlocker's Files tab. Fixed: the router states who a turn is for only when it is a fact; the resolver reads that column and never `source_user_email`.
2. **MEDIUM — same-agent delegated child treated as a person's turn (independent review).** The agent-to-agent tell compared `source_channel_agent` to the executing agent; for a self-task and A→B→A they are equal, and the inherited client was chosen through an agent-typed parent id. Fixed: the column is written by inheritance and nothing else, so presence is the test.
3. **LOW — a zombie `running` row believed in a room (independent review).** Fixed by one rule for live and finished citations alike: a cited id proves the conversation, and the running direct turns of it must agree on the person.

Two test gaps on security-relevant guards were closed in the same pass (mutations that survived the first suite): a NON-owner platform session reading owner-only rows, and the viewer listing ignoring revocation / expiry / the calling-agent binding. Final battery: 33 call-site mutations, all red, run in isolated scratch copies.

Key clearances: resolution binds every identifier to the AUTHENTICATED agent (a foreign execution id validates to nothing); the arm is chosen from the principal, never a header or body field (`actor_is_agent` derives from the agent-scoped key); `trusted_execution_id` is reachable from no request; SQL is SQLAlchemy Core binds, and the two migrations interpolate a constant tuple of column names; an unidentifiable viewer matches nothing (`column == None` compiles to `IS NULL` — guarded and tested); no address is logged (exception type names and booleans only) and none is stored in `idempotency_keys` (hashed key, address-free snapshot); the new prompt and tool-description text is static; vendored-copy parity, agent-auth header, enumeration-uniformity, admin-gate and model-centralisation guards all green.

## Accepted and stated (not findings)
- **The `?sig=` link remains a bearer credential** (AC8): the audience governs the listing, not the download.
- **Same reach as `report`**: a prompt-injected agent can address a file to any person on its own roster, and the refusal tells it whether an address is on that roster.
- **Residual**: a session that carries another conversation's history (a terminal `--resume`, an operator chat's `--continue`) can cite a real id of it; if that client is mid-turn and the model prefers the stale id, the file is addressed to the client. Closed by a platform-injected execution id (#2392), which the resolver already has a slot for.

## STRIDE (file sharing, diff-scoped)
- **Spoofing**: "an agent in a turn" is decided from the authenticated agent-scoped key. **Tampering**: the addressee is platform-decided or roster-validated; the channel address is platform-built. **Repudiation**: `audience_source` + `created_by` on the row, and a structured log naming the rule that fired and the execution. **Information disclosure**: the purpose of the change; every "could not tell" falls to the owner only. **DoS**: one indexed read per share (excluded by rule). **EoP**: none — the override grants nothing the roster does not.

## Data classification (diff-scoped)
- `addressed_to_email` — CONFIDENTIAL (PII) — at rest ≤ 7 days on the share row, deleted with it; withheld from key-authenticated callers; never logged
- `addressed_to_channel` (may be a phone number) — CONFIDENTIAL (PII) — same handling; already at rest on execution rows and channel link tables
- `audience_source` — INTERNAL — closed enum

## Trend
Prior: `cso-diff-2026-09-18-2889-503-skip-laundering` (0 findings). This diff: **0 open**, 3 found-and-fixed during the audit. Direction: stable.

> Not a substitute for a professional security audit — an AI-assisted first pass between professional audits.
