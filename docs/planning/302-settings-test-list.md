# Test List — #302 Settings Tabbed Refactor

> **Canon TDD step 1**: behavioral analysis before any code or test.
> Per Kent Beck — convert exactly one item at a time into a Playwright test,
> make it pass with minimal change, optionally refactor, then move to the next.
> Discovering a new behavior during implementation = add it to this list.

| Field | Value |
|---|---|
| Issue | https://github.com/Abilityai/trinity/issues/302 |
| Branch | `feature/302-settings-tabbed-layout` |
| Test infra | Playwright e2e (`src/frontend/e2e/settings-tabs.spec.js` — NEW) |
| Tag | `@interactive` (more than smoke; routing + role-gated visibility) |
| Methodology | [Canon TDD — Kent Beck](https://tidyfirst.substack.com/p/canon-tdd) |
| Permissions | ROLE-001 (4-tier: user < operator < creator < admin) |

## Behavior list (12 items)

Each row = one Red-Green cycle. Order matters — items 1–4 build the foundation
that 5–12 depend on. Don't skip ahead; each test stays red until the test
above it passes.

| # | Behavior | Audience |
|---|---|---|
| 1 | `/settings` loads with **default tab = General** for an admin user | admin |
| 2 | `/settings?tab=mcp-keys` deep-links into the MCP Keys tab | any |
| 3 | `/settings?tab=foobar` (unrecognised) falls back to default tab without crash | any |
| 4 | Admin sees all 5 tabs in the tab strip | admin |
| 5 | Non-admin (`user` role) sees ONLY the MCP Keys tab in the strip | non-admin |
| 6 | Non-admin loading `/settings` (no `?tab=`) lands on MCP Keys (their only tab) | non-admin |
| 7 | `/api-keys` redirects to `/settings?tab=mcp-keys` (HTTP 301-equivalent in SPA router) | any |
| 8 | NavBar no longer shows a "Keys" top-level link | any |
| 9 | Clicking a tab updates URL `?tab=` param without a full page reload | any |
| 10 | Browser **back / forward** navigates between tab states correctly | any |
| 11 | All 12 pre-existing sections remain functional in their new tabs (regression — covers Anthropic key save, GitHub PAT save, Slack connect, user role edit, agent quotas save, skills library URL, default avatar gen) | per existing role |
| 12 | MCP Keys tab — full CRUD works: create key, list keys, revoke key, delete key, MCP-config snippet visible | any |

## Tab→Section mapping (locked, from issue)

| Tab | Sections | Min role |
|---|---|---|
| **General** | Platform (Public URL), Trinity Prompt, Default Avatars | admin |
| **Access** | Email Whitelist, User Management, SSH Access | admin |
| **Integrations** | API Keys (Anthropic / GitHub PAT), Slack Integration, Claude Subscriptions | admin |
| **MCP Keys** | MCP API key CRUD + connection-info snippet (formerly `/api-keys` page) | user (any) |
| **Agents** | Agent Quotas, Skills Library, GitHub Templates | admin |

## Implementation notes (NOT tests — guides for the Green step)

- New components: `src/frontend/src/components/settings/{General,Access,Integrations,McpKeys,Agents}Tab.vue`
- `Settings.vue` becomes a thin shell: route guard (`authenticated` not `admin`), role-aware tab strip, `<keep-alive>` for tab content, URL `?tab=` <-> active tab two-way sync
- `useRole()` composable: thin wrapper around `authStore.user.role` with `hasMinRole(role)` helper using ROLE-001 hierarchy
- `authStore.role` getter: returns `state.user?.role || 'user'`
- Router: `/api-keys` route becomes a `redirect: { path: '/settings', query: { tab: 'mcp-keys' } }`
- `views/ApiKeys.vue` (573 lines) → contents moved to `McpKeysTab.vue`, file deleted at the end
- `NavBar.vue:58` → "Keys" link removed
- `architecture.md:1465` → doc reference updated to `/settings?tab=mcp-keys`

## Out of scope (defer)

- Vitest unit-test infra (separate PR if desired)
- Backend endpoint changes (`/api/settings/api-keys/anthropic/*` etc. — those API endpoints don't change, only the frontend route does)
- New role-based features (the existing per-section access stays the same; we're only re-organising layout)
- User-docs screenshot updates (`docs/user-docs/images/mcp-api-keys.png` — slight visual drift acceptable, can update post-merge)

## What we will NOT compromise on

- Backend `require_admin` on every admin endpoint stays — UI hiding is convenience, not security
- No regression: every existing button/save/delete that worked yesterday works tomorrow (item 11)
- No broken bookmarks: `/api-keys` redirect handles all external links forever (item 7)

## Sources

- [Canon TDD — Kent Beck](https://tidyfirst.substack.com/p/canon-tdd)
- [Test Driven Development — Martin Fowler](https://martinfowler.com/bliki/TestDrivenDevelopment.html)
