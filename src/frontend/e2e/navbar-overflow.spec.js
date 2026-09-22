import { test, expect } from '@playwright/test'

/**
 * Top nav overflow (#1789).
 *
 * NavBar.vue is one `flex justify-between` row with two children: the logo +
 * router-link row on the left, the controls (connection dot, version chip,
 * docs, theme toggle, user menu) on the right. Neither child used to set
 * `min-w-0`, and a flex item defaults to `min-width: auto` — so once the two
 * clusters' natural widths exceeded the `max-w-7xl` cap they stopped
 * compressing and overflowed INTO each other, which `justify-between` parked
 * in the middle of the bar. On an entitled build (7 links, `Sessions` +
 * `Enterprise` both present) that put the connection indicator on top of the
 * `Enterprise PRO` link at EVERY viewport width, and below ~1180px it pushed
 * the theme toggle and the user menu — the only route to Sign out — past the
 * viewport edge with no horizontal scroll to recover them.
 *
 * These tests pin the three properties the fix guarantees, none of which
 * depend on how many links the build renders:
 *   1. no link ever visually collides with a control,
 *   2. the controls stay fully on-screen and hit-testable,
 *   3. links that don't fit stay reachable — see the amendment below for the
 *      mechanism that delivers it.
 *
 * **Amendment (#1925).** Properties 1 and 2 are unchanged and still measured
 * the same way. Property 3's MECHANISM was replaced: #1789's answer was a
 * horizontal scroll row with its scrollbar suppressed, which kept every link
 * reachable but gave no signal that any were hidden — an overflowed link was
 * invisible and undiscoverable inside a 64px bar. #1925 replaced it with the
 * priority+ pattern the design system already uses for tabs: the links that fit
 * render inline, the rest collapse into a counted "N more" disclosure.
 *
 * So test 3 now pins the new mechanism AND the absence of the old one. That
 * second half matters: a future refactor that reintroduces `overflow-x-auto`
 * here would restore exactly the undiscoverable-link failure #1925 removed, and
 * this is where that would be caught.
 *
 * Widths ≥1280 are the entitled-build regression (the container is capped at
 * `max-w-7xl`, so "just use a wider monitor" never fixed it); the narrow
 * widths keep the spec meaningful on an OSS build too, where the (post-ent#260
 * 4-link) bar only exhausts the bar once the viewport is small — test 3's
 * squeeze branch is conditional on the measured overflow for exactly that
 * reason.
 */

// Viewports: the two laptop widths where the bug was worst, the cap boundary,
// and down to the `sm` breakpoint where the link row first appears.
const WIDTHS = [1920, 1600, 1440, 1366, 1280, 1180, 1024, 900, 768, 640]

// Geometry of the nav, measured the way a user perceives it: link boxes are
// clipped to the scroll row's visible box first, because a link scrolled past
// the boundary still reports full un-clipped `getBoundingClientRect()`
// geometry while being invisible on screen.
async function navGeometry(page) {
  return page.evaluate(() => {
    const row = document.querySelector('nav .flex.justify-between')
    const [left, right] = row.children
    const linkRow = left.children[1]
    const clip = linkRow.getBoundingClientRect()

    const controls = [...right.children].map((c) => c.getBoundingClientRect())
    let worstCollision = 0
    for (const raw of left.querySelectorAll('a')) {
      const b = raw.getBoundingClientRect()
      const l = Math.max(b.left, clip.left)
      const r = Math.min(b.right, clip.right)
      if (r <= l) continue // fully scrolled out of view
      for (const c of controls) {
        worstCollision = Math.max(worstCollision, Math.min(r, c.right) - Math.max(l, c.left))
      }
    }

    const menu = right.lastElementChild
    const menuBox = menu.getBoundingClientRect()
    const hit = document.elementFromPoint(
      menuBox.left + menuBox.width / 2,
      menuBox.top + menuBox.height / 2
    )

    return {
      worstCollision: Math.max(0, worstCollision),
      controlsRightEdge: Math.max(...controls.map((c) => c.right)),
      controlsLeftEdge: Math.min(...controls.map((c) => c.left)),
      userMenuHitTestable: !!(hit && menu.contains(hit)),
      navHeight: row.getBoundingClientRect().height,
      // #1925: the row no longer scrolls — the inner nav clips and the
      // remainder moves into the disclosure. Both are reported so test 3 can
      // assert the new mechanism and the absence of the old one.
      linkRowOverflowX: getComputedStyle(linkRow).overflowX,
      linkRowScrollWidth: linkRow.scrollWidth,
      linkRowClientWidth: linkRow.clientWidth,
      linkCount: left.querySelectorAll('a').length,
      overflowTrigger: (() => {
        const t = document.querySelector('[data-nav-overflow-trigger]')
        return t ? t.innerText.replace(/\s+/g, ' ').trim() : null
      })(),
    }
  })
}

/**
 * #1925: the strip re-measures on a rAF after a resize, so a snapshot taken in
 * the same tick as `setViewportSize` reports the PREVIOUS split — which reads
 * as "it fits" one frame before the links move into the menu. Sample until two
 * consecutive reads agree before asserting anything about the split.
 */
async function settleNav(page) {
  let last = null
  for (let i = 0; i < 20; i++) {
    const now = await page.evaluate(() => {
      const t = document.querySelector('[data-nav-overflow-trigger]')
      const links = document.querySelectorAll(
        'nav .flex.justify-between > div:first-child > div:last-child a'
      )
      return `${links.length}|${t ? t.innerText.replace(/\s+/g, ' ').trim() : ''}`
    })
    if (now === last) return
    last = now
    await page.waitForTimeout(100)
  }
}

test.describe('NavBar overflow (#1789)', () => {
  test('@smoke nav links never collide with the right-hand controls', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({
      timeout: 15000,
    })

    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 })
      await settleNav(page)
      const g = await navGeometry(page)

      // The regression itself: a visible link box overlapping a control box.
      expect(g.worstCollision, `link/control overlap at ${width}px`).toBe(0)

      // The bar must not grow a second line — the pre-fix symptom was the
      // `PRO` badge and version chip wrapping inside a fixed 64px bar.
      expect(g.navHeight, `nav height at ${width}px`).toBe(64)
    }
  })

  test('@smoke controls stay on-screen and clickable at every width', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({
      timeout: 15000,
    })

    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 })
      await settleNav(page)
      const g = await navGeometry(page)

      // Pre-fix the cluster ran to x≈1170 regardless of viewport, and
      // documentElement.scrollWidth stayed at the viewport width — so the
      // overhang was unreachable rather than merely off to the side.
      expect(g.controlsRightEdge, `controls right edge at ${width}px`).toBeLessThanOrEqual(width)
      expect(g.controlsLeftEdge, `controls left edge at ${width}px`).toBeGreaterThanOrEqual(0)

      // The user menu owns Sign out. `elementFromPoint` at its centre is the
      // honest check — a box inside the viewport that something else covers
      // is still not clickable.
      expect(g.userMenuHitTestable, `user menu hit-testable at ${width}px`).toBe(true)
    }

    // And it genuinely opens at the width where it used to be unreachable.
    await page.setViewportSize({ width: 900, height: 900 })
    await settleNav(page)
    await page.locator('nav .flex.justify-between > div:last-child > div:last-child button').click()
    await expect(page.getByRole('button', { name: /sign out/i })).toBeVisible({ timeout: 5000 })
  })

  test('@smoke overflowing links collapse into a counted menu and stay reachable', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({
      timeout: 15000,
    })

    // Wide: everything fits, so there is nothing to disclose. Asserted rather
    // than assumed — a strip that collapses at 1440px would be over-eager, and
    // that failure is invisible without measuring it.
    await page.setViewportSize({ width: 1440, height: 900 })
    await settleNav(page)
    const wide = await navGeometry(page)
    expect(wide.overflowTrigger, 'the bar collapsed at a width where it fits').toBeNull()

    // Squeeze to the `sm` floor, below which the link row hides entirely. An
    // OSS build (4 links) may still fit where an entitled one (6-7) does not,
    // so the assertion forks on the measured state rather than assuming a
    // fleet — the same reason #1789's version forked on scrollWidth.
    await page.setViewportSize({ width: 640, height: 900 })
    await settleNav(page)
    const narrow = await navGeometry(page)

    // The old mechanism must NOT come back: reintroducing a scroll row here
    // restores the undiscoverable-link failure #1925 removed.
    expect(narrow.linkRowOverflowX, 'the link row is scrolling again (#1925)').not.toBe('auto')
    expect(
      narrow.linkRowScrollWidth,
      'the link row overflows its box instead of collapsing (#1925)'
    ).toBeLessThanOrEqual(narrow.linkRowClientWidth + 1)

    if (narrow.overflowTrigger) {
      // Collapsed: the trigger states HOW MANY links are hidden, and every one
      // of them is reachable from the menu it opens.
      expect(narrow.overflowTrigger, 'the trigger does not count what it hides')
        .toMatch(/\d+ more/)

      await page.locator('[data-nav-overflow-trigger]').click()
      const menu = page.locator('[data-nav-overflow-menu]')
      await expect(menu).toBeVisible()

      const hidden = menu.locator('[data-nav-menu-item]')
      const count = await hidden.count()
      expect(count, 'the menu opened with nothing in it').toBeGreaterThan(0)
      expect(String(count), 'the count in the trigger disagrees with the menu')
        .toBe(narrow.overflowTrigger.match(/(\d+) more/)[1])

      for (let i = 0; i < count; i++) {
        await expect(hidden.nth(i)).toBeInViewport()
        await expect(hidden.nth(i)).toHaveAttribute('href', /./)
      }

      // Escape closes it and returns focus to the trigger — the disclosure
      // contract the strip shares with OverflowTabs.
      await page.keyboard.press('Escape')
      await expect(menu).toBeHidden()
    } else {
      // Fits even at 640: every link is on-screen without a disclosure.
      const links = page.locator('nav .flex.justify-between > div:first-child > div:last-child a')
      const n = await links.count()
      expect(n, 'no links and no trigger — the row rendered nothing').toBeGreaterThan(0)
      for (let i = 0; i < n; i++) await expect(links.nth(i)).toBeInViewport()
    }
  })
})
