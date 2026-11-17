import { test, expect } from '@playwright/test'

/**
 * A project's "Needs you" row stays inside its card (#3994).
 *
 * The card sits in the Overview's narrow side column: about 213px at a 1024px
 * viewport, 239 at 1100, 299 at 1280 and 309 from ~1312 up (ProjectPage's
 * `max-w-5xl sm:px-8` + `lg:grid-cols-3 gap-4` beside the 18rem sidebar). An
 * open rail or a wider sidebar makes it narrower still. All three spans of the
 * row's first line used to be `shrink-0`, so the relative time was drawn past
 * the card's border; the review of the first fix then measured a long agent
 * name, and the sync badge on that line, still pushing it out.
 *
 * jsdom has no layout engine, so the unit suite can only pin the structure
 * (`projectNeedsYou.mount.spec.js`). This sweep measures boxes, the
 * `dashboard-stats-overflow.spec.js` (#1830) method:
 *   1. nothing in a row runs past the row's border, and the time is never
 *      clipped — at every side-column width, with the longest relative time
 *      ("45 minutes ago"), a long agent name, and both badge lengths
 *   2. the kind label gives way first: while it still has any width, the agent
 *      name is not truncated
 *
 * The card's width is SET to each side-column width rather than derived from a
 * viewport, so the sweep does not depend on the sidebar or rail state of the
 * stack it runs on. The project, its asks and the roster flag that opens
 * Projects are served by `page.route`, so no fixture project, entitlement or
 * portal email is needed — only a live stack and the cached admin session.
 *
 * @interactive — local only, like the #1830 sweep it is modelled on. Where the
 * project page never renders, the test SKIPS (#2199), never fails.
 */

const PROJECT = 'e2e-3994'
const CARD_WIDTHS = [213, 239, 270, 299, 309]
const LONG = 'customer-onboarding-compliance-steward'
const ME = 'e2e@example.com'

const minutesAgo = (m) => new Date(Date.now() - m * 60_000).toISOString()

// One row per case the review measured: the long name alone, with the short
// aging badge, with the ~156px sync badge, a mid-length name, a short one.
const ASKS = () => [
  { id: 'e2e-a', agent_name: LONG, kind: 'approval', title: 'Approve the plan?', status: 'pending', created_at: minutesAgo(45) },
  { id: 'e2e-b', agent_name: LONG, kind: 'approval', title: 'Ship the release?', status: 'pending', created_at: minutesAgo(46), aging: true },
  { id: 'e2e-c', agent_name: LONG, kind: 'question', title: 'Which region?', status: 'pending', created_at: minutesAgo(47), sync_state: 'changed' },
  { id: 'e2e-d', agent_name: 'compliance-steward', kind: 'approval', title: 'Approve the policy?', status: 'pending', created_at: minutesAgo(48) },
  { id: 'e2e-e', agent_name: 'scout', kind: 'question', title: 'Keep going?', status: 'pending', created_at: minutesAgo(49) },
]

const projectPayload = (asks) => ({
  id: PROJECT, name: 'Overflow check', goal: 'Rows fit.', status: 'active', visibility: 'members',
  created_by: ME, updated_at: minutesAgo(1), archived_at: null,
  steward: { kind: 'person', ref: ME },
  members: [{ email: ME, role: 'creator' }],
  agents: [{ agent_name: LONG, state: 'active' }, { agent_name: 'scout', state: 'active' }],
  my_chats: [], rooms: [], others_chat_count: 0,
  can: { manage_members: false, manage_agents: false, edit: false, archive: false, work: true, contribute: true, set_health: false },
  rollup: { by_status: {}, awaiting_verification: [], stuck: [], stale: [], health: null, health_due: false, last_activity_at: null },
  metrics: [],
  asks: { mine: asks.map((a) => a.id), others_open: 0 },
})

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

// Every row's geometry against its own bordered box.
const PROBE = () =>
  [...document.querySelectorAll('[data-testid^="project-ask-row-"]')].map((row) => {
    const cs = getComputedStyle(row)
    const rr = row.getBoundingClientRect()
    const border = rr.right - parseFloat(cs.borderRightWidth)
    const head = row.querySelector('[data-testid^="project-ask-head-"]')
    const [name, kind, time] = head.children
    const clipped = (e) => e.scrollWidth > e.clientWidth + 0.5
    const past = Math.max(
      ...[...row.querySelectorAll('*')]
        .filter((e) => e.getBoundingClientRect().width > 0)
        .map((e) => e.getBoundingClientRect().right - border)
    )
    return {
      id: row.dataset.testid,
      worstPastBorder: Math.round(past * 10) / 10,
      timeClipped: clipped(time),
      kindWidth: kind.getBoundingClientRect().width,
      nameTruncated: clipped(name),
    }
  })

test.describe('Project "Needs you" row overflow (#3994)', () => {
  for (const theme of ['light', 'dark']) {
    test(`@interactive ${theme}: every row fits its card at every side-column width`, async ({ page }) => {
      const asks = ASKS()
      await page.route(`**/api/enterprise/projects/${PROJECT}`, (route) => route.fulfill(json(projectPayload(asks))))
      await page.route('**/api/enterprise/client-portal/asks**', (route) => route.fulfill(json(asks)))
      // The roster is what raises `projectsAvailable` (strict `=== true`,
      // #2128); served here so the page does not depend on the session's
      // own roster or entitlement.
      await page.route('**/api/enterprise/client-portal/my-agents**', (route) => route.fulfill(json({ agents: [], projects_available: true })))

      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(`/workspace/projects/${PROJECT}`)
      const card = page.getByTestId('project-needs-you')
      const rendered = await card.waitFor({ timeout: 20000 }).then(() => true, () => false)
      test.skip(!rendered, 'the Workspace project page did not render for this session (no portal access)')
      await expect(page.locator('[data-testid^="project-ask-row-"]')).toHaveCount(asks.length)

      await page.evaluate((t) => document.documentElement.classList.toggle('dark', t === 'dark'), theme)

      for (const width of CARD_WIDTHS) {
        await card.evaluate((el, w) => { el.style.width = `${w}px` }, width)
        const rows = await page.evaluate(PROBE)
        for (const r of rows) {
          expect.soft(r.worstPastBorder, `${r.id} @ ${width}px runs ${r.worstPastBorder}px past its border`).toBeLessThanOrEqual(0.5)
          expect.soft(r.timeClipped, `${r.id} @ ${width}px — the time is clipped`).toBe(false)
          if (r.kindWidth > 0.5) {
            expect.soft(r.nameTruncated, `${r.id} @ ${width}px — the agent name truncates while the kind label still has ${r.kindWidth}px`).toBe(false)
          }
        }
      }
    })
  }
})
