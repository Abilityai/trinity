import { test, expect } from '@playwright/test'

// Spec tag convention (#556 follow-up):
//   @smoke       — must always pass; runs in CI on every `ui`-labelled PR.
//   @visual      — visual regression / screenshot baselines; CI runs only
//                  once cross-platform baselines exist (#596).
//   @interactive — exercises forms, modals, multi-step flows; expensive,
//                  usually local-only until the test is stabilised.
//
// CI runs `npm run test:e2e:smoke` (filters by @smoke). Locally,
// `npm run test:e2e` runs everything.
test.describe('smoke', () => {
  test('@smoke dashboard renders for authenticated admin', async ({ page }) => {
    await page.goto('/')
    // Top nav has Dashboard, Library, Operations, Settings (+ gated
    // Enterprise/Sessions). The Agents entry was retired in
    // trinity-enterprise#260 (now the Dashboard's List mode); Templates was
    // renamed to Library in ent#263; Health/Ops/Executions merged into
    // Operations (#1109); Keys link removed in #302 — MCP keys now live in
    // Settings → MCP Keys tab.
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('link', { name: 'Library', exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Settings', exact: true })).toBeVisible()
  })

  test('@smoke /agents redirects to the Dashboard List mode', async ({ page }) => {
    // trinity-enterprise#260: the standalone Agents page is retired; the old
    // URL redirects to /?view=list, which the Dashboard applies (one-shot,
    // non-persisting) and then strips from the URL.
    await page.goto('/agents')
    // Bare root — the ?view=list intent param is stripped after being applied.
    await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/, { timeout: 10000 })
    await expect(page.locator('[data-agent="trinity-system"]')).toBeVisible({ timeout: 15000 })
  })

  test('@smoke operating room page loads', async ({ page }) => {
    // #1109/#1134: /operating-room is a legacy redirect to /operations.
    // Navigating the old path also covers the redirect itself.
    await page.goto('/operating-room')
    await expect(page).toHaveURL(/\/operations/, { timeout: 10000 })
    await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible({ timeout: 10000 })
    // Tab strip confirms the view mounted (not just the route resolved).
    await expect(page.getByRole('button', { name: 'Needs Response' })).toBeVisible()
  })

  test('@smoke library page loads', async ({ page }) => {
    // ent#263 → ent#384 — chrome-only anchors: the CI stack has no configured
    // skills library, so never assert on skill/library DATA (the unconfigured
    // empty state is the expected render), and never getByText(/library/i)
    // (the nav link matches everywhere).
    //
    // ent#384 made the sections TABS, so only the active tab's heading is on
    // screen. Assert the default tab, then switch. Tab labels are asserted at
    // the default desktop viewport on purpose — OverflowTabs collapses the
    // strip behind a counted "More ▾" at narrow widths.
    await page.goto('/library')
    await expect(page.getByRole('heading', { name: 'Library', exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('heading', { name: 'Agent Templates', exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'Skills', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toBeVisible()
    // The tab is URL-addressable, which is what makes the legacy anchors and
    // any future deep link work.
    await expect(page).toHaveURL(/[?&]tab=skills/)
  })

  test('@smoke library skills tab is deep-linkable', async ({ page }) => {
    // ent#384 — a cold load of ?tab=skills must land on Skills, not bounce to
    // the default tab.
    await page.goto('/library?tab=skills')
    await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('heading', { name: 'Agent Templates', exact: true })).toBeHidden()
  })

  test('@smoke library legacy section anchor resolves to its tab', async ({ page }) => {
    // ent#263 shipped in-page anchors; ent#384 replaced them with tabs. Old
    // bookmarks and the /templates redirect (which preserves the hash) must
    // still land on the right tab rather than a dead anchor.
    await page.goto('/library#skills')
    await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page).toHaveURL(/[?&]tab=skills/)
  })

  test('@smoke templates path redirects to library', async ({ page }) => {
    // ent#263 — /templates is a legacy redirect (query+hash preserving).
    await page.goto('/templates')
    await expect(page).toHaveURL(/\/library/, { timeout: 10000 })
  })

  test('@smoke monitoring page loads', async ({ page }) => {
    await page.goto('/monitoring')
    // Header, summary cards, or empty state — any of these confirms the route mounted.
    await expect(
      page.getByText(/monitoring|fleet|healthy|degraded|no agents/i).first()
    ).toBeVisible({ timeout: 10000 })
  })

  test('@smoke api keys page loads', async ({ page }) => {
    await page.goto('/api-keys')
    // Header, info banner, list, or empty state — any confirms the route mounted.
    await expect(
      page.getByText(/mcp api keys|connect to mcp|no api keys|create api key/i).first()
    ).toBeVisible({ timeout: 10000 })
  })
})
