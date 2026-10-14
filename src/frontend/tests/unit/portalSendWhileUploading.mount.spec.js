// @vitest-environment jsdom
/**
 * #3265 review — a send that waits for uploads must not let a second send in.
 *
 * The ordinary send clears the composer and then awaits the in-flight uploads,
 * and `sending` is only set later, inside `deliver()`. While a big file was
 * still uploading, a second Enter with new text passed the guard and two turns
 * ran at once. Mounted (#2918): the defect is two `startPortalChat` calls, so
 * the test counts them.
 *
 * And the per-message bound: chips pile up across drops and pastes, so the
 * composer — not the server's 422 — is where "up to 20" is enforced.
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
import { usePortalFileDrop, MAX_BATCH_FILES } from '@/composables/usePortalFileDrop'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const MAIN = { id: 'm', session_id: 'm', agent_name: 'scout', is_main: true, title: 'Main', last_message_at: '2026-09-20T10:00:00Z' }

let wrapper
let store
let finishUpload
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.fetchHistory = vi.fn(async () => ({ sessionId: 'm', messages: [] }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: 'm' }))
  store.uploadDocument = vi.fn(() => new Promise((resolve) => { finishUpload = resolve }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function type(text) {
  const ta = wrapper.find('textarea')
  await ta.setValue(text)
  await ta.trigger('keydown', { key: 'Enter' })
}

describe('#3265 — send while an upload is still in flight', () => {
  it('runs ONE turn when Enter is pressed again before the upload lands', async () => {
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'scout', playbooks: [] }, threads: [MAIN], sessionId: 'm' },
      attachTo: document.body,
    })
    await flushPromises()

    const file = new File(['x'.repeat(10)], 'big.pdf', { type: 'application/pdf' })
    await wrapper.find('textarea').trigger('paste', { clipboardData: { files: [file], types: ['Files'] } })
    expect(store.uploadDocument).toHaveBeenCalledTimes(1)

    await type('first')
    await flushPromises()
    await type('second')
    await flushPromises()
    expect(store.startPortalChat).not.toHaveBeenCalled()

    finishUpload({})
    await flushPromises()

    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(store.startPortalChat.mock.calls[0][1]).toBe('first')
  })
})

describe('#3265 — the file bound is per message, not per gesture', () => {
  const files = (n, tag) => Array.from({ length: n }, (_, i) => new File(['x'], `${tag}-${i}.txt`))

  it('caps the total across two drops and says so', async () => {
    const drop = usePortalFileDrop(vi.fn(async () => ({})))
    await drop.addFiles(files(11, 'a'))
    await drop.addFiles(files(11, 'b'))
    expect(drop.entries.value).toHaveLength(MAX_BATCH_FILES)
    expect(drop.batchNotice.value).toMatch(/first 9 of 11/)
  })

  it('adds nothing once the message is full, and says that too', async () => {
    const upload = vi.fn(async () => ({}))
    const drop = usePortalFileDrop(upload)
    await drop.addFiles(files(MAX_BATCH_FILES, 'a'))
    upload.mockClear()
    await drop.addFiles(files(3, 'b'))
    expect(upload).not.toHaveBeenCalled()
    expect(drop.entries.value).toHaveLength(MAX_BATCH_FILES)
    expect(drop.batchNotice.value).toMatch(/up to 20 files/)
  })
})
