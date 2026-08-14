import { test, expect } from '@playwright/test'

/**
 * Workspace agent page — Overview geometry (#2169).
 *
 * The three defects this pins are all layout, and layout is the one thing the
 * unit suite structurally cannot see: node/vitest has no layout engine and this
 * project has no component-mount harness, so a source guard can prove the class
 * is on the element and nothing more. The recorded precedent is ent#245 — a
 * wrapper interposed a box, percentage-sized children rendered 24px inside a
 * 32px zone, and it was green in every unit and e2e test because nothing
 * measured. So the assertions here are all `getBoundingClientRect`.
 *
 * What must hold:
 *   1. the top row is TWO equal columns whether or not the agent has open asks
 *      — the column count used to be bound to `asks.length`, so the page
 *      changed shape when a transient operator-queue item opened or closed
 *   2. "Waiting on you", when present, is BELOW that row and full width — not a
 *      third column inside it
 *   3. below the `xl` breakpoint the row stacks, with no horizontal overflow
 *   4. every avatar keeps its outer footprint — the edge is drawn inside the
 *      box (border-box), so no row alignment moves
 *
 * The breakpoint is `xl` (1280) and not `lg` (1024) on measurement, not taste:
 * the Workspace holds a 288px sidebar, so at 1024 each column is ~332px, where
 * the 30-day x-axis truncates to nothing and the nine-bucket legend wraps three
 * to five lines. 1024 is therefore asserted as a STACKED width here, which is
 * the opposite of what an `lg` reading of the issue would expect.
 *
 * @interactive — needs a live stack and a real agent on the caller's roster.
 * The portal is reachable from the cached admin session
 * (`isClientSignedIn = !!portalToken || isPlatformSession`).
 *
 * Required env: ADMIN_PASSWORD (auth.setup.js) and PORTAL_TEST_AGENT
 * (defaults to "testfix"). The agent must exist and be visible to the admin.
 */

const TEST_AGENT = process.env.PORTAL_TEST_AGENT || 'testfix'

const STACKED = [640, 900, 1024, 1279]
const SPLIT = [1280, 1440, 1600]

// Locate the two top-row sections by their headings and report their boxes.
// Headings rather than classes: a class selector re-encodes the implementation
// the test exists to check, so it would pass on a grid that renders wrong.
const PROBE = () => {
  const heading = (text) =>
    [...document.querySelectorAll('h2')].find((h) =>
      (h.textContent || '').trim().startsWith(text)
    )

  const sectionFor = (text) => {
    const h = heading(text)
    return h ? h.closest('section') : null
  }

  const box = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }
  }

  const activity = sectionFor('Activity ·')
  const work = sectionFor('Recent work')
  const asks = sectionFor('Waiting on you')

  return {
    found: { activity: !!activity, work: !!work, asks: !!asks },
    activity: box(activity),
    work: box(work),
    asks: box(asks),
    // Page-level horizontal overflow, the failure a narrow viewport produces.
    overflowX: Math.round(
      document.documentElement.scrollWidth - document.documentElement.clientWidth
    ),
  }
}

// Outer footprint of every portal avatar, keyed by its title (the agent name).
// The edge is a border on a border-box element, so these must be exactly the
// sizes the call sites pass — 1px of growth here shifts every row it sits in.
const AVATAR_PROBE = () =>
  [...document.querySelectorAll('span.rounded-full')]
    .filter((s) => s.style && s.style.width && s.style.height)
    .map((s) => {
      const r = s.getBoundingClientRect()
      return {
        declared: parseInt(s.style.width, 10),
        w: Math.round(r.width),
        h: Math.round(r.height),
      }
    })

const openAgent = async (page) => {
  await page.goto(`/workspace/a/${TEST_AGENT}`)
  // The Overview heading only renders once the page payload lands; the header
  // and tabs paint from the route, so waiting on the shell would race.
  await expect(page.getByRole('heading', { name: /^Activity ·/ })).toBeVisible({ timeout: 20000 })
}

test.describe('Workspace agent page Overview layout', () => {
  test('@interactive the top row is two equal columns at every split width', async ({ page }) => {
    await openAgent(page)

    for (const width of SPLIT) {
      await page.setViewportSize({ width, height: 900 })
      const m = await page.evaluate(PROBE)

      expect(m.found, `sections present at ${width}`).toMatchObject({ activity: true, work: true })
      // Same row: equal top edge, side by side.
      expect(Math.abs(m.activity.y - m.work.y), `same row at ${width}`).toBeLessThanOrEqual(1)
      expect(m.work.x, `recent work sits right of activity at ${width}`).toBeGreaterThan(m.activity.x)
      // 50/50, within a pixel of rounding.
      expect(Math.abs(m.activity.w - m.work.w), `equal columns at ${width}`).toBeLessThanOrEqual(1)
      expect(m.overflowX, `no horizontal overflow at ${width}`).toBeLessThanOrEqual(1)
    }
  })

  test('@interactive the split does not depend on whether asks are present', async ({ page }) => {
    // The headline AC. This is the assertion the unit guards cannot make: the
    // conditional-class defect showed up as a DIFFERENT COLUMN WIDTH for the
    // same agent depending on a transient operator-queue item, so what is
    // compared here is geometry against an agent-independent expectation —
    // each column is half the row, whichever of the two states this agent is in.
    await openAgent(page)
    await page.setViewportSize({ width: 1440, height: 900 })

    const m = await page.evaluate(PROBE)
    const rowWidth = m.work.x + m.work.w - m.activity.x

    expect(Math.abs(m.activity.w - m.work.w)).toBeLessThanOrEqual(1)
    // Neither column is the full row — which is exactly what the collapsed,
    // no-asks state used to render.
    expect(m.activity.w).toBeLessThan(rowWidth * 0.75)
    expect(m.activity.w).toBeGreaterThan(rowWidth * 0.25)
  })

  test('@interactive asks, when present, sit below the row at full width', async ({ page }) => {
    await openAgent(page)
    await page.setViewportSize({ width: 1440, height: 900 })

    const m = await page.evaluate(PROBE)
    test.skip(!m.found.asks, `agent "${TEST_AGENT}" has no open asks — nothing to place`)

    const rowBottom = Math.max(m.activity.y + m.activity.h, m.work.y + m.work.h)
    expect(m.asks.y, 'below the row, not a third column in it').toBeGreaterThanOrEqual(rowBottom)
    // Full width: spans from the activity column's left edge to recent work's
    // right edge. A third grid child would be half of that.
    const rowWidth = m.work.x + m.work.w - m.activity.x
    expect(Math.abs(m.asks.w - rowWidth)).toBeLessThanOrEqual(2)
  })

  test('@interactive the row stacks below the breakpoint with no overflow', async ({ page }) => {
    await openAgent(page)

    for (const width of STACKED) {
      await page.setViewportSize({ width, height: 900 })
      const m = await page.evaluate(PROBE)

      expect(m.work.y, `stacked at ${width}`).toBeGreaterThan(m.activity.y)
      expect(Math.abs(m.activity.x - m.work.x), `same left edge at ${width}`).toBeLessThanOrEqual(1)
      expect(m.overflowX, `no horizontal overflow at ${width}`).toBeLessThanOrEqual(1)
    }
  })

  test('@interactive the avatar edge does not change any outer footprint', async ({ page }) => {
    await openAgent(page)
    await page.setViewportSize({ width: 1440, height: 900 })

    const avatars = await page.evaluate(AVATAR_PROBE)
    expect(avatars.length, 'at least the page header avatar').toBeGreaterThan(0)
    for (const a of avatars) {
      expect(a.w, `avatar declared ${a.declared}px renders ${a.w}px wide`).toBe(a.declared)
      expect(a.h, `avatar declared ${a.declared}px renders ${a.h}px tall`).toBe(a.declared)
    }
  })
})
