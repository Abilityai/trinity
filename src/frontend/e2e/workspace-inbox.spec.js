import { test, expect } from '@playwright/test'

/**
 * The Workspace Inbox — the landing (trinity-enterprise#610).
 *
 * What must hold, measured on a live stack:
 *   1. signing in on bare `/workspace` lands on `/workspace/inbox`, once
 *   2. the sidebar carries the pinned Inbox row
 *   3. an explicit target still wins: `?agent=X` opens X's conversation, and
 *      `/workspace?new=1` stays the new-chat stage (D9 — bare `/workspace` keeps
 *      its meaning; only the bootstrap lands elsewhere)
 *   4. the Inbox list does not shift as the data arrives — sampled
 *      CONTAINER-relative (the #2711 method), never against an absolute pixel
 *      floor, which pins the host's scrollbar rather than the layout
 *
 * Arms 1 and 2 need only a signed-in session; arms 3 and 4 need an agent on
 * the roster and skip (never fail) without one (#2199).
 *
 * In CI (`frontend-e2e.yml`) every roster-gated test SKIPS, by construction,
 * for two independent reasons (#3054 review):
 *   - the workflow pins the e2e baseline at zero user agents
 *     (`TRINITY_DEFAULT_SYSTEM_MANIFEST=disabled`), which other @smoke specs
 *     and the contrast baseline depend on;
 *   - its admin is bootstrapped from ADMIN_PASSWORD with no email, and
 *     `portal_auth` 403s a platform principal without one, so `/my-agents` is
 *     empty even if an agent existed (learnings.md, trinity#2559).
 * Seeding one is a workflow change (an admin email plus an owned agent row
 * that every other spec tolerates), not this spec's. Until then these arms
 * are proven on a live stack with a seeded roster (`npm run test:e2e:smoke`
 * against it). A green CI run here is NOT evidence for them.
 */

async function firstRosterAgent(page) {
  return page.evaluate(async () => {
    const res = await fetch('/api/enterprise/client-portal/my-agents', {
      headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
    })
    if (!res.ok) return null
    const body = await res.json()
    const rows = body.agents || (Array.isArray(body) ? body : [])
    return rows[0]?.name || null
  })
}

test.describe('Workspace Inbox', () => {
  test('@smoke bare /workspace lands on the Inbox, with the pinned row in the sidebar', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/workspace')
    await page.waitForURL('**/workspace/inbox**', { timeout: 20000 })
    const row = page.getByTestId('sidebar-inbox')
    await expect(row).toBeVisible({ timeout: 20000 })
    await expect(row).toHaveAttribute('aria-current', 'page')
  })

  test('@smoke an explicit target is never redirected to the Inbox', async ({ page }) => {
    await page.goto('/workspace/inbox')
    const agent = await firstRosterAgent(page)
    test.skip(!agent, 'no agent on the roster to open')

    await page.goto(`/workspace?agent=${encodeURIComponent(agent)}`)
    await page.waitForLoadState('networkidle')
    expect(new URL(page.url()).pathname).not.toBe('/workspace/inbox')
    await expect(page.getByTestId('inbox')).toHaveCount(0)

    await page.goto('/workspace?new=1')
    await page.waitForLoadState('networkidle')
    expect(new URL(page.url()).pathname).not.toBe('/workspace/inbox')
  })

  test('@smoke the Inbox list does not shift as its data arrives', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/workspace/inbox')
    // With no roster the Inbox never renders — the stage falls through to the
    // roster copy (D9) — so there is no list to measure. Skip, never fail (#2199).
    test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox list to measure')
    const inbox = page.getByTestId('inbox')
    await inbox.waitFor({ timeout: 20000 })
    // The list's top edge RELATIVE to the Inbox root, from the first frame the
    // Inbox exists through the asks and thread reads settling.
    const tops = []
    for (let i = 0; i < 20; i++) {
      const t = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="inbox"]')
        const list = root?.querySelector('[data-testid="inbox-list"], [data-testid="inbox-list-loading"], [data-testid="inbox-empty"], [data-testid="inbox-list-failed"]')
        if (!root || !list) return null
        return Math.round(list.getBoundingClientRect().top - root.getBoundingClientRect().top)
      })
      if (t !== null) tops.push(t)
      await page.waitForTimeout(100)
    }
    expect(tops.length, 'never measured the list').toBeGreaterThan(5)
    const spread = Math.max(...tops) - Math.min(...tops)
    expect(spread, `the list moved ${spread}px while loading — ${tops.join(',')}`).toBeLessThanOrEqual(1)
  })

  // #3060: the Inbox's first load, sampled on EVERY animation frame from the
  // document's start (an init script, so the first paint is in the record).
  // Every Workspace read is held back ~700ms, so the stage skeleton, the
  // Inbox's own loading state and the late counts are each on screen for many
  // frames — without the delay a fast local stack settles before the first
  // sample and the arm proves nothing. Geometry is CONTAINER-relative (#2711):
  // the stage wrapper both the skeleton and the Inbox mount into.
  async function sampleInboxLoad(page, width) {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript(() => {
      const out = []
      window.__inboxFrames = out
      const rel = (el, base) => {
        if (!el || !base) return null
        const r = el.getBoundingClientRect()
        const b = base.getBoundingClientRect()
        return { l: Math.round(r.left - b.left), r: Math.round(r.right - b.left), t: Math.round(r.top - b.top) }
      }
      const tick = () => {
        const root = document.querySelector('[data-testid="inbox-skeleton"], [data-testid="inbox"]')
        if (root) {
          const base = root.parentElement
          const strip = root.querySelector('[role="tablist"][aria-label="Inbox"]')
          const tabs = strip ? [...strip.querySelectorAll('[role="tab"]')].map((t) => rel(t, base)) : []
          const list = root.querySelector('[data-testid="inbox-skeleton-list"], [data-testid="inbox-list-column"]')
          // A LANDED count: a visible numeric span in a tab — not the slot
          // attribute, so the arm still sees counts when the slot is absent.
          const badges = strip ? [...strip.querySelectorAll('[role="tab"] span')]
            .filter((b) => /^\d+\+?$/.test(b.textContent.trim()) && getComputedStyle(b).visibility !== 'hidden').length : 0
          out.push({ phase: root.dataset.testid, frame: rel(root, base), tabs, list: rel(list, base), badges })
        }
        if (out.length < 400) requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    await page.route('**/api/enterprise/client-portal/**', async (route) => {
      await new Promise((r) => setTimeout(r, 700))
      await route.continue()
    })
    await page.goto('/workspace/inbox')
    await page.getByTestId('inbox').waitFor({ timeout: 30000 })
    // Not `networkidle`: the Workspace polls, so it may never be idle. The
    // held-back reads settle within ~2s of the Inbox mounting.
    await page.waitForTimeout(3000)
    return page.evaluate(() => window.__inboxFrames)
  }
  const spreadOf = (xs) => Math.max(...xs) - Math.min(...xs)

  for (const width of [1440, 1024]) {
    test(`@interactive skeleton → ready: no column or tab moves at ${width}px (#3060)`, async ({ page }) => {
      await page.goto('/workspace/inbox')
      test.skip(!await firstRosterAgent(page), 'no agent on the roster, so the stage never reaches the Inbox')
      const frames = await sampleInboxLoad(page, width)
      const phases = new Set(frames.map((f) => f.phase))
      expect(phases.has('inbox-skeleton'), 'never saw the Inbox skeleton — the stage drew another shape').toBe(true)
      expect(phases.has('inbox'), 'never saw the Inbox').toBe(true)
      // The frame's RIGHT edge is the rail column's (reserved while the stage
      // loads, then settled by whether the Inbox previews a row) — not what
      // the skeleton decides. So left / top are held across every frame, and
      // the list's right edge (which a stacked list takes from the frame)
      // across the frames at the skeleton's frame width.
      const skelWidth = frames.find((f) => f.phase === 'inbox-skeleton').frame.r
      for (const [name, pick] of [
        ['the frame', (f) => f.frame && [f.frame.l, f.frame.t]],
        ['the list column', (f) => f.list && [f.list.l, f.list.t]],
        ['the list column\'s right edge', (f) => f.list && f.frame && f.frame.r === skelWidth && [f.list.r]],
        ['the tab strip', (f) => f.tabs.length === 3 && f.tabs.flatMap((t) => [t.l, t.t])],
      ]) {
        const rows = frames.map(pick).filter(Boolean)
        expect(rows.length, `never measured ${name}`).toBeGreaterThan(5)
        for (let k = 0; k < rows[0].length; k++) {
          const xs = rows.map((r) => r[k])
          expect(spreadOf(xs), `${name} moved across the load at ${width}px (coord ${k}): ${[...new Set(xs)].join(' → ')}`).toBeLessThanOrEqual(1)
        }
      }
    })
  }

  test('@interactive the tabs do not slide when the counts land (#3060)', async ({ page }) => {
    await page.goto('/workspace/inbox')
    test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox')
    const frames = (await sampleInboxLoad(page, 1280)).filter((f) => f.tabs.length === 3)
    const before = frames.filter((f) => f.badges === 0)
    const after = frames.filter((f) => f.badges > 0)
    test.skip(!after.length, 'no pending ask and no unread chat, so no count ever lands')
    expect(before.length, 'the counts were already there on the first frame').toBeGreaterThan(0)
    // Unread's and All's left edges: the ones a landing count would push.
    for (const i of [1, 2]) {
      const xs = [...before, ...after].map((f) => f.tabs[i].l)
      expect(spreadOf(xs), `tab ${i} slid as the counts landed: ${[...new Set(xs)].join(' → ')}`).toBeLessThanOrEqual(1)
    }
  })

  // §3g A4: at 768px (sidebar beside it) and at 640×400 (a 1280 window at 200%)
  // the Inbox's container is under 720px, so it STACKS: an opened row's pane
  // takes the whole Inbox instead of the ~150px (768) / 64px (zoomed) the
  // viewport rule left it. Relative geometry only — the pane against the Inbox
  // root, never an absolute pixel floor.
  for (const vp of [{ width: 768, height: 900 }, { width: 640, height: 400 }]) {
    test(`an opened row's pane takes the Inbox's width at ${vp.width}×${vp.height}`, async ({ page }) => {
      await page.setViewportSize(vp)
      await page.goto('/workspace/inbox?tab=all')
      test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox')
      const inbox = page.getByTestId('inbox')
      await inbox.waitFor({ timeout: 20000 })
      const row = page.locator('[data-inbox-row]').first()
      test.skip(!await row.count(), 'nothing in All to open')
      await expect(inbox).toHaveAttribute('data-layout', 'stacked')
      await row.click()
      const pane = page.getByTestId('inbox-pane')
      await expect(pane).toBeVisible()
      await expect(page.getByTestId('inbox-list-column')).toBeHidden()
      const ratio = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="inbox"]').getBoundingClientRect()
        const p = document.querySelector('[data-testid="inbox-pane"]').getBoundingClientRect()
        return p.width / root.width
      })
      expect(ratio, `the pane is ${Math.round(ratio * 100)}% of the Inbox`).toBeGreaterThan(0.95)
    })
  }

  // §3g A6: on a phone, opening a row PUSHES, so the browser's Back returns to
  // the list — in the Inbox, focus on the row — instead of leaving the Workspace.
  test('a phone Back after opening a row stays in the Inbox', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/workspace/inbox?tab=all')
    test.skip(!await firstRosterAgent(page), 'no agent on the roster, so no Inbox')
    await page.getByTestId('inbox').waitFor({ timeout: 20000 })
    const row = page.locator('[data-inbox-row]').first()
    test.skip(!await row.count(), 'nothing in All to open')
    const key = await row.getAttribute('data-inbox-row')
    await row.click()
    await expect(page.getByTestId('inbox-pane')).toBeVisible()
    // F6: the pane's Back is a 44px target on a phone.
    const paneBack = await page.getByTestId('inbox-pane-back').boundingBox()
    expect(paneBack.height).toBeGreaterThanOrEqual(44)
    await page.goBack()
    await expect(page).toHaveURL(/\/workspace\/inbox/)
    await expect(page.getByTestId('inbox-pane')).toHaveCount(0)
    await expect(page.locator(`[data-inbox-row="${key}"]`)).toBeFocused()
    // F6: the touch targets that remain are at least 44px tall.
    const menu = await page.getByTestId('inbox-menu').boundingBox()
    expect(menu.height).toBeGreaterThanOrEqual(44)
  })
})
