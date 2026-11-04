/**
 * What the page reads for the levels, with the person's own session. A read that fails
 * keeps what the last good read saw, so a passing hiccup never unticks a level; before
 * any good read it counts as not seen. Keys are read sparingly: the route is rate
 * limited and audited.
 */
import { describe, expect, it } from 'vitest'
import { readLevelSignals } from '@/components/portal/builder/levelSignals'

const TIMELINE = '/api/activities/timeline?activity_types=agent_collaboration&limit=200'
const RESPONDED = '/api/operator-queue?type=approval&status=responded&limit=200'
const ACKNOWLEDGED = '/api/operator-queue?type=approval&status=acknowledged&limit=200'
const KEYS = (name) => `/api/agents/${name}/credential-requirements`

function fakeHttp(routes) {
  const calls = []
  return {
    calls,
    async get(url) {
      calls.push(url)
      if (!(url in routes)) throw Object.assign(new Error('not found'), { response: { status: 404 } })
      const value = routes[url]
      if (value instanceof Error) throw value
      return { data: value }
    },
  }
}
const refused = (status) => Object.assign(new Error('refused'), { response: { status } })
const success = (over = {}) => ({ status: 'success', triggered_by: 'chat', source_agent_name: null, started_at: '2026-11-03T10:00:00Z', completed_at: '2026-11-03T10:05:00Z', ...over })
const keysReport = (state, summary = {}) => ({ state, summary: { total: 0, set: 0, missing: 0, unknown: 0, blocking: 0, ...summary } })

describe('reading the level signals', () => {
  it('reads each agent, the call timeline once, and the answered approvals', async () => {
    const http = fakeHttp({
      '/api/agents/a/executions?limit=100': [success()],
      '/api/agents/a/shares': [{ shared_with_email: 'Sam@Example.com' }],
      '/api/agents/a/permissions': { permitted_agents: [{ name: 'b' }, {}] },
      [KEYS('a')]: keysReport('ok', { total: 2, set: 2 }),
      [TIMELINE]: { count: 0, activities: [] },
      [RESPONDED]: { items: [] },
      [ACKNOWLEDGED]: { items: [] },
    })
    const signals = await readLevelSignals(http, { own: ['a'] })
    expect(signals.own).toEqual(['a'])
    expect(signals.jobs.a).toHaveLength(1)
    expect(signals.shares.a).toEqual(['sam@example.com'])
    expect(signals.permissions.a).toEqual(['b'])
    expect(signals.keys.a).toBe('all-set')
    expect(http.calls.filter((u) => u === TIMELINE)).toHaveLength(1)
    expect(http.calls).toContain(RESPONDED)
    expect(http.calls).toContain(ACKNOWLEDGED)
    expect(http.calls.some((u) => /slack|telegram|whatsapp/.test(u))).toBe(false)
  })

  it('reads an agent\'s keys as all set, missing, not needed or unknown', async () => {
    const http = fakeHttp({
      [KEYS('set')]: keysReport('ok', { total: 3, set: 2, missing: 1, blocking: 0 }),
      [KEYS('missing')]: keysReport('ok', { total: 2, set: 0, missing: 2, blocking: 2 }),
      [KEYS('none')]: keysReport('no_credentials_required'),
      [KEYS('optional')]: keysReport('ok', { total: 1, set: 0, missing: 1, blocking: 0 }),
      [KEYS('partial')]: keysReport('declaration_incomplete', { total: 1, set: 1 }),
      [KEYS('degraded')]: keysReport('degraded'),
    })
    const signals = await readLevelSignals(http, { own: ['set', 'missing', 'none', 'optional', 'partial', 'degraded', 'gone'] })
    expect(signals.keys).toEqual({ set: 'all-set', missing: 'missing', none: 'needs-none', optional: 'needs-none', partial: 'all-set', degraded: 'unknown', gone: 'unknown' })
  })

  it('reads keys once settled, and again only when asked to recheck', async () => {
    const previous = { own: ['a', 'b', 'c'], keys: { a: 'all-set', b: 'needs-none', c: 'missing' } }
    const http = fakeHttp({ [KEYS('c')]: keysReport('ok', { total: 1, set: 1 }) })
    const quiet = await readLevelSignals(http, { own: ['a', 'b', 'c'], previous })
    expect(http.calls.some((u) => u.includes('credential-requirements'))).toBe(false)
    expect(quiet.keys).toEqual(previous.keys)
    const recheck = await readLevelSignals(http, { own: ['a', 'b', 'c'], previous, recheckKeys: true })
    expect(http.calls.filter((u) => u.includes('credential-requirements'))).toEqual([KEYS('c')])
    expect(recheck.keys).toEqual({ a: 'all-set', b: 'needs-none', c: 'all-set' })
  })

  it('keeps only finished calls between agents, from the timeline', async () => {
    const http = fakeHttp({
      [TIMELINE]: { count: 6, activities: [
        { agent_name: 'a', activity_state: 'completed', started_at: '2026-11-03T10:01:00Z', details: { source_agent: 'a', target_agent: 'b', action: 'chat' } },
        { agent_name: 'a', activity_state: 'completed', timestamp: '2026-11-03T10:02:00Z', details: JSON.stringify({ target_agent: 'c', action: 'parallel_task' }) },
        { agent_name: 'a', activity_state: 'failed', started_at: '2026-11-03T10:03:00Z', details: { source_agent: 'a', target_agent: 'b', action: 'chat' } },
        { agent_name: 'a', activity_state: 'completed', started_at: '2026-11-03T10:04:00Z', details: { source_agent: 'a', target_agent: 'b', action: 'refused' } },
        { agent_name: 'a', activity_state: 'completed', started_at: '2026-11-03T10:05:00Z', details: { source_agent: 'a', action: 'chat' } },
        { agent_name: 'a', activity_state: 'completed', started_at: '2026-11-03T10:06:00Z', details: '{not json' },
      ] },
    })
    const signals = await readLevelSignals(http, { own: ['a', 'b', 'c'] })
    expect(signals.calls).toEqual([
      { from: 'a', to: 'b', at: '2026-11-03T10:01:00Z' },
      { from: 'a', to: 'c', at: '2026-11-03T10:02:00Z' },
    ])
  })

  it('counts approvals the person answered on their own agents', async () => {
    const routes = {
      [RESPONDED]: { items: [
        { agent_name: 'a', responded_by_email: 'Me@Example.com' },
        { agent_name: 'a', responded_by_email: 'someone@example.com' },
        { agent_name: 'colleague-agent', responded_by_email: 'me@example.com' },
      ] },
      [ACKNOWLEDGED]: { items: [{ agent_name: 'b', responded_by_email: 'me@example.com' }] },
    }
    expect((await readLevelSignals(fakeHttp(routes), { own: ['a', 'b'], email: 'me@example.com' })).answered).toBe(2)
    expect((await readLevelSignals(fakeHttp(routes), { own: ['a', 'b'] })).answered).toBe(3)
  })

  it('before any good read, a read that fails, is refused or looks odd counts as not seen', async () => {
    const http = fakeHttp({
      '/api/agents/a/executions?limit=100': [success({ triggered_by: 'schedule' })],
      [KEYS('a')]: refused(429),
      '/api/agents/a/shares': { not: 'a list' },
      [TIMELINE]: {},
      [RESPONDED]: null,
    })
    const signals = await readLevelSignals(http, { own: ['a', 'stopped'] })
    expect(signals.jobs.stopped).toEqual([])
    expect(signals.shares.a).toEqual([])
    expect(signals.keys.a).toBe('unknown')
    expect(signals.calls).toEqual([])
    expect(signals.answered).toBe(0)
  })

  it('after a good read, a read that fails keeps what was seen', async () => {
    const previous = {
      own: ['a'],
      jobs: { a: [success()] },
      shares: { a: ['sam@example.com'] },
      permissions: { a: ['b'] },
      keys: { a: 'missing' },
      playbooks: { a: ['daily-post'] },
      calls: [{ from: 'a', to: 'b', at: '2026-11-03T10:01:00Z' }],
      answered: 1,
    }
    const http = fakeHttp({
      '/api/agents/a/executions?limit=100': refused(503),
      '/api/agents/a/shares': refused(404),
      '/api/agents/a/permissions': refused(409),
      [KEYS('a')]: refused(409),
      [TIMELINE]: refused(500),
      [RESPONDED]: refused(500),
      [ACKNOWLEDGED]: { items: [] },
    })
    const signals = await readLevelSignals(http, { own: ['a'], previous, recheckKeys: true })
    expect(signals).toEqual(previous)
  })

  it('with no agents of their own, reads nothing per agent', async () => {
    const http = fakeHttp({})
    const signals = await readLevelSignals(http, { own: [] })
    expect(signals.own).toEqual([])
    expect(http.calls.some((u) => u.startsWith('/api/agents/'))).toBe(false)
  })
})

// Level 2: a playbook added after the agent was built, so the page reads
// each agent's playbooks as the main app's Skills tab lists them.
describe('reading playbooks', () => {
  const PLAYBOOKS = (name) => `/api/agents/${name}/playbooks?last_known=true`

  it("reads each agent's playbook names, the last known list when it is stopped", async () => {
    const http = fakeHttp({
      [PLAYBOOKS('a')]: { skills: [{ name: 'daily-post', source: 'agent' }, { name: 'onboarding' }, {}], count: 3 },
      [PLAYBOOKS('b')]: { skills: [], count: 0 },
    })
    const signals = await readLevelSignals(http, { own: ['a', 'b'] })
    expect(signals.playbooks).toEqual({ a: ['daily-post', 'onboarding'], b: [] })
  })

  it("keeps the last list when a read fails, and before any good read doesn't know", async () => {
    const http = fakeHttp({ [PLAYBOOKS('a')]: refused(503) })
    expect((await readLevelSignals(http, { own: ['a'], previous: { playbooks: { a: ['x'] } } })).playbooks.a).toEqual(['x'])
    expect((await readLevelSignals(http, { own: ['a'] })).playbooks.a).toBe(null)
  })
})
