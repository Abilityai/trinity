/**
 * #3242 — the reserved off-menu approval answer, at the shared rule and the
 * desktop store: the builder, the chip list, the human label, the gate marker,
 * and the body `respondToItem` actually POSTs.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import {
  SOMETHING_ELSE, buildQueueResponse, offeredChips, decisionLabel, decidedByOptions,
  optionsOf, queueResponseKind,
} from '@/utils/operatorQueue'

describe('buildQueueResponse — the reserved answer', () => {
  it('is the literal the backend reserves', () => {
    expect(SOMETHING_ELSE).toBe('(something else)')
  })
  it.each([[''], ['   '], [undefined]])('needs an instruction (%j → null)', (note) => {
    expect(buildQueueResponse({ kind: 'approval', option: SOMETHING_ELSE, note })).toBeNull()
  })
  it('puts the instruction in response_text, never in response', () => {
    expect(buildQueueResponse({ kind: 'approval', option: SOMETHING_ELSE, note: '  use the blue bucket ' }))
      .toEqual({ response: SOMETHING_ELSE, response_text: 'use the blue bucket' })
  })
  it('leaves an offered option exactly as it was', () => {
    expect(buildQueueResponse({ kind: 'approval', option: 'Approve', note: '' }))
      .toEqual({ response: 'Approve', response_text: null })
  })
})

describe('offeredChips / decisionLabel / decidedByOptions', () => {
  const marker = '(options omitted: exceeded size cap)'
  it('drops the literal and the size-cap marker, keeps the agent options in order', () => {
    expect(offeredChips({ options: ['Approve', SOMETHING_ELSE, marker, 'Deny'] })).toEqual(['Approve', 'Deny'])
  })
  it('leaves the kind rule unfiltered so it agrees with the sink', () => {
    const item = { type: 'approval', options: [SOMETHING_ELSE] }
    expect(optionsOf(item)).toEqual([SOMETHING_ELSE])
    expect(queueResponseKind(item)).toBe('approval')
  })
  it('labels the literal for a person and passes anything else through', () => {
    expect(decisionLabel(SOMETHING_ELSE)).toBe('Something else')
    expect(decisionLabel('Approve')).toBe('Approve')
  })
  it('marks a gate approval by its id or by the projection boolean', () => {
    expect(decidedByOptions({ request_id: 'gate-abc' })).toBe(true)
    expect(decidedByOptions({ id: 'x', decided_by_options: true })).toBe(true)
    expect(decidedByOptions({ request_id: 'approval-1' })).toBe(false)
    expect(decidedByOptions(null)).toBe(false)
  })
})

describe('respondToItem — the body for the reserved answer, and success', () => {
  let store
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    store = useOperatorQueueStore()
    store.items = [{ id: 'a', agent_name: 'x', type: 'approval', status: 'pending', options: ['Approve', 'Deny'] }]
  })
  it('POSTs the literal with the instruction and reports success', async () => {
    axios.post.mockResolvedValueOnce({ data: {} })
    const ok = await store.respondToItem('a', SOMETHING_ELSE, ' use the blue bucket ')
    expect(ok).toBe(true)
    expect(axios.post.mock.calls[0][1]).toEqual({ response: SOMETHING_ELSE, response_text: 'use the blue bucket' })
  })
  it('reports failure on a refusal so a form keeps its text', async () => {
    axios.post.mockRejectedValueOnce({ response: { status: 422, data: { detail: { code: 'instruction_required' } } } })
    expect(await store.respondToItem('a', SOMETHING_ELSE, 'x')).toBe(false)
  })
})
