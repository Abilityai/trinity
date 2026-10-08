// @vitest-environment jsdom
/**
 * trinity-enterprise#754 (C9) — a Workspace reply whose turn skipped approval
 * because the asker IS the approver says so.
 *
 * The server marks the reply row (`gate_self_approved`,
 * `gate_self_approved_by_viewer`); the client must carry the two booleans
 * through BOTH ways a reply reaches the thread — the history load and the
 * reply that just landed (`replyFromHistory` → `assistantRow`) — and the
 * conversation binds them to the marker under the agent's bubble.
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

import { assistantRow, replyFromHistory } from '@/components/portal/portalUtils'
import PortalConversation from '@/components/portal/PortalConversation.vue'
import ExecutionGateMarker from '@/components/skills/ExecutionGateMarker.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const reply = (over = {}) => ({
  id: 'm2', role: 'assistant', content: 'Paid.', created_at: '2026-10-08T09:02:00Z',
  execution_id: 'exec-self', gate_self_approved: true, gate_self_approved_by_viewer: true, ...over,
})

describe('the row mappers carry the marker', () => {
  it('assistantRow takes it from a history row — true only when the server said true', () => {
    expect(assistantRow(reply())).toMatchObject({ selfApproved: true, selfApprovedByViewer: true })
    expect(assistantRow(reply({ gate_self_approved: 'yes', gate_self_approved_by_viewer: 1 })))
      .toMatchObject({ selfApproved: false, selfApprovedByViewer: false })
    expect(assistantRow({ content: 'x' })).toMatchObject({ selfApproved: false, selfApprovedByViewer: false })
  })

  it('a reply that just landed brings it along (no reload needed)', () => {
    const got = replyFromHistory([{ role: 'user', content: 'q', execution_id: 'exec-self' }, reply()],
      null, 'exec-self')
    expect(got).toMatchObject({ response: 'Paid.', gateSelfApproved: true, gateSelfApprovedByViewer: true })
  })
})

describe('the conversation renders it under the agent bubble', () => {
  let wrapper
  beforeEach(() => {
    document.body.innerHTML = ''
    setActivePinia(createPinia())
  })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('marks the self-approved reply, and only that one', async () => {
    const store = useClientPortalStore()
    store.fetchHistory = vi.fn(async () => ({
      sessionId: 's1',
      messages: [
        { id: 'm1', role: 'user', content: '/pay-invoice', execution_id: 'exec-self' },
        reply(),
        { id: 'm3', role: 'user', content: 'thanks', execution_id: 'exec-2' },
        reply({ id: 'm4', content: 'Any time.', execution_id: 'exec-2', gate_self_approved: false,
          gate_self_approved_by_viewer: false }),
      ],
      truncated: false, inFlightExecutionId: null, inFlightWaitBudgetSeconds: null, lastTurnOutcome: null,
    }))
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'finance', playbooks: [] }, sessionId: 's1' },
      attachTo: document.body,
    })
    await flushPromises()

    const markers = wrapper.findAllComponents(ExecutionGateMarker)
    expect(markers.map((m) => [m.props('selfApproved'), m.props('byViewer')])).toEqual([[true, true], [false, false]])
  })
})
