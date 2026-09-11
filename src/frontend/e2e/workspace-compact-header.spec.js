import { test, expect } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * Workspace compact header (trinity-enterprise#547) + the band's stability
 * (abilityai/trinity#2580 defect 1).
 *
 * Every claim here is a MEASUREMENT or a request count, which is why it is an
 * e2e spec and not a unit one. `vitest.config.js` pins `environment: 'node'`
 * with no layout engine and no component-mount harness, so the node suite can
 * prove a class string is present and nothing more — and "the band is at most
 * 60% of its old height", "the composer's buttons are 44px boxes in this order"
 * and "switching chat tabs issued no request" are none of them source
 * properties. `portal-agent-page-overview.spec.js` set the precedent (ent#245:
 * a wrapper interposed a box and every unit and e2e test stayed green because
 * nothing measured).
 *
 * What must hold:
 *   1. the band is ≤ 60% of its pre-ent#547 height at 1440px, with the stats
 *      strip still present, and carries no period selector and no chart legend
 *   2. switching chat tabs on ONE agent issues no `/page` request and shows no
 *      loading placeholder in the band — with a CONTROL proving the probe can
 *      see a request at all, or a zero would prove nothing
 *   3. the composer row is voice-call · attach · dictate · send, every button a
 *      44px box, and the call toggle is NOT inside the region that goes
 *      `pointer-events-none` during a call — it is what ENDS the call
 *   4. the header no longer carries the Agent-details, Files or voice controls
 *   5. Agent info is reachable as a rail tab
 *
 * @interactive — needs a live stack and a real agent on the caller's roster.
 * Required env: ADMIN_PASSWORD (auth.setup.js) and PORTAL_TEST_AGENT
 * (defaults to "testfix"). A MISSING fixture agent reads as SKIPPED, never
 * broken (#2199) — hence the describe-level guard.
 */

const TEST_AGENT = process.env.PORTAL_TEST_AGENT || 'testfix'

// Measured on `dev` at 1440px before ent#547, decomposed so a future reader can
// re-derive it rather than trust it: 10+10 padding, a 78px chart column (10px
// title + 6px gap + 44px bars + 4px + 14px day labels), stats blocks 39px.
const BAND_HEIGHT_BEFORE = 99
const BAND_MAX = Math.floor(BAND_HEIGHT_BEFORE * 0.6)   // 59px

const openAgent = async (page) => {
  await page.goto(`/workspace/a/${TEST_AGENT}`)
  await expect(page.getByTestId('portal-agent-band')).toBeVisible({ timeout: 20000 })
}

test.describe('Workspace compact header', () => {
  test.beforeEach(async ({ baseURL }) => {
    test.skip(
      !(await agentExists(TEST_AGENT, { baseURL })),
      missingAgentReason(TEST_AGENT, 'PORTAL_TEST_AGENT')
    )
  })

  test('@interactive the band is compact, and the stats strip is what now sets its height', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    const probe = await page.evaluate(() => {
      const band = document.querySelector('[data-testid="portal-agent-band"]')
      const r = band.getBoundingClientRect()
      const chartBlock = band.querySelector('.w-\\[26rem\\]')
      return {
        height: Math.round(r.height),
        chartBlockHeight: chartBlock ? Math.round(chartBlock.getBoundingClientRect().height) : null,
        hasPeriodSelector: !!band.querySelector('select'),
        // The side legend is a flex column of swatch+label rows.
        hasSideLegend: !!(chartBlock && chartBlock.querySelector('.flex-col.gap-0\\.5')),
        statsText: band.textContent.replace(/\s+/g, ' ').trim(),
      }
    })

    expect(probe.height).toBeLessThanOrEqual(BAND_MAX)
    // The AC keeps the stats strip. Asserted by its captions, not by a class:
    // a height target is trivially met by deleting the thing being measured.
    expect(probe.statsText).toMatch(/tasks · last 7 days/)
    expect(probe.statsText).toMatch(/completed/)
    expect(probe.statsText).toMatch(/first try/)
    // Fixed at 7 days: no selector, and no legend beside the bars.
    expect(probe.hasPeriodSelector).toBe(false)
    expect(probe.hasSideLegend).toBe(false)
    // The load-bearing invariant behind the number: the chart column must stay
    // within one stat block (39px), or it starts driving the band's height
    // again and the target moves with the data.
    if (probe.chartBlockHeight !== null) expect(probe.chartBlockHeight).toBeLessThanOrEqual(39)
  })

  test('@interactive switching chat tabs on one agent neither refetches nor flashes the band', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    // Count `/page` requests at the network layer rather than by patching
    // `fetch` in the page: Playwright sees the request whichever transport the
    // API client uses, and cannot be defeated by load order.
    const pageRequests = []
    page.on('request', (r) => { if (r.url().includes('/page')) pageRequests.push(r.url()) })

    const tabs = page.getByTestId('portal-chat-tabs').locator('nav button')
    const count = await tabs.count()
    test.skip(count < 2, `needs at least two chats on ${TEST_AGENT}; found ${count}`)

    const before = pageRequests.length
    let placeholderSeen = false
    for (let round = 0; round < 2; round++) {
      for (let i = 0; i < Math.min(count, 3); i++) {
        await tabs.nth(i).click()
        await page.waitForTimeout(500)
        // A skeleton or a scanline inside the band is the visible half of the
        // defect. Sampled after each switch; the band must never be in a
        // "no data yet" state for an agent whose page is already in hand.
        placeholderSeen ||= await page.evaluate(() => {
          const b = document.querySelector('[data-testid="portal-agent-band"]')
          return !!b && !!(b.querySelector('[aria-busy="true"]') || b.querySelector('.animate-pulse'))
        })
      }
    }
    const causedBySwitching = pageRequests.length - before
    expect(placeholderSeen).toBe(false)
    expect(causedBySwitching).toBe(0)

    // CONTROL. Without it a zero above is unfalsifiable — it would read the
    // same if the listener were simply blind. Reloading the agent must produce
    // at least one `/page` request.
    const beforeControl = pageRequests.length
    await page.reload()
    await expect(page.getByTestId('portal-agent-band')).toBeVisible({ timeout: 20000 })
    expect(
      pageRequests.length,
      'control failed: the probe never saw a /page request, so the zero above proves nothing'
    ).toBeGreaterThan(beforeControl)
  })

  test('@interactive the composer owns attach and voice, and the call toggle stays live during a call', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    const probe = await page.evaluate(() => {
      const call = document.querySelector('[data-testid="portal-voice-call"]')
      const form = call ? call.closest('form') : document.querySelector('form');
      // A DESCENDANT query, not a direct-children walk. #2662 wrapped the
      // composer in a shell, so the inert region is `form` -> `div.rounded-2xl`
      // -> `div.mt-1.flex` -> here, two levels below the form. The children walk
      // this replaces matched nothing after that change and left both assertions
      // below reading `null` — and this spec is `@interactive`, which CI never
      // runs (frontend-e2e.yml runs `@smoke` only), so nothing would have said so.
      const inert = form && form.querySelector('.flex-1.min-w-0')
      const buttons = form ? [...form.querySelectorAll('button')] : []
      const header = document.querySelector('header')
      return {
        callRendered: !!call,
        order: buttons.map((b) => (b.getAttribute('title') || b.getAttribute('aria-label') || '?')),
        boxes: buttons.map((b) => {
          const r = b.getBoundingClientRect()
          return { w: Math.round(r.width), h: Math.round(r.height) }
        }),
        inertRegionFound: !!inert,
        callInsideInertRegion: call && inert ? inert.contains(call) : null,
        attachInsideInertRegion: inert ? !!inert.querySelector('button[title^="Attach"]') : null,
        headerText: header ? header.textContent.replace(/\s+/g, ' ').trim() : '',
        headerHasDetails: !!document.querySelector('[data-testid="portal-open-agent-details"]'),
        headerHasVoice: !!(header && header.querySelector('[data-testid="portal-voice-call"]')),
        headerFileButtons: header
          ? [...header.querySelectorAll('button')].filter((b) => /Files/i.test(b.getAttribute('title') || '')).length
          : null,
      }
    })

    // AC 3 + AC 4: the header keeps none of the three moved controls.
    expect(probe.headerHasDetails).toBe(false)
    expect(probe.headerHasVoice).toBe(false)
    expect(probe.headerFileButtons).toBe(0)

    if (probe.callRendered) {
      // Ruled order: voice call, then attach, then dictation, then send.
      expect(probe.order[0]).toMatch(/voice call/i)
      expect(probe.order[1]).toMatch(/Attach/i)
      // THE dead-affordance guard. The composer goes `pointer-events-none` for
      // the call's duration; the toggle that ENDS the call must sit outside
      // that region or it renders pressed and refuses the click — the exact
      // dead control ent#547's own AC 5 forbids, created by AC 4's move.
      // Named separately so the next nesting change reports its own cause
      // instead of a confusing `null !== false` two lines down.
      expect(
        probe.inertRegionFound,
        'the inert region was not found — the composer nesting moved again'
      ).toBe(true)
      expect(probe.callInsideInertRegion).toBe(false)
      expect(probe.attachInsideInertRegion).toBe(true)
    }
    // #2259's 44px boxes still hold for every button in the row, including the
    // one that just moved in from the header at `p-2`.
    for (const b of probe.boxes) {
      expect(b.w).toBe(44)
      expect(b.h).toBe(44)
    }
  })

  test('@interactive Agent info is reachable as a rail tab', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    // The rail may place Info under the counted overflow depending on width —
    // that is OverflowTabs working, not a failure. Try the strip, then the menu.
    const rail = page.getByTestId('portal-rail')
    const direct = rail.getByRole('button', { name: 'Info', exact: true })
    if (await direct.count()) {
      await direct.first().click()
    } else {
      await rail.locator('button[data-overflow-trigger]').click()
      await page.locator('#overflow-tabs-menu').getByRole('button', { name: 'Info', exact: true }).click()
    }

    const panel = page.getByTestId('portal-agent-details')
    await expect(panel).toBeVisible()
    await expect(panel.getByRole('heading', { name: 'Your chats' })).toBeVisible()

    // The rail body is the single scroll axis (contract principle 8). A panel
    // that kept its own `overflow-y-auto` would nest two scrollbars.
    const nested = await page.evaluate(() => {
      const p = document.querySelector('[data-testid="portal-agent-details"]')
      return [p, ...p.querySelectorAll('*')].filter((e) => {
        const o = getComputedStyle(e).overflowY
        return o === 'auto' || o === 'scroll'
      }).length
    })
    expect(nested).toBe(0)
  })
})
