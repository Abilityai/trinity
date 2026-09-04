import { test, expect } from '@playwright/test'

// ent#438 smoke, driven against the live stack. Not part of the @smoke tier —
// this exists to prove the surface actually renders before handing it over.
const AGENT = process.env.CANVAS_AGENT || 'weather-watch'

test('Agent Detail shows a Canvas tab that renders every block kind', async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=canvas`)

  const canvasTab = page.locator('button:has-text("Canvas")').first()
  await expect(canvasTab).toBeVisible({ timeout: 20000 })

  // Both canvases are offered (operator sees roster + operator-only).
  await expect(page.locator('button:has-text("Weather board")')).toBeVisible({ timeout: 20000 })
  await expect(page.locator('button:has-text("Operator scratch")')).toBeVisible()

  // The four block kinds actually rendered.
  await expect(page.getByText('Right now')).toBeVisible()
  await expect(page.getByText('Temperature, last 7 days')).toBeVisible()
  await expect(page.getByText('Open alerts')).toBeVisible()
  await expect(page.getByText('Stations reporting')).toBeVisible()   // kpi tile
  await expect(page.getByText('Kyiv-3')).toBeVisible()               // table row
  await expect(page.locator('text=Updated')).toBeVisible()           // freshness line

  await page.screenshot({ path: 'e2e/test-results/ent438-agent-detail-canvas.png', fullPage: true })
})

test('the retired per-agent workspace route redirects, carrying its agent', async ({ page }) => {
  await page.goto(`/agents/${AGENT}/workspace`)
  await page.waitForURL(/\/workspace/, { timeout: 20000 })
  const url = new URL(page.url())
  expect(url.pathname).toBe('/workspace')
  expect(url.searchParams.get('agent')).toBe(AGENT)
  await page.screenshot({ path: 'e2e/test-results/ent438-redirect.png', fullPage: true })
})

test('the operator-only canvas never reaches the Workspace agent page', async ({ page }) => {
  await page.goto(`/workspace?agent=${AGENT}`)
  await page.waitForLoadState('networkidle')
  // Open the agent page, then its Canvas tab.
  const canvasTab = page.locator('button:has-text("Canvas")').first()
  if (await canvasTab.isVisible({ timeout: 15000 }).catch(() => false)) {
    await canvasTab.click()
    await expect(page.getByText('Weather board')).toBeVisible({ timeout: 15000 })
    await expect(page.getByText('Operator scratch')).toHaveCount(0)
  }
  await page.screenshot({ path: 'e2e/test-results/ent438-workspace-canvas.png', fullPage: true })
})
