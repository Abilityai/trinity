import { test, expect } from '@playwright/test'
import { ALL_LAYOUT_KEYS } from '../src/utils/gridStorageKeys.js'

/**
 * Grid org overlay e2e (trinity-enterprise#305).
 *
 * Departments (dept-* tags → derived zone frames) + reporting lines
 * (reports-to-* tags → manager→report arrows) over the Grid lattice.
 * Runs against a live stack — `trinity-system` always exists, so the specs
 * create org facts via the tags API on it and clean up after themselves.
 */

// Layout keys come from the store's own module (#2199) — this file's
// hand-copied 'v1' literal silently stopped clearing the real (v2) layout
// after the #2042 bump. ORG_KEY stays local on purpose: it has never been
// bumped and this copy matches composables/useOrgOverlay.js.
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

async function getTags(page, agent) {
  return page.evaluate(async (name) => {
    const res = await fetch(`/api/agents/${name}/tags`, {
      headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
    })
    if (!res.ok) return []
    return (await res.json()).tags || []
  }, agent)
}

test.describe('grid org overlay (trinity-enterprise#305)', () => {
  // trinity-system is SHARED state — snapshot its tags before each test and
  // restore exactly that set after, instead of blind-PUTting [] (which wiped
  // any pre-existing tags on the operator's live agent).
  let priorSystemTags = null

  test.beforeEach(async ({ page }) => {
    // Spread into one flat array and iterate — nesting would call removeItem()
    // with an Array and silently clear nothing (#2199).
    await page.addInitScript(
      (keys) => {
        keys.forEach((k) => localStorage.removeItem(k))
      },
      [...ALL_LAYOUT_KEYS, ORG_KEY]
    )
    priorSystemTags = null
  })

  test.afterEach(async ({ page }) => {
    // Restore the pre-test tag set (drops test-added org tags, keeps
    // whatever was already there).
    if (priorSystemTags !== null) {
      await setTags(page, 'trinity-system', priorSystemTags)
    }
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
    if (priorSystemTags === null) priorSystemTags = await getTags(page, 'trinity-system')
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
    if (priorSystemTags === null) priorSystemTags = await getTags(page, 'trinity-system')
    await setTags(page, 'trinity-system', ['dept-e2e-platform'])
    await expect(page.locator('.gv-zone')).toBeVisible({ timeout: 15000 })

    await page.getByRole('button', { name: 'Zones', exact: true }).click()
    await expect(page.locator('.gv-zone')).toHaveCount(0)

    await page.getByRole('button', { name: 'Zones', exact: true }).click()
    await expect(page.locator('.gv-zone')).toBeVisible()
  })
})
