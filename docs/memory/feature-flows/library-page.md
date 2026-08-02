# Feature: Library Page

## Overview
The Library page (`/library`, trinity-enterprise#263 — formerly the Templates page at `/templates`) is the single surface for **installable assets**: an **Agent Templates** section (browse Starter/GitHub templates, create agents) and a **Skills** section (fleet-level browse over the shared skills library). The legacy `/templates` path redirects (query **and** hash preserving), so old bookmarks and deep links keep working.

Naming rule (the ent#263 AC#4 reading): page **identity** is Library — nav label, route path/name, `meta.title`, page h1, e2e title assertions. The word "template" survives inside the page as the **asset-kind noun** (Starter/GitHub Templates section headers, Use Template buttons, the `GET /api/templates` API).

## User Story
As a platform user, I want one place to browse everything installable on my fleet — agent templates and skills — so library-shaped features stop scattering across pages, and so I can see the skills library's sync state honestly without opening an individual agent.

## Entry Points

### Navigation
- **Primary**: NavBar link **Library** → `/library` (active state via `$route.path.startsWith('/library')` so future `/library/...` deep links keep the tab lit)
- **Legacy**: `/templates` → function-form redirect to `/library` carrying `query` AND `hash` (hash matters — the page uses `#agent-templates` / `#skills` in-page anchors)
- **In-page**: header jump anchors "Agent templates · Skills" (deliberately NOT `?kind=` filter pills — two disjoint section shapes, stacked)

### Routes
| Route | Component | Auth Required |
|-------|-----------|---------------|
| `/library` | `Library.vue` | Yes (`meta.title: 'Library'`) |
| `/templates` | redirect → `/library` | — |

```javascript
// src/frontend/src/router/index.js
{
  path: '/library',
  name: 'Library',
  component: () => import('../views/Library.vue'),
  meta: { requiresAuth: true, title: 'Library' }
},
{
  path: '/templates',
  redirect: to => ({ path: '/library', query: to.query, hash: to.hash })
}
```

The redirect is function-form per the #1109 `/operating-room` precedent (a string/object redirect drops the incoming query), plus `hash` carry (the #1109 precedent drops hash — left as a named follow-up there).

---

## Frontend Layer

### Library.vue (`src/frontend/src/views/Library.vue`)
Renamed from `Templates.vue` via a **pure `git mv` commit** followed by a content commit (defuses the parallel-edit modify/delete conflict class; git rename detection holds). Page shell:

| Element | Purpose |
|---------|---------|
| h1 "Library" + subtitle | "Installable assets for your fleet — agent templates and skills" |
| Header jump anchors | `#agent-templates` · `#skills` |
| `<section id="agent-templates">` | The whole former Templates page content (heading "Agent Templates"; inner Starter/GitHub/Custom headings demoted h2→h3) |
| `<section id="skills">` | `<LibrarySkillsSection />` |
| `CreateAgentModal` | Opened by "Use Template" / "Create Blank Agent" with `initial-template`; `onAgentCreated` navigates to `/agents/{name}` |

**Per-section failure isolation** is a page invariant: the templates fetch and the skills fetches are independent — each section owns its own loading/error/empty states, so a failure in one never blanks the other.

**Agent Templates section** (unchanged behavior from the Templates page): single `GET /api/templates` fetch (now via the shared `api` client — Invariant #7; previously raw axios with a hand-built auth header), split client-side into Starter (`source === 'local' || !source`) and GitHub (`source === 'github'`) grids, plus the Custom/Blank Agent card and a "No templates configured" empty state. The backend already excludes hidden fixtures (#1513). `CreateAgentModal.vue`'s single `/api/templates` call was migrated to the shared client in the same change, so no half-migrated consumer of the endpoint remains.

**GitHub-zero empty state (#1931).** `DEFAULT_GITHUB_TEMPLATE_REPOS` is now empty, so a default install has **zero** GitHub templates. The section previously wrapped everything in `v-if="githubTemplates.length > 0"` and therefore *vanished* — honouring §4.5's "empty states teach the next action" only by accident (it had never been zero before). It now renders on `v-if="!noTemplatesAtAll"` and branches internally: grid when there are entries, else a dashed-border placeholder card matching the Blank Agent idiom on the same page. Card content, in the order it is read — **marketplace first** (the operator decision): the `abilityai/abilities` marketplace + `create-agent` wizards as the *recommended* path, then a **Create from a GitHub repository** button for "I already have a repo" (both roles; wired to `useTemplate({ id: 'github-custom' })` — `CreateAgentModal`'s own sentinel for the free-form `owner/repo` option, explicitly exempted from that component's unknown-template reset — deliberately **not** `useTemplate(null)`, which is byte-identically the Blank Agent button), then a role-branched curation hint via `useRole()` (admin → `/settings?tab=agents`; non-admin → ask an admin), mirroring `LibrarySkillsSection.vue`'s convention on this same page.

**Precedence — the two empty states are mutually exclusive by construction.** `noTemplatesAtAll = templates.length === 0` is computed over the **whole** `/api/templates` response (local *and* github), and the page-level "No templates configured" block keeps its existing `v-if="templates.length === 0 && !loading"` **unchanged**:

| local | github | `templates.length` | page-level empty | GitHub section | GitHub inner |
|---|---|---|---|---|---|
| 3 | 0 | 3 | no | yes | **placeholder** ← the new default install |
| 3 | 6 | 9 | no | yes | grid |
| 0 | 6 | 6 | no | yes | grid |
| 0 | 0 | 0 | **yes** | no | — |

Exactly one empty state renders in every row. `githubTemplates ⊆ templates`, so `githubTemplates.length > 0` *implies* `!noTemplatesAtAll` — the section condition needs no second disjunct. The page-level hint's copy was also corrected (#1931): it pointed at `config.py`, which is not an operator surface and is now empty by design.

### LibrarySkillsSection.vue (`src/frontend/src/components/LibrarySkillsSection.vue`)
Fleet-level **browse** over the skills library — read-only discovery, NOT a second assignment path (strategy epic ent#182: one skill model, no parallel mechanisms). Assignment stays on each agent's Skills tab (PR #1877); cards link there via `/agents`.

**Sync-state header** — leads with what the disk knows:
- `commit_sha` (short) + `skill_count` + branch — disk-derived, reliable across uvicorn workers
- `last_sync` rendered **only when truthy** — it is per-worker in-memory state and reads `null` on the other worker / after restart; never render "Last synced: never"
- `cloned=false` renders "Configured — never synced"
- Repo URL: **admin-only**, **userinfo-stripped** (the clone path accepts and stores `https://user:token@host/...` verbatim), labeled **"Primary source"** (post-ent#237/PR #1901 the flat `url`/`branch` are just the first source in resolution order), and hidden entirely when `status.sources` reports more than one source
- Admin-only **Sync now** button (disabled while in flight; a concurrent-sync 400 surfaces honestly via `syncError`)

**Empty states (AC#5)** — driven by the store's fleet-scoped discriminator; each teaches the next action per viewer role:

| State | Condition | Render |
|-------|-----------|--------|
| `unconfigured` | `!status.configured` | admin → CTA to `/settings?tab=agents`; non-admin → "Ask your admin to configure a skills library." |
| `not_cloned` | `configured && !cloned` | "Configured but never synced" → admin Sync CTA |
| `empty` | `cloned && 0 skills` | "Add a skill directory to the repository, then Sync." |
| error | fetch failed | error text + retry — an error is never presented as an empty library (no-swallow rule) |

**Skill cards** render the ent#183 contract fields (`SkillInfo`): name, description, automation/invocability/package chips via the shared seam (below), `version` short-SHA, `requires` line, and an "Assign via an agent's Skills tab →" link. **Interpolation only** — no `v-html`, no `:href` bound to any library-derived string (skills come from a synced git repo — semi-trusted content). Dormant forward-compat slots render a `source_name` badge and a `shadowed_by` indicator when PR #1901's fields appear.

### stores/skillsLibrary.js (`src/frontend/src/stores/skillsLibrary.js`)
Fleet-scoped Pinia store, **deliberately separate** from the agent-scoped `stores/skills.js`: `App.vue` wraps AgentDetail in `KeepAlive`, so `SkillsPanel`'s `onUnmounted → clear()` never fires on nav-away (deactivated ≠ unmounted). Shared refs would let the Library page's writes — including a failed fetch's error — render inside the **cached** per-agent Skills tab (an agent suddenly "has no skills"). This module imports NOTHING from `stores/skills.js`; the ~40 duplicated fetch lines are deliberate and recorded.

| Member | Behavior |
|--------|----------|
| `load()` | `GET /api/skills/library/status`; then `GET /api/skills/library` **only if `configured`** (no-swallow: any other failure → `error`, never a confident empty) |
| `emptyReason` | computed 4-state discriminator (`unconfigured` / `not_cloned` / `empty` / null; error carried separately) |
| `sync()` | `POST /api/skills/library/sync` with a **180s** request timeout (a first clone can exceed api.js's 30s default); on `ECONNABORTED` → **re-fetch status instead of claiming failure** (client timeout ≠ server failure); on success → refetch status + list |

### Shared contract-chips seam (`src/frontend/src/components/skills/`)
Extracted from `SkillsPanel.vue` (ent#263) so the per-agent Skills tab and the Library browse render the ent#183 package facts from **one** seam and can't drift:
- `SkillContractChips.vue` — automation chip, "not user-invocable" chip, `file_count`/`size_bytes`, opt-in `showVersion` short-SHA
- `contract.js` — `formatBytes()`, `deps()` (declared binaries/packages/env as one display line), `stripUserinfo()` (credential scrub for rendered library URLs — adversarially tested against non-parseable shapes; used by both this section's Primary-source line and SkillsPanel's empty-state URL)

`SkillsPanel.vue` now consumes the seam (its local `SkillMeta`/`formatBytes`/`deps` removed); `stores/skills.js` untouched.

### NavBar (`src/frontend/src/components/NavBar.vue`)
Single desktop link: label **Library**, `to="/library"`, active check `$route.path.startsWith('/library')`. Nav order unchanged (Dashboard · Agents · Library · Operations · Settings).

---

## Backend Layer

**Zero backend change in ent#263.** The page composes existing read surfaces:

### Templates Router (`src/backend/routers/templates.py` — 2 endpoints)
| Endpoint | Purpose |
|----------|---------|
| `GET /api/templates` | List templates — admin-configured GitHub list (TMPL-001, `system_settings` key `github_templates`) or `config.py` defaults, plus curated local templates; hidden fixtures excluded (#1513); GitHub metadata cached 10 min/repo (#843); sorted by `priority` then `display_name` |
| `GET /api/templates/{template_id:path}` | Single template details |

(`POST /api/templates/refresh` and `GET /api/templates/env-template` no longer exist — the refresh icon on the page is a client refetch.)

### Skills read surfaces (`src/backend/routers/skills.py`)
| Endpoint | Auth | Used for |
|----------|------|----------|
| `GET /api/skills/library/status` | any auth user | sync-state header + empty-state discriminator (`{configured, url, branch, cloned, last_sync, commit_sha, skill_count}` — PR #1901 keeps these flat fields verbatim and adds `sources[]`) |
| `GET /api/skills/library` | any auth user | skill cards (`SkillInfo` contract, ent#183) |
| `POST /api/skills/library/sync` | admin | Sync now button |

Deep dives: [skills-library-sync.md](skills-library-sync.md), [skill-assignment.md](skill-assignment.md), [skill-injection.md](skill-injection.md).

---

## Security Considerations

1. **Authentication required**: route has `meta: { requiresAuth: true }`; all fetches via the shared `api` client (auth interceptor, 401 → `/login`).
2. **Source-URL disclosure**: library repo URLs are admin-sensitive (PR #1901 classes its `GET /api/skills/sources` admin-only for this reason). The Library renders the URL admin-only, strips embedded userinfo (`user:token@host` — the clone path stores credentialed URLs verbatim), and hides the single-source presentation when multiple sources exist.
3. **Semi-trusted library content**: skill names/descriptions/contract fields come from a synced GitHub repo — interpolation only, no `v-html`, no `:href` from library-derived strings.
4. **Sync is admin-only** server-side; the UI additionally hides the button for non-admins.
5. **No sensitive template data**: template list shows metadata only; PAT values never appear in API responses.

---

## e2e Coverage (`src/frontend/e2e/`)

| Spec | Assertion |
|------|-----------|
| `smoke.spec.js` `@smoke library page loads` | chrome-only anchors: `getByRole('heading')` for h1 "Library" + "Agent Templates" + "Skills" (CI has no configured skills library — the unconfigured empty state is the expected render; never assert on skill data, never `getByText(/library/i)`) |
| `smoke.spec.js` `@smoke templates path redirects to library` | `goto('/templates')` → URL matches `/library` |
| `browser-tab-titles.spec.js` | nav click on "Library" → title `Trinity — Library`; `/templates` redirect resolves to the same title |

CI runs admin-authenticated (`e2e/.auth/admin.json`), so the non-admin empty-state branches are honestly untested.

---

## Status
**Working** — shipped with ent#263 (2026-07-31). Follow-ups (named at ship): per-source display post-PR #1901; fleet-level assignment read (`GET /api/skills/assignments`).

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-08-01 | **#1931 — catalog honesty**: the 11 `dd-*` VC-demo templates go `hidden: true` (visible catalog 14 → 3: `sage`/`scout`/`scribe`), `DEFAULT_GITHUB_TEMPLATE_REPOS` emptied (6 → 0, so `GET /api/templates` makes no outbound GitHub calls on a cold cache), new marketplace-first GitHub-zero placeholder card + corrected page-level hint copy, and the demo fleet stays deployable as a set via the promoted `config/manifests/vc-due-diligence.yaml`. |
| 2026-07-31 | **ent#263 — Templates page → Library**: file renamed from `templates-page.md`; full rewrite. `/library` route + query/hash-preserving `/templates` redirect, NavBar rename, Agent Templates section (content preserved, fetch migrated to shared api client), new Skills fleet-browse section (`LibrarySkillsSection.vue` + `stores/skillsLibrary.js` + shared `components/skills/` chips seam), per-kind empty states, security notes. Previous revision documented the standalone Templates page (incl. the long-gone `AgentSubNav` and dead `env-template`/`refresh` endpoints). |
| 2026-03-04 | TMPL-001 configurable GitHub templates (historical — see git history of `templates-page.md`). |
| 2026-01-21 | Initial creation as `templates-page.md`. |

---

## Related Flows

- **Upstream**: [platform-settings.md](platform-settings.md) — TMPL-001 GitHub Templates configuration; skills library URL/branch config (rewritten by ent#237/PR #1901 into per-source management)
- **Downstream**: [template-processing.md](template-processing.md) — template processing during agent creation
- **Downstream**: [agent-lifecycle.md](agent-lifecycle.md) — agent creation and container initialization
- **Related**: [skill-assignment.md](skill-assignment.md) — per-agent Skills tab (assignment surface)
- **Related**: [skills-library-sync.md](skills-library-sync.md) — what Sync now actually runs
- **Related**: [credential-injection.md](credential-injection.md) — how template credentials are injected
