# /cso --diff — feature/pro-assistant (abilityai/trinity-enterprise#1283)

Date 2026-11-19 · cso 1.2.3 · daily gate · diff against `dev` at `00b1e5ae91` · not a full run.

Scope: the public half only (email relay seam, first-run install mark, the gated assistant panel, the system views dropdown). The module behind the panel is in the private submodule and its review is recorded there.

## Findings

| # | Sev | Conf | Category | Finding | State |
|---|---|---|---|---|---|
| 1 | MEDIUM | 9/10 | secret-in-log | A sign-in code was written to the log when a registered mail relay refused (`src/backend/services/email_service.py`) | fixed on branch |
| 2 | LOW | 9/10 | identity-minted-on-read | GET /api/onboarding/first-run called a get_or_create accessor (the #2669 class) (`src/backend/services/onboarding_service.py`) | fixed on branch |
| 3 | LOW | 7/10 | unvalidated-link | Links supplied by a remote service were bound to href / window.open without a scheme check (`src/frontend/src/utils/assistantCards.js`) | fixed on branch |

Each has a regression test named in the JSON beside this file.

## Checked and clean

- Secrets: no known-prefix hit in added lines; no `.env` tracked.
- Dependencies, CI, Docker: no file of those kinds changed.
- Auth: no public route added; `first-run` keeps its existing gate. 257 guard tests green (admin gate, auth wiring, settings sink, agent-auth header, enumeration uniformity, cmdline leak, source-agent attribution, minting accessors).
- Rendering: every `v-html` in the changed components goes through `utils/markdown.js` (DOMPurify).
- Hidden characters: none in any changed text file.
- Enterprise-docs guard: no hit on added doc or seam lines.

## Coverage gaps

- Guards that import the real app (route census, ops-write sweep, human-only refusal audit) could not run on the audit host (a Python module is missing there). CI runs them; they must be green before merge.
- No dependency scanner ran, because no dependency changed.
- The gated panel's user-facing text names the paid tier. The guard's pattern does not flag it; left for the owner to judge.

## Hypothesis (not counted)

- A registered mail relay receives the sign-in code it is asked to send. That is the seam's purpose; whether to accept that trust is a deployment decision.
