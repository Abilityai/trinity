import { describe, it, expect, beforeEach, vi } from 'vitest'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

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

/**
 * #1927 — the operator-queue landing rule lives in the STORE, not the view.
 *
 * `utils/loadingState.js::decideAutoExpand` proves the decision table; what it
 * structurally cannot prove is the wiring that gives the table its inputs: the
 * armed bit starts true, is CONSUMED by an auto-expand, is CLEARED by any human
 * toggle, and is RE-ARMED when the open set drains. A view-local "once per
 * mount" had no way to express "the human took control" across polls, WS
 * deltas and remounts — the exact overrides the issue's fourth surface is about
 * (design-system p5: updates preserve expansion).
 */

function item(id, status = 'pending', priority = 'medium', createdAt = '2026-08-21T10:00:00Z') {
  return { id, agent_name: 'a', type: 'question', status, priority, title: id, question: id, created_at: createdAt }
}

async function serve(store, items) {
  axios.get.mockResolvedValueOnce({ data: { items, count: items.length } })
  await store.fetchItems()
  await nextTick()
}

describe('operatorQueue store — armed auto-expand episode (#1927)', () => {
  let store
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useOperatorQueueStore()
  })

  it('starts armed and expands the first OPEN item (priority order) exactly once', async () => {
    expect(store.autoExpandArmed).toBe(true)
    await serve(store, [item('low-1', 'pending', 'low'), item('crit-1', 'pending', 'critical'), item('done-1', 'responded')])
    expect(store.maybeAutoExpand()).toBe(true)
    expect(store.expandedItemId).toBe('crit-1')
    expect(store.autoExpandArmed).toBe(false)
    // A second evaluation (the next poll) is a no-op: consumed.
    expect(store.maybeAutoExpand()).toBe(false)
    expect(store.expandedItemId).toBe('crit-1')
  })

  it('a human toggle disarms: a later poll delta never re-expands a collapsed card', async () => {
    await serve(store, [item('a'), item('b')])
    store.maybeAutoExpand()
    expect(store.expandedItemId).toBe('a')

    store.toggleExpand('a') // collapse on purpose
    expect(store.expandedItemId).toBe(null)
    expect(store.autoExpandArmed).toBe(false)

    await serve(store, [item('a'), item('b'), item('c')]) // 2 → 3, the delta that used to re-expand
    expect(store.maybeAutoExpand()).toBe(false)
    expect(store.expandedItemId).toBe(null)
  })

  it('a human expanding something else is also "in control" — not overridden by a delta', async () => {
    await serve(store, [item('a'), item('b')])
    store.maybeAutoExpand()
    store.toggleExpand('a') // collapse a
    store.toggleExpand('b') // open b
    expect(store.expandedItemId).toBe('b')
    await serve(store, [item('a'), item('b'), item('c')])
    expect(store.maybeAutoExpand()).toBe(false)
    expect(store.expandedItemId).toBe('b')
  })

  it('re-arms when the open set drains to zero, so the next 0→N arrival lands expanded again', async () => {
    await serve(store, [item('a')])
    store.maybeAutoExpand()
    store.toggleExpand('a') // disarm
    expect(store.autoExpandArmed).toBe(false)

    await serve(store, [item('a', 'responded')]) // queue drained (0 open)
    expect(store.openItems).toHaveLength(0)
    expect(store.autoExpandArmed).toBe(true)

    await serve(store, [item('a', 'responded'), item('z')]) // 0 → 1
    expect(store.maybeAutoExpand()).toBe(true)
    expect(store.expandedItemId).toBe('z')
  })

  it('a STALE expandedItemId (answered while away) never blocks the landing rule', async () => {
    await serve(store, [item('a'), item('b')])
    store.maybeAutoExpand() // a expanded, disarmed
    // Drain → re-arm, but leave expandedItemId pointing at the now-closed 'a'.
    await serve(store, [item('a', 'responded')])
    expect(store.autoExpandArmed).toBe(true)
    expect(store.expandedItemId).toBe('a') // stale
    await serve(store, [item('a', 'responded'), item('b2')])
    expect(store.maybeAutoExpand()).toBe(true)
    expect(store.expandedItemId).toBe('b2')
  })

  it('does nothing while there are no open items, and leaves the armed bit alone', async () => {
    await serve(store, [])
    expect(store.maybeAutoExpand()).toBe(false)
    expect(store.expandedItemId).toBe(null)
    expect(store.autoExpandArmed).toBe(true)
  })
})
