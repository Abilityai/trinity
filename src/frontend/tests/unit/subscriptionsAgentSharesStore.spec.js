/**
 * trinity-enterprise#1059 — `fetchAgentShares` reads one share per agent, at
 * most six at a time.
 *
 * A subscription's breakdown can list many agents and each share is one GET,
 * so the store drains a shared queue over a fixed number of lanes. This drives
 * the real action against a deferred `getUsageShare` and records how many
 * reads are in flight at once: the high-water mark must stay at six, and every
 * name must still land in `shareByAgent` — a failed read as `{ error: true }`,
 * never dropped and never 0%.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import { useAgentsStore } from '@/stores/agents'
import { useSubscriptionsStore } from '@/stores/subscriptions'

const NAMES = Array.from({ length: 15 }, (_, i) => `agent-${i + 1}`)

beforeEach(() => {
  setActivePinia(createPinia())
})

/** A `getUsageShare` whose reads stay open until the test releases them. */
function deferredReads(agents, { failFor = new Set() } = {}) {
  const pending = []
  let inFlight = 0
  let highWater = 0
  agents.getUsageShare = vi.fn((name) => {
    inFlight += 1
    highWater = Math.max(highWater, inFlight)
    return new Promise((resolve, reject) => {
      pending.push(() => {
        inFlight -= 1
        if (failFor.has(name)) reject(new Error('404'))
        else resolve({ agent: name, pct: 10 })
      })
    })
  })
  return {
    pending,
    get highWater() { return highWater },
    get inFlight() { return inFlight },
  }
}

const flush = () => new Promise((r) => setTimeout(r, 0))

describe('fetchAgentShares', () => {
  it('never has more than six reads in flight and writes every name', async () => {
    const agents = useAgentsStore()
    const store = useSubscriptionsStore()
    const reads = deferredReads(agents, { failFor: new Set(['agent-7']) })

    const done = store.fetchAgentShares(NAMES)
    await flush()
    expect(reads.inFlight).toBe(6)

    // Release one read at a time: a freed lane picks up the next name, and the
    // count never climbs past six.
    while (reads.pending.length) {
      reads.pending.shift()()
      await flush()
      expect(reads.inFlight).toBeLessThanOrEqual(6)
    }
    await done

    expect(reads.highWater).toBe(6)
    expect(agents.getUsageShare).toHaveBeenCalledTimes(15)
    expect(Object.keys(store.shareByAgent).sort()).toEqual([...NAMES].sort())
    expect(store.shareByAgent['agent-7']).toEqual({ error: true })
    expect(store.shareByAgent['agent-15']).toEqual({ agent: 'agent-15', pct: 10 })
  })

  it('opens only as many lanes as there are names', async () => {
    const agents = useAgentsStore()
    const store = useSubscriptionsStore()
    const reads = deferredReads(agents)

    const done = store.fetchAgentShares(['a', 'b'])
    await flush()
    expect(reads.inFlight).toBe(2)
    while (reads.pending.length) {
      reads.pending.shift()()
      await flush()
    }
    await done
    expect(Object.keys(store.shareByAgent).sort()).toEqual(['a', 'b'])
  })
})
