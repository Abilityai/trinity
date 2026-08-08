# CSO Audit — ent#237 multi-source skills library (diff-scoped)

**Skill**: cso v1.1 · **Mode**: daily (8/10 confidence gate) · **Scope**: `--diff`
**Date**: 2026-08-04
**Branch**: `feature/ent-237-multi-source-skills` @ merge-base `3f1d4c89` (origin/dev)
**Diff**: 27 files, +3626/−746

---

## Phase 0 — Architecture model (diff-scoped)

The diff replaces a single admin-configured `skills_library_url` system setting with a
**multi-source model**: a `skill_sources` table, per-source git checkouts under
`/data/skills-library/<source_id>/`, and a merge step that resolves name collisions by
priority (custom-wins). Sources are registered by admins and cloned with the platform
GitHub PAT spliced in for private repos.

The security-relevant property is that **this subsystem executes third-party code**.
Skills carry `scripts/` (ent#183) that the ent#139 runner executes inside agent
containers, and ent#236 makes sync + fleet-wide re-injection automatic and unattended.
The trust boundary is therefore *which commit* each source resolves to — which is what
AC#5's tag pinning exists to control.

New trust boundary introduced by this diff: **the bundled community source**, a repo
that accepts public PRs, seeded by default on fresh installs.

## Phase 1 — Attack surface delta

| Surface | Count | Notes |
|---|---|---|
| New endpoints | 5 | `GET/POST /skills/sources`, `PUT/DELETE /skills/sources/{id}`, `POST /skills/sources/{id}/sync` — all `require_admin` |
| New outbound network | 1 class | `SkillSourceClone` — `git clone/fetch/reset/checkout` against an admin-supplied URL |
| New DB table | 1 | `skill_sources` (dual-track: SQLite + Alembic) |
| New config | 3 | `TRINITY_DEFAULT_SKILL_SOURCE{,_REF}`, source name |
| New unauthenticated surface | **0** | — |

---

## Findings

```
#   Sev    Conf    Status      Category          Finding                                        Phase    File:Line
──  ────   ─────   ────────    ────────          ───────                                        ─────    ─────────
1   HIGH   10/10   FIXED       Supply Chain      Tag pin bypassed on the clone path             P9/A08   skill_source_clone.py:90
```

### [1] HIGH · 10/10 · VERIFIED (exploit executed) · FIXED in this branch

**Tag pinning is enforced only on the update path; a fresh clone adopts a moved tag silently.**

`services/skill_source_clone.py:90`

```python
if (self.path / ".git").exists():
    result = self._update(expected_sha)     # pin enforced here
else:
    result = self._clone(auth_url)          # ...and nowhere on this path
```

`_update_tag` enforces the pin two ways — a `fetch` without `--force` (git refuses to
clobber an existing tag ref) and an explicit comparison against the recorded SHA — and
**both are properties of an existing checkout**. The clone path received no
`expected_sha` and performed no comparison.

Its own docstring claimed otherwise: *"the explicit SHA comparison below is the belt to
that suspenders — it catches the same condition on a fresh clone where no local tag ref
exists yet to conflict."* The fresh-clone path never reaches that code.

**Exploit scenario** (executed, not reasoned):

1. A source is pinned to tag `v0.1.0`; it syncs and records SHA `A`.
2. The local checkout is lost. This is routine, not exotic — `_quarantine_non_repo_dir`
   in this same class *renames the directory away* whenever it finds a non-repo path, and
   a restored `/data` backup or a recreated volume does the same.
3. Upstream force-moves `v0.1.0` onto a commit adding an executable. For the bundled
   community source this means a maintainer or a repo compromise; the repo accepts
   public PRs.
4. Next sync: `.git` is absent → clone → the moved tag is adopted.

Observed output before the fix:

```
re-sync WITH expected_sha=6f203e2e5b70: success=True moved_tag=None action=cloned
commit now: 6f1f727c2189  (pinned was 6f203e2e5b70)
>>> BACKDOOR ON DISK: True
```

**Impact.** Sync reports success with a changed commit, so `commit_changed` goes true and
ent#236's fleet re-inject pushes the moved tag's `scripts/` to **every running agent**,
unattended. This is precisely the scenario AC#5 exists to prevent, and it defeats the
control silently — the operator sees a successful sync.

**Why it went unnoticed.** `test_moved_tag_is_refused_and_payload_never_lands` clones
*before* moving the tag, so it can only ever exercise the update path. It passed
throughout.

**Fix applied** — `_refuse_moved_pin_after_clone`: verify the resolved HEAD against the
recorded SHA after a fresh clone, and **delete the checkout** on refusal (a failed sync
that leaves the tree on disk still serves it to `list_skills` and to injection). No
`expected_sha` (genuine first sync) is untouched. Regression test added that fails
without the fix; a companion test pins that the legitimate first clone still works.

---

## Clean / not-a-finding (verified, not assumed)

| Check | Evidence |
|---|---|
| **A10 SSRF** | `validate_skills_library_url` allowlists `{github.com, www.github.com}`, enforced on create (`skills.py:461`), on update (`:504`), **and re-validated at sync** (`skill_service.py:381`) — so a row written before a hardening, or by direct DB write, is still caught |
| **A03 command injection** | `_REF_RE = ^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$` forces an alphanumeric first char, so no `--option` ref; `--` separator precedes the URL in `clone`; all args list-form, no `shell=True`. Parametrized guard tests cover `--upload-pack=evil` and traversal |
| **A03 SQL injection** | `db/skill_sources.py` is SQLAlchemy Core throughout (8 `select/insert/update/delete` statements); the only f-strings build a `secrets.token_hex` id and error text |
| **A01 access control** | All 5 routes `require_admin`; the 4 that mutate **plus the LIST read** carry `reject_agent_principal` — correct, since an agent-scoped key resolves to its owner *carrying the owner's role* (ent#293) and the rows disclose private repo URLs. Sync stays role-only by documented decision (use, not grant) |
| **Credential exposure** | The PAT-spliced URL is never logged or returned; every git stderr passes `redact()`; the durable status error passes `_scrub_pat` + a 500-char cap before reaching `system_settings` |
| **Host confusion** | `_authenticated_url` decides the splice by **parsing** the host against the allowlist, not by substring — `https://evil.com/?x=github.com` cannot attract the PAT |
| **Embedded credentials** | `reject_embedded_credentials` on create and update, so a `https://<token>@host` URL cannot be stored |
| **P2 secrets** | No secret-shaped literals in the diff; the two `ghp_` hits are `ghp_placeholder` inside a test asserting the PAT does **not** leak |
| **P3 supply chain** | No dependency changes |
| **Timeouts** | Every `subprocess.run` carries an explicit timeout (clone 120s, fetch 60s, quick 30s) |
| **Fresh-install seed** | Fail-safe (never raises — `init_database` runs at import), idempotent, seeds a **row** not a read-time default, and is tag-pinned |

## Not audited (out of `--diff` scope)

Phases 4–8 (CI/CD, infrastructure, skill supply chain in `.claude/skills/`, LLM surfaces)
were not re-run — this diff changes none of those surfaces. The pre-existing ent#293
finding (agent-scoped key satisfies `assert_admin`) remains open and is **mitigated on
this diff's routes** by `reject_agent_principal`.

## Trend

| | |
|---|---|
| Prior audit of this surface | `cso-diff-2026-07-29-ent236-skills-lifecycle.md` |
| Resolved since | [1] ent#236's automation on-switch — this diff keeps `reject_agent_principal` on the source mutations |
| Persistent | ent#293 (pre-existing, tracked separately) |
| New | 1 (fixed before merge) |
| Direction | **improving** — the new finding was caught and closed pre-merge, with a regression test |

## Incident response

Not applicable — no credential was leaked. The supply-chain finding was fixed before the
branch was pushed; no instance ever ran the vulnerable code, so no revocation or
history scrub is required.
