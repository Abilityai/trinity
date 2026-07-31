import { test, expect } from '@playwright/test'

/**
 * Grid org overlay e2e (trinity-enterprise#305).
 *
 * Departments (dept-* tags → derived zone frames) + reporting lines
 * (reports-to-* tags → manager→report arrows) over the Grid lattice.
 * Runs against a live stack — `trinity-system` always exists, so the specs
 * create org facts via the tags API on it and clean up after themselves.
 */

const LAYOUT_KEY = 'trinity-grid-layout-v1'
const ORG_KEY = 'trinity-grid-org-v1'

async function gotoGrid(page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'grid', exact: true }).click()
  await expect(page.locator('.fleet-canvas')).toBeVisible()
}

async function setTags(page, agent, tags) {
  // Same-origin request with the app's own JWT — mirrors the store's PUT.
  const status = await page.evaluate(
    async ([name, tagList]) => {
      const res = await fetch(`/api/agents/${name}/tags`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('token')}`,
        },
        body: JSON.stringify({ tags: tagList }),
      })
      return res.status
    },
    [agent, tags]
  )
  expect(status).toBe(200)
}

test.describe('grid org overlay (trinity-enterprise#305)', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(
      ([layoutKey, orgKey]) => {
        localStorage.removeItem(layoutKey)
        localStorage.removeItem(orgKey)
      },
      [LAYOUT_KEY, ORG_KEY]
    )
  })

  test.afterEach(async ({ page }) => {
    // Never leave org tags on the shared system agent.
    await setTags(page, 'trinity-system', [])
  })

  test('@smoke org controls render and a dept-* tag produces a zone with ribbon', async ({
    page,
  }) => {
    await gotoGrid(page)

    // Controls present.
    await expect(page.getByRole('button', { name: 'Zones', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Lines', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Group by dept' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'New dept' })).toBeVisible()

    // Tag the system agent into a department via the API; the
    // agent_tags_changed broadcast re-derives zones without a reload.
    await setTags(page, 'trinity-system', ['dept-e2e-platform'])

    const zone = page.locator('.gv-zone', { hasText: 'e2e-platform' })
    await expect(zone).toBeVisible({ timeout: 15000 })
    await expect(zone.locator('.gv-zonehead')).toContainText('1 agent')

    // Tile carries the department ribbon.
    await expect(
      page.locator('.gv-tile[data-agent="trinity-system"] .dept-ribbon')
    ).toBeVisible()
  })

  test('New department popover validates the name and names the problem', async ({
    page,
  }) => {
    await gotoGrid(page)
    await page.getByRole('button', { name: 'New dept' }).click()

    const input = page.locator('#gv-newdept-name')
    await expect(input).toBeVisible()
    await input.fill('Not Valid!!')
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    // Named validation error, not a bare red border.
    await expect(page.locator('.gv-newdept .err')).toContainText('lowercase')

    // Esc closes the popover.
    await input.press('Escape')
    await expect(input).not.toBeVisible()
  })

  test('Zones toggle hides and shows zone frames', async ({ page }) => {
    await gotoGrid(page)
    await setTags(page, 'trinity-system', ['dept-e2e-platform'])
    await expect(page.locator('.gv-zone')).toBeVisible({ timeout: 15000 })

    await page.getByRole('button', { name: 'Zones', exact: true }).click()
    await expect(page.locator('.gv-zone')).toHaveCount(0)

    await page.getByRole('button', { name: 'Zones', exact: true }).click()
    await expect(page.locator('.gv-zone')).toBeVisible()
  })
})
