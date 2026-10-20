import { test, expect } from '@playwright/test'

/**
 * The page body never scrolls horizontally (#2197).
 *
 * The design-system contract is explicit: wide content scrolls inside its own
 * container, never by widening the page. Two pages broke it below 1024px —
 * Settings and Agent Detail — and the cause was NOT the tab strips the original
 * report blamed. `OverflowTabs` was collapsing correctly the whole time; the
 * hidden mirror row it measures against sits in a 0x0 `overflow: hidden` box,
 * so a probe that does not skip clipped subtrees reports it as the widest thing
 * on the page. It is invisible to the user and to `scrollWidth` alike.
 *
 * The real causes were rigid flex rows with no wrap:
 *   * Agent Detail — three `AgentHeader` rows (identity + actions, toggles +
 *     live CPU/MEM/uptime, lifetime cost) summing to ~790px of min-content.
 *   * Settings — a `<select>` with `flex-1` and the default `min-width: auto`,
 *     whose min-content is its widest option (~417px).
 *
 * So this spec measures the ONE thing the contract actually states — the
 * document scroll width — rather than asserting a particular element is narrow.
 * That is deliberate: an element-shaped assertion is what produced a report
 * naming the wrong element.
 */

// 375 = iPhone-class phone, 640/768 = the tablet band where the report
// measured +178/+132, 1024 = the first width that was already clean.
const WIDTHS = [375, 640, 768, 1024]

// `ready` is load-bearing, not politeness. A fixed settle measures whatever has
// painted so far, and the widest markup on both broken pages is gated behind a
// later render: Settings' Default Model <select> only exists once /api/users/me
// confirms the admin role, and Agent Detail's live CPU/MEM cluster only once the
// stats poll lands. Measured without these, the Settings arm of this very suite
// PASSED with the fix reverted — a green test for a broken page.
const PAGES = [
  ['/settings', 'Settings', () => document.querySelectorAll('select').length > 0],
  ['/', 'Dashboard', null],
  ['/operations', 'Operations', null],
  ['/library', 'Library', null],
]

const overflow = () => {
  const de = document.documentElement
  return { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth }
}

// The offenders, named. A bare scrollWidth assertion says a page is broken but
// not by what, and the last report cost a diagnosis by naming the wrong node —
// so on failure this reports the widest element that is NOT inside a clipping
// ancestor (the mirror-row trap above).
const BLAME = () => {
  const de = document.documentElement
  const vw = de.clientWidth
  const clipped = (el) => {
    for (let a = el.parentElement; a; a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible' &&
          a.getBoundingClientRect().right <= vw + 1) return true
    }
    return false
  }
  const hits = []
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect()
    if ((r.width === 0 && r.height === 0) || r.right <= vw + 1 || clipped(el)) continue
    hits.push(`${el.tagName}.${String(el.className).split(' ').slice(0, 3).join('.')}` +
      ` +${Math.round(r.right - vw)}px "${(el.textContent || '').trim().slice(0, 24)}"`)
  }
  return hits.slice(0, 3)
}

async function sweep(page, path, label, ready) {
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 812 })
    await page.goto(path)
    await page.waitForLoadState('networkidle')
    if (ready) {
      await page.waitForFunction(ready, null, { timeout: 15000 })
    }
    // Live telemetry and the strips' ResizeObserver both settle after paint;
    // measuring before that reports the pre-layout width, not the widest state.
    await page.waitForTimeout(800)
    const r = await page.evaluate(overflow)
    const blame = r.scrollWidth > r.clientWidth ? await page.evaluate(BLAME) : []
    expect(
      r.scrollWidth,
      `${label} @ ${width}px — body scrolls horizontally by ` +
      `${r.scrollWidth - r.clientWidth}px. Widest unclipped: ${blame.join(' | ') || 'none found'}`
    ).toBeLessThanOrEqual(r.clientWidth)
  }
}

test.describe('page body horizontal overflow (#2197)', () => {
  for (const [path, label, ready] of PAGES) {
    test(`@interactive ${label} never widens the body`, async ({ page }) => {
      await sweep(page, path, label, ready)
    })
  }

  test('@interactive Agent Detail never widens the body', async ({ page }) => {
    // Agent-scoped, so the agent is resolved from the live fleet rather than
    // hard-coded — this suite runs against whatever stack it is pointed at.
    await page.goto('/')
    const name = await page.evaluate(async () => {
      const res = await fetch('/api/agents', {
        headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
      })
      const body = await res.json()
      const rows = Array.isArray(body) ? body : body.agents || []
      // Prefer a RUNNING agent: the header's live CPU/MEM/uptime cluster only
      // renders while running, and it is the widest row on the page — testing a
      // stopped agent would measure the narrow variant and pass vacuously.
      return (rows.find((a) => a.status === 'running') || rows[0])?.name || null
    })
    expect(name, 'no agents on this stack to measure').not.toBeNull()
    // The header's live CPU/MEM/uptime cluster is the widest row on the page and
    // renders only once the stats poll answers; without waiting for it this arm
    // measures the narrow variant and passes on a broken page.
    await sweep(page, `/agents/${name}`, `Agent Detail (${name})`,
      () => /\bCPU\b/.test(document.body.innerText))
  })

  // The contract is about the page, but a strip that collapses must also fit
  // what it collapses INTO — a "More" trigger wider than its own container is
  // the failure the original report believed it was seeing.
  test('@interactive a collapsed tab strip fits its container at 375px', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await page.goto('/settings')
    // Settings renders ONE tab (MCP Keys) until /api/users/me confirms the admin
    // role, and one tab never overflows. Measuring on a fixed timeout catches
    // that state and reports a strip that "did not need to collapse" — a false
    // pass dressed as a skip. Wait for the real tab set instead.
    await page.waitForFunction(
      () => document.querySelectorAll('[data-measure-tab]').length > 2,
      null,
      { timeout: 15000 }
    )
    await page.waitForTimeout(400) // let the measured split settle

    const fit = await page.evaluate(() => {
      const trigger = document.querySelector('[data-overflow-trigger]')
      if (!trigger) return { collapsed: false, tabs: document.querySelectorAll('[data-measure-tab]').length }
      const strip = trigger.parentElement // the visible row
      return {
        collapsed: true,
        text: trigger.innerText.replace(/\s+/g, ' ').trim(),
        triggerRight: Math.round(trigger.getBoundingClientRect().right),
        stripRight: Math.round(strip.getBoundingClientRect().right),
      }
    })

    // Six admin tabs cannot fit 375px, so a strip that did NOT collapse is the
    // bug, not a skip condition.
    expect(fit.collapsed, `strip did not collapse at 375px with ${fit.tabs} tabs`).toBe(true)
    expect(
      fit.triggerRight,
      `the "${fit.text}" trigger runs past the strip it collapsed into`
    ).toBeLessThanOrEqual(fit.stripRight + 1)
  })
})
