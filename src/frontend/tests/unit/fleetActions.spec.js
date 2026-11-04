/**
 * What a Confirm click does: the same route the main app's own control calls, with the
 * person's session, once; then a read-back to check it took.
 */
import { describe, expect, it, vi } from 'vitest'
import { changeHeld, outcomeMessage, readFleet, runChange, writeFleetFile } from '../../src/components/portal/builder/fleetActions'

function fakeHttp(responses = {}) {
  const calls = []
  const handle = (method) => vi.fn(async (url, body) => {
    calls.push([method, url, body])
    const r = responses[`${method} ${url}`]
    if (r instanceof Error || (r && r.response)) throw r
    return { data: r ?? {} }
  })
  return { calls, get: handle('GET'), post: handle('POST'), put: handle('PUT'), delete: handle('DELETE') }
}
const card = (action, fields) => ({ kind: 'confirm', id: 'p1', action, words: 'Let Researcher call Writer', ...fields })

describe('runChange', () => {
  it('calls exactly the route the main app uses, once', async () => {
    const cases = [
      [card('allow-call', { from: 'researcher', to: 'writer' }), ['POST', '/api/agents/researcher/permissions/writer']],
      [card('stop-call', { from: 'researcher', to: 'writer' }), ['DELETE', '/api/agents/researcher/permissions/writer']],
      [card('share', { agent: 'writer', email: 'person@example.com' }), ['POST', '/api/agents/writer/share', { email: 'person@example.com' }]],
      [card('unshare', { agent: 'writer', email: 'person@example.com' }), ['DELETE', '/api/agents/writer/share/person%40example.com']],
      [card('schedule-on', { agent: 'researcher', schedule_id: 's1' }), ['POST', '/api/agents/researcher/schedules/s1/enable']],
      [card('schedule-off', { agent: 'researcher', schedule_id: 's1' }), ['POST', '/api/agents/researcher/schedules/s1/disable']],
      [card('public-link', { agent: 'writer' }), ['POST', '/api/agents/writer/public-links', { name: 'Public chat', link_type: 'chat' }]],
      [card('add-skill', { agent: 'writer', skill: 'summarise' }), ['POST', '/api/agents/writer/skills/summarise']],
    ]
    for (const [c, expected] of cases) {
      const http = fakeHttp()
      expect(await runChange(http, c)).toEqual({ ok: true })
      expect(http.calls).toEqual([[...expected, ...(expected.length === 2 ? [undefined] : [])]])
    }
  })

  it('a refusal changes nothing, says why, and is not retried', async () => {
    const http = fakeHttp({ 'POST /api/agents/researcher/permissions/writer': { response: { status: 403, data: { detail: 'Only the owner can modify agent permissions' } } } })
    expect(await runChange(http, card('allow-call', { from: 'researcher', to: 'writer' }))).toEqual({ ok: false, refused: true, reason: 'Only the owner can modify agent permissions' })
    expect(http.calls).toHaveLength(1)
  })

  it('a lost connection says so', async () => {
    const http = fakeHttp({ 'POST /api/agents/researcher/permissions/writer': new Error('Network Error') })
    expect(await runChange(http, card('allow-call', { from: 'researcher', to: 'writer' }))).toEqual({ ok: false, refused: false, reason: "Couldn't reach Trinity, so it may not have been made." })
  })

  it('never runs anything that is not a confirm card', async () => {
    const http = fakeHttp()
    expect((await runChange(http, { kind: 'refused', id: 'p', reason: 'x' })).ok).toBe(false)
    expect((await runChange(http, { kind: 'confirm', id: 'p', action: 'delete-agent', agent: 'writer' })).ok).toBe(false)
    expect(http.calls).toEqual([])
  })
})

describe('changeHeld', () => {
  it('reads it back from the agent', async () => {
    const http = fakeHttp({ 'GET /api/agents/researcher/permissions': { permitted_agents: [{ name: 'writer', permitted: true }] } })
    expect(await changeHeld(http, card('allow-call', { from: 'researcher', to: 'writer' }))).toBe(true)
    expect(await changeHeld(http, card('stop-call', { from: 'researcher', to: 'writer' }))).toBe(false)
  })
  it('checks shares, schedules, links and skills the same way', async () => {
    const http = fakeHttp({
      'GET /api/agents/writer/shares': [{ shared_with_email: 'person@example.com' }],
      'GET /api/agents/researcher/schedules': [{ id: 's1', enabled: true }],
      'GET /api/agents/writer/public-links': [{ id: 'l1' }],
      'GET /api/agents/writer/skills': [{ skill_name: 'summarise' }],
    })
    expect(await changeHeld(http, card('share', { agent: 'writer', email: 'person@example.com' }))).toBe(true)
    expect(await changeHeld(http, card('schedule-on', { agent: 'researcher', schedule_id: 's1' }))).toBe(true)
    expect(await changeHeld(http, card('schedule-off', { agent: 'researcher', schedule_id: 's1' }))).toBe(false)
    expect(await changeHeld(http, card('public-link', { agent: 'writer' }))).toBe(true)
    expect(await changeHeld(http, card('add-skill', { agent: 'writer', skill: 'summarise' }))).toBe(true)
  })
})

describe('telling the guide, and keeping its fleet file fresh', () => {
  it('says what happened in one line the guide reacts to', () => {
    const c = card('allow-call', { from: 'researcher', to: 'writer' })
    expect(outcomeMessage(c, 'done')).toBe('Done: Let Researcher call Writer.')
    expect(outcomeMessage(c, 'declined')).toBe('Not now: Let Researcher call Writer.')
    expect(outcomeMessage(c, { ok: false, reason: 'Only the owner can modify agent permissions' })).toBe("That didn't work: Let Researcher call Writer. Only the owner can modify agent permissions")
  })

  it('reads every own agent with the person session, and writes the file into their guide', async () => {
    const http = fakeHttp({
      'GET /api/agents/researcher/permissions': { permitted_agents: [{ name: 'writer' }] },
      'GET /api/agents/researcher/shares': [],
      'GET /api/agents/researcher/schedules': [{ id: 's1', name: 'Morning scan', enabled: false }],
      'GET /api/agents/researcher/public-links': [],
      'GET /api/agents/researcher/skills': [{ skill_name: 'summarise' }],
      'GET /api/agents/researcher/autonomy': { autonomy_enabled: false, total_schedules: 1, enabled_schedules: 0 },
    })
    const fleet = await readFleet(http, [{ name: 'researcher', display_label: 'Researcher' }])
    expect(fleet.agents[0]).toEqual({ name: 'researcher', label: 'Researcher', may_call: ['writer'], shared_with: [], schedules: [{ id: 's1', name: 'Morning scan', enabled: false }], autonomy: false, public_links: 0, skills: ['summarise'] })
    await writeFleetFile(http, 'atlas-22222222', fleet)
    expect(http.calls.at(-1)).toEqual(['PUT', '/api/agents/atlas-22222222/files?path=fleet-state.json', { content: JSON.stringify(fleet, null, 2) }])
  })

  it('an unreadable agent reads as nothing known, not as a failure', async () => {
    const http = fakeHttp({ 'GET /api/agents/researcher/permissions': { response: { status: 404 } } })
    const fleet = await readFleet(http, [{ name: 'researcher' }])
    expect(fleet.agents[0].may_call).toEqual([])
    expect(fleet.agents[0].autonomy).toBe(null)
  })
})

describe('final review: refused, or not sure', () => {
  it('a server error is not a refusal: it may have been made', async () => {
    const http = fakeHttp({ 'POST /api/agents/researcher/permissions/writer': { response: { status: 502, data: {} } } })
    expect(await runChange(http, card('allow-call', { from: 'researcher', to: 'writer' }))).toEqual({ ok: false, refused: false, reason: 'Trinity had a problem (502), so it may not have been made.' })
  })

  it('tells the guide when the page is not sure', () => {
    const c = card('allow-call', { from: 'researcher', to: 'writer' })
    expect(outcomeMessage(c, { ok: false, refused: false, reason: "Couldn't reach Trinity, so it may not have been made." }))
      .toBe("Not sure it worked: Let Researcher call Writer. Couldn't reach Trinity, so it may not have been made.")
  })

  it('compares emails without case', async () => {
    const http = fakeHttp({ 'GET /api/agents/writer/shares': [{ shared_with_email: 'Bob@Example.com' }] })
    expect(await changeHeld(http, card('share', { agent: 'writer', email: 'bob@example.com' }))).toBe(true)
  })
})


// When coming back to the Build tab rewrites the fleet file Atlas reads (final review, small issues).
describe('fleetRefreshDue', async () => {
  const { fleetRefreshDue, FLEET_BACK_MS, FLEET_WALK_MS } = await import('../../src/components/portal/builder/fleetActions')
  const now = 10_000_000

  it('on every return for a while after a walk was opened, not only the first', () => {
    const walkOpenedAt = now - 120_000
    expect(fleetRefreshDue({ now, readAt: now - 20_000, walkOpenedAt })).toBe(true)
    expect(fleetRefreshDue({ now, readAt: now - 5_000, walkOpenedAt })).toBe(true)
  })

  it('otherwise only after five quiet minutes, since each read is several routes per agent', () => {
    expect(FLEET_BACK_MS).toBe(300_000)
    expect(fleetRefreshDue({ now, readAt: now - 120_000, walkOpenedAt: 0 })).toBe(false)
    expect(fleetRefreshDue({ now, readAt: now - FLEET_BACK_MS, walkOpenedAt: 0 })).toBe(true)
    expect(fleetRefreshDue({ now, readAt: now - 120_000, walkOpenedAt: now - FLEET_WALK_MS - 1 })).toBe(false)
  })
})
