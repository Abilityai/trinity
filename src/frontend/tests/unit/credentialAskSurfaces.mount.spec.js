// @vitest-environment jsdom
/**
 * trinity-enterprise#1066 — a `credential` ask everywhere but the secret field.
 *
 * - The shared kind rule (`queueResponseKind`) names it, so no surface falls
 *   through to a free-text or acknowledge control: an operator's answer would
 *   put the value into `response`, which the backend refuses anyway
 *   (`credential_answer_via_field`).
 * - The Operating Room card and detail panel are MOUNTED: no textarea, no
 *   "Got it", one line saying where it is answered.
 * - The thread's history row reads "NAME provided", never the answer value.
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive } from 'vue'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import { queueResponseKind, queueTypeLabel, workspaceOwnAnswer } from '@/utils/operatorQueue'
import { askHistoryLine } from '@/components/portal/portalChatAsks'
import QueueCard from '@/components/operator/QueueCard.vue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'

const item = {
  id: 'q1', agent_name: 'scout', request_id: 'credential-API_KEY', type: 'credential', status: 'pending',
  priority: 'medium', title: 'Needs API_KEY', question: 'To call the API.', options: [],
  context: { credential_name: 'API_KEY' }, created_at: '2026-11-02T10:00:00Z',
}

describe('the shared kind rule', () => {
  it('names a credential ask, so no surface offers a typed or acknowledge answer', () => {
    expect(queueResponseKind({ type: 'credential' })).toBe('credential')
    expect(queueResponseKind({ type: 'credential', options: ['x'] })).toBe('credential')
    expect(queueTypeLabel('credential')).toBe('Needs a credential')
  })
})

describe('the thread’s history row', () => {
  const ended = {
    id: 'c1', kind: 'credential', title: 'Needs API_KEY', credential_name: 'API_KEY',
    status: 'answered', ended_at: '2026-11-02T10:01:00Z', ended_by: 'you', answer: 'provided',
  }
  it('reads Credential · NAME provided', () => {
    const line = askHistoryLine(ended, Date.parse('2026-11-02T10:02:00Z'))
    expect(line.kind).toBe('Credential')
    expect(line.ending).toBe('API_KEY provided')
  })
  it('the viewer’s own answer is "You provided NAME", never the quoted response', () => {
    expect(workspaceOwnAnswer(ended).text).toBe('You provided API_KEY')
  })
  it('a dismissed credential ask keeps the ordinary ending', () => {
    const line = askHistoryLine({ ...ended, status: 'dismissed', answer: null, ended_by: 'you' })
    expect(line.ending).not.toContain('provided')
  })
})

async function mountCard() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useOperatorQueueStore()
  store.items = [item]
  store.expandedItemId = 'q1'
  const w = mount(QueueCard, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
  await nextTick()
  return w
}

describe('the Operating Room card', () => {
  it('has no respond box and no Got it — it says where the ask is answered', async () => {
    const w = await mountCard()
    expect(w.find('textarea').exists()).toBe(false)
    expect(w.findAll('button').some((b) => /got it|send answer/i.test(b.text()))).toBe(false)
    expect(w.find('[data-testid="queue-credential-notice"]').text()).toMatch(/Workspace/)
  })
})

describe('the Operating Room detail panel', () => {
  it('has no respond box and no Acknowledge', async () => {
    const detailStore = reactive({ selectedItem: item, selectedItemId: 'q1' })
    vi.doMock('@/stores/operatorQueue', () => ({ useOperatorQueueStore: () => detailStore }))
    vi.doMock('@/stores/agents', () => ({ useAgentsStore: () => ({ agents: [], agentRefForSlug: (name) => ({ name }) }) }))
    vi.resetModules()
    const { default: QueueItemDetail } = await import('@/components/operator/QueueItemDetail.vue')
    const w = mount(QueueItemDetail)
    expect(w.find('textarea').exists()).toBe(false)
    expect(w.findAll('button').some((b) => /acknowledge|send answer|submit/i.test(b.text()))).toBe(false)
    expect(w.find('[data-testid="queue-credential-notice"]').text()).toMatch(/Workspace/)
  })
})
