import { test, expect } from '@playwright/test'

/**
 * Operations lists scroll inside the window (#3451, design-system p28 + p5 + p30).
 *
 * The defect: every list on /operations rendered into the page, so a populated
 * queue grew it without limit — 5,955 px on Needs Response, 5,277 on
 * Notifications, 5,329 on Executions and 16,811 on Resolved in the sweep that
 * filed it. The ruling: the list takes the window's remaining height and
 * scrolls inside it; the header, tabs, stats, filters and stated totals stay in
 * view; Load more sits at the end of the scroll; the list keeps a 20rem floor,
 * and on a window too short for that the page scrolls only by the shortfall.
 *
 * WHAT IS PROVED, AND HOW.
 *   - The page height is judged FIRST, before any hook the fix adds, so the
 *     pre-fix tree fails on the defect itself ("page 14350px in a 900px
 *     window"), not on a missing test id. The page may exceed the window only
 *     when the list sits at its computed min-height — that is the shortfall.
 *   - Filling is relative, never a pixel count: the region reaches the bottom of
 *     `main`'s content box, and it grows by exactly what the window grows by.
 *   - "The page never follows the item count": one short window rendered with
 *     400 rows and with 3 gives the same page. Safe across two renders because
 *     `html` reserves the scrollbar gutter (#954) and flex, not content, sets the
 *     region's height.
 *   - p5 is about what the reader sees, not `scrollTop`: a refresh that files a
 *     row ABOVE the reader moves `scrollTop` by design (scroll anchoring), so
 *     the check is that the row they were reading stays where it was, is the
 *     same node, and that the region was never re-created — through the queue
 *     poll (held open first), Load more + the executions poll, and a WebSocket
 *     notification.
 *   - p30: ticking a row and runs starting do not move the list.
 *   Geometry is read in ONE evaluate per check (2026-09-11 learning); the only
 *   absolute number is the 20rem the CSS states, read from computed style
 *   (2026-09-08 learning); the window height is `documentElement.clientHeight`.
 *
 * CI scope: `@smoke`, hermetic. Every list the tabs read, the WebSocket ticket
 * and the socket itself are synthetic (`page.route` / `page.routeWebSocket`),
 * so the zero-agent CI stack runs it; `page.clock` is installed before
 * navigation so no poll fires unless a test drives it.
 */

const startsWithPath = (prefix) => (url) => new URL(url).pathname.startsWith(prefix)
const isPath = (pathname) => (url) => new URL(url).pathname === pathname

const NOW = Date.parse('2026-10-23T12:00:00Z')
const ago = (minutes) => new Date(NOW - minutes * 60_000).toISOString()

function queueRow(i, status, extra = {}) {
  return {
    id: `e2e-3451-${status}-${i}`,
    agent_name: `e2e-agent-${i % 7}`,
    type: 'question',
    status,
    priority: 'medium',
    title: `Question ${i} from e2e-agent-${i % 7}`,
    question: `Is item ${i} inside the list's own scroll?`,
    options: null,
    context: {},
    execution_id: null,
    created_at: ago(i),
    expires_at: null,
    response: status === 'pending' ? null : 'yes',
    response_text: null,
    responded_by_email: null,
    responded_at: status === 'pending' ? null : ago(i / 2),
    acknowledged_at: null,
    ...extra,
  }
}

const notification = (i) => ({
  id: `e2e-3451-n-${i}`,
  agent_name: `e2e-agent-${i % 5}`,
  notification_type: 'info',
  priority: 'normal',
  status: 'pending',
  title: `Notification ${i}`,
  message: `Body of notification ${i}`,
  category: null,
  metadata: null,
  created_at: ago(i),
})

const execution = (i) => ({
  id: `e2e-3451-exec-${String(i).padStart(4, '0')}`,
  agent_name: `e2e-agent-${i % 6}`,
  status: 'success',
  triggered_by: 'schedule',
  started_at: ago(i),
  message: `Scheduled run ${i}`,
  duration_ms: 1200,
  cost: 0.01,
  context_used: 1000,
})

function freshState() {
  return {
    queue: [
      ...Array.from({ length: 120 }, (_, i) => queueRow(i + 1, 'pending')),
      ...Array.from({ length: 260 }, (_, i) => queueRow(i + 1, 'responded')),
    ],
    queueGate: null,
    queueReads: 0,
    notifications: Array.from({ length: 50 }, (_, i) => notification(i + 1)),
    executions: Array.from({ length: 400 }, (_, i) => execution(i + 1)),
    executionReads: 0,
    running: 0,
    socket: null,
  }
}

/**
 * The four tabs' list endpoints, served from test-scoped state. The queue route
 * answers in offset mode the way `GET /api/operator-queue` does (pending first,
 * `total`, `has_more`), which is all `readQueueSnapshot` needs; while
 * `state.queueGate` is set every queue request waits on it (a held poll).
 */
async function mockLists(page, state) {
  await page.route(startsWithPath('/api/operator-queue'), async (route) => {
    if (state.queueGate) await state.queueGate
    state.queueReads += 1
    const q = new URL(route.request().url()).searchParams
    const status = q.get('status')
    const rank = { critical: 0, high: 1, medium: 2, low: 3 }
    const rows = state.queue
      .filter((r) => !status || r.status === status)
      .sort((a, b) => (a.status === 'pending' ? 0 : 1) - (b.status === 'pending' ? 0 : 1)
        || (a.status === 'pending' ? rank[a.priority] - rank[b.priority] : 0))
    const limit = Number(q.get('limit') || 100)
    const offset = Number(q.get('offset') || 0)
    const items = rows.slice(offset, offset + limit)
    const more = rows.length > offset + limit
    return route.fulfill({ json: {
      items, count: items.length, total: rows.length, has_more: more,
      next_offset: more ? offset + limit : null, next_cursor: null,
      undelivered_count: 0, closed_by_filer_count: 0,
    } })
  })
  await page.route(isPath('/api/notifications'), (route) =>
    route.fulfill({ json: { count: state.notifications.length, notifications: state.notifications } }))
  await page.route(isPath('/api/notifications/count'), (route) =>
    route.fulfill({ json: { count: state.notifications.length } }))
  await page.route(isPath('/api/executions'), (route) => {
    state.executionReads += 1
    const q = new URL(route.request().url()).searchParams
    const limit = Number(q.get('limit') || 50)
    const offset = Number(q.get('offset') || 0)
    return route.fulfill({ json: state.executions.slice(offset, offset + limit) })
  })
  await page.route(isPath('/api/executions/stats'), (route) => route.fulfill({ json: {
    total: state.executions.length, success_rate: 100, failed_count: 0, total_cost: 4, running_count: state.running,
  } }))
  // The app's socket: a synthetic ticket, and a socket the test can push to.
  await page.route(isPath('/api/ws/ticket'), (route) => route.fulfill({ json: { ticket: 'e2e-3451' } }))
  await page.routeWebSocket((url) => url.pathname === '/ws', (ws) => { state.socket = ws })
}

// Each tab, with the stated total that must stay in view and its loaded text.
const TABS = [
  { id: 'needs-response', total: 'ops-subtitle', loaded: /120 pending responses/ },
  { id: 'notifications', total: 'notif-stat-shown', loaded: /50/ },
  { id: 'executions', total: 'executions-shown', loaded: /50 shown/ },
  { id: 'resolved', total: 'ops-subtitle', loaded: /260 resolved · latest 200 shown/ },
]
const tabById = (id) => TABS.find((t) => t.id === id)

async function openOperations(page, state, { theme = 'light', width = 1440, height = 900 } = {}) {
  await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width, height })
  await page.clock.install({ time: NOW })
  await mockLists(page, state)
}

async function openTab(page, tab) {
  await page.goto(`/operations?tab=${tab.id}`)
  await expect(page.getByTestId(tab.total)).toHaveText(tab.loaded, { timeout: 15000 })
}

/** Page and region geometry from one frame. */
function geometry(page, totalTestId) {
  return page.evaluate((totalId) => {
    const region = document.querySelector('[data-testid="ops-list-region"]')
    const total = document.querySelector(`[data-testid="${totalId}"]`)
    const main = document.querySelector('main')
    const windowHeight = document.documentElement.clientHeight
    const t = total?.getBoundingClientRect()
    const loadMore = document.querySelector('[data-testid="executions-load-more"]')
    let box = null
    if (region) {
      const r = region.getBoundingClientRect()
      const m = main.getBoundingClientRect()
      box = {
        overflowY: getComputedStyle(region).overflowY,
        height: r.height,
        minHeight: parseFloat(getComputedStyle(region).minHeight) || 0,
        top: r.top,
        clientHeight: region.clientHeight,
        scrollHeight: region.scrollHeight,
        clientWidth: region.clientWidth,
        scrollWidth: region.scrollWidth,
        // How far the region stops short of main's content box: 0 when it takes
        // all the height the chrome leaves.
        gapBelow: (m.bottom - parseFloat(getComputedStyle(main).paddingBottom)) - r.bottom,
      }
    }
    return {
      pageHeight: document.scrollingElement.scrollHeight,
      windowHeight,
      region: box,
      totalOutsideRegion: !!total && !!region && !region.contains(total),
      totalInView: !!t && t.top >= 0 && t.bottom <= windowHeight,
      loadMoreLastInRegion: loadMore
        ? !!region && region.contains(loadMore) && region.lastElementChild?.contains(loadMore)
        : null,
    }
  }, totalTestId)
}

const atFloor = (g) => !!g.region && Math.abs(g.region.height - g.region.minHeight) <= 1

/** The first row whose top is inside the region, and where it sits on screen. */
async function anchorRow(page, rowSelector) {
  const handle = await page.evaluateHandle((sel) => {
    const region = document.querySelector('[data-testid="ops-list-region"]')
    const top = region.getBoundingClientRect().top
    return [...region.querySelectorAll(sel)].find((el) => el.getBoundingClientRect().top >= top)
  }, rowSelector)
  const y = await handle.evaluate((el) => el.getBoundingClientRect().top)
  return { handle, y }
}

async function expectAnchoredAt(anchor) {
  expect(await anchor.handle.evaluate((el) => el.isConnected), 'the row the reader was on is the same node').toBe(true)
  await expect
    .poll(async () => Math.abs((await anchor.handle.evaluate((el) => el.getBoundingClientRect().top)) - anchor.y))
    .toBeLessThanOrEqual(2)
}

test.describe('Operations lists scroll inside the window (#3451)', () => {
  for (const [width, height] of [[1440, 900], [768, 1024]]) {
    for (const theme of ['light', 'dark']) {
      test(`@smoke the four lists never grow the page — ${width}×${height}, ${theme}`, async ({ page }) => {
        const state = freshState()
        await openOperations(page, state, { theme, width, height })

        for (const tab of TABS) {
          await openTab(page, tab)
          const g = await geometry(page, tab.total)

          // The defect, judged before anything the fix adds.
          expect(g.pageHeight <= g.windowHeight + 1 || atFloor(g),
            `${tab.id}: page ${g.pageHeight}px in a ${g.windowHeight}px window`).toBe(true)

          // One region holds the overflow, on one axis, and takes the height the page leaves.
          expect(g.region, `${tab.id}: no list region`).toBeTruthy()
          expect(g.region.overflowY).toBe('auto')
          expect(g.region.scrollHeight).toBeGreaterThan(g.region.clientHeight)
          expect(g.region.scrollWidth, `${tab.id}: the list overflows sideways`).toBeLessThanOrEqual(g.region.clientWidth)
          expect(Math.abs(g.region.gapBelow), `${tab.id}: the list stops ${g.region.gapBelow}px short`).toBeLessThanOrEqual(1)

          // The stated total stays in view, outside the scroll.
          expect(g.totalOutsideRegion, `${tab.id}: the stated total scrolls with the list`).toBe(true)
          expect(g.totalInView, `${tab.id}: the stated total is out of view`).toBe(true)

          // Load more sits at the end of the scroll (Executions has a second page here).
          if (tab.id === 'executions') expect(g.loadMoreLastInRegion).toBe(true)
        }
      })
    }
  }

  test('@smoke every list takes the window\'s remaining height, and the page stays the window', async ({ page }) => {
    // Two windows tall enough that no tab sits at its floor: every pixel the
    // window gains must reach the list. A link missing from the flex chain
    // leaves the list short with a band of empty page below it.
    const state = freshState()
    await openOperations(page, state, { width: 1440, height: 1100 })
    const short = {}
    for (const tab of TABS) {
      await openTab(page, tab)
      short[tab.id] = await geometry(page, tab.total)
    }

    // Resized BEFORE navigating: the installed clock holds rAF, and a resize
    // under it would measure a layout that never re-ran.
    await page.setViewportSize({ width: 1440, height: 1400 })
    for (const tab of TABS) {
      await openTab(page, tab)
      const tall = await geometry(page, tab.total)
      const was = short[tab.id]
      expect(was.pageHeight, `${tab.id}: page ${was.pageHeight}px in a 1100px window`).toBeLessThanOrEqual(1101)
      expect(tall.pageHeight, `${tab.id}: page ${tall.pageHeight}px in a 1400px window`).toBeLessThanOrEqual(1401)
      // Not a fixed cap: the 300px the window gained all went to the list.
      expect(Math.abs(tall.region.height - was.region.height - 300),
        `${tab.id}: the list grew ${tall.region.height - was.region.height}px of 300`).toBeLessThanOrEqual(1)
    }
  })

  test('@smoke a short window keeps the list at 20rem and the page height never follows the item count', async ({ page }) => {
    const state = freshState()
    const tab = tabById('executions')
    await openOperations(page, state, { width: 1440, height: 560 })

    await openTab(page, tab)
    const many = await geometry(page, tab.total)

    state.executions = state.executions.slice(0, 3)
    await openTab(page, { ...tab, loaded: /3 shown/ })
    const few = await geometry(page, tab.total)

    // The window is too short for chrome + 20rem, so the page scrolls by the
    // shortfall — and by nothing else: 400 rows and 3 make the same page.
    expect(Math.abs(many.pageHeight - few.pageHeight), `400 rows: ${many.pageHeight}px, 3 rows: ${few.pageHeight}px`)
      .toBeLessThanOrEqual(1)
    expect(many.pageHeight).toBeGreaterThan(560)
    expect(atFloor(many), `the list keeps its ${many.region.minHeight}px floor (is ${many.region.height}px)`).toBe(true)
  })

  test('@smoke a refresh keeps the rows Load more added (Executions)', async ({ page }) => {
    // The 30 s poll and the WS refresh used to replace the list with page one,
    // dropping every row Load more had added — and the reader's place with them.
    const state = freshState()
    await openOperations(page, state)
    await openTab(page, tabById('executions'))

    await page.getByTestId('executions-load-more').click()
    await expect(page.getByTestId('executions-shown')).toHaveText(/100 shown/)

    // The poll brings a new run at the top; waiting for it proves the refresh
    // LANDED before the count is read (a request sent is not a list replaced).
    state.executions = [execution(9001), ...state.executions]
    await page.clock.runFor(30_500)
    await expect(page.getByTestId('ops-list-region').getByText('Scheduled run 9001')).toHaveCount(1)
    await expect(page.getByTestId('executions-shown')).toHaveText(/100 shown/)
    await expect(page.getByTestId('executions-load-more')).toBeVisible()
  })

  test('@smoke Needs Response: a poll that files an ask above the reader leaves their card where it was (p5)', async ({ page }) => {
    const state = freshState()
    await openOperations(page, state)
    await openTab(page, tabById('needs-response'))

    const region = page.getByTestId('ops-list-region')
    const regionNode = await region.elementHandle()
    await region.evaluate((el) => { el.scrollTop = 2000 })
    const anchor = await anchorRow(page, '[data-testid="queue-card"]')

    // Hold the 10 s poll open: while it is in flight nothing may move or blank.
    let release
    state.queueGate = new Promise((r) => { release = r })
    await page.clock.runFor(10_500)
    expect(await region.evaluate((el) => el.scrollTop)).toBe(2000)
    await expect(page.getByText('Checking the queue…')).toHaveCount(0)

    // It lands with a critical ask filed at the TOP and one card's title edited.
    state.queue = [queueRow(0, 'pending', { priority: 'critical', title: 'A critical ask' }), ...state.queue]
    state.queue[5] = { ...state.queue[5], title: 'An edited title' }
    state.queueGate = null
    release()
    await expect(page.getByTestId('ops-subtitle')).toHaveText(/121 pending responses/)

    expect(await regionNode.evaluate((el) => el.isConnected), 'the region was re-created').toBe(true)
    await expectAnchoredAt(anchor)
  })

  test('@smoke Executions: Load more and the poll after it leave the reader\'s row where it was (p5)', async ({ page }) => {
    const state = freshState()
    await openOperations(page, state)
    await openTab(page, tabById('executions'))

    // A reader reaches Load more at the end of the list; the rows it adds open
    // below, and the row they were on stays put.
    const region = page.getByTestId('ops-list-region')
    const regionNode = await region.elementHandle()
    await region.evaluate((el) => { el.scrollTop = el.scrollHeight })
    const anchor = await anchorRow(page, '[data-testid="execution-row"]')

    await page.getByTestId('executions-load-more').click()
    await expect(page.getByTestId('executions-shown')).toHaveText(/100 shown/)
    await expectAnchoredAt(anchor)

    // A run starts: the poll after Load more brings a new row at the top.
    state.executions = [execution(9001), ...state.executions]
    const reads = state.executionReads
    await page.clock.runFor(30_500)
    await expect.poll(() => state.executionReads).toBeGreaterThan(reads)
    await expect(page.getByTestId('ops-list-region').getByText('Scheduled run 9001')).toHaveCount(1)
    await expect(page.getByTestId('executions-shown')).toHaveText(/100 shown/)

    expect(await regionNode.evaluate((el) => el.isConnected), 'the region was re-created').toBe(true)
    await expectAnchoredAt(anchor)
  })

  test('@smoke Notifications: a live notification above the reader leaves their row where it was (p5)', async ({ page }) => {
    const state = freshState()
    await openOperations(page, state)
    await openTab(page, tabById('notifications'))

    const region = page.getByTestId('ops-list-region')
    await region.evaluate((el) => { el.scrollTop = 1500 })
    const anchor = await anchorRow(page, '[data-testid="notification-row"]')

    await expect.poll(() => state.socket, { message: 'the app opened its socket' }).toBeTruthy()
    state.socket.send(JSON.stringify({
      event: 'agent_notification', notification_id: 'e2e-3451-n-live', agent_name: 'e2e-agent-1',
      notification_type: 'info', title: 'A live notification', priority: 'normal',
      timestamp: new Date(NOW).toISOString(),
    }))
    await expect(page.getByTestId('notification-row')).toHaveCount(51)
    await expectAnchoredAt(anchor)
  })

  test('@smoke the region scrolls from the keyboard', async ({ page }) => {
    const state = freshState()
    await openOperations(page, state)
    await openTab(page, tabById('resolved'))

    const region = page.getByTestId('ops-list-region')
    await region.focus()
    await page.keyboard.press('PageDown')
    await expect.poll(() => region.evaluate((el) => el.scrollTop)).toBeGreaterThan(0)
  })

  test('@smoke a filter change starts the list at its first row; a poll does not', async ({ page }) => {
    const state = freshState()
    await openOperations(page, state)
    await openTab(page, tabById('executions'))

    const region = page.getByTestId('ops-list-region')
    await region.evaluate((el) => { el.scrollTop = 1200 })
    // The poll edits a row in place (no row filed above, so anchoring keeps
    // scrollTop as is); the edit showing proves the refresh landed.
    state.executions = state.executions.map((r, i) => (i === 2 ? { ...r, message: 'An edited run' } : r))
    await page.clock.runFor(30_500)
    await expect(region.getByText('An edited run')).toHaveCount(1)
    expect(await region.evaluate((el) => el.scrollTop)).toBe(1200)

    await page.locator('select', { has: page.locator('option[value="failed"]') }).selectOption('failed')
    await expect.poll(() => region.evaluate((el) => el.scrollTop)).toBe(0)
  })

  test('@smoke ticking a row and runs starting do not move the list (p30)', async ({ page }) => {
    const state = freshState()
    await openOperations(page, state)

    await openTab(page, tabById('notifications'))
    const notifTop = (await geometry(page, 'notif-stat-shown')).region.top
    await page.getByTestId('notification-row').first().locator('input[type="checkbox"]').check()
    await expect(page.getByTestId('notifications-list-header')).toContainText('1 selected')
    await expect(page.getByTestId('notifications-list-header')).toContainText('Acknowledge Selected')
    expect((await geometry(page, 'notif-stat-shown')).region.top).toBe(notifTop)

    await openTab(page, tabById('executions'))
    const execTop = (await geometry(page, 'executions-shown')).region.top
    state.running = 2
    await page.clock.runFor(30_500)
    await expect(page.getByTestId('executions-running')).toHaveText(/2 running now/)
    expect((await geometry(page, 'executions-shown')).region.top).toBe(execTop)
  })
})
