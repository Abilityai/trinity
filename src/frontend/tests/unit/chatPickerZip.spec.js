// @vitest-environment jsdom
/**
 * #2749 — the Chat tab's paperclip picker offers ZIP files, which the upload
 * service already accepts (stored as-is). Before, only drag-and-drop could
 * attach one.
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: [] })),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import ChatInput from '../../src/components/chat/ChatInput.vue'

describe('chat file picker', () => {
  it('accepts ZIP files', () => {
    setActivePinia(createPinia())
    const w = mount(ChatInput, { props: { agentName: 'fin', agentStatus: 'running' } })
    const accept = w.find('input[type="file"]').attributes('accept').split(',')
    expect(accept).toContain('.zip')
    expect(accept).toContain('application/zip')
    w.unmount()
  })
})
