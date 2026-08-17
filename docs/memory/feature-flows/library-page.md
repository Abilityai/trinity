# Feature: Library Page

## Overview
The Library page (`/library`, trinity-enterprise#263 — formerly the Templates page at `/templates`) is the single surface for **installable assets**, presented as three tabs since ent#384: **Agent Templates** (browse Starter/GitHub templates, create agents), **Systems** (deploy a manifest, creator+ only) and **Skills** (fleet-level browse over the shared skills library, with the agents holding each skill). The legacy `/templates` path redirects (query **and** hash preserving), so old bookmarks and deep links keep working.

Naming rule (the ent#263 AC#4 reading): page **identity** is Library — nav label, route path/name, `meta.title`, page h1, e2e title assertions. The word "template" survives inside the page as the **asset-kind noun** (Starter/GitHub Templates section headers, Use Template buttons, the `GET /api/templates` API).

## User Story
As a platform user, I want one place to browse everything installable on my fleet — agent templates and skills — so library-shaped features stop scattering across pages, and so I can see the skills library's sync state honestly without opening an individual agent.

## Entry Points

### Navigation
- **Primary**: NavBar link **Library** → `/library` (active state via `$route.path.startsWith('/library')` so future `/library/...` deep links keep the tab lit)
- **Legacy**: `/templates` → function-form redirect to `/library` carrying `query` AND `hash` (hash still matters — ent#263's `#agent-templates`/`#systems`/`#skills` anchors are now *migrated* to the matching tab and then cleared, so `/templates#skills` lands on the Skills tab)
- **In-page**: a tab strip — Agent Templates · Systems · Skills (ent#384; the ent#263 jump anchors are gone, and the legacy `#agent-templates`/`#systems`/`#skills` hashes migrate to the matching tab, see below)

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
| h1 "Library" + subtitle | "Installable assets for your fleet — agent templates, systems, and skills" (ent#126 added the third kind) |
| Tab strip (`OverflowTabs`) | `templates` · `systems` (creator+ only) · `skills`, addressed by `?tab=` (ent#384) |
| `<section id="agent-templates">` | The whole former Templates page content (heading "Agent Templates"; inner Starter/GitHub/Custom headings demoted h2→h3) |
| `<section id="systems">` | `<SystemInstallPanel />` — install a multi-agent system from a manifest (ent#126). Placed **between** templates and skills: it and Agent Templates both *install agents*, while Skills configures agents that already exist. **Role-gated** (`hasMinRole('creator')`, mirroring `POST /api/systems/deploy`) and hidden outright below that role — with its TAB gated alongside (ent#384), so the strip never points at an unrendered panel and a non-creator deep-linking `?tab=systems` resolves to the default tab |
| `<section id="skills">` | `<LibrarySkillsSection />` |
| `CreateAgentModal` | Opened by "Use Template" / "Create Blank Agent" with `initial-template`; `onAgentCreated` navigates to `/agents/{name}` |

**Per-section failure isolation** is a page invariant: the templates fetch, the systems catalog fetch and the skills fetches are independent — each section owns its own loading/error/empty states, so a failure in one never blanks the others.

**Tabs were considered and rejected in ent#263** — "disjoint section shapes that stack, and a tab strip would hide two-thirds of the page behind a click on a surface whose whole job is browsing what is installable" — and ent#126 conformed rather than reopening it. **ent#384 reversed that decision deliberately**, for the whole page rather than special-casing Skills: the page was carrying three sections of materially different size (the templates half alone is three grids), the Skills section grew a per-skill agent list, and a tabs-plus-stacked hybrid would have left the page with two competing models. A new asset kind is now a new entry in `visibleTabs` + a panel.

**Tab mechanics (ent#384).** `?tab=` is the address; `router.replace` on click, so switching tabs pushes no history entries (five clicks must not cost five Backs to leave). A `route.query.tab` watch makes the render follow the URL for deep links, external links and history entries — reading the query once at setup, `Operations.vue`'s shape, leaves those changing the address bar while the panel stays put. The Systems tab keeps its `hasMinRole('creator')` gate with **both** arms of a late-role watch, because `stores/auth.js` reports `user` until `/api/users/me` lands and a creator hard-loading `?tab=systems` would otherwise land on Templates and never recover. Panels **lazy-mount-once** (`v-if` visited + `v-show` active): plain `v-if` would refetch status+library+assignments on every switch — the #1109 teardown rationale does not apply, since `stores/skillsLibrary.js` owns no poll — while plain `v-show` would mount Skills and Systems for a Templates-only visitor; it also preserves `SystemInstallPanel`'s editor state.

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
Fleet-level **browse** over the skills library — read-only discovery, NOT a second assignment path (strategy epic ent#182: one skill model, no parallel mechanisms). Assignment stays a WRITE on each agent's Skills tab (PR #1877); cards link there via `/agents`.

**Per-skill "Assigned to" (ent#384).** Each card names the agents that already hold the skill — chips rendering `display_label || name`, linking to `/agents/{name}?tab=skills`, bounded with a counted "+N more" so a skill on sixty agents cannot grow the card without limit. Three states stay distinct in `components/skills/AssignedAgents.vue`: **could-not-load** (a retry, never silence), **nobody-holds-it**, and the list — an empty map and a failed fetch render identically if you only inspect the map, and showing the second as the first is the confident-wrong-zero this feature exists to remove. The zero wording follows the payload's `scope`: `all` (admin) reads "no agents yet", `accessible` reads "none of your agents", so a non-admin is never told a skill has no holders when they merely cannot see the ones it has. The per-card "Assign via an agent's Skills tab →" link stays on **every** card, not just the zero-holder one — the chips point at existing holders, which is the wrong direction for "give this to another agent".

**Orphaned assignments (ent#384).** Rows whose skill is no longer in the library get their own bounded list under the grid. ent#237's revocation model is "cut a new tag without the offending skill", after which the operator's first question is *who still has it* — and a grid keyed by the library listing answers that with silence, permanently.

The assign/unassign **writes** on this page were scoped out to ent#386; delivery semantics are ent#385. This surface must not claim, poll, or invent delivery status.

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

**Zero backend change in ent#263**; ent#384 added exactly one read (below). Otherwise the page composes existing read surfaces:

### Templates Router (`src/backend/routers/templates.py` — 2 endpoints)
| Endpoint | Purpose |
|----------|---------|
| `GET /api/templates` | List templates — the GitHub half resolves **admin-configured list (TMPL-001, `system_settings` key `github_templates`) → remote template registry (TMPL-002, trinity-enterprise#14) → `config.py` defaults (empty since #1931)**, and every registry failure degrades back to that floor (see [template-registry.md](template-registry.md)); plus curated local templates; hidden fixtures excluded (#1513); GitHub metadata cached 10 min/repo (#843); sorted by `priority` then `display_name` |
| `GET /api/templates/{template_id:path}` | Single template details |

(`POST /api/templates/refresh` and `GET /api/templates/env-template` no longer exist — the refresh icon on the page is a client refetch.)

### Skills read surfaces (`src/backend/routers/skills.py`)
| Endpoint | Auth | Used for |
|----------|------|----------|
| `GET /api/skills/library/status` | any auth user | sync-state header + empty-state discriminator (`{configured, cloned, branch, commit_sha, last_sync, skill_count}` + `sources[]`). **No `url`** — ent#334 put a `response_model` allow-list on this route: it is open to every authenticated caller including agent-scoped keys, while source repo URLs are admin-sensitive and served only by the admin-gated `GET /api/skills/sources`. The per-source `url` and `last_error` are withheld for the same reason (`last_error` is git failure text, which echoes the PAT-spliced clone URL). `configured`/`cloned` are the load-bearing pair — the empty-state discriminator derives from them |
| `GET /api/skills/library` | any auth user | skill cards (`SkillInfo` contract, ent#183) |
| `POST /api/skills/library/sync` | admin | Sync now button |
| `GET /api/skills/assignments` | any auth user, **human-only** | the per-skill "Assigned to" rows + the orphaned-assignments list (ent#384) |

Deep dives: [skills-library-sync.md](skills-library-sync.md), [skill-assignment.md](skill-assignment.md), [skill-injection.md](skill-injection.md).

---

## Security Considerations

1. **Authentication required**: route has `meta: { requiresAuth: true }`; all fetches via the shared `api` client (auth interceptor, 401 → `/login`).
2. **Source-URL disclosure**: library repo URLs are admin-sensitive (PR #1901 classes its `GET /api/skills/sources` admin-only for this reason). The Library renders the URL admin-only, strips embedded userinfo (`user:token@host` — the clone path stores credentialed URLs verbatim), and hides the single-source presentation when multiple sources exist.
3. **Semi-trusted library content**: skill names/descriptions/contract fields come from a synced GitHub repo — interpolation only, no `v-html`, no `:href` from library-derived strings.
4. **Sync is admin-only** server-side; the UI additionally hides the button for non-admins.
5. **No sensitive template data**: template list shows metadata only; PAT values never appear in API responses.
6. **Skill-assignment disclosure (ent#384)**: `GET /api/skills/assignments` is access-scoped — admin unfiltered, everyone else owned ∪ shared. Unscoped it is a fleet-wide agent-name enumeration oracle for any authenticated `role=user` (the Invariant #8 class already called out for `GET /api/subscriptions`), and what it discloses even inside that grant is a *capability map* of the fleet. It carries `reject_agent_principal` (an agent-scoped key resolves to its owner carrying the owner's role — ent#293 — and there is deliberately no agent consumer) and a `response_model` allow-list naming `name` + `display_label` only, so a future sensitive `agent_ownership` column is fail-closed (the ent#334 rule). The accessible set is derived from `db.get_all_agent_metadata()` rather than `accessible_agent_names`: the latter resolves through `list_all_agents_fast()`, which returns `[]` on any Docker fault, and would report "no agent holds any skill" to every non-admin behind a throttled WARNING.

---

## e2e Coverage (`src/frontend/e2e/`)

| Spec | Assertion |
|------|-----------|
| `smoke.spec.js` `@smoke library page loads` | chrome-only anchors: h1 "Library" + the default tab's "Agent Templates" heading, then a click on the **Skills tab** reveals its heading and the URL carries `?tab=skills`. Only the ACTIVE tab's heading is on screen since ent#384 (CI has no configured skills library — the unconfigured empty state is the expected render; never assert on skill data, never `getByText(/library/i)`, and never assert tab labels at a narrow viewport, where `OverflowTabs` collapses them behind "More ▾") |
| `smoke.spec.js` `@smoke library skills tab is deep-linkable` | cold load of `/library?tab=skills` lands on Skills, not the default tab (ent#384) |
| `smoke.spec.js` `@smoke library legacy section anchor resolves to its tab` | `/library#skills` → Skills tab + `?tab=skills`; the ent#263 anchors and the `/templates` redirect that preserves them keep working (ent#384) |
| `smoke.spec.js` `@smoke templates path redirects to library` | `goto('/templates')` → URL matches `/library` |
| `browser-tab-titles.spec.js` | nav click on "Library" → title `Trinity — Library`; `/templates` redirect resolves to the same title |

CI runs admin-authenticated (`e2e/.auth/admin.json`), so the non-admin empty-state branches are honestly untested.

---

## Status
**Working** — shipped with ent#263 (2026-07-31); tabbed + per-skill assignment read since ent#384 (2026-08-12). Open follow-ups: per-source display post-PR #1901; assign/unassign **writes** from this page (ent#386); delivery-on-assign (ent#385).

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-08-12 | **ent#384 — tabs + per-skill assignment read**: the three stacked sections become an `OverflowTabs` strip (`?tab=` addressed, legacy `#`-anchors migrated then cleared, creator-gated Systems tab with both arms of a late-role watch, lazy-mount-once panels), **deliberately reversing** ent#263's stacked-sections decision for the whole page. Each skill block lists the agents holding it via the new access-scoped `GET /api/skills/assignments` — the "fleet assignment read" this doc named as a follow-up at ent#263 ship — plus a bounded orphaned-assignments list for skills revoked from the library. Assignment stays a write on the per-agent Skills tab; the Library writes were split to ent#386. **OSS-core by decision.** |
| 2026-08-04 | **ent#14 — remote template registry**: the `GET /api/templates` row now names the three-tier ladder (admin list → registry → bundled defaults) instead of claiming the GitHub half is admin-list-or-`config.py`. **Zero page change** — registry entries are `source: "github"` and land in the existing grid, which is what keeps this page (and MCP `list_templates`) untouched. |
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
