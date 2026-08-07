# CSO Audit — `--diff` (feature/ent263-library-page)

- **Date**: 2026-07-31 · **Mode**: daily (8/10 confidence gate) · **Scope**: `--diff` — 24 files changed vs merge-base `39f29c64` (branch `feature/ent263-library-page`, trinity-enterprise#263: Templates → Library rename + fleet skills-browse section)
- **Diff shape**: pure frontend (1 renamed view, 2 new components, 1 new store, router redirect, NavBar) + docs + e2e/test cleanup. **Zero backend change** (verified: no `src/backend/` code in the diff; `routers/templates.py`, `routers/skills.py`, `skill_service.py` untouched).

## Attack-surface delta

| Category | Delta |
|---|---|
| Backend endpoints | **0** — the new UI consumes only pre-existing `GET /api/templates(/{id})`, `GET /api/skills/library(/status)`, `POST /api/skills/library/sync` |
| Auth boundaries | unchanged — status/list are `get_current_user` (any auth), sync is `require_admin` (verified in `routers/skills.py:64,101`); client-side `isAdmin` gating is display-only convenience over those server gates |
| Frontend routes | `/library` (requiresAuth) + `/templates` fixed-path redirect (`{ path: '/library', query, hash }` — no user-controlled destination, no open redirect) |
| New rendered inputs | skill metadata from the synced library repo (semi-trusted) + the stored library repo URL |

## Findings

### 1. `stripUserinfo()` credential scrubber bypassable on non-parseable / opaque-parse URL shapes — MEDIUM · VERIFIED (10/10) · **REMEDIATED IN-BRANCH**

`src/frontend/src/components/LibrarySkillsSection.vue` (as introduced by commit 24d1476a) scrubbed embedded credentials from the displayed library repo URL via `new URL()` + a `\w+:\/\/`-anchored regex fallback. Executed adversarial testing (node, literal function) proved **five leak shapes**:

1. `user:token@host/path` (schemeless) — *parses* as a WHATWG URL (scheme `user`, opaque hostless path) where `.username = ''` is a **silent no-op**, so the success lane returned the credential verbatim;
2. `user:token@host:path` (scp-style with credential-shaped userinfo) — same opaque-parse no-op;
3. `//user:token@host/…` (protocol-relative) — URL() throws, regex requires a scheme;
4. `git+ssh://user:token@[invalid-host]/…` — URL() throws, `\w+` misses the `+` in the scheme;
5. leading whitespace before a non-parseable shape — defeats the `^` anchor.

**Exploit scenario**: an admin stores a credentialed library URL in one of these shapes (scp-with-token is the realistic one); the Library header — and, pre-fix, the per-agent SkillsPanel `library_empty` state, which rendered the URL **raw to any agent accessor, not only admins** — paints the token on screen (screenshots, screen-shares, shoulder-surfing). Client-side display layer only; the API disclosure boundary is unchanged (see posture note 1).

**Remediation (this branch)**: `stripUserinfo` moved to the shared seam `components/skills/contract.js`, hardened — the parsed lane is trusted only when `parsed.host && !parsed.username && !parsed.password` *after* assignment (defeats the opaque/hostless and engine-no-op cases), else a widened textual scrub (`[A-Za-z][\w+.-]*` scheme charset, protocol-relative, schemeless colon-user, `.trim()`); **variant analysis** wired the same scrub into `SkillsPanel.vue`'s empty-state URL (the weaker sibling). Verified by a 26-case executed suite (23 adversarial + 3 must-survive-unmangled): ALL PASS. Durable class captured in `docs/memory/learnings.md` (2026-07-31 entry). Independent-verifier note: verification was by literal execution of the shipped function against the attack shapes — stronger than refutation review; no subagent spawned.

## Posture notes (pre-existing, out of diff — not findings under `--diff`)

1. `GET /api/skills/library/status` returns the raw `url` (potentially credentialed) to **any authenticated user** — the Library page's admin-only display is UI defense-in-depth over an endpoint that does not enforce it. Pre-dates this branch; PR #1901 (ent#237) classes source URLs admin-sensitive on its new `/api/skills/sources` but deliberately keeps the flat compat fields on `/status`. Worth a server-side scrub-or-gate follow-up on the status endpoint when #1901 lands (owner: ent#237 lineage).
2. `views/Settings.vue:8` comment still names the "Templates" nav entry — stale comment in a PR-#1901-owned file this branch is contractually hands-off on; cosmetic.

## Clean categories (checked, nothing found)

- **Secrets archaeology** (P2): `git log -p 39f29c64..HEAD` gregreped for AKIA/sk-/ghp_/gho_/github_pat_/xox*/whsec_ — clean. No `.env` added; no workflow changes.
- **Enterprise disclosure** (ent#45 guard): changed docs reference enterprise issues by number only (allowed precedent) and describe an OSS-core feature per the recorded ent#263/ent#237-AC#7 decision; no paid-module catalog, no `enterprise_*` DDL, no gating design added — grep clean.
- **XSS / rendered-field sanitization** (P7): all new rendering is interpolation-only ({{ }} and escaped `:title` attribute bindings); **no `v-html`, no `:href` bound to library-derived strings** in `LibrarySkillsSection.vue`, `SkillContractChips.vue`, or the reworked `Library.vue` — grep + read verified. Skill metadata (name/description/automation/requires/source_name/shadowed_by) treated as semi-trusted throughout.
- **Supply chain** (P3): no dependency or lockfile changes (`package.json` untouched).
- **CI/CD** (P4): no workflow changes.
- **Auth wiring** (A01): no new endpoints; store calls go through the shared `api` client (Invariant #7 — this diff *removes* two hand-built raw-axios auth headers). Sync button hidden for non-admins client-side AND `require_admin` server-side.
- **Open redirect**: `/templates` redirect is fixed-path with query/hash carry — destination not attacker-controllable.

## Verdict

**PASS for this diff** — zero backend surface change, one MEDIUM display-layer scrubber finding, proven and remediated in-branch with variant coverage and an executed regression suite. No CRITICAL/HIGH.
