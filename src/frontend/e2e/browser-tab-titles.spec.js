import { test, expect } from '@playwright/test'

// #1418 — per-route browser-tab titles. The router's `afterEach` guard sets
// `document.title` to `Trinity — <label>` from each route's `meta.title`;
// unlabeled routes (redirects, catch-all) fall back to the branded default
// `Trinity — Agent Orchestration` that index.html ships (#1416).
//
// Dynamic agent-scoped titles (`meta.title` as a fn of `to.params.name`) are
// covered by unit logic; here we assert the deterministic static/redirect
// routes and — critically — that CLIENT-SIDE nav updates the title with no
// full reload (the whole point of the guard vs a static <title>).
//
// @smoke: runs in the `frontend-e2e` CI workflow on every `ui`-labelled PR.
test.describe('browser tab titles (#1418)', () => {
  test('@smoke SPA navigation updates the tab title per route', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true }))
      .toBeVisible({ timeout: 10000 })
    await expect(page).toHaveTitle('Trinity — Dashboard')

    // Top-nav clicks are client-side transitions (no reload) — the guard must
    // still fire and repaint the title.
    await page.getByRole('link', { name: 'Agents', exact: true }).click()
    await expect(page).toHaveTitle('Trinity — Agents')

    await page.getByRole('link', { name: 'Templates', exact: true }).click()
    await expect(page).toHaveTitle('Trinity — Templates')
  })

  test('@smoke redirect resolves to the destination route title', async ({ page }) => {
    // /operating-room → /operations (legacy redirect). afterEach fires once for
    // the FINAL destination, so the title reflects Operations, not a blank hop.
    await page.goto('/operating-room')
    await expect(page).toHaveURL(/\/operations/, { timeout: 10000 })
    await expect(page).toHaveTitle('Trinity — Operations')
  })
})
