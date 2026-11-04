// @vitest-environment jsdom
/**
 * Show me walks light an agent page's tab by its id (`[data-tab="permissions"]`), in the
 * strip or, when it doesn't fit, in the More list. The hidden measuring row must not carry
 * it: it has boxes too, and a walk lighting an invisible copy would point at nothing.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import OverflowTabs from '@/components/OverflowTabs.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const TABS = ['overview', 'tasks', 'chat', 'schedules', 'permissions'].map((id) => ({ id, label: id }))

let w
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

// A 200px strip of 100px tabs, so the last ones go under More (the overflowTabsTablist harness).
async function openMenu() {
  const proto = HTMLElement.prototype
  const rect = proto.getBoundingClientRect
  const cw = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth')
  proto.getBoundingClientRect = function () { return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 } }
  Object.defineProperty(Element.prototype, 'clientWidth', { configurable: true, get() { return 200 } })
  try {
    w = mount(OverflowTabs, { props: { tabs: TABS, modelValue: 'overview' }, attachTo: document.body })
    await nextTick(); await nextTick()
    await w.find('[data-overflow-trigger]').trigger('click')
    await nextTick()
  } finally {
    proto.getBoundingClientRect = rect
    if (cw) Object.defineProperty(Element.prototype, 'clientWidth', cw)
  }
}

describe('tabs a walk can find', () => {
  it('marks each tab in the strip and in the More list with its id, once', async () => {
    await openMenu()
    const marked = [...document.querySelectorAll('[data-tab]')].map((el) => el.getAttribute('data-tab'))
    expect([...marked].sort()).toEqual([...TABS.map((t) => t.id)].sort())
    expect(document.querySelector('[data-overflow-menu] [data-tab="permissions"]')).not.toBe(null)
  })

  it('leaves the hidden measuring row unmarked', async () => {
    await openMenu()
    expect(document.querySelectorAll('[data-measure-tab][data-tab]')).toHaveLength(0)
    expect(document.querySelectorAll('[data-measure-more][data-overflow-trigger]')).toHaveLength(0)
  })
})
