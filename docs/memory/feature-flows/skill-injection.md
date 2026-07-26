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

## Architecture

```
/data/skills-library (git clone)
        │  git ls-tree HEAD .claude/skills/        → per-skill TREE SHA (= version)
        │  git archive HEAD -- .claude/skills/<n>  → atomic tar source
        ▼
services/skill_packaging.py (pure)
        │  filter_skill_archive: REGTYPE only (symlinks NEVER ship — build-side
        │    exfiltration guard + agent-side KeyError guard), litter + protected
        │    basenames dropped w/ warnings, caps enforced
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
| Concurrency | Redis `skill_inject:{name}` SETNX+TTL fail-open lock (outside `agent:*` — `compat_fix` precedent); manual inject → 409, start path → skip |
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
| 2026-07-19 | **trinity-enterprise#183 full-directory packages**: git-archive tar source, agent-server restore transport, tree-SHA versioning + `.trinity-skill.json`, manifest prune, frontmatter contract + declaration-only dep check, honest per-skill warnings, gitignore/untrack guard, repair path, injection lock. Replaces the single-file `write_file`-per-skill design. |
| 2026-01-25 | CLAUDE.md "Platform Skills" section; initial documentation |
