import { test, expect } from '@playwright/test'
import { openOrb, growInspector, wheelAt, camTarget } from './helpers/brain-orb.js'

/**
 * #2538 — Brain Orb: trackpad momentum must never leak into camera zoom.
 *
 * The orb's single window-level `wheel` listener routes each event either to
 * native panel scrolling or to `camTargetDist` (zoom). Before the fix that
 * decision was made PER EVENT, so the macOS inertia tail that keeps arriving
 * after a panel reaches its edge was routed to zoom. The fix latches the route
 * per GESTURE: the first vertical event of a run of wheel events (inter-event
 * gap ≤ 150 ms) decides, and every later event of that gesture keeps the
 * route. A FRESH gesture (gap > 150 ms) over an already-edged panel still
 * zooms — the deliberate "scroll to the end, then keep going to zoom out"
 * chaining is preserved. Scrollability is read from computed style, so the
 * old selector allowlist (which missed `#scopePanel`) is gone.
 *
 * All bursts are dispatched SYNCHRONOUSLY inside one `page.evaluate` (gap ≈ 0)
 * so a loaded runner can never split a gesture by accident; the only
 * wall-clock wait is the "> 150 ms" gap, which uses 300 ms and can only be
 * longer, never shorter. Synthetic wheel events do not scroll, so the specs
 * move `scrollTop` by hand; the one trusted `page.mouse.wheel` in the first
 * test is the proof that the native scroll path still works end to end.
 *
 * `defaultPrevented` is asserted for these synthetic (cancelable) events
 * only. Trusted Chrome tails are non-cancelable by browser design once the
 * first event of a scroll sequence went uncancelled, so the camera — not
 * `defaultPrevented` — is the primary observable.
 *
 * Standalone page, FALLBACK graph: no agent, no auth. Skips without WebGL.
 */

const ZOOM_PER_40 = 40 * 0.28 // 11.2 — one +40 wheel step on the zoom path
const PINCH_PER_40 = 40 * 1.6 // 64 — one +40 ctrl+wheel step

test.describe('brain orb — gesture-latched wheel routing (#2538)', () => {
  test('@smoke A1 a wheel gesture over a scrollable panel scrolls it and never zooms', async ({ page }) => {
    await openOrb(page)
    await growInspector(page)
    const before = await camTarget(page)

    // Synthetic event on a deep element inside #inspContent at scrollTop 0:
    // routed to the panel (not prevented), camera untouched.
    expect(await wheelAt(page, '#e2eInspPara', { deltaY: 40 })).toBe(false)
    expect(await camTarget(page)).toBe(before)

    // Trusted wheel through the real input pipeline: the panel actually scrolls.
    // #inspector is height-capped (#2539) and itself scrolls, so bring
    // #inspContent into its visible box first and prove the aim point lands on
    // it — a mis-aimed wheel over the void would fail this loudly, not vaguely.
    await page.evaluate(() => document.getElementById('inspContent').scrollIntoView({ block: 'nearest' }))
    const box = await page.locator('#inspContent').boundingBox()
    const aim = { x: box.x + box.width / 2, y: box.y + box.height / 2 }
    const under = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y)
      return el && el.closest('#inspContent') ? 'inspContent' : el ? el.id || el.tagName : 'none'
    }, aim)
    expect(under, `trusted wheel must land on #inspContent, landed on ${under}`).toBe('inspContent')
    await page.mouse.move(aim.x, aim.y)
    await page.mouse.wheel(0, 60)
    await expect
      .poll(() => page.evaluate(() => document.getElementById('inspContent').scrollTop))
      .toBeGreaterThan(0)
    expect(await camTarget(page)).toBe(before)
  })

  test('@smoke A2+A3 a momentum tail that runs out of scroll is swallowed; a fresh gesture over the edged panel still zooms', async ({ page }) => {
    await openOrb(page)
    await growInspector(page)
    const before = await camTarget(page)

    // Step 1 — one synchronous gesture: 5 events while scrolling, then both
    // scrollers hit their edge, then a 25-event decaying tail. The last 5 of
    // the tail land on the void (#scene): fingers are off the trackpad, so
    // the cursor is free to drift.
    const burst = await page.evaluate(() => {
      const body = document.getElementById('inspContent')
      const insp = document.getElementById('inspector')
      const para = document.getElementById('e2eInspPara')
      const scene = document.getElementById('scene')
      const fire = (target, deltaY) => {
        const e = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY })
        target.dispatchEvent(e)
        return e.defaultPrevented
      }
      const preScroll = []
      for (let i = 0; i < 5; i++) {
        preScroll.push(fire(para, 40))
        body.scrollTop += 40 // synthetic events do not scroll — advance by hand
      }
      body.scrollTop = body.scrollHeight
      insp.scrollTop = insp.scrollHeight
      const deltas = Array.from({ length: 25 }, (_, i) => Math.max(1, Math.round(40 - i * 1.625)))
      const tail = deltas.map((d, i) => fire(i >= 20 ? scene : para, d))
      return { preScroll, tail, deltas }
    })
    expect(burst.preScroll).toEqual([false, false, false, false, false])
    // Every post-edge event of the gesture is swallowed (prevented), not chained…
    expect(burst.tail).toEqual(new Array(25).fill(true))
    // …and the camera never moved.
    expect(await camTarget(page)).toBe(before)

    // Step 2 — a deliberate second gesture (gap > 150 ms) over the still-edged
    // panel zooms exactly as before the fix: edge-chaining is preserved.
    await page.waitForTimeout(300)
    expect(await wheelAt(page, '#e2eInspPara', { deltaY: 40 })).toBe(true)
    expect(await camTarget(page)).toBeCloseTo(before + ZOOM_PER_40, 1)
  })

  test('@smoke A4 a gesture that starts over the void keeps zooming even when it drifts onto a scrollable panel', async ({ page }) => {
    await openOrb(page)
    await growInspector(page)
    const before = await camTarget(page)
    const prevented = await page.evaluate(() => {
      const fire = (target, deltaY) => {
        const e = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY })
        target.dispatchEvent(e)
        return e.defaultPrevented
      }
      return [fire(document.getElementById('scene'), 40), fire(document.getElementById('e2eInspPara'), 40)]
    })
    expect(prevented).toEqual([true, true])
    expect(await camTarget(page)).toBeCloseTo(before + 2 * ZOOM_PER_40, 1)
  })

  test('@smoke A5 #scopePanel — a scroller the old allowlist never knew — scrolls under the wheel, then chains after a gap', async ({ page }) => {
    await openOrb(page)
    await page.evaluate(() => {
      const sp = document.getElementById('scopePanel')
      sp.innerHTML =
        '<h3>scopes</h3>' +
        Array.from(
          { length: 120 },
          (_, i) => `<div class="scoperow"${i === 0 ? ' id="e2eScopeRow"' : ''}><input type="checkbox" /><span>scope ${i + 1}</span></div>`
        ).join('')
      sp.scrollTop = 0
      sp.classList.add('show')
    })
    const scrollable = await page.evaluate(() => {
      const sp = document.getElementById('scopePanel')
      return sp.scrollHeight > sp.clientHeight + 1 && getComputedStyle(sp).overflowY === 'auto'
    })
    expect(scrollable).toBe(true)
    const before = await camTarget(page)

    expect(await wheelAt(page, '#e2eScopeRow', { deltaY: 40 })).toBe(false)
    expect(await camTarget(page)).toBe(before)

    await page.evaluate(() => {
      const sp = document.getElementById('scopePanel')
      sp.scrollTop = sp.scrollHeight
    })
    await page.waitForTimeout(300)
    expect(await wheelAt(page, '#e2eScopeRow', { deltaY: 40 })).toBe(true)
    expect(await camTarget(page)).toBeCloseTo(before + ZOOM_PER_40, 1)
  })

  test('@smoke A6 #brief3 .list still scrolls under the wheel (regression guard)', async ({ page }) => {
    await openOrb(page)
    await page.waitForFunction(() => !!document.querySelector('#brief3 .list'))
    await page.evaluate(() => {
      const list = document.querySelector('#brief3 .list')
      list.insertAdjacentHTML(
        'afterbegin',
        Array.from(
          { length: 40 },
          (_, i) => `<div class="li"${i === 0 ? ' id="e2eBriefRow"' : ''}><span class="lidot amber"></span><span>row ${i + 1}</span></div>`
        ).join('')
      )
      list.scrollTop = 0
    })
    const before = await camTarget(page)
    expect(await wheelAt(page, '#e2eBriefRow', { deltaY: 40 })).toBe(false)
    expect(await camTarget(page)).toBe(before)
  })

  test('@smoke A7 ctrl+wheel (trackpad pinch) zooms anywhere, panel or not', async ({ page }) => {
    await openOrb(page)
    await growInspector(page)
    const before = await camTarget(page)
    expect(await wheelAt(page, '#e2eInspPara', { deltaY: 40, ctrlKey: true })).toBe(true)
    expect(await camTarget(page)).toBeCloseTo(before + PINCH_PER_40, 1)
  })

  test('@smoke A8 a pure-horizontal opener does not decide the route', async ({ page }) => {
    await openOrb(page)
    await growInspector(page)
    const before = await camTarget(page)
    const prevented = await page.evaluate(() => {
      const para = document.getElementById('e2eInspPara')
      const fire = (init) => {
        const e = new WheelEvent('wheel', { bubbles: true, cancelable: true, ...init })
        para.dispatchEvent(e)
        return e.defaultPrevented
      }
      return [fire({ deltaY: 0, deltaX: 30 }), fire({ deltaY: 40 })]
    })
    // The horizontal opener falls through to the (zero-delta) zoom branch as
    // before the fix; the vertical event that follows is routed to the panel.
    expect(prevented).toEqual([true, false])
    expect(await camTarget(page)).toBe(before)
  })

  test('@smoke A9 a non-Element target (document) takes the zoom path', async ({ page }) => {
    await openOrb(page)
    const before = await camTarget(page)
    expect(await wheelAt(page, 'document', { deltaY: 40 })).toBe(true)
    expect(await camTarget(page)).toBeCloseTo(before + ZOOM_PER_40, 1)
  })
})
