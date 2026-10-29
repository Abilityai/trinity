// @vitest-environment jsdom
/**
 * #3451 — `BoundedRegion`, the bounded viewport of design-system principle 28.
 *
 * MOUNTED (#2918). jsdom has no layout, so what is proved here is the contract a
 * consumer relies on: the region is named and keyboard-reachable, `fill` puts it
 * in the flex chain with a 0px basis and the 20rem floor (never `flex-1`, whose
 * 0% basis falls back to content height in an auto-height column and regrows the
 * page — measured at 30,306 px), and `scrollToTop()` resets it. That the page
 * stays the window is proved in the browser (e2e/operations-bounded-lists.spec.js).
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BoundedRegion from '@/components/BoundedRegion.vue'

const mountRegion = (props = {}, attrs = {}) =>
  mount(BoundedRegion, { props: { label: 'Executions', ...props }, attrs, slots: { default: '<p class="row">a row</p>' } })

describe('BoundedRegion (#3451)', () => {
  it('is a named, keyboard-reachable region around its content', () => {
    const w = mountRegion()
    const el = w.element
    expect(el.getAttribute('role')).toBe('region')
    expect(el.getAttribute('aria-label')).toBe('Executions')
    expect(el.getAttribute('tabindex')).toBe('0')
    expect(w.find('p.row').exists()).toBe(true)
  })

  it('fills the rest of a flex column from a 0px basis, scrolls on one axis, keeps a 20rem floor', () => {
    const classes = mountRegion().classes()
    expect(classes).toEqual(expect.arrayContaining(['grow', 'basis-0', 'min-h-[20rem]', 'overflow-y-auto', 'overflow-x-hidden']))
    expect(classes).not.toContain('flex-1')
  })

  it('without fill it only scrolls — the consumer sets its cap', () => {
    const classes = mountRegion({ fill: false }, { class: 'max-h-72' }).classes()
    expect(classes).toEqual(expect.arrayContaining(['overflow-y-auto', 'max-h-72']))
    expect(classes).not.toEqual(expect.arrayContaining(['grow']))
    expect(classes).not.toContain('min-h-[20rem]')
  })

  it('passes a consumer test id and classes through to the region itself', () => {
    const w = mountRegion({}, { 'data-testid': 'ops-list-region', class: 'rounded-lg border' })
    expect(w.element.getAttribute('data-testid')).toBe('ops-list-region')
    expect(w.classes()).toEqual(expect.arrayContaining(['rounded-lg', 'border', 'overflow-y-auto']))
  })

  it('scrollToTop() returns the region to its first row', () => {
    const w = mountRegion()
    w.element.scrollTop = 240
    w.vm.scrollToTop()
    expect(w.element.scrollTop).toBe(0)
  })
})
