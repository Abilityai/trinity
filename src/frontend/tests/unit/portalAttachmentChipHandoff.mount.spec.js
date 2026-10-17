// @vitest-environment jsdom
/**
 * #3738 — the chip × is locked while the 1:1 composer has handed its files to
 * the shell for a room.
 *
 * An @mention of another agent escalates the message: the composer emits the
 * text and a copy of its chips, and the shell carries each landed file from the
 * origin agent's inbox into the room. The chips stay standing until the room
 * route unmounts this component — and a × in that window would delete the
 * origin agent's copy while the shell is still carrying it. `handedOff` holds
 * the × from the emit until the shell hands the text back after a failed
 * escalation (the `prefill` watcher).
 *
 * Mounted (#2918): the real PortalConversation (shallow) with the real
 * PortalAttachmentChip; the store's network calls are stubbed at the store
 * seam, and the shell's hand-back is played by `setProps({ prefill })`, which
 * is what Portal.vue does on a failed escalation. The escalation setup (rooms
 * on, two agents on the roster) is the one portalInChatReply.mount.spec.js uses.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

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
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const CHIP = '[data-testid="portal-attachment-chip"]'
const REMOVE = '[data-testid="portal-attachment-chip-remove"]'
const MESSAGE = '@sage can you check this'

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.multiAgentChatAvailable = true
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [] }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: 's1' }))
  store.uploadDocument = vi.fn(async (agent, f) => ({ filename: `stored_${f.name}` }))
  store.deleteUpload = vi.fn(async () => {})
  store.noteUploadPending = vi.fn()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountWithUploadedChip() {
  wrapper = shallowMount(PortalConversation, {
    props: {
      agent: { name: 'scout', playbooks: [] }, sessionId: 's1',
      roster: [{ name: 'scout' }, { name: 'sage' }],
    },
    global: { stubs: { PortalAttachmentChip: false } },
    attachTo: document.body,
  })
  await flushPromises()
  const f = new File(['x'.repeat(10)], 'report.pdf', { type: 'application/pdf' })
  await wrapper.find('textarea').trigger('paste', { clipboardData: { files: [f], types: ['Files'] } })
  await flushPromises()
  expect(store.uploadDocument).toHaveBeenCalledTimes(1)
  expect(wrapper.findAll(CHIP)).toHaveLength(1)
  return f
}

async function escalate() {
  const ta = wrapper.find('textarea')
  await ta.setValue(MESSAGE)
  await ta.trigger('keydown', { key: 'Enter' })
  await flushPromises()
}

// Both a VTU trigger (which skips a disabled element) and a native click on the
// element, so "nothing deleted" does not rest on the test library's behaviour.
async function clickRemove() {
  const button = wrapper.find(REMOVE)
  await button.trigger('click')
  button.element.click()
  await flushPromises()
}

describe('#3738 — the chip × during the escalation hand-off', () => {
  it('is enabled on a pending uploaded chip before any escalation', async () => {
    await mountWithUploadedChip()
    expect(wrapper.find(REMOVE).attributes('disabled')).toBeUndefined()
  })

  it('once the chips are handed to the shell, the × is disabled and a click deletes nothing', async () => {
    const f = await mountWithUploadedChip()
    await escalate()

    const [payload] = wrapper.emitted('escalate-to-room')[0]
    expect(payload.agents).toEqual(['scout', 'sage'])
    expect(payload.attachments.map((e) => e.file)).toEqual([f])
    // Still standing: on a real escalation the room route unmounts this
    // component, and until then the chip is what the shell is carrying.
    expect(wrapper.findAll(CHIP)).toHaveLength(1)

    expect(wrapper.find(REMOVE).attributes('disabled')).toBeDefined()
    await clickRemove()
    expect(store.deleteUpload).not.toHaveBeenCalled()
    expect(wrapper.findAll(CHIP)).toHaveLength(1)
  })

  it('once the shell hands the text back, the × is enabled again and a click removes the entry', async () => {
    await mountWithUploadedChip()
    await escalate()
    expect(wrapper.find(REMOVE).attributes('disabled')).toBeDefined()

    // A failed escalation: Portal.vue hands the message back through `prefill`.
    await wrapper.setProps({ prefill: MESSAGE })
    await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe(MESSAGE)

    expect(wrapper.find(REMOVE).attributes('disabled')).toBeUndefined()
    await wrapper.find(REMOVE).trigger('click')
    await flushPromises()
    expect(wrapper.findAll(CHIP)).toHaveLength(0)
    expect(store.deleteUpload).toHaveBeenCalledTimes(1)
    expect(store.deleteUpload).toHaveBeenCalledWith('scout', 'stored_report.pdf')
  })
})
