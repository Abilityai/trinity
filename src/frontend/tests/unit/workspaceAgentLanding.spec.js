/**
 * `/workspace?agent=<name>` landing (ent#358).
 *
 * The Agent Detail Session surface is retired, and its deep links redirect
 * here. A redirected link carries an AGENT, never a thread id — so this
 * resolver decides which conversation the user actually lands in.
 *
 * The cases that matter are the ones where "just open something" would be
 * wrong: an agent the caller cannot reach (a stale or hand-edited link must not
 * conjure a conversation with it), and `?new=1` (an explicit ask for a fresh
 * thread must not silently resume the last one).
 */
import { describe, it, expect } from 'vitest'
import { resolveAgentLanding, deliveryFailureReason } from '@/components/portal/portalUtils'

const agents = [{ name: 'scribe' }, { name: 'auditor' }]
// As `fetchAllSessions` returns them: most recent first.
const threads = [
  { id: 's3', agent_name: 'auditor' },
  { id: 's2', agent_name: 'scribe' },
  { id: 's1', agent_name: 'scribe' },
]

describe('resolveAgentLanding', () => {
  it('resumes the most recent thread with that agent', () => {
    expect(resolveAgentLanding({ agent: 'scribe', agents, threads }))
      .toEqual({ agentName: 'scribe', sessionId: 's2' })
  })

  it('opens a fresh thread when the agent has none yet', () => {
    expect(resolveAgentLanding({ agent: 'auditor', agents, threads: [] }))
      .toEqual({ agentName: 'auditor', sessionId: null })
  })

  it('?new=1 starts fresh even when a thread exists', () => {
    expect(resolveAgentLanding({ agent: 'scribe', forceNew: true, agents, threads }))
      .toEqual({ agentName: 'scribe', sessionId: null })
  })

  it('ignores an agent that is not on the roster', () => {
    // Not an error state: the roster is the authority, and a bad link should
    // land in the Workspace rather than assert a relationship that isn't there.
    expect(resolveAgentLanding({ agent: 'ghost', agents, threads })).toBeNull()
  })

  it('ignores a missing or malformed agent query', () => {
    for (const agent of [undefined, null, '', 42, ['scribe']]) {
      expect(resolveAgentLanding({ agent, agents, threads })).toBeNull()
    }
  })

  it('tolerates a thread list that uses session_id instead of id', () => {
    const alt = [{ session_id: 'sX', agent_name: 'scribe' }]
    expect(resolveAgentLanding({ agent: 'scribe', agents, threads: alt }))
      .toEqual({ agentName: 'scribe', sessionId: 'sX' })
  })

  it('survives absent inputs rather than throwing at the landing', () => {
    expect(resolveAgentLanding()).toBeNull()
    expect(resolveAgentLanding({ agent: 'scribe' })).toBeNull()
    expect(resolveAgentLanding({ agent: 'scribe', agents, threads: null }))
      .toEqual({ agentName: 'scribe', sessionId: null })
  })
})

describe('deliveryFailureReason', () => {
  const err = (status, detail) => ({ response: { status, data: { detail } } })

  it('shows the backend reason verbatim — that is the whole point', () => {
    // Each of these is a real ClientPortalError the Workspace can hit, and the
    // user cannot tell them apart from a bare "Not delivered".
    for (const detail of [
      "The agent couldn't respond (it may be offline). Please try again.",
      'The agent is busy. Please try again shortly.',
      'The request timed out — try a simpler message.',
      'This conversation is already handling a message. Please try again shortly.',
    ]) {
      expect(deliveryFailureReason(err(502, detail))).toBe(detail)
    }
  })

  it('never renders a non-string detail at the user', () => {
    // FastAPI 422s send a list; a naive `String(detail)` prints "[object Object]".
    for (const detail of [[{ msg: 'bad' }], { msg: 'bad' }, null, undefined, '', '   ']) {
      const out = deliveryFailureReason(err(422, detail))
      expect(typeof out).toBe('string')
      expect(out).not.toMatch(/object Object/)
      expect(out.length).toBeGreaterThan(0)
    }
  })

  it('distinguishes a dead connection from a server answer', () => {
    expect(deliveryFailureReason({ message: 'Network Error' })).toMatch(/connection/i)
    expect(deliveryFailureReason(undefined)).toMatch(/connection/i)
  })

  it('has its own words for the statuses that carry no detail', () => {
    expect(deliveryFailureReason(err(413))).toMatch(/too large/i)
    expect(deliveryFailureReason(err(429))).toMatch(/too many/i)
    expect(deliveryFailureReason(err(500))).toMatch(/500/)
  })
})

describe('retired Sessions routes (ent#381)', () => {
  // The router file is the contract here: a redirect that drops the room id
  // would land a shared deep link on a generic index, which is how "we kept
  // your links working" quietly stops being true.
  it('maps a room deep link onto the equivalent workspace room', async () => {
    const src = await import('fs').then((fs) =>
      fs.readFileSync(new URL('../../src/router/index.js', import.meta.url), 'utf8'))

    expect(src).not.toMatch(/component:.*enterprise\/Sessions\.vue/)
    expect(src).toMatch(/path: '\/sessions\/:roomId\?'/)
    // The room id must reach the workspace room route, and query + hash survive.
    expect(src).toMatch(/\/workspace\/r\/\$\{to\.params\.roomId\}/)
    expect(src).toMatch(/query: to\.query, hash: to\.hash/)
  })

  it('the nav entry is gone', async () => {
    const src = await import('fs').then((fs) =>
      fs.readFileSync(new URL('../../src/components/NavBar.vue', import.meta.url), 'utf8'))
    expect(src).not.toMatch(/to="\/sessions"/)
  })
})
