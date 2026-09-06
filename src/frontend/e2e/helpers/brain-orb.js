import { test, expect } from '@playwright/test'

/**
 * Shared helpers for the Brain Orb HUD specs (#2538 wheel routing, #2539
 * inspector layout).
 *
 * The orb is a vanilla ES module under `public/brain-orb/` — vitest cannot
 * load it (it imports `./vendor/three.module.js` and touches WebGL at module
 * top level), so Playwright against the STANDALONE page is the only harness.
 * `/brain-orb/index.html` opened outside the AgentBrainOrb.vue iframe has no
 * parent frame, so the embed handshake resolves `null`, `./data.json` answers
 * with the SPA's HTML, `r.json()` throws, and the built-in FALLBACK graph
 * renders: **no agent, no auth, no network beyond the static assets**. The
 * inherited admin storageState is harmless.
 *
 * Capability contract (the suite's rule: an absent environment capability
 * reads as SKIPPED, never broken — see e2e/README.md → Fixture agents):
 *   - no WebGL context in this Chromium → `test.skip` (a runner limitation,
 *     not a product defect — it must never turn the nightly red);
 *   - WebGL present but the orb never boots → HARD FAIL (a real regression).
 *
 * Observables, both pre-existing (no source seam was added for the tests):
 *   - `window.orbState().camTargetDist` — the cumulative zoom target;
 *   - `WheelEvent.defaultPrevented` as read back from a synthetic event after
 *     dispatch — the orb's window-level listener is registered first, so its
 *     `preventDefault()` is visible on the event object afterwards.
 *
 * NOTE: this file lives under `e2e/helpers/` and is NOT collected as a test
 * (Playwright's default testMatch wants `.spec`/`.test`).
 */

export const ORB_PATH = '/brain-orb/index.html'

/** The issue's own long title — wraps to 6 lines at the inspector's 384px. */
export const LONG_TITLE =
  'Aeroponics RESEARCH — multi-system greenhouse tomato yield comparison across ' +
  'nutrient-film, deep-water and high-pressure aeroponic rigs (season 3 interim report)'

/**
 * Navigate to the standalone orb and wait for it to boot.
 * Skips (not fails) when the Chromium has no WebGL at all; fails loudly when
 * WebGL is present but `window.orbState` never appears.
 */
export async function openOrb(page) {
  await page.goto(ORB_PATH)
  const hasWebGL = await page.evaluate(() => {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl'))
  })
  test.skip(!hasWebGL, 'no WebGL in this Chromium')
  try {
    await page.waitForFunction(() => typeof window.orbState === 'function', null, { timeout: 15000 })
  } catch {
    throw new Error(
      'Brain Orb did not boot: window.orbState never appeared although WebGL is available — ' +
        'a real regression in public/brain-orb/orb.js, not a runner limitation'
    )
  }
  await expect(page.locator('#loading')).toBeHidden({ timeout: 15000 })
}

/**
 * Make the inspector tall the way a long note does, then show it.
 * The first injected paragraph carries `id="e2eInspPara"` so specs can target
 * a deep element inside `#inspContent` by id.
 */
export async function growInspector(page, { title = LONG_TITLE, paragraphs = 40, connections = 6 } = {}) {
  await page.evaluate(
    ({ title, paragraphs, connections }) => {
      document.getElementById('inspTitle').textContent = title
      const body = document.getElementById('inspContent')
      body.innerHTML = Array.from(
        { length: paragraphs },
        (_, i) => `<p${i === 0 ? ' id="e2eInspPara"' : ''}>paragraph ${i + 1} — ${'lorem ipsum dolor sit amet '.repeat(6)}</p>`
      ).join('')
      const conns = document.getElementById('inspConns')
      conns.innerHTML = Array.from(
        { length: connections },
        (_, i) => `<div class="conn"><span class="et">relates_to</span><span>connection ${i + 1}</span></div>`
      ).join('')
      const insp = document.getElementById('inspector')
      insp.scrollTop = 0
      body.scrollTop = 0
      insp.classList.add('show')
    },
    { title, paragraphs, connections }
  )
}

/**
 * Dispatch ONE synthetic, cancelable wheel event on `selector` (an id
 * selector, or the literal `'document'`) and return `defaultPrevented`.
 * Synthetic wheel events never scroll anything — that is what makes the
 * routing assertions deterministic; specs move `scrollTop` by hand.
 */
export function wheelAt(page, selector, { deltaY = 0, deltaX = 0, ctrlKey = false } = {}) {
  return page.evaluate(
    ({ selector, deltaY, deltaX, ctrlKey }) => {
      const target = selector === 'document' ? document : document.querySelector(selector)
      if (!target) throw new Error(`wheelAt: no element for ${selector}`)
      const e = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY, deltaX, ctrlKey })
      target.dispatchEvent(e)
      return e.defaultPrevented
    },
    { selector, deltaY, deltaX, ctrlKey }
  )
}

/** `window.orbState().camTargetDist` — the orb's pre-existing debug seam. */
export function camTarget(page) {
  return page.evaluate(() => window.orbState().camTargetDist)
}

/** `getBoundingClientRect()` of `#id` as a plain object. */
export function rectOf(page, id) {
  return page.evaluate((id) => {
    const r = document.getElementById(id).getBoundingClientRect()
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height, width: r.width }
  }, id)
}
