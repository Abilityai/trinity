import { test, expect } from '@playwright/test'

/**
 * Agent Detail "Overview" default tab + Info redesign e2e (#1107).
 *
 * Verifies:
 *  1. Overview is the DEFAULT landing tab on /agents/{name} (replaces Tasks).
 *  2. The `?tab=` deep-link is honored for both the new 'overview' id and the
 *     existing 'tasks' id — guards the hoisted VALID_TABS list (the
 *     onMounted/onActivated two-copy drift that this PR collapsed).
 *  3. The Info tab leads with About and tucks exhaustive template.yaml metadata
 *     behind a collapsed "Technical details" <details> (data-dependent — only
 *     asserts the collapse when the test agent actually ships such metadata).
 *
 * Auth is supplied by auth.setup.js storageState (see playwright config). The
 * real backend serves GET /api/agents/{name}, so a TEST_AGENT must exist; the
 * suite skips cleanly if the detail page can't load.
 */

const TEST_AGENT = process.env.TEST_AGENT || 'trinity-system'

// TEST_AGENT defaults to 'trinity-system' — the platform system agent, which
// always exists on any Trinity stack (it cannot be deleted). No skip-probe: a
// missing agent should fail loudly rather than silently skip.
test.describe('agent detail overview tab (#1107)', () => {
  test('@smoke Overview is the default landing tab', async ({ page }) => {
    await page.goto(`/agents/${TEST_AGENT}`)

    // Overview panel renders without any tab click.
    await expect(page.getByTestId('overview-panel')).toBeVisible({ timeout: 15000 })

    // The task-entry shim (the action affordance preserved from Tasks) is present.
    await expect(page.getByPlaceholder(/give .* a task/i)).toBeVisible()

    // The Overview tab button is the active one (action-primary border token).
    const overviewTab = page.getByRole('button', { name: 'Overview', exact: true })
    await expect(overviewTab).toBeVisible()
    await expect(overviewTab).toHaveClass(/border-action-primary-500/)
  })

  test('?tab= deep-link is honored for tasks and overview (VALID_TABS guard)', async ({ page }) => {
    // Deep-link straight to Tasks — must NOT land on Overview.
    await page.goto(`/agents/${TEST_AGENT}?tab=tasks`)
    await expect(page.getByTestId('overview-panel')).toHaveCount(0, { timeout: 15000 })
    // TasksPanel leads with its New Task textarea.
    await expect(page.getByPlaceholder(/enter task message/i)).toBeVisible()

    // Deep-link to Overview explicitly — must render the Overview.
    await page.goto(`/agents/${TEST_AGENT}?tab=overview`)
    await expect(page.getByTestId('overview-panel')).toBeVisible({ timeout: 15000 })
  })

  test('in-app navigation away and back preserves the deep-linked tab (KeepAlive)', async ({ page }) => {
    // Land on Tasks via deep-link, navigate to the fleet list, then back.
    await page.goto(`/agents/${TEST_AGENT}?tab=tasks`)
    await expect(page.getByPlaceholder(/enter task message/i)).toBeVisible({ timeout: 15000 })

    await page.getByRole('link', { name: /agents/i }).first().click()
    await page.waitForURL(/\/agents\/?$/, { timeout: 15000 }).catch(() => {})

    // Back into the same agent — the onActivated `?tab=` path keeps Tasks.
    await page.goBack()
    // Either Tasks stays (KeepAlive cached) or a cold reload re-applies ?tab=tasks.
    await expect(page.getByTestId('overview-panel')).toHaveCount(0, { timeout: 15000 })
  })

  test('Info tab leads with About; technical metadata is collapsed behind a toggle', async ({ page }) => {
    // Mock the template info so the assertion is deterministic regardless of the
    // test agent's real template (trinity-system ships none). Mirrors the
    // route-mock approach in circuit-breaker-badge.spec.js.
    await page.route('**/api/agents/*/info', (route) =>
      route.fulfill({
        json: {
          has_template: true,
          name: TEST_AGENT,
          display_name: 'Overview E2E Agent',
          description: 'About-lead narrative for the Info redesign test.',
          tools: ['Bash', 'Read'],
          sub_agents: [{ name: 'helper', description: 'does things' }],
        },
      })
    )
    await page.goto(`/agents/${TEST_AGENT}?tab=info`)

    // About leads — the description is visible above the fold.
    await expect(
      page.getByText('About-lead narrative for the Info redesign test.')
    ).toBeVisible({ timeout: 15000 })

    // Technical metadata is tucked behind a collapsed <details>; inner sections
    // (e.g. "Enabled Tools") are hidden until the user expands it.
    const tech = page.getByTestId('info-technical-details')
    await expect(tech).toBeVisible()
    await expect(tech).not.toHaveJSProperty('open', true)
    await expect(page.getByText('Enabled Tools')).toBeHidden()

    // Expanding reveals the metadata.
    await tech.locator('summary').click()
    await expect(tech).toHaveJSProperty('open', true)
    await expect(page.getByText('Enabled Tools')).toBeVisible()
  })
})
