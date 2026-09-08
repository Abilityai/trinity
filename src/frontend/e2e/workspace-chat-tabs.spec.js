import { test, expect } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * Workspace chat tabs — fixed width, clamped labels, counted overflow (#2579).
 *
 * The AC names fixed-width tabs, and NO node-env pin can execute that. vitest
 * runs `environment: 'node'` with no layout engine and this project has no
 * component-mount harness, so `portalChatTabsAndTitles.spec.js` can prove the
 * class string is on the element and nothing more — that a `w-40` tab actually
 * renders 160px wide, that a long title actually ellipsises, and that the strip
 * actually repacks, are measurements.
 *
 * Learning #1500 is why this is not optional: the last structural change to a
 * shared tab strip shipped with regex coverage only, and the specs anchored on
 * it rotted silently.
 *
 * What must hold:
 *   1. every VISIBLE tab button is exactly FIXED_TAB_WIDTH — including the
 *      pinned Main tab, whose label is four characters (the operator ruled the
 *      width uniform: "all tabs share the width, Main included")
 *   2. a long title is clipped, not laid out wide, and carries its full text in
 *      `title=` so hovering recovers it
 *   3. the counted "N more" trigger appears once the tabs stop fitting, and the
 *      strip repacks when the conversation column is narrowed
 *   4. the strip never makes the page scroll sideways
 *
 * @interactive — needs a live stack and a real agent on the caller's roster.
 * The Workspace is reachable from the cached admin session
 * (`isClientSignedIn = !!portalToken || isPlatformSession`).
 *
 * Required env: ADMIN_PASSWORD (auth.setup.js) and PORTAL_TEST_AGENT
 * (defaults to "testfix"). A MISSING fixture agent reads as SKIPPED, never
 * broken (#2199) — hence the describe-level guard.
 *
 * ⚠️ This spec needs the fixture agent to have SEVERAL chats. It creates none:
 * a chat exists once its first message is sent, and sending real turns from a
 * layout test would make it a slow, model-dependent integration test. With
 * fewer than two tabs the geometry cases skip themselves with that reason,
 * which is the honest outcome — a fixture shortfall is not a product failure.
 */

const TEST_AGENT = process.env.PORTAL_TEST_AGENT || 'testfix'

// Must equal `FIXED_TAB_WIDTH` in components/OverflowTabs.vue ('w-40').
const TAB_WIDTH = 160

// The strip's own box, plus every visible tab button inside it. Located
// through the strip's `data-testid` and the button role rather than through
// the width class, which would re-encode the implementation under test.
const STRIP_PROBE = () => {
  const strip = document.querySelector('[data-testid="portal-chat-tabs"]')
  if (!strip) return { found: false }
  const nav = strip.querySelector('nav')
  const buttons = [...nav.querySelectorAll('button')].filter((b) => !b.hasAttribute('data-overflow-trigger'))
  const box = (el) => {
    const r = el.getBoundingClientRect()
    return { x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height) }
  }
  return {
    found: true,
    tabs: buttons.map((b) => ({
      ...box(b),
      label: (b.textContent || '').trim(),
      title: b.getAttribute('title'),
      // The label span: clipped when its scroll width exceeds its box.
      clipped: [...b.querySelectorAll('span')].some((s) => s.scrollWidth - s.clientWidth > 1),
    })),
    hasMore: !!strip.querySelector('[data-overflow-trigger]'),
    moreText: (strip.querySelector('[data-overflow-trigger]')?.textContent || '').trim(),
    overflowX: Math.round(
      document.documentElement.scrollWidth - document.documentElement.clientWidth
    ),
  }
}

const openAgent = async (page) => {
  await page.goto(`/workspace/a/${TEST_AGENT}`)
  // The strip paints only once the thread list lands, so waiting on the shell
  // would race. This is also the assertion that Main got minted (#2579): a
  // never-visited agent has no chats at all, and the strip renders anyway.
  await expect(page.getByTestId('portal-chat-tabs')).toBeVisible({ timeout: 20000 })
}

test.describe('Workspace chat tabs', () => {
  test.beforeEach(async ({ baseURL }) => {
    test.skip(
      !(await agentExists(TEST_AGENT, { baseURL })),
      missingAgentReason(TEST_AGENT, 'PORTAL_TEST_AGENT')
    )
  })

  test('@interactive New chat draws a tab and puts the caret in the composer, in one gesture', async ({ page }) => {
    // AC 1 + AC 2, and the two defects that are one action apart: the strip
    // used to render nothing at all for an unsaved chat, and the remount threw
    // away any focus set before the press.
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    await page.getByTestId('new-chat-header').click()

    const strip = page.getByTestId('portal-chat-tabs')
    await expect(strip).toBeVisible()
    await expect(strip.getByRole('button', { name: 'New chat', exact: true })).toBeVisible()

    // The caret, not merely a focusable composer. `document.activeElement`
    // rather than a Playwright `toBeFocused` on a locator, because what the AC
    // asks for is that typing goes to the composer with no further gesture.
    const focused = await page.evaluate(() => {
      const el = document.activeElement
      return { tag: el?.tagName, isComposer: !!el?.closest('form, [data-testid="portal-composer"]') || el?.tagName === 'TEXTAREA' }
    })
    expect(focused.tag, 'the composer holds focus after New chat').toBe('TEXTAREA')
  })

  test('@interactive every visible tab is exactly one fixed width, Main included', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    const m = await page.evaluate(STRIP_PROBE)
    expect(m.found, 'the strip is present').toBe(true)
    test.skip(m.tabs.length < 2, `fixture agent "${TEST_AGENT}" has fewer than two chats — nothing to measure`)

    for (const tab of m.tabs) {
      expect(tab.w, `tab "${tab.label}" is one fixed width`).toBe(TAB_WIDTH)
    }
    // Uniform means Main too — a four-character label costs the same 160px as
    // a sentence. That is the trade the operator's ruling buys, and asserting
    // it here is what stops someone "optimising" Main back to intrinsic width.
    const main = m.tabs.find((t) => t.label === 'Main')
    if (main) expect(main.w, 'the pinned Main tab shares the width').toBe(TAB_WIDTH)

    expect(m.overflowX, 'the strip never makes the page scroll sideways').toBeLessThanOrEqual(1)
  })

  test('@interactive a long title clips and keeps its full text on hover', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openAgent(page)

    const m = await page.evaluate(STRIP_PROBE)
    test.skip(m.tabs.length < 2, `fixture agent "${TEST_AGENT}" has fewer than two chats — nothing to measure`)

    // Every tab carries its own label as the tooltip, clipped or not: the
    // tooltip is what makes the clamp lossless, so it must not be conditional
    // on the clipping.
    for (const tab of m.tabs) {
      expect(tab.title, `tab "${tab.label}" carries its full text`).toBeTruthy()
    }
    // At least one long-titled tab is clipped rather than laid out wide. Only
    // asserted when the fixture actually HAS a title longer than the box —
    // a fixture whose chats are all short-named is not a defect.
    const long = m.tabs.filter((t) => t.title && t.title.length > 30)
    if (long.length) {
      expect(long.some((t) => t.clipped), 'a long title ellipsises inside the fixed box').toBe(true)
    }
  })

  test('@interactive the strip repacks under a counted "N more" as the column narrows', async ({ page }) => {
    // #492's promise, re-asserted on the fixed-width strip: the overflow rule
    // is the primitive's and must survive the width change.
    await page.setViewportSize({ width: 1600, height: 900 })
    await openAgent(page)

    const wide = await page.evaluate(STRIP_PROBE)
    test.skip(wide.tabs.length < 2, `fixture agent "${TEST_AGENT}" has fewer than two chats — nothing to repack`)

    await page.setViewportSize({ width: 700, height: 900 })
    // The primitive re-measures on ResizeObserver; give it a frame.
    await page.waitForTimeout(300)
    const narrow = await page.evaluate(STRIP_PROBE)

    expect(narrow.tabs.length, 'fewer tabs fit at 700px').toBeLessThanOrEqual(wide.tabs.length)
    if (narrow.tabs.length < wide.tabs.length) {
      expect(narrow.hasMore, 'the counted overflow trigger appears').toBe(true)
      expect(narrow.moreText, 'and it is counted, not bare "More"').toMatch(/^\d+ more/)
    }
    for (const tab of narrow.tabs) {
      expect(tab.w, `tab "${tab.label}" keeps its width at 700px`).toBe(TAB_WIDTH)
    }
    expect(narrow.overflowX, 'no horizontal page overflow at 700px').toBeLessThanOrEqual(1)
  })
})
