import { test, expect } from '@playwright/test'

/**
 * Schedules tab — a restricted schedule's row at phone width (#4281).
 *
 * The run-tier reason box carries "Created by <email>", and an email is one
 * unbreakable word (dots and `@` are not break opportunities). At 390px its
 * min-content held the row's text column wider than the space left beside the
 * action icons, so the page scrolled sideways and Delete sat past the screen
 * edge (401px of 390). Letting only the creator line break anywhere then
 * exposed the box's own layout: the text sat beside "Take over" in a column a
 * few pixels wide. Below `sm` the box now stacks — text, then the button —
 * and from `sm` up the desktop row is unchanged.
 *
 * HERMETIC, like `self-change-permissions-narrow.spec.js`: CI's e2e baseline
 * holds no user agents, so the agent and its schedules exist only in the route
 * stubs below. jsdom does no layout, so this is the only place the claim can
 * be measured: boxes from one render, judged against the row's own edges.
 */
const AGENT = 'e2e-run-tier'
// No hyphen: a hyphen is a break opportunity; this must be one unbreakable run.
const LONG_EMAIL = 'averylongreviewer.name.for.release.testing@examplecompany.com'
const REASON = "Its runs have the access of the person who created it, who is not the agent's owner or an admin: no shell commands and no tools the owner added. To give them the owner's access, the agent's owner or an admin can take it over after reviewing what it runs."

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const schedule = (over) => ({
  agent_name: AGENT,
  description: null,
  cron_expression: '0 9 * * *',
  message: 'Summarise yesterday\'s open issues and post the list.',
  enabled: true,
  timezone: 'UTC',
  timeout_seconds: 900,
  allowed_tools: null,
  model: null,
  created_at: new Date(Date.now() - 86400000).toISOString(),
  updated_at: new Date(Date.now() - 86400000).toISOString(),
  last_run_at: null,
  next_run_at: new Date(Date.now() + 3600000).toISOString(),
  runs_as: 'owner',
  ...over,
})

const SCHEDULES = [
  schedule({
    id: 'e2e-restricted',
    name: 'share-holder-daily',
    runs_as: 'shared_person',
    runs_as_reason: { cause: 'creator_not_owner_tier', message: REASON },
    created_by: LONG_EMAIL,
    can_take_over: true,
  }),
  schedule({ id: 'e2e-owner', name: 'owner-weekly', cron_expression: '0 8 * * 1' }),
]

async function openPanel(page, { width, theme }) {
  await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
  await page.setViewportSize({ width, height: 900 })
  await page.route(`**/api/agents/${AGENT}`, (route) => route.fulfill(json({
    name: AGENT,
    status: 'running',
    port: 2299,
    created: new Date().toISOString(),
    resources: { cpu: '1', memory: '1g' },
    container_id: `${AGENT}-container`,
    template: 'local:test-echo',
    runtime: 'claude-code',
    ephemeral: false,
    owner: 'admin',
    is_owner: true,
    is_shared: false,
    is_system: false,
    can_share: true,
    can_delete: true,
    autonomy_enabled: true,
    read_only_enabled: false,
    avatar_url: null,
    shares: [],
    circuit_breaker: null,
  })))
  await page.route(`**/api/agents/${AGENT}/schedules`, (route) => route.fulfill(json(SCHEDULES)))
  await page.route(`**/api/agents/${AGENT}/schedules/analytics-summary**`, (route) => route.fulfill(json({ schedules: [] })))
  await page.route(`**/api/agents/${AGENT}/autonomy`, (route) => route.fulfill(json({ autonomy_enabled: true })))
  await page.route(/\/api\/operator-queue(\?|$)/, (route) => route.fulfill(json({ items: [] })))

  await page.goto(`/agents/${AGENT}?tab=schedules`)
  await expect(page.getByTestId('schedule-created-by')).toContainText(LONG_EMAIL, { timeout: 20000 })
}

/** The restricted row's boxes and the page's overflow, from one frame. */
function measure(page) {
  return page.evaluate(() => {
    const box = (el) => {
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }
    }
    // The painted extent of an element's text — it can pass its own box.
    const inkRight = (el) => {
      const range = document.createRange()
      range.selectNodeContents(el)
      return Math.max(...[...range.getClientRects()].filter((r) => r.width > 0).map((r) => r.right))
    }
    const rows = [...document.querySelectorAll('[data-testid="schedule-row"]')].map((row) => {
      const actions = row.querySelector(':scope > div > div:last-child')
      return { id: row.dataset.scheduleId, row: box(row), actions: box(actions) }
    })
    const reason = document.querySelector('[data-testid="schedule-runs-as-reason"]')
    const created = document.querySelector('[data-testid="schedule-created-by"]')
    const button = document.querySelector('[data-testid="schedule-take-over"]')
    const textCol = reason.parentElement
    const reasonBox = textCol.parentElement
    return {
      pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      rows,
      reasonBox: box(reasonBox),
      textCol: box(textCol),
      created: { ...box(created), inkRight: inkRight(created) },
      button: box(button),
    }
  })
}

const PX = 0.5

for (const theme of ['light', 'dark']) {
  // 390 only: at 320px every schedule row — an owner's plain row included —
  // already ran its action icons past the card before #4281, so a 320 case
  // here would measure that older layout, not this change.
  for (const width of [390]) {
    test(`@smoke at ${width}px a restricted schedule's row stays on screen and its reason box stacks — ${theme}`, async ({ page }) => {
      await openPanel(page, { width, theme })
      const m = await measure(page)

      expect.soft(m.pageOverflow, 'the page scrolls sideways').toBeLessThanOrEqual(0)
      for (const r of m.rows) {
        // Every action icon (Delete is last) inside its own row.
        expect.soft(r.actions.right, `${r.id}: the action icons run ${Math.round(r.actions.right - r.row.right)}px past the row`).toBeLessThanOrEqual(r.row.right + PX)
      }
      // The email breaks inside the reason box instead of widening it.
      expect.soft(m.created.inkRight, `"Created by …" runs ${Math.round(m.created.inkRight - m.reasonBox.right)}px past the reason box`).toBeLessThanOrEqual(m.reasonBox.right + PX)
      // Stacked: the button under the text, and the text gets the box's width
      // (side by side it was left ~11px wide).
      expect.soft(m.button.top, 'Take over sits beside the text, not under it').toBeGreaterThanOrEqual(m.textCol.bottom - PX)
      expect.soft(m.textCol.width, `the reason text is ${Math.round(m.textCol.width)}px wide in a ${Math.round(m.reasonBox.width)}px box`).toBeGreaterThanOrEqual(m.reasonBox.width * 0.7)
    })
  }

  test(`@smoke at 1280px the desktop reason box is kept, Take over beside the text — ${theme}`, async ({ page }) => {
    await openPanel(page, { width: 1280, theme })
    const m = await measure(page)

    expect.soft(m.pageOverflow, 'the page scrolls sideways').toBeLessThanOrEqual(0)
    expect.soft(m.button.top, 'Take over dropped below the text').toBeLessThan(m.textCol.bottom)
    expect.soft(m.button.left, 'Take over is not to the right of the text').toBeGreaterThanOrEqual(m.textCol.right - PX)
  })
}
