# CSO Audit — `--diff` — trinity-enterprise#236 skills lifecycle automation

**Date**: 2026-07-29
**Skill**: cso v1.1
**Mode**: `--diff`, daily (8/10 confidence gate)
**Branch**: `feature/ent-236-skills-lifecycle-automation` vs merge-base `a8418a2f`
**Phases run**: 0–14 (scanning constrained to the branch diff)

---

## Phase 0 — Architecture model (diff-scoped)

The change adds an **unattended write path into every running agent's filesystem**. That
is the whole security story of this diff:

```
GitHub skills library ──git pull──▶ /data/skills-library (shared clone, one per install)
                                          │
                    (NEW: scheduled loop, leader-locked)
                                          ▼
                        fleet sweep ──restore tar──▶ every running agent's
                                                     ~/.claude/skills/<name>/
                                                     + CLAUDE.md rewritten
```

Trust boundary: the library repo is **trusted content** — a `SKILL.md` is instructions
Claude executes. The boundary protecting it is "only an admin configures
`skills_library_url`". Before this diff a human also had to click *Sync Library* and then
inject per agent. This diff removes the human from steps 2 and 3.

## Phase 1 — Attack surface delta

| Category | Delta |
|---|---|
| Admin endpoints | **+2** (`GET`/`PUT /api/settings/skills-library`) |
| Public/unauthenticated endpoints | 0 |
| Endpoints accepting an agent's own key | 0 |
| Background jobs | **+1** (`skills_sync_service`, leader-locked, default OFF) |
| New agent-container write paths | **+1** (fleet re-inject; removal deletes) |
| New in-container `exec` payloads | **+2** (`_list_managed_skills`, `_finalize_removed_dirs`) |
| Dependencies added | 0 |
| CI/CD workflows changed | 0 |
| Dockerfiles / compose changed | 0 |

## Findings

```
#   Sev    Conf   Status      Category            Finding                                          Phase   File
──  ────   ────   ────────    ────────            ───────                                          ─────   ────
1   HIGH   9/10   FIXED       Access Control      Automation on-switch passes for agent-scoped key  P9/A01  routers/settings.py
2   HIGH   8/10   REDACTED    Access Control      Pre-existing, still open — see ent#293            P9/A01  (withheld)
```

> **Redaction note.** This repository is public. Finding 2 is **not yet fixed**, so its
> mechanism and exploit path are recorded only in the private tracker
> (`abilityai/trinity-enterprise#293`) and withheld here. Finding 1 is published in full
> because it is fixed in the same commit as this report.

---

### [1] HIGH · 9/10 · VERIFIED — `PUT /api/settings/skills-library` grants unattended fleet-wide write, gated only by role

**File**: `src/backend/routers/settings.py:1876` (new in this diff)

```python
async def update_skills_library_automation_setting(
    body: SkillsLibraryAutomationUpdate, request: Request,
    current_user: User = Depends(get_current_user),
):
    assert_admin(current_user)          # ← role gate only
```

`assert_admin` (`dependencies.py:740`) rejects **connector** principals and then checks
`role == "admin"`. An agent-scoped MCP key resolves to its owner *carrying the owner's
role*, so on a default admin-owned install any non-ephemeral agent's injected
`TRINITY_MCP_API_KEY` satisfies it.

**Third documented occurrence of this class** (`docs/memory/learnings.md:147`, `:176` —
trinity-ops-agent#232, then trinity#1644, then trinity#1816). The ledger's own rule is
decisive here:

> "the endpoint that USES a capability may be agent-callable; the endpoint that GRANTS
> it must be human-only."

This endpoint *grants* the capability.

**Exploit scenario**
1. An agent is prompt-injected (via a web page it reads, an inbound channel message, a
   task payload — the standard assumption behind `reject_agent_principal` existing).
2. It arranges for the configured library to carry attacker-authored skills. *(Mechanism
   REDACTED — it depends on finding 2, which is still open; see the private tracker.)*
3. It calls `PUT /api/settings/skills-library` `{auto_sync_enabled: true,
   auto_reinject_enabled: true}` → passes `assert_admin`.
4. Within one interval the loop pulls the attacker's repo and the fleet sweep writes
   `SKILL.md` files into **every running agent's** `~/.claude/skills/`, rewriting each
   agent's CLAUDE.md to advertise them.
5. Every agent in the fleet now reads attacker-authored instructions on every subsequent
   turn. Persistent (survives restart — the packages are on the home volume), unattended,
   and self-reinforcing.

**Impact**: privilege escalation + fleet-wide lateral movement. The ent#183 archive vetting
limits the *shape* of the write (no symlinks, `.env`/`CLAUDE.md`/`.mcp.json` basenames
refused) but `SKILL.md` body content is arbitrary instructions by design.

**Why this diff owns it**: pre-#236 an admin had to click Sync and inject per agent, so a
human was in the loop at steps 4–5. This is the automation-re-prices-an-existing-gate
pattern already recorded for this branch in the learnings ledger.

**Status**: **FIXED on this branch** — `reject_agent_principal(current_user)` added to the PUT before
`assert_admin`. The GET may stay role-only (read of non-secret config; the error string is
PAT-scrubbed as of this branch).

---

### [2] HIGH · 8/10 · VERIFIED — **REDACTED** (pre-existing, still open)

**Details withheld from this public report.** This finding is a pre-existing access-control
weakness in the same area, not introduced by this diff, and it is **not yet fixed** — so the
mechanism, the affected setting, the file:line, and the exploit path are recorded only in
the private tracker.

**Tracked at**: `abilityai/trinity-enterprise#293` (P1, `type-bug`, `theme-security`).

**Why it is in this report at all**: it is step 2 of finding 1's chain, and #236's
automation is what would let it reach the fleet unattended. Finding 1's fix (shipped in
this branch) breaks the chain at step 3 regardless, so the two are independently useful —
but a reader auditing this area should know a second, open finding exists rather than
concluding the area is clean.

**Why redacted rather than published**: `abilityai/trinity` is a public repository, and
publishing a working exploit path for an unfixed vulnerability is a disclosure, not
documentation. The full write-up — evidence, chain, and structural remediation options —
lives on #293 and should be folded back into this file once that issue closes.

---

## Clean / not-a-finding (verified, not assumed)

| Check | Result |
|---|---|
| P2 secrets in diff | Clean. One `ghp_`-shaped string exists in `tests/unit/test_ent236_skills_lifecycle.py` — a synthetic fixture asserting the new PAT scrub; `tests/` is a documented blanket gitleaks path exclusion (`.gitleaks.toml:51`), so it neither leaks nor blocks CI. FP per "test fixtures excluded". |
| P2 new persistence surface | `system_settings['skills_library_last_error']` is a NEW durable, admin-rendered string fed by `sync_library`'s outer `except Exception as e`. Was raw `str(e)` with the PAT-bearing remote URL in the subprocess argv → **fixed on this branch during `/review`** (`_scrub_pat` at the write, test asserts `https://***@`). G-04 class, closed. |
| P3 supply chain | No dependency, lockfile, or `package.json` change. |
| P4 CI/CD | No workflow files touched. |
| P5 infra | No Dockerfile/compose change; no new service on the agent network; Redis ACL model untouched. `skill_packaging.py` is **not** vendored into the base image, so Invariant #5 parity is unaffected — parity tests re-run green (4 passed). |
| P5 two-network invariant | The new background service runs in the backend (already bridges both networks); no agent-network member added. |
| P6 agent-key self-boundaries | No new endpoint accepts an agent's own key. Heartbeat/result-callback/report boundaries untouched. |
| P6 backend→agent auth | Fleet re-inject reuses `inject_skills`, which goes through the existing `agent_httpx_client`/restore path; removal uses `client.delete` and `execute_command_in_container`. No new raw `agent-{name}:8000` caller. |
| P7 in-container exec injection | Both new exec payloads embed every library/agent-derived value via `json.dumps` into a base64'd python script — zero shell interpolation. Names pass `validate_skill_name` before use; `_list_managed_skills` re-validates names read back off the agent's own filesystem. |
| P7 path confinement | Deletion paths come from `compute_removal` → `compute_prune`, which prefix-confines to `.claude/skills/<name>/` and rejects `..` segments. The `rmdir` climb re-checks the prefix **and** `..` independently (a traversal segment satisfies a bare prefix check). |
| P7 alarm content | The reconcile alarm carries counts only; the fleet alarm carries validated agent names only. No agent-authored free text enters durable operator state (G-04 discipline). |
| P9 A01 route ordering | `/skills-library` registered at 1828/1876, before the `/{key}` catch-all at 2271+ (Invariant #4). |
| P9 A01 read/mutate tiers | Both new routes are admin; no accessor-tier gate on a mutating route. |
| P9 A03 injection | No SQL added; no `subprocess`/`os.system` added; no new `v-html`. |
| P9 A04 insecure design | Interval range-validated 300–86400 at the boundary **and** read-clamped at the sink; the three keys are blocked on the unvalidated generic PUT. |
| P9 A09 logging | Audit rows added for scheduled sync, fleet re-inject, and every removal (`source=system` for automated paths). |
| P10 STRIDE (delta) | Elevation of Privilege → finding 1. Tampering → the library clone had no lock (two `git reset --hard` racing); **fixed on this branch during `/review`**. Information Disclosure → the persisted error string; fixed. Repudiation → covered by new audit rows. |
| P11 data classification | `skills_library_last_error` = INTERNAL (scrubbed). Fleet report = INTERNAL (agent names + counts). No RESTRICTED data added. |

## Trend

89 prior reports in `docs/security-reports/`. Finding 1 is **persistent-class, new-instance**:
the `assert_admin`-is-not-a-human-gate class appears for the third time
(trinity-ops-agent#232 → trinity#1644 → trinity#1816 → **here**). Recurrence at this rate
suggests the fix belongs at the gate, not per-endpoint — e.g. a capability-granting key
allowlist, or making `reject_agent_principal` the default for `PUT`/`DELETE` on settings.
That is a platform change, out of scope for #236; recommend filing it.

## Incident response

Not applicable — no leaked credential found. The PAT-into-`system_settings` path was closed
before merge, and no such row can have been written on this branch (the code never ran
outside tests).
