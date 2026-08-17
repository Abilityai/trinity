# Feature: Skill Injection (Full Directory Packages)

## Overview

Skill Injection ships each assigned skill's **entire directory package**
(`SKILL.md` + `scripts/` + resources) from the platform's skills library into a
running agent's `~/.claude/skills/`, and updates the agent's `CLAUDE.md` with a
"Platform Skills" section (annotated with missing-dependency warnings). Since
trinity-enterprise#183 the unit of injection is the **skill package**, not a
single file: a vetted tar built from `git archive` of the library clone is
delivered through the existing agent-server restore primitive, versioned by git
tree SHA, and pruned by manifest on re-injection.

## User Story

As an agent owner, I want assigned skills — including multi-file skills with
scripts and resources — to arrive on my agent completely and honestly: unmet
declared dependencies or partially-shipped files produce named warnings, never
silent success.

## Entry Points

- **API**: `POST /api/agents/{agent_name}/skills/inject` (owner-only; `force=True` — unconditional repair)
- **MCP**: `sync_agent_skills` (same endpoint; surfaces per-skill warnings even on success)
- **Automatic**: `services/agent_service/lifecycle.py::inject_assigned_skills` during agent start (`force=False` — version-unchanged skills are skipped)
- **Fleet** (ent#236): `skills_sync_service.run_fleet_reinject` after a library sync that moved the commit — running non-ghost agents, `force=False`, bounded concurrency

## Removal (ent#236)

Injection's inverse, added because an unassigned skill previously stayed on the
agent forever — still in `~/.claude/skills/`, still listed in CLAUDE.md, still
invocable.

```
DELETE /skills/{name}  ·  PUT /skills (dropped names)  ·  start-path reconcile
        │
        ▼
skill_service.remove_skills   (SAME skill_inject:{name} lock as injection)
        │  read .trinity-skill.json  → no meta ⇒ not_managed, nothing deleted
        │  compute_removal(meta.manifest) = compute_prune(prev, [], name) + meta
        │  DELETE /api/files per path (the prune transport, protected-path guard)
        ▼
_finalize_removed_dirs (ONE exec): rmdir the emptied dirs (incl. the skill root)
        · strip the skill's `.gitignore` line  · CLAUDE.md section rebuilt
```

| Property | Mechanism |
|---|---|
| Only platform-written files die | delete set is the previous injection's own manifest — agent-authored files and runtime artifacts (`__pycache__`, models) are never in it |
| Directories survive if not empty | `os.rmdir` refuses a non-empty dir, so a skill dir holding agent files stays |
| Unmanaged dir untouched | no `.trinity-skill.json` ⇒ `not_managed` + `unmanaged_dir_kept` (the mirror of injection's overwrite-only `unmanaged_dir_overwritten` — overwrite is recoverable, deletion is not) |
| Meta removed last | while it exists the package is still managed, so an interrupted removal resumes. Included even when the manifest is missing/garbage, or the dir would stay in every future reconcile's inventory with nothing to delete |
| Truncation (>200 paths) | `removal_truncated` and the meta is **kept** — dropping it would strand the remaining files as unmanaged orphans |
| Unassign never fails | DB row is authoritative and already committed; a stopped agent / busy lock / dead transport degrades to `removal_deferred:*` and the start-path reconcile finishes it |
| Probe failure ≠ "nothing there" | the batch meta read returns `{}` on ANY exec failure, so an empty result is reported `deferred`, never `not_present` |
| Gitignore line stripped | else `.gitignore` grows a dead line per skill ever injected, and a later agent-authored skill of the same name would be silently un-committed by auto-sync |

**Reconciliation, not tombstones.** By the time a stopped agent starts, the
assignment row is gone — there is nothing to replay. `reconcile_agent_skills`
diffs the agent's platform-managed skill dirs against the current assignment set
instead, so every removal route converges (single DELETE, bulk-PUT shrink, direct
DB edit) with no new table and no migration. It runs after injection (same lock,
so it cannot run inside it) and **also when zero skills are assigned** — that is
precisely the "unassigned the last skill" case.

**Blast-radius guard.** A reconcile proposing more than
`SKILLS_RECONCILE_MAX_REMOVALS` (10) removals for one agent refuses wholesale,
logs ERROR, and raises an operator alarm on the uncreatable sentinel
`_skills-sync`. A wiped or reset `agent_skills` table is indistinguishable from a
legitimate mass-unassign, and one of those two readings erases every package in
the fleet on the next restart — keeping files is the recoverable direction
(#1638/#1644). The alarm carries counts only, never skill names (G-04's lesson).

## Architecture

```
/data/skills-library/<source_id> (per-source git clone, ent#237)
        │  skills_rel_root(): catalog.yaml skills_root: → skills/ probe →
        │    .claude/skills/ fallback (ent#332 — per source, every invalid
        │    tier falls through; dual-layout keeps legacy + layout_conflict)
        │  git ls-tree HEAD -- <root>/         → per-skill TREE SHA (= version,
        │    layout-independent: a restructure with same content keeps versions)
        │  git archive HEAD -- <root>/<n>      → atomic tar source
        ▼
services/skill_packaging.py (pure)
        │  filter_skill_archive(source_root=…): REGTYPE only (symlinks NEVER
        │    ship — build-side exfiltration guard + agent-side KeyError guard),
        │    litter + protected basenames dropped w/ warnings, caps enforced;
        │    ent#332: arcnames REWRITTEN <root>/<n>/… → .claude/skills/<n>/…
        │    (identity for legacy) — the ONE point source layout becomes the
        │    agent destination, so everything below stays destination-canonical
        │  build_injection_tar: uncompressed; generated .trinity-skill.json
        │    {version, commit, manifest, injected_at} appended LAST (partial-
        │    extraction ordering guard)
        ▼
services/skill_service.py (orchestration, per-agent Redis lock skill_inject:{name})
        │  POST /api/agent-server/restore  (EXISTING primitive #384/#1169;
        │    allowlist [".claude/skills/<n>/**"], traversal-guarded, 300s)
        │  files_written = response `restored`; sent−restored → restore_skipped:*
        │  404 (old image) → legacy SKILL.md write + multi_file_dropped_old_image
        │  restore failure → ONE repair retry: delete skill dir + re-restore
        │    (dir→file type transitions would wedge restore forever otherwise)
        │  prune = previous meta manifest − new manifest (cap 200/skill) via
        │    agent-server DELETE /api/files — platform deletes ONLY what it wrote
        │  injected-python execs (base64, zero shell interpolation of
        │    library-derived names): meta batch-read · chmod +x from git modes ·
        │    dep probe (shutil.which + env-key NAMES from process env ∪ .env) ·
        │    .gitignore per-skill lines + git rm --cached (auto-sync bloat guard)
        ▼
agent /home/developer/.claude/skills/<name>/  (+ .trinity-skill.json provenance)
```

## Key Behaviors

| Behavior | Mechanism |
|---|---|
| Idempotent start | agent meta `version` == library tree SHA → `unchanged`, no transfer (dep check still runs so CLAUDE.md annotations stay fresh) |
| Deleted library files propagate | manifest diff prune on next inject |
| Agent runtime files survive | prune only touches previous-manifest paths — `__pycache__`, downloaded models, agent notes untouched |
| Same-named agent-authored dir | no meta → overwrite-only + `unmanaged_dir_overwritten`, never pruned |
| Repo bloat guard | injected names appended to agent's `.gitignore` + untracked, so the 15-min auto-sync (which deliberately commits `.claude/`) never commits platform packages (#1595/#1596 class); Playbooks keep committing |
| Concurrency | Redis `skill_inject:{name}` SETNX+TTL fail-open lock via the shared `redis_breaker_util.SingleFlightLock` (#1920; injected `_redis_client`, `_acquire_inject_lock` still raises `SkillInjectionBusy` on contention) — outside `agent:*` (`compat_fix` precedent); manual inject → 409, start path → skip |
| Caps | `SKILL_MAX_BYTES` (10 MiB) / `SKILLS_TOTAL_MAX_BYTES` (50 MiB), env-tunable; over-cap → named error, other skills continue |

## Result Contract (honest per skill)

```json
{
  "success": true,
  "skills_injected": 2, "skills_unchanged": 17, "skills_failed": 0,
  "results": {
    "clip-video": {
      "success": true, "status": "injected", "files_written": 7,
      "warnings": ["missing_binary:ffmpeg", "packages_not_checked"]
    }
  }
}
```

`status` ∈ `injected | unchanged | fallback | failed`. Warning codes:
`missing_binary:*`, `missing_env:*`, `packages_not_checked`, `dep_check_skipped`,
`skill_too_large` (error), `symlink_skipped:*`, `protected_name_skipped:*`,
`restore_skipped:*`, `stale_delete_failed:*`, `prune_truncated`,
`unmanaged_dir_overwritten`, `repair_reinjected`, `multi_file_dropped_old_image`,
`frontmatter_invalid`, `invalid_skill_name`, `gitignore_update_failed`,
`finalize_partial:*`.

## Frontmatter Contract

Parsed by `skill_packaging.parse_frontmatter`/`extract_contract` (64 KiB cap,
alias-refusing SafeLoader — billion-laughs guard, #919 convention; every field
isinstance-guarded; unknown keys ignored — skills are authored for Claude Code
first). Flat keys are the convention; a `trinity:` mapping wins when present:

```yaml
---
description: Cut and publish clips
automation: gated            # surfaced, not enforced (trinity#518 input)
user_invocable: true         # default true
allowed-tools: Bash, Read
requires:
  packages: [pillow]         # surfaced; NOT probed in v1 (packages_not_checked)
  binaries: [ffmpeg]         # probed via shutil.which at injection
  env: [ELEVENLABS_API_KEY]  # probed by NAME (process env ∪ .env keys); values never read
---
```

Dep names are regex-gated (`binaries ^[A-Za-z0-9._+-]+$`, `env ^[A-Z][A-Z0-9_]*$`)
before probing — library-derived strings never reach a shell (all in-container
work runs as base64-injected python, the compatibility-collector idiom).
Provisioning (installing deps) is Phase 2 — placement / skill-runner
(trinity-enterprise#139).

## CLAUDE.md Section

Rebuilt from **all** assigned skills present on the agent (`unchanged`
included — a 1-of-19-changed start must not shrink the section), annotated:

```markdown
## Platform Skills

- `/clip-video` - Use with /clip-video command — ⚠ missing: ffmpeg
- `/verification` - Use with /verification command
```

Section replacement is line-anchored (`^## Platform Skills$`) so an
`### Platform Skills`-prefixed heading elsewhere is never clobbered.

## Error Handling

| Error Case | Result |
|---|---|
| Agent not running / transport down | per-skill `failed` (repair retry first); start-path re-inject self-heals |
| Skill not in library | per-skill `Skill not found in library` |
| Traversal-shaped skill name | per-skill `invalid_skill_name` (ONE guard in get_skill/assign/inject: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, not `.`/`..`) |
| Restore endpoint 404 (pre-#384 image) | `fallback` status: SKILL.md only + `multi_file_dropped_old_image` (multi-file skills only) |
| Empty package (no committed regular files) | per-skill `failed` |
| Concurrent injection | `SkillInjectionBusy` → REST 409 / start-path skip |
| Dep probe unavailable | `dep_check_skipped` (fail-open, injection proceeds) |

## Security Considerations

1. **No new endpoints** — transfer rides the existing authed agent-server restore (per-agent HMAC token #1159); allowlist + traversal re-checked agent-side (defense in depth).
2. **Symlinks never ship** — dropped at build with `symlink_skipped` (library symlink at e.g. `/data/trinity.db` must not be resolvable into a tar).
3. **Protected basenames** (`.env`, `CLAUDE.md`, `.mcp.json`, `.gitignore`, …) refused at build — restore could write them while prune could never remove them.
4. **Zero shell interpolation** of library content — injected-python scripts with embedded JSON args for every in-container step.
5. **Provenance spoof-proof** — a library-committed `.trinity-skill.json` is dropped; only the generated meta ships. (An agent can still edit its own meta to pin a stale skill — self-harm only, repaired by any forced sync.)
6. **Env checks report key NAMES only** — values never enter results, logs, or CLAUDE.md.

## Testing

`tests/unit/test_ent183_skill_packages.py` (42 tests): contract-parse matrix
(alias bomb, garbage `requires`, malicious dep names), archive vetting, tar
round-trip against the REAL `restore_from_tar`, real-git end-to-end
(repo → archive → filter → restore, tree-SHA determinism across clones),
orchestration (skip-vs-force, 404 fallback, repair path, manifest prune,
unmanaged-dir guard, caps, dep warnings, lock contention, CLAUDE.md rebuild).

## Related Flows

| Flow | Relationship |
|------|--------------|
| **Upstream**: [skills-library-sync.md](skills-library-sync.md) | Library must be synced; sync invalidates the list cache |
| **Upstream**: [skill-assignment.md](skill-assignment.md) | Assignment selects what to inject |
| **Downstream**: [agent-lifecycle.md](agent-lifecycle.md) | Start-path injection (`force=False`) |
| **Related**: [github-sync.md](github-sync.md) | Injected dirs are gitignored/untracked against the auto-sync loop |
| **Related**: [playbooks-tab.md](playbooks-tab.md) | Agent-local skills — same directory, agent-authored, never pruned |

---

## Revision History

| Date | Change |
|------|--------|
| 2026-08-04 | **trinity-enterprise#332 per-source skills root**: source layout resolvable per source (`catalog.yaml` `skills_root:` → evidence-gated `skills/` probe → `.claude/skills/` fallback; segment-wise validation, ent#314 hardened parse, lstat/containment guards, dual-layout keeps legacy + `layout_conflict`); `filter_skill_archive(source_root=…)` rewrites arcnames to the canonical agent-side destination so manifests/prune/removal stay destination-canonical with zero migration. Requirements §21.1.4. |
| 2026-07-29 | **trinity-enterprise#236 lifecycle automation**: removal-on-unassign (`remove_skills` + `compute_removal`, manifest-driven, same inject lock), start-path reconciliation with a blast-radius refusal, and fleet-wide re-inject after a commit-changing library sync. See also [skills-library-sync.md](skills-library-sync.md) for the scheduled sync. |
| 2026-07-19 | **trinity-enterprise#183 full-directory packages**: git-archive tar source, agent-server restore transport, tree-SHA versioning + `.trinity-skill.json`, manifest prune, frontmatter contract + declaration-only dep check, honest per-skill warnings, gitignore/untrack guard, repair path, injection lock. Replaces the single-file `write_file`-per-skill design. |
| 2026-01-25 | CLAUDE.md "Platform Skills" section; initial documentation |
