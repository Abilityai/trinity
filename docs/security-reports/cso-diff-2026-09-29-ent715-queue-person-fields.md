# CSO Diff Audit — 2026-09-29 — abilityai/trinity-enterprise#715 (operator-queue reads without a person's identity)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/715-queue-person-emails` → `dev` · **Skill**: cso v1.1 · **Audited**: the branch diff against merge-base `e87163125` (commit `3f13c6b26`).

## Architecture (Phase 0)

The change narrows what a MACHINE principal reads from the operator queue. A machine principal is any key that `dependencies.is_person_principal` refuses: agent, system, ops, connector and portal_delegate keys, a user-scoped key carrying an agent identity, and any future scope.

- `routers/operator_queue.py`: the three read routes (`GET /api/operator-queue`, `/{id}`, `/agents/{name}`) give a machine an ALLOWLIST projection, `_MACHINE_ROW_FIELDS`, in place of a two-key pop list. The five person fields are outside it: `responded_by_id`, `responded_by_email`, `addressed_to_email`, `disposed_by_email` and `resolved_to`.
- `services/operator_queue_service.is_about_a_person`: the platform's heads-ups ABOUT a person are not returned to a machine at all (list skip, item 404). These are the ent#499 problem report (a client's email and verbatim comment in `question`) and the ent#308 inbox collision (client addresses in `question`, an email slug in the id).
- `portal-inbox-collision-` joins `_RESERVED_ID_PREFIXES`. `is_platform_minted` and every sink keyed on it now treat that alert as a platform alarm: the file write-back, the resume wake, the sync exclusions and the Execution Context "Ended asks" line.
- The file write-back no longer writes `responded_by`, on delivery or on reconstruction.
- The clear-resolved `/ws` trigger drops `cleared_by`.

Trust boundary: persisted rows about people → keys held by machines (agents are prompt-injectable, and an admin-owned agent key reaches every agent's rows over REST).

## Attack surface (Phase 1, diff-scoped)

| Surface | Change |
|---|---|
| Endpoints | None new. Three existing reads return LESS to machine principals; the item route answers 404 for a hidden row (the same body as a missing id). |
| MCP tools | None changed. `list_operator_queue` / `get_operator_queue_item` pass the narrower backend response through; the item tool turns the 404 into a plain error. `types.ts` changes a comment only. |
| WebSocket | `operator_queue_cleared` (router copy) loses one key. |
| Agent-facing sinks | The file write-back writes one field fewer, and one more id prefix counts as a platform alarm. |
| Prompt | One sentence of the Operator Communication section (platform-authored, no caller input). |
| Migrations, dependencies, CI, Docker/compose, vendored policy files, skills | Unchanged. |

## Findings (Phases 2–12)

**None.** Every phase ran against the diff:

- **Phase 2, secrets:** `git log -p e87163125..HEAD` added lines carry no known secret prefix, no private IP, and no email outside `@example.com`. The branch REMOVES one real internal address from a public requirements doc (an example payload in `docs/requirements/OPERATOR_QUEUE_OPERATING_ROOM.md`).
- **Phases 3–5, supply chain / CI / infra:** no dependency, lockfile, workflow, Dockerfile, compose or vendored-copy file is touched.
- **Phase 6, integrations:** no webhook or agent-server call path is touched.
- **Phase 7, LLM:** the change removes a prompt-injection path. The ent#499 problem report carried a stranger's verbatim words, which ent#366 deliberately withholds from the rated agent, and that agent's key could read them over the queue. No new model-facing input.
- **Phase 9, A01:** the machine rule is `is_person_principal`, an allowlist over `mcp_scope` that fails closed on a principal without one. Hidden rows answer 404 only after the access check, so they cannot reveal a row the caller could not already reach. A03: no SQL or command construction changed; the new predicate is a `startswith` over a normalised id.
- **Phase 10, STRIDE (operator-queue read surface):** information disclosure reduced; no change to spoofing, tampering, repudiation, DoS or elevation.
- **Phase 11, data:** person emails and client comments are CONFIDENTIAL (PII); they now stay on the person side of every queue read.

### Candidates considered and not reported

1. **The item route's pre-existing 404 (missing) → 403 (inaccessible) split** (Invariant #8 class). It predates this diff. Item ids are `uuid.uuid4().hex` (`db/operator_queue.py:420`), so the split leaks nothing enumerable. Excluded.
2. **Old `responded_by` values in existing agent files.** The write-back never scrubs history (a claim-time ruling). Reaching another agent's file is the REST breadth class owned by trinity-enterprise#719. Registered, not new.
3. **The execution row's `source_user_email`.** The answer and ending wakes still attribute the turn to the person, and the executions API returns that field to keys. Out of scope by ruling; registered in the debt inbox (`debt:2026-09-29-executions-api-returns-person-emails-to-machine-keys`).
4. **`notifications_cleared.cleared_by`**, the same `/ws` pattern in another router. Registered (`debt:2026-09-29-notifications-cleared-trigger-carries-operator-email`).
5. **Operator-typed free text** (`response_text`, `disposition_reason`) may name a person. By design: it is the answer the agent is meant to read.

## Active verification (Phase 12)

- **Unit, over the real routers and real SQLite:** `tests/unit/test_ent715_queue_person_fields.py` (63). It pins the machine key set as a literal across seven machine principals and one real minted agent key, and drives both platform emitters for real. Nine call-site mutations each turn it red.
- **Live, on localhost, by the operator** (main checkout detached at the branch commit, the ask answered through the UI):
  - A person's JWT list shows `responded_by_email` and both alerts.
  - The agent key's list shows 0 alerts, no person-field keys and 0 `example.com` hits.
  - The item route answers the agent key 404 (the same body as a nonexistent id) and the JWT 200.
  - The UI still names the answerer.
  - The `/ws` Clear All frame carries `scope` and `count` only.

## Trend

This resolves the residual the ent#611 PR B diff audit registered (`cso-diff-2026-09-25-ent611-native-ask.md`: "A file entry's addressee is the agent's own input and stays"). No new findings.
