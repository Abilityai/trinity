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
import { resolveAgentLanding } from '@/components/portal/portalUtils'

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
