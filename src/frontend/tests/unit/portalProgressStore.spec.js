/**
 * ent#1175 — the progress store reads through the Workspace client, treats the
 * WS event as a thin trigger (#918), and never caches a failed read.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const get = vi.fn()
vi.mock('../../src/stores/clientPortal', () => ({
  portalHttp: { get: (...a) => get(...a) },
  useClientPortalStore: () => ({ authHeader: { Authorization: 'Bearer portal-token' } }),
}))

const { usePortalProgressStore } = await import('../../src/stores/portalProgress')

const ok = (steps, all_done = false) => ({ data: { steps, all_done, active: true } })
const URL = '/api/enterprise/client-portal/agents/regent-bob/progress'

describe('portalProgress store', () => {
  beforeEach(() => { setActivePinia(createPinia()); get.mockReset() })

  it('reads with the portal auth header', async () => {
    get.mockResolvedValue(ok([{ key: 'a', state: 'done' }]))
    const s = usePortalProgressStore()
    await s.fetchProgress('regent-bob')
    expect(get).toHaveBeenCalledWith(URL, { headers: { Authorization: 'Bearer portal-token' } })
    expect(s.progressFor('regent-bob').active).toBe(true)
  })

  it('reuses a good read on remount', async () => {
    get.mockResolvedValue(ok([]))
    const s = usePortalProgressStore()
    await s.fetchProgress('regent-bob')
    await s.fetchProgress('regent-bob')
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('retries after a failed read instead of caching it', async () => {
    get.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(ok([{ key: 'a', state: 'done' }], true))
    const s = usePortalProgressStore()
    await s.fetchProgress('regent-bob')
    expect(s.progressFor('regent-bob').active).toBe(false)
    await s.fetchProgress('regent-bob')
    expect(get).toHaveBeenCalledTimes(2)
    expect(s.progressFor('regent-bob').all_done).toBe(true)
  })

  it('a WS event refetches; it never trusts a payload', async () => {
    get.mockResolvedValue(ok([{ key: 'a', state: 'in_progress' }]))
    const s = usePortalProgressStore()
    await s.fetchProgress('regent-bob')
    get.mockResolvedValue(ok([{ key: 'a', state: 'done' }], true))
    await s.handleWebSocketEvent({ type: 'member_progress_update', agent_name: 'regent-bob',
      steps: [{ key: 'a', state: 'failed' }] })
    expect(get).toHaveBeenCalledTimes(2)
    expect(s.progressFor('regent-bob').steps[0].state).toBe('done')
  })

  it('refetchAll re-reads every held card (resync_required)', async () => {
    get.mockResolvedValue(ok([]))
    const s = usePortalProgressStore()
    await s.fetchProgress('regent-bob')
    await s.fetchProgress('regent-amy')
    await s.refetchAll()
    expect(get).toHaveBeenCalledTimes(4)
  })
})
