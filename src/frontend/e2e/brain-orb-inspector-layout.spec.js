import { test, expect } from '@playwright/test'
import { openOrb, growInspector, rectOf, LONG_TITLE } from './helpers/brain-orb.js'

/**
 * #2539 — Brain Orb: the node inspector must not overlap the state card.
 *
 * `#stateBox` (top:26px, ~174px tall) and `#inspector` (vertically centred,
 * up to 82vh) are both right-anchored `.panel`s at z-index 10. Before the fix
 * a tall inspector rose into the state card and, being later in DOM order,
 * painted over "STATE / Idle" (measured overlap 103–128px at common desktop
 * viewports). The fix is CSS only: the inspector stays centred while short
 * and its TOP edge is floored at 224px (26 + 174 + 24 gap) once tall; its
 * height is capped so the bottom edge stays 26px inside the viewport. A
 * dragged or restored inspector (inline `left/top`, `transform:none`,
 * `right:auto`) is untouched by the floor.
 *
 * Viewports: the three the issue names, the real iframe height at 1280×800
 * (window minus the ~44px host header), and one under the ≤760px media query.
 *
 * Standalone page, FALLBACK graph: no agent, no auth. Skips without WebGL.
 */

const VIEWPORTS = [
  [1280, 800],
  [1440, 900],
  [1920, 1080],
  [1280, 756],
  [720, 900],
]
const GAP = 16
const FLOOR_PREMISE_PX = 200 // #stateBox.bottom must stay ≤ 200 (26 + ≤174)
const LONGEST_STATE_DESC = 'spreading activation · lateral inhibition · intent aurora' // STATES.thinking
const LONG_BOOT_DESC = 'reflecting last run · converged 3 days ago' // the boot-time override shape

test.describe('brain orb — inspector floored below the state card (#2539)', () => {
  for (const [w, h] of VIEWPORTS) {
    test(`@smoke B1 a tall inspector never rises into the state card at ${w}×${h}`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h })
      await openOrb(page)
      await growInspector(page, { title: LONG_TITLE, paragraphs: 40, connections: 6 })

      const state = await rectOf(page, 'stateBox')
      const insp = await rectOf(page, 'inspector')
      expect(insp.height).toBeGreaterThan(0)
      expect(insp.top, `inspector.top ${insp.top} vs stateBox.bottom ${state.bottom}`).toBeGreaterThanOrEqual(state.bottom + GAP)
      expect(insp.bottom, `inspector.bottom ${insp.bottom} vs viewport ${h}`).toBeLessThanOrEqual(h - 8)

      // The state value is the hit-target at its own centre — the inspector
      // never paints over it. At the ≤760px media-query viewport the
      // top-centred #dock (wider than the viewport, z-index 10) already covers
      // the value row before this fix and independently of the inspector — a
      // pre-existing narrow-width collision outside #2539's scope (follow-up
      // with the #controls / #scopePanel gutter collisions) — so there the
      // assertion is the inspector-specific claim only.
      const hit = await page.evaluate(() => {
        const r = document.getElementById('stateName').getBoundingClientRect()
        const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return {
          insideStateBox: !!(el && el.closest('#stateBox')),
          insideInspector: !!(el && el.closest('#inspector')),
          hit: el ? (el.id ? `#${el.id}` : el.tagName.toLowerCase()) : 'none',
        }
      })
      expect(hit.insideInspector, `elementFromPoint at #stateName centre hit ${hit.hit}`).toBe(false)
      if (w > 760) expect(hit.insideStateBox, `elementFromPoint at #stateName centre hit ${hit.hit}`).toBe(true)

      // The floor's premise is asserted, not assumed: the longest STATES
      // string and a long boot string must keep the card's bottom ≤ 200px.
      for (const desc of [LONGEST_STATE_DESC, LONG_BOOT_DESC]) {
        await page.evaluate((d) => {
          document.getElementById('stateDesc').textContent = d
        }, desc)
        const s = await rectOf(page, 'stateBox')
        expect(s.bottom, `stateBox.bottom with desc "${desc}"`).toBeLessThanOrEqual(FLOOR_PREMISE_PX)
        const i = await rectOf(page, 'inspector')
        expect(i.top).toBeGreaterThanOrEqual(s.bottom + GAP)
      }
    })
  }

  test('@smoke B2 a short inspector is still vertically centred', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openOrb(page)
    await growInspector(page, { title: 'Short note', paragraphs: 1, paragraphText: 'One short sentence.', connections: 1 })
    const insp = await rectOf(page, 'inspector')
    // Premise first: the box must sit inside the centring band (h ≤ 100vh − 2×224),
    // otherwise the floor legitimately applies and "centred" is the wrong claim.
    expect(insp.height, `inspector height ${insp.height} must be ≤ ${900 - 448} to be centrable`).toBeLessThanOrEqual(900 - 448)
    const centreY = (insp.top + insp.bottom) / 2
    expect(Math.abs(centreY - 450), `centreY ${centreY}`).toBeLessThanOrEqual(1.5)
  })

  test('@smoke B3 dragging the inspector still moves it and the position survives a reload', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openOrb(page)
    await growInspector(page)
    try {
      const title = await page.locator('#inspTitle').boundingBox()
      const startX = title.x + 10
      const startY = title.y + 10
      await page.mouse.move(startX, startY)
      await page.mouse.down()
      await page.mouse.move(startX + 80, startY + 80, { steps: 8 })
      await page.mouse.up()

      const inline = await page.evaluate(() => {
        const s = document.getElementById('inspector').style
        return { transform: s.transform, right: s.right, left: s.left, top: s.top }
      })
      expect(inline.transform).toBe('none')
      expect(inline.right).toBe('auto')
      expect(inline.left).not.toBe('')
      expect(inline.top).not.toBe('')

      const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('orbPanels') || '{}').inspector)
      expect(saved).toBeTruthy()

      await page.reload()
      await openOrb(page)
      const restored = await page.evaluate(() => {
        const s = document.getElementById('inspector').style
        return { transform: s.transform, right: s.right, left: s.left, top: s.top }
      })
      expect(restored.transform).toBe('none')
      expect(restored.right).toBe('auto')
      expect(restored.left).toBe(saved.l)
      expect(restored.top).toBe(saved.t)
    } finally {
      await page.evaluate(() => localStorage.removeItem('orbPanels')).catch(() => {})
    }
  })
})
