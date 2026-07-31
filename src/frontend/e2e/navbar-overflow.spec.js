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
 *   3. links that don't fit stay reachable via a scroll container rather than
 *      being clipped dead or overlapping.
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
      linkRowOverflowX: getComputedStyle(linkRow).overflowX,
      linkRowScrollWidth: linkRow.scrollWidth,
      linkRowClientWidth: linkRow.clientWidth,
      linkCount: left.querySelectorAll('a').length,
    }
  })
}

test.describe('NavBar overflow (#1789)', () => {
  test('@smoke nav links never collide with the right-hand controls', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({
      timeout: 15000,
    })

    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 })
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
    await page.locator('nav .flex.justify-between > div:last-child > div:last-child button').click()
    await expect(page.getByRole('button', { name: /sign out/i })).toBeVisible({ timeout: 5000 })
  })

  test('@smoke overflowing links stay reachable via the scroll row', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible({
      timeout: 15000,
    })

    // The mechanism that replaces overlap: the link row is a scroll container,
    // so a link set too wide for the bar scrolls instead of spilling over the
    // controls. Guards against a future refactor dropping `overflow-x-auto`.
    await page.setViewportSize({ width: 1440, height: 900 })
    const wide = await navGeometry(page)
    expect(wide.linkRowOverflowX).toBe('auto')

    // Squeeze to the `sm` floor (below it the link row hides entirely) and
    // check both regimes. ent#260 removed the Agents link, so the 4-link OSS
    // bar may now FIT at 640px where the 5-link bar overflowed — a fitting row
    // can't exhibit the clipped-dead failure mode, so the honest assertion is
    // conditional on the measured geometry (an entitled 6-7-link build still
    // exercises the overflow branch):
    //   overflowing → the last link must be recoverable via scroll,
    //   fitting     → every link must already be fully in view.
    await page.setViewportSize({ width: 640, height: 900 })
    const narrow = await navGeometry(page)

    const lastLink = page.locator('nav .flex.justify-between > div:first-child > div:last-child a').last()
    if (narrow.linkRowScrollWidth > narrow.linkRowClientWidth) {
      // Overflow: clipped-but-scrollable, not clipped-dead.
      await lastLink.scrollIntoViewIfNeeded()
      await expect(lastLink).toBeInViewport()
    } else {
      // No overflow: the whole link set is on-screen without scrolling.
      await expect(lastLink).toBeInViewport()
    }
  })
})
