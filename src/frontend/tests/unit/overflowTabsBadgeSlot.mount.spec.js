// @vitest-environment jsdom
/**
 * #3060 — `tab.badgeSlot`, an OPT-IN reservation for a count that lands after
 * the strip renders. The Inbox's Action / Unread counts arrive after the tabs
 * and slid Unread and All ~29px right; with the slot the badge span is drawn
 * from the first frame — `invisible` (its width kept) until the count lands —
 * in the visible row AND the measuring mirror.
 *
 * A tab WITHOUT the field must render exactly what it did: no span when it
 * has no badge, and the badge's class string unchanged when it has one.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import OverflowTabs from '@/components/OverflowTabs.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const SLOT = ['min-w-[1.75rem]', 'text-center', 'tabular-nums']
// The visible pill's class string before #3060, verbatim.
const PILL = 'ml-1.5 shrink-0 px-1.5 py-0.5 text-[10px] font-semibold bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300 rounded-full leading-none'
const MIRROR_PILL = 'ml-1.5 px-1.5 py-0.5 text-[10px] font-semibold rounded-full leading-none'

let w
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })
const mk = (tabs) => (w = mount(OverflowTabs, { props: { tabs, modelValue: tabs[0].id }, attachTo: document.body }))
const visible = (wr) => wr.findAll('nav button:not([data-measure-tab]):not([data-measure-more]):not([data-overflow-trigger])')
const mirror = (wr) => wr.findAll('[data-measure-tab]')
// A tab's badge span: the one after the label (the label span is `truncate`).
const badgeIn = (btn) => btn.findAll('span').find((s) => !s.classes().includes('truncate') && s.classes().includes('rounded-full') && !s.classes().includes('w-1.5') && !s.classes().includes('w-2'))

describe('#3060 badgeSlot reserves the count before it lands', () => {
  it('a slotted tab with no count yet draws the badge INVISIBLE, at the slot width, in both rows', () => {
    mk([{ id: 'a', label: 'Action', badgeSlot: true, badge: null }, { id: 'b', label: 'All' }])
    const span = badgeIn(visible(w)[0])
    expect(span, 'the slot is not drawn before the count').toBeTruthy()
    expect(span.classes()).toContain('invisible')
    for (const c of SLOT) expect(span.classes()).toContain(c)
    expect(span.text()).toBe('')
    const m = badgeIn(mirror(w)[0])
    expect(m, 'the mirror does not measure the slot — the fit would be computed on a narrower tab').toBeTruthy()
    for (const c of SLOT) expect(m.classes()).toContain(c)
  })

  it('the count lands INTO the slot: same element classes, minus invisible', async () => {
    mk([{ id: 'a', label: 'Action', badgeSlot: true, badge: null }])
    const before = badgeIn(visible(w)[0]).classes().filter((c) => c !== 'invisible')
    await w.setProps({ tabs: [{ id: 'a', label: 'Action', badgeSlot: true, badge: 7 }] })
    const span = badgeIn(visible(w)[0])
    expect(span.classes()).not.toContain('invisible')
    expect(span.classes()).toEqual(before)
    expect(span.text()).toBe('7')
  })

  it('a zero count keeps the reserved slot (no collapse when the count clears)', async () => {
    mk([{ id: 'a', label: 'Action', badgeSlot: true, badge: 3 }])
    await w.setProps({ tabs: [{ id: 'a', label: 'Action', badgeSlot: true, badge: null }] })
    expect(badgeIn(visible(w)[0]).classes()).toContain('invisible')
  })
})

describe('#3060 a tab without badgeSlot renders exactly what it did', () => {
  it('no count → no badge span at all, in either row', () => {
    mk([{ id: 'a', label: 'Action' }])
    expect(badgeIn(visible(w)[0])).toBeUndefined()
    expect(badgeIn(mirror(w)[0])).toBeUndefined()
    expect(w.find('[data-badge-slot]').exists()).toBe(false)
  })

  it('a count → the pre-#3060 class strings, byte for byte', () => {
    mk([{ id: 'a', label: 'Action', badge: 4 }])
    expect(badgeIn(visible(w)[0]).attributes('class')).toBe(PILL)
    expect(badgeIn(mirror(w)[0]).attributes('class')).toBe(MIRROR_PILL)
  })
})
