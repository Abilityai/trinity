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
    // Top nav has Dashboard, Agents, Templates, Health, Ops, Settings.
    // (Keys link removed in #302 — MCP keys now live in Settings → MCP Keys tab.)
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({ timeout: 10000 })
    await expect(page.getByRole('link', { name: 'Agents', exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Settings', exact: true })).toBeVisible()
  })

  test('@smoke agents page loads', async ({ page }) => {
    await page.goto('/agents')
    await expect(page.getByText(/agent|create/i).first()).toBeVisible({ timeout: 10000 })
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

  test('@smoke templates page loads', async ({ page }) => {
    await page.goto('/templates')
    await expect(page.getByText(/template/i).first()).toBeVisible({ timeout: 10000 })
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
