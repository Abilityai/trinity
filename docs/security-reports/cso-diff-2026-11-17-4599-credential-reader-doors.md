# CSO Diff Audit — 2026-11-17 — the remaining doors of a credential ask (#4599)

**Mode**: `--diff`, daily gate (8/10), cso v1.2.3
**Base**: merge base `ee8fc4463` on `dev` (= `HEAD` of `feature/4599-credential-reader-doors`)
**What was audited**: an **uncommitted working tree**. No commit of this change exists yet, so the diff is `git diff HEAD -- src/ config/ tests/` plus one untracked test file. A later commit that differs from this tree is not covered.
**Docker**: reachable (one daemon status query), not used: the audit was run read-only, without the stack. No image-internal check has overlap with this diff.

## What was examined

Thirteen changed files and one new file:

- `src/backend/services/role_addressing.py`
- `src/backend/services/ask_service.py`
- `src/backend/services/skill_gate_service.py`
- `src/backend/services/operator_queue_service/ingest.py`
- `src/backend/services/operator_queue_service/filechannel.py`
- `src/backend/services/platform_prompt_service.py`, `config/trinity-meta-prompt/prompt.md` (the injected agent prompt)
- `src/mcp-server/src/tools/operator_queue.ts`, `src/mcp-server/src/operator_queue.test.ts`
- `tests/registry.json`, three changed unit test files, and the new `tests/unit/test_4599_credential_reader_doors.py`

Read as unchanged context, because the diff's safety depends on them: the secret field's door and sink (`client_portal/asks/service.py`, `services/credential_sink.py`, `services/credential_destination.py`, `services/credential_ask_policy.py`), the Workspace sign-in checks (`client_portal/portal_auth.py`, `client_portal/db.py`, `client_portal/service.py`), the account reads (`db/users.py`), the assignments seam (`services/assignment_provider.py`), the raise and re-address routes (`routers/operator_queue.py`), and the gate's refusal type and handler (`services/skill_gate_errors.py`, `error_handlers.py`).

Not examined: the `docs/` changes in the same working tree (being edited separately) and the `src/mcp-server/node_modules` link.

## Phase 0 — what this change is

A `credential` ask (an agent asks a person for a secret by name) ends at one door: the Workspace secret field. #4579 made the native raise pick a reader who can open that door. Two doors stayed open, and this change closes them:

1. **The queue-file channel.** An agent could file a `type: credential` entry in its queue file and write the addressee itself. Now the poller runs the kind's own raise checks and the reader chain of the native raise; the address the agent wrote is ignored. An entry that cannot be filed is held and named in the file's `platform.ingestion` marker.
2. **Account state.** The reach rules now read the Workspace client block, and the general reach rule reads a deactivated account. Both are tri-state (yes / no / could not check); the yes/no forms fail closed.

It also words each `role_cannot_open_agent` refusal by its real cause, makes the skill gate answer `503 gate_unavailable` when it could not check, and corrects the MCP tool text and the agent prompt.

Stack CVE quicklist: no row has overlap (no pin, image, lockfile, workflow or compose change).

## Phase 1 — attack surface delta

| Surface | Before | After |
|---|---|---|
| Endpoints, MCP tools, WebSocket channels | — | none added; no router file changed |
| Queue file, `type: credential` entry | addressee = the address the agent wrote, kept only if on the shared roster and the one primary; else an unaddressed row nobody could fill; none of the kind's checks ran | addressee = the platform's chain (primary, else owner); the name, option, context and destination checks of the native raise run; no row unless a reader can open the field |
| Who can be recorded as a secret's reader | native: {primary, owner}; file: the agent's pick within the rule above | {primary, owner} on both channels |
| Who may fill the field | the recorded reader, on the roster, signed in | unchanged (`_owned_ask` and the sink are not touched) |
| Agent-readable signal | native refusal text | the same, plus three marker keys in the agent's own file listing its own entry ids |
| Agent-context ingress | — | none added; platform-written text changed in one tool description and one prompt paragraph |

## Focus questions, answered against the code

**1. Can an agent pick the reader through its queue file?** No.
- The poller sends every new credential entry through `CredentialCycle.judge` (`ingest.py:451-469`) and passes the verdict to the clamp (`ingest.py:500`).
- The clamp's credential arm sets the addressee from that verdict and never reads the entry's `addressed_to_email` (`filechannel.py:169-177`). Called without a verdict it leaves the row unaddressed (`filechannel.py:174-175`), and the poller never reaches the create without one (`ingest.py:457-469` continues on every hold).
- The reader depends on the agent alone: `file_credential_reader` (`ask_service.py:1934-1942`) calls `_address_credential`, the function the native raise calls (`ask_service.py:1609`), which calls `role_addressing.resolve_credential_reader`.
- The test "is this a credential entry" and the clamp's own test use one helper on the same field (`filechannel.py:510-512`, `filechannel.py:160-169`), so an entry cannot be credential for one and not for the other. Probed with six spellings of `type`: they agree on all six.

**2. Can the file route store a destination the native route would refuse?** No.
- `file_credential_context` (`ask_service.py:1945-1966`) calls `_credential_destination_stored` (`ask_service.py:1894-1909`), the function `_validated_ask` now calls too (`ask_service.py:2045-2047`). One implementation, two callers.
- The stored destination is rebuilt by `credential_destination.validate` from the instance name and agent name only; an `endpoint_id` or URL the agent wrote into an `instance` destination is discarded (`credential_destination.py:326-351`).
- One native check does not run on the file route: the raise route's read of the asking agent's own key variable. That check is a courtesy before a person types; the sink reads the key again at delivery and refuses a missing or unusable one (`credential_sink.py:503-532`). No authorization difference.
- Delivery re-checks everything against the stored row, whatever the raise did: the name (`credential_sink.py:307`), the outbound switch and the registry (`credential_sink.py:652-663`), the registered entry by stored id and its origin (`credential_sink.py:571-598`).

**3. Does ignoring `addressed_to_email` open or close anything?** It closes two things and opens nothing beyond the native route. Closed: the agent's choice of reader, and the unaddressed credential row that only an operator could cancel. Newly possible on the file route: the owner as reader. That is the native rule since #4579.

**4. Can the `changed_fields` exemption redirect a secret?** No.
- The exemption applies only when the **stored row's** type is `credential` (`filechannel.py:409`); it removes the destination from both sides of the context comparison (`filechannel.py:410-412`) and skips the addressee (`filechannel.py:415`).
- The secret field reads the name and the destination from the stored row and nothing else (`client_portal/asks/service.py:858-862`); the file entry is never read on the answer path. The card shows the stored destination too.
- Still flagged, so the field still refuses (`client_portal/asks/service.py:853-857`): a rewritten title, question, type, deadline, priority, or any other context key including `credential_name`.
- Probed: a rewritten destination and a rewritten addressee return no change; a rewritten name, title or type each return a change.

**5. Disclosure and oracles.**
- `AskRejected.cause` is an attribute, never in `extra` (`ask_service.py:859-862`). Both agent-facing handlers serialise `code`, `message` and `extra` only (`routers/operator_queue.py:703`, `:778`).
- `SkillGateRefused.unchecked` is an attribute set after construction (`skill_gate_service.py:415`); `detail()` returns `status`, `code`, the message and `extra` (`skill_gate_errors.py:110`).
- The new messages name the agent, a role, "its owner" and "a system agent". None carries an address. None says which account state stops a person: blocked, deactivated and not shared read the same.
- The marker lists the agent's own entry ids, shape-checked before they are used (`ingest.py:408-418`), at most ten per key (`filechannel.py:503-506`), under three fixed words. It is coarser than the native refusal: it does not carry the cause. "Could not check" is never written to the file (`filechannel.py:463-467`, `ingest.py:458-460`).
- The reader is never the agent's choice, so neither channel can be pointed at a person to learn their account state. What the agent learns is one bit about its own roster, as it could since #4579.
- The admin re-address message names causes to an instance admin on a route that requires an admin and a person (`routers/operator_queue.py:597-603`).
- Logs name the agent, the entry id and a fixed hold word (`ingest.py:466-468`); no address.

**6. Failure direction of each new lookup.**

| Lookup | When it raises | Direction |
|---|---|---|
| `is_client_blocked` (`role_addressing.py:335-339`) | not swallowed there; caught in `agent_reach` / `workspace_reach`, which return "could not check" (`:385-388`, `:467-470`) | closed: the yes/no forms read it as no (`:401`, `:476`) |
| `is_email_account_suspended` (`:462`) | raises (the helper that answers "deactivated" on error is deliberately not used) | closed, and worded "try again" instead of "not a reader" |
| `get_user_by_email` (`:374`) | same handler | closed |
| `is_workspace_only_role` (`:342-346`) | pure; a missing or unknown role counts as Workspace-only | closed: the block then applies |
| `is_system_agent` (`:479-487`) | returns False | safe: reached only after both reach checks already refused (`:550`); it picks the sentence, never a reader |
| the reader chain (`:534-548`) | a primary who could not be checked is refused, not passed over for the owner; the same for the owner | closed |
| the poller (`filechannel.py:543-554`, `:571-576`) | any error holds the entry for the cycle | closed: no row |

No arm is fail-open. Each states its direction in its docstring.

**7. Work an agent-authored file can cause.** Bounded, per agent per cycle: the entry-only checks run on up to 500 entries and read nothing; the reader chain runs once (`filechannel.py:537-554`), and only if an entry passed the entry-only checks; the registry is read at most ten times (`filechannel.py:566-569`, counted before the read, so failing checks count). The hold line is logged once per agent, entry and hold word, in a set capped at 5,000 (`ingest.py:461-468`). The marker is rewritten only when it differs. Probed with 500 entries: one reader read, ten registry reads.

**8. Invariant #8.** No route changed, so the enumeration rule has nothing new to hold. The refusals name roles. The guard's exemption entry moved from `can_open_agent` to `agent_reach` because the `can_user_access_agent` call moved there (agent-reach guard test in `tests/unit`, `_EXEMPT`, lines 419-423). The reason on the entry still holds: every caller passes an address resolved from a role, never a calling principal (`role_addressing.py:401`, `:408`, `:416`; `skill_gate_service.py:383`).

**9. Secrets and private references in the added lines.** No key shape, no private IP, no host path. Five addresses, all `example.com`; one URL, `example.com`. Seven references to the private tracker appear on added lines; each of the six distinct numbers is already cited in at least three files of the public tree at the merge base, so none is new to the repository. No paid-module table or catalog token.

## Phases run and verdicts

**P2 Secrets** (diff): clean, see item 9. The changed code never handles a value.

**P3 / P4 / P5 / P6**: no overlap with the diff (recorded as not applicable, not as clean).

**P7 Agentic**
- **ASI03.** No grant is added and one is removed: the file channel no longer lets an agent choose who is asked for a secret.
- **ASI09.** The tool text used to say the ask reaches "the person of the current chat", which was false and could make an agent tell a person a field was waiting for them. It now says the platform picks. The refusal tells the agent to get the agent shared or to try again; it does not tell it to ask someone else for the secret.
- **ASI08.** Not applicable: no dispatch path.
- **LLM02 / LLM07.** The prompt paragraph and the tool description are the same for every agent; no per-agent data.
- **ASI01 / LLM01.** No third-party text reaches a prompt. The marker carries the agent's own ids.

**P8 Agent-shipped content**: invisible-Unicode scan over nine changed files: none. No injection string, URL or network call on the added lines.

**P9 OWASP 2025**
- **A01**: no route change. The reader set is {primary, owner} on both channels.
- **A05**: no SQL, shell or template on the added lines.
- **A09**: a held entry is logged once by name; a refused raise is logged with its real cause.
- **A10**: see item 6. One point of care for later edits: the poller recognises "could not check" by comparing `cause` with the literal `"unchecked"` (`filechannel.py:544`) instead of the constant in `role_addressing`. If the constant's value changed, an outage would be written into the file as "nobody can open you". Still closed; wording only.

**P10 STRIDE** (delta): *Elevation* — none; the file channel loses a choice it had. *Tampering* — a credential row's reader and destination are written by the platform only; an entry rewrite cannot change them. *Information disclosure* — item 5. *Denial of service* — item 7. *Spoofing, Repudiation* — unchanged.

**P11 Data classification**: no new stored data; the marker holds entry ids.

## Findings

None at or above the 8/10 gate.

Candidates considered and dropped:

- **The file marker as a cheaper probe than the native raise.** A held entry spends no rate token (`ingest.py:445-469` precede the caps at `:473` and `:484`), while a refused native raise does (`ask_service.py:1584` precedes `:1609`). So an agent can watch one bit, "can anyone who may be asked open me", change every five seconds for free. Dropped: the bit is the same one the native route returns, it is about the agent's own roster, and the agent cannot choose whose state it reads.
- **"Its owner cannot open it in their Workspace" implies the owner is blocked or deactivated.** For an agent that is not a system agent those are the only causes. Dropped: the sentence before this change already said the owner could not open it; the new wording adds the shape of the roster, not the account state.
- **A failed read of the primary seat hands a credential ask to the owner.** The assignments seam returns "no answer" when its provider raises (`services/assignment_provider.py:314-321`), and "no answer" means the owner is the primary (`role_addressing.py:199-203`). So the new rule "a primary who could not be checked is not passed over" holds for the reach reads, not for the seat read. A file row carries no role, so nothing moves it back. Dropped: unchanged code, identical on the native route, and the owner is inside the allowed set. Availability and routing accuracy only. Worth a follow-up decision.
- **Entries beyond the tenth destination check are not judged.** Ten permanently invalid destination entries at the top of a file keep later ones from being read, and count toward the flood alert. Dropped: self-inflicted, bounded, no boundary crossed.
- **Log volume while a lookup is down.** The reach helpers log a warning with a traceback on every failed read (`role_addressing.py:386-387`, `:468-469`), and the poller asks once per agent per cycle. Dropped: excluded class, bounded by one chain per agent per cycle.

## Hypotheses

None in daily mode.

## Coverage gaps

- **No test was executed.** The audit machine has no Python environment with the backend's dependencies, and the stack was not to be used. The new test file (47 test functions), the three changed test files and every guard the skill names (admin gate, auth wiring, enumeration uniformity, route census, agent reach, settings sink) were read, not run. CI is the evidence for them.
- **Substitute verification, weaker than the tests.** A standard-library probe loaded the real `role_addressing.py` and `filechannel.py` with stub collaborators and drove 45 checks: the reader chain under each failed lookup, the tri-state reach, the clamp, the entry test against the clamp, the two bounds, the marker, and the fingerprint. 44 came back as expected. The 45th was the probe's own wrong expectation (a rewritten title is reported as `title` and `question`, because the question defaults to the title); the control it tested holds. The probe did not load `ask_service.py`; its three `file_credential_*` functions were verified by reading.
- **Independent verifier not used.** No finding reached the gate, and no second agent was available; the candidates above were challenged in sequence.
- **MCP server tests not run** (no Node run in this audit). The change there is text.
- **No live run.** Nothing here was observed on a running stack: the 5-second cadence, the marker write-back and the Workspace door were read, not exercised.
- **The private tree** was searched only for callers of the changed functions. It has two, both of the yes/no reach helper, both narrowed by this change. Its own tests were not run.
- **The docs changes** in the same working tree were out of scope.

## Trend

Three earlier diff reports cover this surface (2026-11-04, 2026-11-12 and 2026-11-14, the #4579 report): no findings then, none now.

One candidate the #4579 report dropped is now closed by new evidence: "a transient lookup error on the primary hands the ask to the owner". The reach reads now refuse instead (`role_addressing.py:537-538`, `:547-548`; probed). The same class remains for the seat read, recorded above.

Still open from that report and unchanged here: reach is checked at the raise only, on both channels.

## Steps of the skill not performed

- **Remediation questions**: none to ask; there is no finding.
- **Posting, committing, opening issues**: not done. A follow-up issue would say: "decide whether a failed read of the primary seat should refuse a credential ask with 'could not check' instead of falling back to the owner, on both channels".
- **Skill self-improvement**: not applied. The note it would record: when a guard's exemption is keyed by function name, a refactor that moves the guarded call must move the entry with it, and the audit should re-read every caller of the new holder.
