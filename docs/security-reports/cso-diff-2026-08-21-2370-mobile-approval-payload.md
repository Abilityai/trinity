# CSO Diff Audit — trinity#2370 `/m` approval-card payload + explicit submit

- **Date**: 2026-08-21 · **Mode**: `--diff` (daily, ≥8/10 confidence gate) · **Branch**: `feature/2370-mobile-approval-payload` (working tree vs merge-base `b7bac09d`, pre-commit)
- **Scope**: 11 files — frontend `views/MobileAdmin.vue`, `stores/operatorQueue.js`, `components/operator/QueueCard.vue`, new `utils/operatorQueue.js`; 2 new test files (vitest + Playwright); 5 docs
- **Phases**: 0–14. Phases 3 (deps), 4 (CI/CD), 5 (infra/Docker), 6 (webhooks/integrations), 8 (skills) are **N/A** — the diff touches no backend, no requirements/lockfile, no `.github/workflows/`, no Dockerfile/compose, no `src/mcp-server/`, no `.claude/skills/`

## Findings

**None at the ≥8 confidence gate.**

No new endpoint, no auth-dependency change, no new SQL, no `subprocess`, no file-path handling, no new dependency, no schema change. The diff changes **what the client sends** to an existing, access-checked endpoint and how the mobile card behaves — it is an integrity **fix**: the old `/m` body (`{response:'approved', response_text:<tapped option>}`) mis-recorded the operator's decision; the new body (`{response:<option|answer|'acknowledged'>, response_text:<note|null>}`) is the desktop's, built by one shared module.

## Clean categories (what was checked)

| Axis | Verdict | Basis |
|---|---|---|
| Attack surface delta | CLEAN | zero new routes / WS / uploads / integrations / jobs. The only POST is the pre-existing `POST /api/operator-queue/{id}/respond` (`routers/operator_queue.py:198-262`: `get_current_user` → `_assert_agent_accessible` → pending check → CAS → 409 on the lost race). The request body still matches `OperatorResponse` (`response: str`, `response_text: Optional[str]`) |
| Authorization / IDOR | CLEAN | `/m` is the admin-only PWA behind the shared `authStore` JWT (axios default header, `stores/auth.js:173`); the item `id` interpolated into the URL is the platform-minted uuid from the list the same caller fetched (#1631 — never agent-authored), and the server re-checks accessibility on every respond. No enumeration differential introduced (no new agent-scoped handler) |
| Injection (A03) | CLEAN | no SQL, no shell, no `v-html`/`innerHTML` in added lines (sink scan: 0). Agent-authored strings — option labels, `title`, `question`, `agent_name` — render only through Vue mustache interpolation (`MobileAdmin.vue` template: `{{ opt }}`, `{{ item.title }}`, `Send: {{ selectedOptions[item.id] }}`); `/m` renders `question` as plain text (stricter than desktop's DOMPurify'd markdown) |
| Prompt-injection surface (Phase 7) | CLEAN / unchanged | agent-authored option text was already a button label; it now also appears in the restated-consequence line and the `Send: <option>` label — escaped, and the restatement is the mitigation for the one real hazard here (a look-alike option = social engineering, volume-bounded by #1632). The operator's note/answer flows into the agent file and ent#329's `_framed_message` exactly as before (operator = trusted principal; server-side `RESPONSE_MAX_CHARS` cap unchanged) |
| Integrity direction | IMPROVED | the decision now lands in the field the consumer reads (write-back `operator_queue_service.py:1011-1014`, resume framing `operator_resume_service.py:60`); the server's acceptance of any string as a decision is the residual → filed as **#2376** (server-side `response ∈ options` check) |
| Credential / secret exposure | CLEAN | secret-pattern scan over added lines: 0; no `.env`, no CI secrets. `console.error` on a failed send now logs `status ?? message` instead of the whole axios error object (which carries `config.headers.Authorization`) — a tightening of a pre-existing file-wide pattern, devtools-only either way |
| Logging / audit (A09) | CLEAN | server-side audit unchanged (`responded_by_*` stamped in the DB write); no new client logging of content |
| Client-side state | CLEAN | per-card state (`selectedOptions`, `responseTexts`, `respondErrors`) lives in component memory only — no `localStorage`/`sessionStorage` (scan: 0), pruned on every poll and cleared on logout |
| Tests as attack surface | CLEAN | fixtures are synthetic (`e2e-agent`, `testfix` labels, fake ids); the Playwright harness reads `ADMIN_PASSWORD` from the environment (pre-existing `auth.setup.js`), nothing in the diff |
| Enterprise-docs-guard (ent#45) | CLEAN | the guard's own `PATTERN` run over the five touched docs: 0 hits. The docs name the OSS-core Workspace asks surface (ent#356/ent#428, already in `architecture.md`) and `/api/enterprise/client-portal/asks` — a retained-history prefix, not a paid-module token |
| Ratchets as a security property | CLEAN | raw-colour scan MobileAdmin `0/4/128` vs baseline `0/4/130`; loading-gate ratchet unchanged — no silent relaxation of a frontend guard |
| Agent-key self-boundaries / backend→agent auth / vendored parity / channels / CI / Docker / deps | N/A | untouched by this diff |

## Below the gate (recorded, not findings)

| Item | Confidence | Disposition |
|---|---|---|
| An agent can author an option string that reads like the safe choice (`"Approve (dry run)"`) — the decision is recorded verbatim and the agent acts on it. Pre-existing on desktop; `/m` now restates exactly what will be sent and requires a second, named tap. | 5/10 | Accepted residual — bounded by #1632 ingestion caps; the consequence line + `Send: <option>` are the mitigation; #2376 adds a server-side membership check (does not address look-alikes, by design). |
| The options size-cap marker `(options omitted: exceeded size cap)` renders as a selectable decision on both surfaces. | 4/10 | Registered: `.claude/DEBT_INBOX.md` `debt:2026-08-21-options-size-cap-marker-selectable`. |
| The Workspace asks panel still hand-builds the same body and sends a typed answer as `response_text` (the agent reads `response: ""`). | n/a (sibling) | Filed **#2375** (P1) — out of this diff's scope; named as a known non-consumer in the flow doc. |

## Verification

No finding survived the confidence gate, so the independent adversarial pass was vacuous by construction; the load-bearing *clean* verdicts (endpoint/auth unchanged, escaped rendering of agent strings, id provenance, no storage, guard pattern) were each traced to a quoted line. The behavioural proof that the **integrity fix is real** is the Playwright spec run against the pre-fix code: it fails with `captured POSTs: [{"response":"approved","response_text":"Deny"}]`.

## Trend

Fourth `--diff` audit of 2026-08-21 (#2322, #1927, #2320, #2370), fourth with zero gate-passing findings. Unlike the previous three this one **raises** integrity rather than widening a read: a client that mis-recorded operator decisions for five months now sends the truth, and the class got a server-side guard issue (#2376) plus a sibling-surface issue (#2375) so it cannot re-ship quietly.

## Verdict

Frontend-only integrity fix on an existing, access-checked endpoint; no new surface, no new input path beyond an optional free-text note that already existed as a "Type response" box. Ship-clean.
