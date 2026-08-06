# CSO Audit — `--diff` — ent#332 per-source skills root

**Date**: 2026-08-04 · **Mode**: daily (8/10 gate) · **Scope**: `--diff`
**Branch**: `feature/ent-332-per-source-skills-root` → `feature/ent-237-multi-source-skills`
**Merge-base**: `0bd6a8c5` · **Diff**: 9 files, +354/−30
**Phases**: 0, 1, 2, 7, 9, 12, 13, 14

## Verdict

**Zero findings introduced by this diff.** One pre-existing MEDIUM (conditionally HIGH)
was surfaced while scanning the diff's blast radius; it is inherited from `dev` and
belongs to the ent#237 branch, not this PR.

| # | Sev | Conf | Status | Introduced here? | Category | Finding | File |
|---|-----|------|--------|------------------|----------|---------|------|
| 1 | MED (→HIGH conditional) | 9/10 | VERIFIED | **No** — inherited from `dev` | Access Control / Credential Disclosure | Skills-source URLs returned to any authenticated principal, incl. agent keys; legacy adoption can launder an embedded PAT into that field | `routers/skills.py:154` |

## New attack surface introduced by this diff

Exactly one: **`catalog.yaml` at a skills-source repo root** — author-controlled repo
content, same trust tier as SKILL.md frontmatter. Its `skills_root` value reaches four
sinks, each independently bounded:

| Sink | Guard | Evidence |
|---|---|---|
| Filesystem path joins | Segment-wise validation + lstat symlink refusal + realpath containment against the **realpath'd** base | `skill_source_clone.py:96-107, 437-460, 503-520` |
| git argv (`ls-tree`, `archive`) | `--` separator at every call site; leading `-` rejected by validation | `skill_source_clone.py:254, 556, 581` |
| Tar prefix math | Defensive re-validation at the packaging layer; `ValueError` on anything unsafe | `skill_packaging.py:243-249` |
| Display strings (`path`, status) | Charset-restricted `[A-Za-z0-9._-]` per segment; no `v-html` on any skills surface | `skill_source_clone.py:83` |
| Parse | ent#314 hardened loader, `AliasPolicy.REJECT`, bounded `read(cap+1)` (a post-read length check is defeated by `catalog.yaml -> /dev/zero`) | `skill_source_clone.py:377-398` |

**Actively verified** (not merely reasoned): an attack corpus of traversal-shaped member
names (`skills/x/../../../../etc/passwd`, `skills/x/../../.env`, `skills/x/a/../../../outside`)
produced **zero escapes** from the destination prefix, and every unsafe `source_root`
(`../../etc`, `skills/../..`, `.`, `-oProxyCommand=x`, `/etc`, `a//b`) raised `ValueError`
rather than silently mis-prefixing. Protected basenames (`.env`, `CLAUDE.md`,
`.trinity-skill.json`) are still dropped under a custom root — the check runs on the
pre-rewrite relative path, so the rewrite cannot bypass it.

**Agent isolation intact**: the author-controlled root never reaches an in-container exec.
All five in-agent scripts hardcode `~/.claude/skills` (`skill_service.py:1042, 1076, 1191,
1212, 1276`); because `filter_skill_archive` rewrites arcnames to the canonical
destination, the restore `paths` allowlist, the manifest, prune confinement and the
`.gitignore` lines stay destination-canonical regardless of source layout.

## Finding 1 — skills-source URL disclosure (pre-existing, NOT this PR)

**Severity**: MEDIUM unconditionally · **HIGH** on any install meeting the precondition
**Confidence**: 9/10 · **Status**: VERIFIED by independent fresh-context refutation (6 attempts, all failed)
**Introduced by**: `dev` (pre-ent#237). ent#237 widens one URL into an N-source array and
launders the un-validated legacy value into a durable row. ent#332 touches neither.

**Evidence**:
- `routers/skills.py:154` — `@router.get("/skills/library/status")` /
  `async def get_library_status(current_user: User = Depends(get_current_user))`.
  No `require_admin`, no `reject_agent_principal`, no `response_model`. Its own siblings
  on the same router *are* hardened: `/skills/library/{name}` calls
  `reject_agent_principal` (`:184`), `/skills/sources` is `require_admin` (`:427`).
- `skill_service.py:891` — `"url": src.url` per source; `:942` the legacy flat `url`.
  No userinfo stripping on any reader (`_scrub_pat` is applied only to sync *error*
  strings, `:638-648`).
- `skill_service.py:560` — `_adopt_legacy_clone` calls
  `db.create_skill_source(url=url, …)` with **neither** `reject_embedded_credentials`
  nor `validate_skills_library_url`, both of which the POST/PUT source routes apply
  (`routers/skills.py:457,461,500,504`).
- `utils/url_validation.py:31` documents that credential-rejection was deliberately kept
  out of `validate_skills_library_url` "so an install relying on an embedded token for
  private-repo access would break on upgrade" — the codebase itself asserts such installs
  plausibly exist. Empirically confirmed: that validator returns a PAT-bearing URL
  unchanged (it checks `parsed.hostname`, which ignores userinfo).
- `src/mcp-server/src/tools/skills.ts:192` — `get_skills_library_status` has no
  `canAccess`/`connectorDenied` gate, so it is advertised to every non-connector agent key.

**Exploit path**: operator configures `skills_library_url` with an embedded PAT (accepted
without complaint) → any sync, including the unattended ent#236 auto-sync, adopts it
verbatim into `skill_sources` and deletes the legacy setting → any authenticated
principal (a `user`-role chat-only grantee from `/share`, or any non-ephemeral
agent-scoped MCP key reached by prompt injection in any agent workspace) calls
`GET /api/skills/library/status` and reads the PAT in cleartext.

**Unconditional lesser variant**: even with no PAT, private skills-repo URLs (org/repo
names) are disclosed to every authenticated principal including agents.

**Recommended fix** (belongs on the ent#237 branch — still unmerged, so this is the cheap
moment):
1. Add `require_admin` + `reject_agent_principal` to `/skills/library/status`, **or**
   strip userinfo from `url` in `get_library_status` (`skill_service.py:891, 942`).
2. Call `reject_embedded_credentials` in `_adopt_legacy_clone` and either refuse adoption
   or strip the userinfo before `create_skill_source` (`skill_service.py:560`).

## Clean categories

- **P2 Secrets** — zero secret-shaped tokens on added lines (word-boundary scan for
  `sk-`/`ghp_`/`gho_`/`github_pat_`/`xox[bp]-`/`AKIA`/`PRIVATE KEY`). No `.env` or CI config touched.
- **P9 A01 Access Control** — zero endpoints, auth dependencies or route decorators added
  or altered; `routers/skills.py` untouched by this diff.
- **P9 A03 Injection / traversal** — see the actively-verified table above.
- **P9 A08 Integrity** — ent#314 consolidation guard green (21 passed); no new bare
  `yaml.safe_load` on author-controlled content.
- **P9 A03 XSS** — no `v-html` on skills surfaces; new fields are auto-escaped interpolation.
- **P5 Vendored parity (Invariant #5)** — zero `docker/base-image` files touched.
- **P9 A09 Logging** — author-controlled values logged via `%r` (repr-escaped, defeating
  newline log-injection), bounded by the 64 KB catalog cap. No credential material logged.

## Below the confidence gate (not findings)

- `skills_root` echoed into logs via `%r` without truncation — bounded by the 64 KB catalog
  cap and repr-escaped. Hardening nicety. 3/10.
- `skill.path` now carries a source-repo-relative directory name to MCP `list_skills`
  callers — not a URL, charset-restricted, discloses only a directory name in an
  operator-configured repo. 3/10.

## Trend

| | |
|---|---|
| **Prior report** | `cso-diff-2026-08-04-ent237-multi-source-skills` (1 HIGH) |
| **Resolved** | HIGH 10/10 — tag pin enforced only on the update path. Still fixed (`_refuse_moved_pin_after_clone` at `skill_source_clone.py:169`; 7 pin tests pass). |
| **Persistent** | none |
| **New** | 1 MEDIUM — inherited from `dev`, surfaced by blast-radius scanning |
| **Direction** | **Improving** — prior HIGH resolved and holding; this diff introduces zero findings |

---
*AI-assisted scan, not a substitute for professional penetration testing.*
