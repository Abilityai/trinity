// @vitest-environment jsdom
/**
 * ent#841 — `tab.closable`, an OPT-IN × on a tab (the Workspace chat strip).
 *
 * The × is a SIBLING button (nesting a button in a button is invalid HTML),
 * emits `close` with the tab id and never selects the tab, is inert with a
 * reason when `closeDisabled`, and the tab reserves its room in the mirror too.
 * A strip with no closable tab must keep its DOM: no wrapper, no ×.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import OverflowTabs from '@/components/OverflowTabs.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let w
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })
const mk = (tabs, model = tabs[0].id) => (w = mount(OverflowTabs, { props: { tabs, modelValue: model }, attachTo: document.body }))

describe('ent#841 closable tabs', () => {
  it('draws an accessible × only on closable tabs', () => {
    mk([
      { id: 'main', label: 'Main', pinned: true },
      { id: 'c1', label: 'First', closable: true, closeLabel: 'Archive chat' },
    ])
    const xs = w.findAll('[data-tab-close]')
    expect(xs).toHaveLength(1)
    expect(xs[0].attributes('data-tab-close')).toBe('c1')
    expect(xs[0].attributes('aria-label')).toBe('Archive chat')
    expect(xs[0].element.tagName).toBe('BUTTON')
    // Inline, the wrapper is the tab's own size — never the menu row's full
    // width, which would spread the strip.
    expect(xs[0].element.parentElement.className).toContain('shrink-0')
    expect(xs[0].element.parentElement.className).toContain('inline-flex')
    // A sibling of the tab button, never inside it.
    expect(xs[0].element.closest('button[type="button"]:not([data-tab-close])')).toBeNull()
  })

  it('clicking × emits close and does NOT select the tab', async () => {
    mk([{ id: 'a', label: 'A', closable: true }, { id: 'b', label: 'B', closable: true }], 'a')
    await w.find('[data-tab-close="b"]').trigger('click')
    expect(w.emitted('close')).toEqual([['b']])
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('an inert × says why and emits nothing', async () => {
    mk([{ id: 'a', label: 'A', closable: true, closeDisabled: true, closeTitle: 'Wait for the reply' }])
    const x = w.find('[data-tab-close="a"]')
    expect(x.attributes('disabled')).toBeDefined()
    expect(x.attributes('title')).toBe('Wait for the reply')
    await x.trigger('click')
    expect(w.emitted('close')).toBeUndefined()
  })

  it('reserves the ×\'s room in the visible tab AND the mirror', () => {
    mk([{ id: 'a', label: 'A', closable: true }, { id: 'b', label: 'B' }])
    const visible = w.findAll('nav button:not([data-measure-tab]):not([data-measure-more]):not([data-overflow-trigger]):not([data-tab-close])')
    expect(visible[0].classes()).toContain('pr-7')
    expect(visible[1].classes()).not.toContain('pr-7')
    const mirror = w.findAll('[data-measure-tab]')
    expect(mirror[0].classes()).toContain('pr-7')
    expect(mirror[1].classes()).not.toContain('pr-7')
  })

  it('a strip with no closable tab renders no × and no wrapper', () => {
    mk([{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }])
    expect(w.findAll('[data-tab-close]')).toHaveLength(0)
    expect(w.findAll('.group\\/tab')).toHaveLength(0)
  })
})
