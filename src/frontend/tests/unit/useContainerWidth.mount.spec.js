// @vitest-environment jsdom
/**
 * trinity-enterprise#610 §3g A4 — `useContainerWidth` follows its element:
 * one that mounts LATE (a `v-if` column), one that goes away (width reads 0),
 * and it lets go of its observer on unmount (round-3 review F8).
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, ref, nextTick } from 'vue'
import { useContainerWidth } from '@/composables/useContainerWidth'

function fakeRO() {
  const all = []
  class RO {
    constructor(cb) { this.cb = cb; this.els = []; this.off = false; all.push(this) }
    observe(el) { this.els.push(el) }
    unobserve() {}
    disconnect() { this.off = true; this.els = [] }
  }
  return { RO, all }
}

function harness(RO, show) {
  let width
  const C = defineComponent({
    setup() {
      const el = ref(null)
      width = useContainerWidth(el, { ResizeObserver: RO })
      return () => h('div', [show.value ? h('div', { ref: el, 'data-x': '1' }) : null])
    },
  })
  return { C, width: () => width.value }
}

describe('useContainerWidth', () => {
  it('observes an element that mounts after its owner, and reads 0 once it is gone', async () => {
    const { RO, all } = fakeRO()
    const show = ref(false)
    const { C, width } = harness(RO, show)
    const w = mount(C)
    expect(all.length).toBe(0)
    show.value = true
    await nextTick(); await nextTick()
    expect(all.length).toBe(1)
    all[0].cb([{ contentRect: { width: 212.4 } }])
    expect(width()).toBe(212)
    show.value = false
    await nextTick(); await nextTick()
    expect(all[0].off).toBe(true)
    expect(width()).toBe(0)
    w.unmount()
  })

  it('disconnects its observer on unmount', async () => {
    const { RO, all } = fakeRO()
    const show = ref(true)
    const { C } = harness(RO, show)
    const w = mount(C)
    await nextTick()
    expect(all.length).toBe(1)
    expect(all[0].off).toBe(false)
    w.unmount()
    expect(all[0].off).toBe(true)
  })
})
