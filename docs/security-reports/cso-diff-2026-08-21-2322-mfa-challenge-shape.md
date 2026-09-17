# CSO Audit — `--diff` — #2322 MFA challenge response shape

- **Skill**: cso v1.1 (daily mode, 8/10 confidence gate)
- **Date**: 2026-08-21
- **Scope**: `--diff` — branch `fix/2322-mfa-challenge-response-shape` (PR #2357) vs `76615f26`
- **Phases run**: 0, 1, 2, 3, 5, 9, 10, 11, 12, 13, 14 (4, 6, 7, 8 no-op — nothing in the diff touches CI, webhooks, LLM paths, or skills)
- **Result**: **0 findings.** The diff is a net reduction in disclosure.

## Phase 0 — Mental model (diff-scoped)

The change touches one trust boundary: the **unauthenticated → authenticated**
transition at `POST /token`. It alters what that boundary *emits* on the
second-factor-pending path, and hardens two first-party clients that consume it.
It adds no endpoint, removes no auth dependency, and changes no gate.

Trust boundary before/after:

| | Before | After |
|---|---|---|
| Correct password, no 2FA | 200 `{access_token, token_type}` + 4 null 2FA fields | 200 `{access_token, token_type}` |
| Correct password, 2FA pending | 200 `{access_token: null, token_type: "bearer", …challenge}` | 200 `{…challenge}` |
| Wrong password | 401 | 401 (unchanged) |

Every delta **removes** a field. There is no path on which this diff emits more
than it did.

## Phase 1 — Attack surface census (diff)

| Category | Count | Delta |
|---|---|---|
| Public/unauthenticated endpoints | 2 touched (`/token`, `/api/token`) | 0 added |
| Authenticated endpoints | 0 | 0 |
| Admin endpoints | 0 | 0 |
| Auth dependencies added/removed | 0 | — |
| File upload points | 0 | — |
| WebSocket channels | 0 | — |
| Background jobs | 0 | — |
| CI workflows | 0 changed | — |
| Docker/compose | 0 changed | — |
| Dependencies | 0 added (no `package.json` / `requirements*` in diff; lockfile untouched) | — |

## Phase 2 — Secrets archaeology (diff-scoped)

`git log -p 76615f26..HEAD` scanned for `AKIA`, `sk-`, `ghp_`, `gho_`,
`github_pat_`, `xox*`, and password/secret/token assignments. **No hits.**

Two credential-hygiene checks specific to this diff, both clean:

1. **Neither new client error message echoes token material.** The CLI prints a
   fixed string; the MCP client interpolates only a fixed `reason`. Verified by
   reading both.
2. **A doc example that echoed the response body was caught and fixed during
   `/review`** (commit `05e84bfc`, finding I1). It printed `$resp`, which carries
   `challenge_token` — a five-minute bearer for the `/2fa/login/*` endpoints — and
   the snippet is the kind operators paste into CI, where stderr is archived. Now
   prints a fixed message. Recorded here because it was real and pre-merge, not
   because it survives.

Nothing in the diff writes to `schedule_executions.backlog_metadata` (the G-04
plaintext-persistence class).

### Enterprise-disclosure guard (trinity-enterprise#45)

The PR changes six files under `docs/**`, so `enterprise-docs-guard.yml`
**runs on it**. Executed its exact PCRE locally against the branch:

```
PATTERN='\bSIEM\b|\bSCIM\b|\bpermissions_matrix\b|\btwo_factor\b|2fa_recovery_codes|\benterprise_(?!features\b|…)[a-z_]+'
```

**Clean — zero hits.** The new prose says "2FA" / "second factor" /
`/api/enterprise/2fa/login/*`, never the forbidden module identifier
`two_factor` or an `enterprise_<table>` name.

Worth stating explicitly since this PR adds the most detailed public description
of the 2FA login flow to date: it discloses nothing new. The OSS tree already
documents the mechanism more fully than these docs do —
`services/mfa_gate.py`'s module docstring describes the whole provider protocol
including the entitled path, and `dependencies.py` + `models.py` already named
"enterprise 2FA (#5)". The new text describes the **generic seam and observable
behaviour**; it adds no paid-feature catalog entry, no `enterprise_*` schema, and
no gating/monetization rationale.

## Phase 3 — Dependency supply chain

No dependency manifests in the diff. `npm ci` was run in the worktree to execute
the MCP test suite; `package.json` / `package-lock.json` are **unmodified**
(`git status` clean for both). No new install-script dependencies.

## Phase 5 — Infrastructure

No Dockerfile, compose, or network changes. Vendored-copy parity (Invariant #5)
re-run as a collateral check: `test_credential_paths_parity.py` +
`test_model_context_parity.py` → **4 passed**.

## Phase 9 — OWASP (diff-scoped)

| Category | Verdict | Evidence |
|---|---|---|
| **A01 Broken Access Control** | Clean | No gate, dependency, or role check touched. `response_model_exclude_none` is serialization-only; `HTTPException` bypasses `response_model` entirely, so no error path is reshaped. |
| **A01 — enumeration (#186)** | Clean, unchanged | The `mfa_required` flag is only reachable by a caller who already supplied the **correct** password (wrong password → 401 before the gate). Not a pre-auth oracle, and the flag predates this diff. |
| **A02 Cryptographic Failures** | N/A | No crypto, JWT minting, or key handling changed. The `challenge_token` is the pre-existing #5 primitive — 5-min TTL, scope-locked to `/2fa/login/*`, rejected by `decode_token` (pinned by a new test). |
| **A03 Injection** | N/A | No SQL, subprocess, or template surface in the diff. |
| **A04 Insecure Design** | Improved | A refused grant is now distinguishable from a granted one by construction rather than by a null check the caller had to know to make. |
| **A05 Misconfiguration** | N/A | No CORS/CSP/debug settings. |
| **A07 Authentication Failures** | **Improved** | The core of the change: an incomplete authentication no longer presents as a complete one. Three first-party clients that silently accepted the ambiguous response now fail closed at the login call. |
| **A08 Integrity** | N/A | No deserialization or template-integrity surface. |
| **A09 Logging** | Unchanged | `mfa_challenge_issued` audit entry untouched; no new log statements carry token material. |
| **A10 SSRF** | N/A | No outbound URL construction. |

## Phase 10 — STRIDE (the one boundary this diff moves)

**Component: `POST /token` — unauthenticated → authenticated boundary**

| | Assessment |
|---|---|
| **Spoofing** | Unchanged. Password verification and rate limiting are upstream of the modified code. |
| **Tampering** | Unchanged. No new writable state. |
| **Repudiation** | Unchanged. Both the challenge and grant paths keep their existing audit entries. |
| **Information Disclosure** | **Reduced.** Two fields removed from the challenge response, four from the grant response. No field added on any path. |
| **Denial of Service** | Out of scope per hard exclusion #1; no rate-limit behaviour changed regardless. |
| **Elevation of Privilege** | **Reduced.** Previously a client could hold a value it believed was a session and act on that belief. It now cannot obtain one. |

## Phase 11 — Data classification (diff-scoped)

| Data | Class | Where | Protection after this diff |
|---|---|---|---|
| `access_token` (session JWT) | RESTRICTED | response body → client storage | Unchanged; now **absent** rather than null on a refused grant |
| `challenge_token` | RESTRICTED | response body → frontend memory | Unchanged (5-min, scope-locked). No longer echoed by the doc example |
| `mfa_required` / `mfa_enrolled` / `enrollment_required` | INTERNAL | response body | Post-password only; unchanged, now absent on the grant path |

## Phase 12 — Filtering & verification

- Hard exclusions applied: #4 (input validation without proven impact), #5
  (hardening without a concrete vulnerability) — both would otherwise have
  produced noise from the client-side guards, which are defence-in-depth, not
  vulnerability fixes.
- **Independent verification**: performed **in-context** rather than by
  subagent, per this operator's standing instruction not to spawn agents
  unrequested. The skill's documented fallback. Noted so the substitution is
  visible, not silent.
- Every "clean" verdict above is backed by a command that was run or a line that
  was read — no category is marked clean on plausibility.

## Findings

**None.** No finding reached the 8/10 gate; none reached 5/10.

The `/review` pass that preceded this audit found and fixed one
credential-hygiene issue (I1, the echoed response body) already in the branch.
It is recorded in Phase 2 for completeness, not as an open finding.

## Trend

41 prior reports in `docs/security-reports/`. This is a scoped `--diff` audit of
a single branch, not a fleet posture scan; no fingerprint comparison is
meaningful against the full-scan history. Most recent comparable `--diff`
audits: `cso-diff-2026-08-17-2258-workspace-signout`,
`cso-diff-2026-08-16-2214-portal-turn-bound`.

## Residual risk (not findings — noted for the reviewer)

1. **`/token` still answers HTTP 200 for a grant that issued no session.** RFC
   6749 says a failed grant is an error response. This is a deliberate scope
   boundary on #2322, deferred to the issue owner. It is a **correctness and
   contract** concern rather than a security one — after this change the response
   carries no credential-shaped field to misread — but a client that gates on
   status code alone still cannot tell the two apart.
2. **A role policy sweeps unattended machine credentials into a human-interactive
   second factor.** Enabling `require_for_admin` / `require_for_creator` defers
   every admin/creator-role login immediately, including automation that cannot
   complete a TOTP challenge and including accounts that have not enrolled. That
   is an availability property of the #5 design, not a defect introduced here,
   and it is the second question deferred to the issue owner.

---

## Addendum — 2026-08-21, after R1 was implemented

**R1 is closed.** It is left in the Residual Risk section above as the
point-in-time record of what the audit found; this addendum is the correction
rather than a rewrite of the finding.

`/token` no longer answers HTTP 200 for a grant that issued no session. It
answers **403** with `{"detail": "mfa_required", …challenge}` — the issue's own
Suggested Fix. Three outcomes now carry three status codes (200 grant / 403
second-factor-pending / 401 rejected).

Two security-relevant properties of that change, both newly pinned by tests:

1. **A rejected password still returns 401 and carries no challenge.** The two
   failure modes stay distinguishable; collapsing them would hand a challenge
   token to a caller who never proved the first factor
   (`test_wrong_password_is_still_401_and_carries_no_challenge`).
2. **403 was chosen over 401 partly for security-adjacent reasons.** A 401
   drives the frontend's global axios interceptor into `authStore.logout()` +
   redirect, and the CLI's `_handle_response` into a hard exit telling the user
   to re-login — both wrong for a login still in flight, and both would have
   made the refusal *less* legible, which is the whole defect class #2322 is
   about (`test_the_403_is_not_a_401`).

Posture is unchanged from the audit's conclusion: still a net reduction in
disclosure. The challenge response lost two fields and gained `detail`, which
carries no secret.

**R2 remains open and is NOT a defect** — see the residual note above. Trinity
has no service-account principal, so "exempt machine credentials from the 2FA
policy" is a new capability rather than a fix, and any such exemption is a
second-factor bypass by construction. The supported answer today is that
automation should use an MCP API key (`trinity_mcp_*`), which is not subject to
the second-factor flow; the user-facing docs updated in this PR now say so
explicitly.
