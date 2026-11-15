// @vitest-environment jsdom
/**
 * The tab strip trusts a hidden mirror row for its fit decision. When the
 * mirror under-measures — a kept-alive page re-activated at a new width, an
 * engine whose `<button>` flex items refuse to shrink (WebKit), a late glyph —
 * the arithmetic says "everything fits", every tab renders inline, and the
 * last ones paint straight over whatever sits to the strip's right: on the
 * agent page, the pinned Settings tab ("FilesSettings").
 *
 * Two layers close that. The visible nav clips (`overflow-hidden`), so a
 * mis-fit can never paint over a neighbour; and after each render the strip
 * checks the REAL row — `scrollWidth` against `clientWidth` — and, when it
 * overflows, re-measures and then drops tabs into More one at a time until
 * the row genuinely fits.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import OverflowTabs from '@/components/OverflowTabs.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const CONTAINER = 500
const MEASURED = 100   // what the mirror reports per tab
const RENDERED = 108   // what the visible row actually draws per tab
const MORE = 80

const tabs = ['a', 'b', 'c', 'd', 'e'].map((id) => ({ id, label: id.toUpperCase() }))

let w
let rectSpy, clientSpy, scrollSpy, rafSpy
const inlineButtons = (nav) => [...nav.querySelectorAll('button')].filter((b) => !b.hasAttribute('data-overflow-trigger'))
const visibleNav = (el) => el.querySelector('nav:not([aria-hidden] nav)')

beforeEach(() => {
  rafSpy = vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => setTimeout(() => cb(performance.now()), 0))
  // The mirror row's measurements: every tab MEASURED wide, More MORE wide.
  rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    const width = this.hasAttribute('data-measure-tab') ? MEASURED : this.hasAttribute('data-measure-more') ? MORE : 0
    return { width, height: 0, x: 0, y: 0, top: 0, left: 0, right: width, bottom: 0, toJSON() {} }
  })
  // The container (and its visible nav) is CONTAINER wide.
  clientSpy = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function () {
    return (this.tagName === 'NAV' || this.querySelector('[data-measure-tab]')) ? CONTAINER : 0
  })
  // The REAL visible row: each inline tab draws RENDERED wide, plus the trigger.
  scrollSpy = vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockImplementation(function () {
    if (this.tagName !== 'NAV' || this.closest('[aria-hidden]')) return 0
    const trigger = this.querySelector('[data-overflow-trigger]') ? MORE : 0
    return inlineButtons(this).length * RENDERED + trigger
  })
})

afterEach(() => {
  w?.unmount(); w = null; document.body.innerHTML = ''
  rectSpy.mockRestore(); clientSpy.mockRestore(); scrollSpy.mockRestore(); rafSpy.mockRestore()
})

const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 2)) }

describe('the strip corrects a fit the mirror got wrong', () => {
  it('clips the visible row so a mis-fit never paints over the strip\'s neighbour', () => {
    w = mount(OverflowTabs, { props: { tabs, modelValue: 'a' }, attachTo: document.body })
    expect(visibleNav(w.element).className).toContain('overflow-hidden')
  })

  it('drops tabs into More until the REAL row fits, when the arithmetic said everything fit', async () => {
    w = mount(OverflowTabs, { props: { tabs, modelValue: 'a' }, attachTo: document.body })
    await settle()
    const nav = visibleNav(w.element)
    // The arithmetic: 5 × 100 = 500 ≤ 500 → all inline. The real row: 5 × 108
    // = 540 > 500. Dropping to 4 → 4 × 108 + 80 = 512, still over; 3 → 404 fits.
    expect(inlineButtons(nav).map((b) => b.textContent.trim())).toEqual(['A', 'B', 'C'])
    expect(nav.querySelector('[data-overflow-trigger]'), 'the hidden tabs are reachable through More').toBeTruthy()
    expect(nav.scrollWidth).toBeLessThanOrEqual(nav.clientWidth)
  })

  it('leaves a row that genuinely fits alone', async () => {
    scrollSpy.mockImplementation(function () {
      if (this.tagName !== 'NAV' || this.closest('[aria-hidden]')) return 0
      return inlineButtons(this).length * MEASURED
    })
    w = mount(OverflowTabs, { props: { tabs, modelValue: 'a' }, attachTo: document.body })
    await settle()
    const nav = visibleNav(w.element)
    expect(inlineButtons(nav).length).toBe(5)
    expect(nav.querySelector('[data-overflow-trigger]')).toBeNull()
  })
})
