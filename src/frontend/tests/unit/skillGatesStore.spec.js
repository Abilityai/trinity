/**
 * trinity-enterprise#754 — the agent-scoped gate store behind the Skills tab.
 *
 * Load the map first, probe the in-agent hook after (the probe never holds the
 * toggles back); a write sends the selected approver kind and re-reads the map
 * for the agent it was written for — never for whichever agent the cached page
 * moved to meanwhile (the 10-07 write-then-reload learning); per-skill busy
 * and error state, keyed by the lower-cased gate key.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))

import { useSkillGatesStore } from '../../src/stores/skillGates'

const MAP = (over = {}) => ({
  agent_name: 'a1',
  gates: [{ skill_name: 'pay', approver: 'primary', approver_reachable: true, origin: 'set' }],
  cleared_defaults: ['old'],
  approver_kinds: ['primary'],
  approvers: [{ kind: 'primary', reachable: true, viewer_fills: true }],
  default_deadline_hours: 24,
  hook: null,
  ...over,
})

function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of Object.values(api)) f.mockReset()
})

describe('load and probe', () => {
  it('loads the map without the probe, then probes in the background', async () => {
    api.get.mockImplementation((url, cfg) => Promise.resolve({
      data: cfg?.params?.probe ? MAP({ hook: 'predates' }) : MAP() }))
    const s = useSkillGatesStore()

    await s.load('a1', { probe: true })
    await Promise.resolve(); await Promise.resolve()

    expect(api.get.mock.calls[0]).toEqual(['/api/agents/a1/skill-gates'])
    expect(api.get.mock.calls[1]).toEqual(['/api/agents/a1/skill-gates', { params: { probe: true } }])
    expect(s.hasLoaded).toBe(true)
    expect(s.gates.find((g) => g.skill_name === 'pay').approver).toBe('primary')
    expect(s.approvers).toEqual([{ kind: 'primary', reachable: true, viewer_fills: true }])
    expect(s.hook).toBe('predates')
  })

  it('does not probe unless asked', async () => {
    api.get.mockResolvedValue({ data: MAP() })
    const s = useSkillGatesStore()
    await s.load('a1')
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(s.hook).toBeNull()
  })

  it('a failed read is named and keeps no stale map', async () => {
    api.get.mockRejectedValue({ response: { data: { detail: 'nope' } } })
    const s = useSkillGatesStore()
    await s.load('a1')
    expect(s.error).toBe('nope')
    expect(s.hasLoaded).toBe(false)
  })

  it('a failed probe is no answer, never an error', async () => {
    api.get.mockImplementation((url, cfg) => (cfg?.params?.probe
      ? Promise.reject(new Error('boom')) : Promise.resolve({ data: MAP() })))
    const s = useSkillGatesStore()
    await s.load('a1', { probe: true })
    await Promise.resolve(); await Promise.resolve()
    expect(s.error).toBeNull()
    expect(s.hook).toBeNull()
  })

  it('an answer for a previous agent is dropped', async () => {
    const slow = deferred()
    api.get.mockImplementationOnce(() => slow.promise).mockResolvedValue({ data: MAP({ agent_name: 'b2', gates: [] }) })
    const s = useSkillGatesStore()
    const first = s.load('a1')
    await s.load('b2')
    slow.resolve({ data: MAP() })
    await first
    expect(s.agentName).toBe('b2')
    expect(s.gates).toEqual([])
  })
})

describe('writes', () => {
  it('turning approval on sends the selected kind and re-reads the map', async () => {
    api.get.mockResolvedValue({ data: MAP({ gates: [] }) })
    api.put.mockResolvedValue({ data: { gate: {}, changed: true, warnings: ['approver_unassigned'] } })
    const s = useSkillGatesStore()
    await s.load('a1')
    api.get.mockResolvedValue({ data: MAP() })

    const ok = await s.setGate('Pay', 'approver')

    expect(ok).toBe(true)
    expect(api.put).toHaveBeenCalledWith('/api/agents/a1/skill-gates/pay', { approver: 'approver' })
    expect(s.gates.map((g) => g.skill_name)).toEqual(['pay'])
    expect(s.busy.pay).toBe(false)
  })

  it('turning approval off clears the gate', async () => {
    api.get.mockResolvedValue({ data: MAP() })
    api.delete.mockResolvedValue({ data: { changed: true, cleared: 'deleted' } })
    const s = useSkillGatesStore()
    await s.load('a1')
    api.get.mockResolvedValue({ data: MAP({ gates: [] }) })

    expect(await s.clearGate('pay')).toBe(true)

    expect(api.delete).toHaveBeenCalledWith('/api/agents/a1/skill-gates/pay')
    expect(s.gates).toEqual([])
  })

  it('a refused write names its reason on that skill only', async () => {
    api.get.mockResolvedValue({ data: MAP({ gates: [] }) })
    api.put.mockRejectedValue({ response: { status: 422, data: { detail: {
      status: 'refused', code: 'ephemeral_agent', message: 'Gates are not set on ephemeral agents.' } } } })
    const s = useSkillGatesStore()
    await s.load('a1')

    expect(await s.setGate('pay', 'primary')).toBe(false)

    expect(s.errors.pay).toBe('Gates are not set on ephemeral agents.')
    expect(s.errors.other).toBeUndefined()
    expect(s.busy.pay).toBe(false)
  })

  it('a write whose agent was left mid-flight does not reload the new agent', async () => {
    api.get.mockResolvedValue({ data: MAP({ gates: [] }) })
    const slow = deferred()
    api.put.mockImplementation(() => slow.promise)
    const s = useSkillGatesStore()
    await s.load('a1')
    const write = s.setGate('pay', 'primary')

    api.get.mockResolvedValue({ data: MAP({ agent_name: 'b2', gates: [] }) })
    await s.load('b2')
    const reads = api.get.mock.calls.length
    slow.resolve({ data: { warnings: [] } })
    await write

    expect(api.get.mock.calls.length).toBe(reads)     // nothing re-read for b2
    expect(s.agentName).toBe('b2')
    expect(s.gates).toEqual([])                       // b2's map, untouched by a1's write
  })
})
