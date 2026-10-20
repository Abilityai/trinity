// @vitest-environment jsdom
/**
 * #1923 — the modal shell's WIRING, proven by mounting it.
 *
 * `focusTrap.spec.js` proves the rules; this file proves that `BaseModal.vue`
 * actually attaches them to a DOM: focus moves in on open, Tab wraps inside,
 * Esc emits `close`, focus goes home on dismiss, the scroll lock is taken and
 * released — and that two nested instances share one lock. Every one of these
 * was a mutation the whole suite could not see before this spec existed (the
 * merge-train ejection of #2778); with the wiring gutted, the suite was
 * byte-identical green.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import BaseModal from '../../src/components/base/BaseModal.vue'
import { bodyScrollLock } from '../../src/utils/focusTrap.js'

const mounted = []
afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount()
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

/** A page with a trigger button and a modal holding two buttons, the second destructive. */
function harness(extra = {}) {
  const open = ref(false)
  const closes = []
  const Host = defineComponent({
    setup() {
      return () => h('div', [
        h('button', { id: 'trigger', onClick: () => { open.value = true } }, 'Open'),
        h(BaseModal, {
          modelValue: open.value,
          'onUpdate:modelValue': (v) => { open.value = v },
          onClose: () => closes.push(1),
          ...extra,
        }, () => [
          h('p', 'Some dialog text nobody can focus'),
          h('button', { id: 'cancel' }, 'Cancel'),
          h('button', { id: 'delete', 'data-destructive': '' }, 'Delete'),
        ]),
      ])
    },
  })
  const w = mount(Host, { attachTo: document.body })
  mounted.push(w)
  return { w, open, closes }
}

function press(target, key, init = {}) {
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }))
}

const overlay = () => document.querySelector('[role="dialog"]')

describe('BaseModal (mounted)', () => {
  it('opens with focus on the first non-destructive control and remembers the trigger', async () => {
    const { open } = harness()
    document.getElementById('trigger').focus()
    open.value = true
    await nextTick(); await nextTick()
    expect(overlay()).not.toBeNull()
    expect(document.activeElement.id).toBe('cancel')      // never Delete first (p19)
  })

  it('Tab wraps last → first and Shift+Tab first → last, inside the overlay', async () => {
    const { open } = harness()
    open.value = true
    await nextTick(); await nextTick()
    document.getElementById('delete').focus()
    press(document.activeElement, 'Tab')
    expect(document.activeElement.id).toBe('cancel')
    press(document.activeElement, 'Tab', { shiftKey: true })
    expect(document.activeElement.id).toBe('delete')
  })

  it('Esc closes: `close` and `update:modelValue(false)` fire, and focus goes home', async () => {
    const { open, closes } = harness()
    document.getElementById('trigger').focus()
    open.value = true
    await nextTick(); await nextTick()
    press(document.activeElement, 'Escape')
    await nextTick(); await nextTick()
    expect(closes).toHaveLength(1)
    expect(open.value).toBe(false)
    expect(overlay()).toBeNull()
    expect(document.activeElement.id).toBe('trigger')     // focus return
  })

  it('the overlay itself is focusable, so Esc works when focus is not on a control', async () => {
    // Clicking non-focusable text lands focus on the nearest focusable
    // ancestor. Without tabindex="-1" that was <body>, and the overlay's
    // keydown never fired — Esc did nothing. The review's second defect.
    const { open, closes } = harness()
    open.value = true
    await nextTick(); await nextTick()
    const ov = overlay()
    expect(ov.getAttribute('tabindex')).toBe('-1')
    ov.focus()
    expect(document.activeElement).toBe(ov)
    press(ov, 'Escape')
    await nextTick()
    expect(closes).toHaveLength(1)
  })

  it('a modal with no tabbable children still takes focus and can be dismissed', async () => {
    const open = ref(false)
    const closes = []
    const Host = defineComponent({
      setup: () => () => h(BaseModal, {
        modelValue: open.value, 'onUpdate:modelValue': (v) => { open.value = v }, onClose: () => closes.push(1),
      }, () => [h('p', 'Just text')]),
    })
    mounted.push(mount(Host, { attachTo: document.body }))
    open.value = true
    await nextTick(); await nextTick()
    expect(document.activeElement).toBe(overlay())
    press(document.activeElement, 'Escape')
    await nextTick()
    expect(closes).toHaveLength(1)
  })

  it('backdrop click closes; a click inside the panel does not; closeOnBackdrop=false opts out', async () => {
    const { open, closes } = harness()
    open.value = true
    await nextTick(); await nextTick()
    document.getElementById('cancel').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(closes).toHaveLength(0)
    overlay().dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(closes).toHaveLength(1)

    const b = harness({ closeOnBackdrop: false })
    b.open.value = true
    await nextTick(); await nextTick()
    const dialogs = document.querySelectorAll('[role="dialog"]')
    dialogs[dialogs.length - 1].dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(b.closes).toHaveLength(0)
  })

  it('locks body scroll while open and restores the previous value on close', async () => {
    document.body.style.overflow = 'auto'
    const { open } = harness()
    expect(document.body.style.overflow).toBe('auto')     // mounting closed touches nothing
    open.value = true
    await nextTick()
    expect(document.body.style.overflow).toBe('hidden')
    open.value = false
    await nextTick()
    expect(document.body.style.overflow).toBe('auto')     // restored, not cleared
    expect(bodyScrollLock.holders).toBe(0)
  })

  it('two nested modals share one lock: the inner one mounting or closing never unlocks the outer', async () => {
    // The #2780 shape: a ConfirmDialog declared INSIDE another modal's slot.
    const outer = ref(false)
    const inner = ref(false)
    const Host = defineComponent({
      setup: () => () => h(BaseModal, {
        modelValue: outer.value, 'onUpdate:modelValue': (v) => { outer.value = v },
      }, () => [
        h('button', { id: 'edit' }, 'Edit'),
        h(BaseModal, {
          modelValue: inner.value, 'onUpdate:modelValue': (v) => { inner.value = v },
        }, () => [h('button', { id: 'confirm' }, 'Confirm')]),
      ]),
    })
    mounted.push(mount(Host, { attachTo: document.body }))
    outer.value = true
    await nextTick(); await nextTick()
    expect(document.body.style.overflow).toBe('hidden')   // the inner mounted CLOSED and did not reset it
    inner.value = true
    await nextTick(); await nextTick()
    expect(bodyScrollLock.holders).toBe(2)
    inner.value = false
    await nextTick()
    expect(document.body.style.overflow).toBe('hidden')   // the outer still holds it
    expect(bodyScrollLock.holders).toBe(1)
    outer.value = false
    await nextTick()
    expect(document.body.style.overflow).toBe('')
    expect(bodyScrollLock.holders).toBe(0)
  })

  it('unmounting an OPEN modal releases its lock; unmounting a closed one releases nothing', async () => {
    document.body.style.overflow = 'hidden'                // someone else's lock
    const closed = harness()
    closed.w.unmount(); mounted.splice(mounted.indexOf(closed.w), 1)
    expect(document.body.style.overflow).toBe('hidden')    // untouched

    document.body.style.overflow = ''
    const { w, open } = harness()
    open.value = true
    await nextTick()
    expect(bodyScrollLock.holders).toBe(1)
    w.unmount(); mounted.splice(mounted.indexOf(w), 1)
    expect(bodyScrollLock.holders).toBe(0)
    expect(document.body.style.overflow).toBe('')
  })
})
