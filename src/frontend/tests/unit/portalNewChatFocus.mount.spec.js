// @vitest-environment jsdom
/**
 * trinity-enterprise#784 — who gets the caret when a fresh composer mounts.
 *
 * Opening an agent now lands on a NEW chat, which means the #2579 mount focus
 * fires on a path the person did not ask for in those words. On a phone that
 * slides the on-screen keyboard up over the transcript — the "unprompted"
 * the issue names. So the focus is gated on WHY this instance mounted:
 *
 *   `always`       — a gesture (New chat, ⌘J, the agent picker, switch-agent).
 *                    They asked for a composer; focus on any pointer.
 *   `fine-pointer` — a landing (`landOnAgent`, the `?agent=` deep link).
 *                    Focus only where it cannot summon a keyboard.
 *
 * Mounted (#2918), not read as source: the predicate and the prop default are
 * the whole behaviour, and an inverted one reads byte-identically to a regex.
 * Recipe from `portalThreadMissing.mount.spec.js`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let wrapper
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
})
afterEach(() => {
  wrapper?.unmount(); wrapper = null
  delete window.matchMedia
})

// The pointer is read through `window.matchMedia`, so it has to be stubbed
// BEFORE the mount — the gate runs in `onMounted`.
function pointer(kind) {
  window.matchMedia = vi.fn((q) => ({ matches: kind === 'fine' && q === '(pointer: fine)' }))
}

async function mountFresh(props = {}) {
  // The mutation check from the 2026-10-01 learnings: focus has to be provably
  // somewhere else first, or "the textarea is focused" can pass on a harness
  // that focuses it for unrelated reasons.
  expect(document.activeElement).toBe(document.body)
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: null, newChat: true, ...props },
    attachTo: document.body,
  })
  await flushPromises()
  await nextTick()
  return wrapper.find('textarea').element
}

describe('ent#784 — a landing does not summon the keyboard', () => {
  it('a landing mount on a COARSE pointer leaves the composer unfocused', async () => {
    pointer('coarse')
    const el = await mountFresh({ focusOnMount: 'fine-pointer' })
    expect(document.activeElement).not.toBe(el)
  })

  it('a landing mount on a FINE pointer focuses the composer', async () => {
    // Desktop AC: open an agent and start typing, no click first.
    pointer('fine')
    const el = await mountFresh({ focusOnMount: 'fine-pointer' })
    expect(document.activeElement).toBe(el)
  })

  it('a phone with no matchMedia at all is treated as coarse', async () => {
    const el = await mountFresh({ focusOnMount: 'fine-pointer' })
    expect(document.activeElement).not.toBe(el)
  })
})

describe('#2579 AC 2 — an explicit gesture still focuses, on any pointer', () => {
  it.each([['fine'], ['coarse']])('a gesture mount on a %s pointer focuses', async (kind) => {
    pointer(kind)
    const el = await mountFresh({ focusOnMount: 'always' })
    expect(document.activeElement).toBe(el)
  })

  it('defaults to the gesture reading, so a caller that says nothing keeps #2579', async () => {
    pointer('coarse')
    const el = await mountFresh()
    expect(document.activeElement).toBe(el)
  })
})

describe('ent#784 — the gate is about a FRESH composer only', () => {
  it('a mount that is not a new chat never auto-focuses, fine pointer or not', async () => {
    pointer('fine')
    const el = await mountFresh({ newChat: false, focusOnMount: 'always' })
    expect(document.activeElement).not.toBe(el)
  })
})
