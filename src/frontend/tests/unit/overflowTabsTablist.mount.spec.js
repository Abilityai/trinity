// @vitest-environment jsdom
/**
 * trinity-enterprise#610 §3g L1 (A3a / A8c / B6a) — three OPT-IN fields on the
 * tab primitive, and the guard that the eight existing strips do not move.
 *
 *   tab.badgeVariant  `success` (default, the tinted pill every strip has
 *                     today) · `urgent` · `primary` — solid white on the 700
 *                     tier (5.18 / 7.90:1), the Inbox's two counters (A3)
 *   tab.badgeLabel    the tab's accessible name when a bare count would read
 *                     as "Action 21" (A8c); the badge is then aria-hidden
 *   tablistLabel      role=tablist / tab, aria-selected, roving tabindex,
 *                     Arrow / Home / End with MANUAL activation (B6a); the
 *                     More trigger stays outside the tablist
 *
 * Every field defaults off, and at its default the strip renders what it
 * rendered before: no role, no tabindex, no aria on the tabs, and the success
 * pill's class string. Flipping the default is #3056.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import OverflowTabs from '@/components/OverflowTabs.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const TABS = [
  { id: 'action', label: 'Action', badge: 21 },
  { id: 'unread', label: 'Unread', badge: 157 },
  { id: 'all', label: 'All' },
]
const SUCCESS_PILL = 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300'

let w
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })
const mk = (props) => (w = mount(OverflowTabs, { props: { tabs: TABS, modelValue: 'action', ...props }, attachTo: document.body }))
// The visible row's tab buttons (the mirror row's carry data-measure-tab).
const tabsOf = (wr) => wr.findAll('nav button:not([data-measure-tab]):not([data-measure-more]):not([data-overflow-trigger])')
const badgeOf = (btn) => btn.findAll('span').find((s) => /\d/.test(s.text()))

describe('defaults: every existing strip renders as before', () => {
  it('no tablist, no tab roles, no tabindex, no aria-label on the tabs', () => {
    mk()
    expect(w.find('[role="tablist"]').exists()).toBe(false)
    for (const b of tabsOf(w)) {
      expect(b.attributes('role')).toBeUndefined()
      expect(b.attributes('aria-selected')).toBeUndefined()
      expect(b.attributes('tabindex')).toBeUndefined()
      expect(b.attributes('aria-label')).toBeUndefined()
    }
  })

  it('the badge is the tinted success pill, spoken with its tab', () => {
    mk()
    const badge = badgeOf(tabsOf(w)[0])
    expect(badge.attributes('class')).toContain(SUCCESS_PILL)
    expect(badge.attributes('aria-hidden')).toBeUndefined()
  })
})

describe('badgeVariant (A3a): one colour per fact, solid', () => {
  it('urgent is white on status-urgent-700, primary is white on action-primary-700', () => {
    mk({ tabs: [{ ...TABS[0], badgeVariant: 'urgent' }, { ...TABS[1], badgeVariant: 'primary' }, TABS[2]] })
    const [a, u] = tabsOf(w)
    expect(badgeOf(a).classes()).toEqual(expect.arrayContaining(['bg-status-urgent-700', 'text-white']))
    expect(badgeOf(u).classes()).toEqual(expect.arrayContaining(['bg-action-primary-700', 'text-white']))
    // Mutually exclusive arms (#2662): no success tint rides along.
    for (const b of [badgeOf(a), badgeOf(u)]) {
      expect(b.classes().filter((k) => /^(dark:)?(bg|text)-(?!\[)/.test(k)).length).toBe(2)
    }
  })

  it('neutral is a gray tint — a per-agent share of a count, not an event outcome (ent#610 A2 r1)', () => {
    mk({ tabs: [{ ...TABS[0], badgeVariant: 'neutral' }, TABS[1], TABS[2]] })
    const cls = badgeOf(tabsOf(w)[0]).classes()
    expect(cls).toEqual(expect.arrayContaining(['bg-gray-100', 'dark:bg-gray-750', 'text-gray-700', 'dark:text-gray-300']))
    expect(cls.some((k) => k.includes('status-'))).toBe(false)
  })

  it('an unknown variant falls back to the default pill, never to no colour', () => {
    mk({ tabs: [{ ...TABS[0], badgeVariant: 'nope' }] })
    expect(badgeOf(tabsOf(w)[0]).attributes('class')).toContain(SUCCESS_PILL)
  })
})

describe('badgeLabel (A8c): the tab says what the count counts', () => {
  it('names the tab and hides the bare number from assistive tech', () => {
    mk({ tabs: [{ ...TABS[0], badgeLabel: 'Action, 21 asks need you' }, TABS[1], TABS[2]] })
    const [a, u] = tabsOf(w)
    expect(a.attributes('aria-label')).toBe('Action, 21 asks need you')
    expect(badgeOf(a).attributes('aria-hidden')).toBe('true')
    // A tab without one is untouched.
    expect(u.attributes('aria-label')).toBeUndefined()
    expect(badgeOf(u).attributes('aria-hidden')).toBeUndefined()
  })
})

describe('tablistLabel (B6a): real tabs, manual activation', () => {
  it('renders a labelled tablist of tabs with aria-selected and one tab stop', () => {
    mk({ tablistLabel: 'Inbox', modelValue: 'unread' })
    const list = w.find('[role="tablist"]')
    expect(list.exists()).toBe(true)
    expect(list.attributes('aria-label')).toBe('Inbox')
    const tabs = list.findAll('[role="tab"]')
    expect(tabs.map((t) => t.attributes('aria-selected'))).toEqual(['false', 'true', 'false'])
    expect(tabs.map((t) => t.attributes('tabindex'))).toEqual(['-1', '0', '-1'])
  })

  it('arrows move focus without activating; Enter activates', async () => {
    mk({ tablistLabel: 'Inbox', modelValue: 'action' })
    const tabs = () => w.findAll('[role="tab"]')
    tabs()[0].element.focus()
    await tabs()[0].trigger('keydown', { key: 'ArrowRight' })
    expect(document.activeElement).toBe(tabs()[1].element)
    expect(w.emitted('update:modelValue')).toBeUndefined()
    await tabs()[1].trigger('keydown', { key: 'End' })
    expect(document.activeElement).toBe(tabs()[2].element)
    await tabs()[2].trigger('keydown', { key: 'ArrowRight' }) // wraps
    expect(document.activeElement).toBe(tabs()[0].element)
    await tabs()[0].trigger('keydown', { key: 'ArrowLeft' }) // wraps back
    expect(document.activeElement).toBe(tabs()[2].element)
    await tabs()[2].trigger('keydown', { key: 'Home' })
    expect(document.activeElement).toBe(tabs()[0].element)
    expect(w.emitted('update:modelValue')).toBeUndefined()
    await tabs()[1].trigger('click')
    expect(w.emitted('update:modelValue')).toEqual([['unread']])
  })

  it('keeps the More trigger outside the tablist', async () => {
    // Force an overflow: a 200px strip of 100px tabs.
    const proto = HTMLElement.prototype
    const rect = proto.getBoundingClientRect
    const cw = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')
    proto.getBoundingClientRect = function () { return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 } }
    Object.defineProperty(Element.prototype, 'clientWidth', { configurable: true, get() { return 200 } })
    try {
      mk({ tablistLabel: 'Inbox' })
      await nextTick(); await nextTick()
      expect(w.find('[role="tablist"]').exists()).toBe(true)
      const more = w.find('[data-overflow-trigger]')
      expect(more.exists()).toBe(true)
      expect(more.element.closest('[role="tablist"]')).toBeNull()
      expect(w.findAll('[role="tablist"] [role="tab"]').length).toBeLessThan(TABS.length)
    } finally {
      proto.getBoundingClientRect = rect
      if (cw) Object.defineProperty(Element.prototype, 'clientWidth', cw)
    }
  })

  it('a tab moved into the More menu keeps its badgeLabel as its name (round 3)', async () => {
    const proto = HTMLElement.prototype
    const rect = proto.getBoundingClientRect
    const cw = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')
    proto.getBoundingClientRect = function () { return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 } }
    Object.defineProperty(Element.prototype, 'clientWidth', { configurable: true, get() { return 200 } })
    try {
      const tabs = TABS.map((t) => (t.badge ? { ...t, badgeLabel: `${t.label}, ${t.badge} things` } : t))
      mk({ tabs, tablistLabel: 'Inbox' })
      await nextTick(); await nextTick()
      await w.find('[data-overflow-trigger]').trigger('click')
      await nextTick()
      const items = w.findAll('[data-menu-item]')
      expect(items.length).toBeGreaterThan(0)
      for (const it of items) {
        const t = tabs.find((x) => it.text().startsWith(x.label))
        expect(it.attributes('aria-label')).toBe(t.badgeLabel)
      }
    } finally {
      proto.getBoundingClientRect = rect
      if (cw) Object.defineProperty(Element.prototype, 'clientWidth', cw)
    }
  })
})
