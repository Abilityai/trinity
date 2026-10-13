/**
 * #2915 — the sync/delivery/aging badge is ONE rule every surface calls, and a
 * response refused with 409 `item_diverged` is a verb outcome the store turns
 * into a notice plus an acknowledged resend — never a silent 200 and never the
 * fetch-error banner.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import {
  queueSyncBadge,
  respondRefusedAsDiverged,
  SYNC_BADGE_COPY,
  QUEUE_RESPONSE_DIVERGED,
} from '@/utils/operatorQueue'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
import axios from 'axios'
import { useOperatorQueueStore } from '@/stores/operatorQueue'

describe('queueSyncBadge — one rule, silent when there is nothing to say', () => {
  it('a confirmed, unaged, delivered item has no badge', () => {
    expect(queueSyncBadge({ sync_state: 'confirmed', aging: false })).toBeNull()
    expect(queueSyncBadge({ sync_state: 'confirmed', delivery_state: 'delivered' })).toBeNull()
    expect(queueSyncBadge(null)).toBeNull()
  })

  it('names each divergence with the copy the operator reads', () => {
    expect(queueSyncBadge({ sync_state: 'changed', sync_detail: 'title,expires_at' })).toMatchObject({
      label: SYNC_BADGE_COPY.changed, variant: 'warning',
    })
    expect(queueSyncBadge({ sync_state: 'changed', sync_detail: 'title,expires_at' }).title).toContain('title, expires_at')
    expect(queueSyncBadge({ sync_state: 'closed_by_filer' }).label).toBe(SYNC_BADGE_COPY.closed_by_filer)
    expect(queueSyncBadge({ sync_state: 'missing' }).label).toBe(SYNC_BADGE_COPY.missing)
    expect(queueSyncBadge({ sync_state: 'stale_id' }).variant).toBe('neutral')
  })

  it('unconfirmed explains why and when it was last confirmed', () => {
    const b = queueSyncBadge({ sync_state: 'unconfirmed', sync_detail: 'agent_not_running', last_confirmed_at: '2026-09-22T10:00:00Z' })
    expect(b.label).toBe(SYNC_BADGE_COPY.unconfirmed)
    expect(b.title).toContain('not running')
    expect(b.title).toContain('2026-09-22T10:00:00Z')
    expect(queueSyncBadge({ sync_state: 'unconfirmed' }).title).toContain('Never confirmed')
  })

  it('an undelivered answer outranks the sync state on an answered item', () => {
    const b = queueSyncBadge({ sync_state: 'changed', delivery_state: 'undelivered', delivery_detail: 'entry_changed' })
    expect(b.label).toBe(SYNC_BADGE_COPY.undelivered)
    expect(b.variant).toBe('danger')
    expect(b.title).toContain('rewrote')
  })

  it('aging is the quietest badge and reads the server verdict, never recomputes', () => {
    const b = queueSyncBadge({ sync_state: 'confirmed', aging: true, aged_since: '2026-09-23T11:00:00Z' })
    expect(b.label).toBe(SYNC_BADGE_COPY.aging)
    expect(b.title).toContain('2026-09-23T11:00:00Z')
    // no created_at arithmetic here: an old item the server did not flag is not aged
    expect(queueSyncBadge({ sync_state: 'confirmed', created_at: '2020-01-01T00:00:00Z' })).toBeNull()
  })

  it('reads the portal projection spelling too', () => {
    expect(queueSyncBadge({ sync: 'closed' }).label).toBe(SYNC_BADGE_COPY.closed_by_filer)
    expect(queueSyncBadge({ sync: 'changed' }).label).toBe(SYNC_BADGE_COPY.changed)
    expect(queueSyncBadge({ sync: 'confirmed', aging: false })).toBeNull()
  })

  it('every variant is a BaseBadge variant, never a raw colour', () => {
    const allowed = ['success', 'warning', 'danger', 'info', 'urgent', 'autonomous', 'locked', 'claude', 'gemini', 'purple', 'neutral']
    const items = [
      { sync_state: 'changed' }, { sync_state: 'closed_by_filer' }, { sync_state: 'missing' },
      { sync_state: 'stale_id' }, { sync_state: 'unconfirmed' }, { aging: true },
      { delivery_state: 'undelivered' },
    ]
    for (const item of items) expect(allowed).toContain(queueSyncBadge(item).variant)
  })
})

describe('respondRefusedAsDiverged', () => {
  it('is exactly a 409 carrying code item_diverged', () => {
    expect(respondRefusedAsDiverged({ response: { status: 409, data: { detail: { code: 'item_diverged' } } } })).toBe(true)
    expect(respondRefusedAsDiverged({ response: { status: 409, data: { detail: 'Item is no longer pending' } } })).toBe(false)
    expect(respondRefusedAsDiverged({ response: { status: 400, data: { detail: { code: 'item_diverged' } } } })).toBe(false)
    expect(respondRefusedAsDiverged({})).toBe(false)
  })
})

describe('store — a refused response becomes a notice and an acknowledged resend', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    axios.get.mockReset()
    axios.post.mockReset()
  })

  const item = { id: 'i1', agent_name: 'a', type: 'approval', status: 'pending', options: ['approve', 'reject'], created_at: '2026-09-01T00:00:00Z' }

  it('carries the header counts from the list response', async () => {
    axios.get.mockResolvedValueOnce({ data: { items: [item], count: 1, undelivered_count: 2, closed_by_filer_count: 1 } })
    const store = useOperatorQueueStore()
    await store.fetchItems()
    expect(store.undeliveredCount).toBe(2)
    expect(store.closedByFilerCount).toBe(1)
  })

  it('409 item_diverged marks the card, refetches, and the next send acknowledges', async () => {
    axios.get.mockResolvedValue({ data: { items: [{ ...item, sync_state: 'changed', sync_detail: 'title' }], count: 1, undelivered_count: 0, closed_by_filer_count: 0 } })
    const store = useOperatorQueueStore()
    store.items = [item]
    axios.post.mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: 'item_diverged', message: 'changed' } } } })
    await store.respondToItem('i1', 'approve', '')
    expect(store.divergedItemId).toBe('i1')
    expect(store.expandedItemId).toBe('i1')
    expect(axios.get).toHaveBeenCalled()                       // the badge now renders from the refetch
    expect(axios.post.mock.calls[0][1]).not.toHaveProperty('acknowledge_divergence')
    expect(store.items[0].status).toBe('pending')              // never an optimistic "responded"
    expect(store.QUEUE_RESPONSE_DIVERGED).toBe(QUEUE_RESPONSE_DIVERGED)

    axios.post.mockResolvedValueOnce({ data: {} })
    await store.respondToItem('i1', 'approve', '')
    expect(axios.post.mock.calls[1][1]).toMatchObject({ response: 'approve', acknowledge_divergence: true })
    expect(store.divergedItemId).toBeNull()
  })

  it('a sync trigger over the WebSocket refetches the list', async () => {
    axios.get.mockResolvedValue({ data: { items: [], count: 0 } })
    const store = useOperatorQueueStore()
    store.handleWebSocketEvent({ type: 'operator_queue_sync', data: {} })
    expect(axios.get).toHaveBeenCalledTimes(1)
  })
})

describe('queueSyncBadge — review round 2 (#2989)', () => {
  it('a wrong-shape queue file is named', () => {
    expect(queueSyncBadge({ sync_state: 'unconfirmed', sync_detail: 'wrong_shape' }).title).toContain('not the expected shape')
  })
  it('an answer held by a stopped agent says so and promises delivery on start', () => {
    const t = queueSyncBadge({ delivery_state: 'undelivered', delivery_detail: 'agent_not_running' }).title
    expect(t).toContain('not running')
    expect(t).toContain('starts')
  })
  it('a cancellation whose entry the agent dropped never promises a retry', () => {
    const t = queueSyncBadge({ status: 'cancelled', delivery_state: 'undelivered', delivery_detail: 'entry_missing' }).title
    expect(t).not.toContain('retrying')
    expect(t).toContain('dropped')
  })
  it('a transient failure names when it was last tried', () => {
    const t = queueSyncBadge({ delivery_state: 'undelivered', delivery_detail: 'conflict', delivery_updated_at: '2026-09-24T09:00:00Z' }).title
    expect(t).toContain('2026-09-24T09:00:00Z')
  })
})
