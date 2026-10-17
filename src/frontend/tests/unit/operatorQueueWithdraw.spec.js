/**
 * trinity-enterprise#901 — an ask the agent WITHDREW, in the one ending rule.
 * The server derives the kind once (`agent_ending`: replaced | withdrawn) and
 * every reader keys on it — never on the reason text, never on a `replaced_by`
 * the Workspace projection may withhold.
 */
import { describe, it, expect } from 'vitest'
import { queueEnding, queueEndingText, workspaceEndingText, ENDING_LABELS } from '@/utils/operatorQueue'

const row = (over = {}) => ({
  id: 'i1', agent_name: 'scout', type: 'approval', status: 'cancelled', disposition: 'cancelled',
  disposed_by: 'agent', disposed_at: '2026-10-14T10:00:00Z', ...over,
})

describe('queueEnding — a withdraw', () => {
  it('reads "Withdrawn by the agent" and carries the agent\'s reason apart from the text', () => {
    const e = queueEnding(row({ agent_ending: 'withdrawn', disposition_reason: 'The build fixed itself.' }))
    expect(e.kind).toBe('withdrawn')
    expect(e.who).toBe('the agent')
    expect(e.reason).toBe('The build fixed itself.')
    expect(queueEndingText(e)).toBe('Withdrawn by the agent')
    expect(ENDING_LABELS.withdrawn).toBe('Withdrawn')
  })

  it('on a Workspace ask the reason is the projection\'s ending_reason', () => {
    const ask = { status: 'cancelled', ended_by: 'agent', agent_ending: 'withdrawn',
      ending_reason: 'Not needed any more', ended_at: '2026-10-14T10:00:00Z', replaced_by: null }
    expect(queueEnding(ask).reason).toBe('Not needed any more')
    expect(workspaceEndingText(ask)).toBe('Withdrawn by the agent')
  })

  it('a reason that reads "replaced" does not make a withdraw a replace', () => {
    const e = queueEnding(row({ agent_ending: 'withdrawn', disposition_reason: 'replaced' }))
    expect(e.kind).toBe('withdrawn')
  })

  it('a replace is unchanged and carries no reason', () => {
    const e = queueEnding(row({ agent_ending: 'replaced', disposition_reason: 'replaced', replaced_by: 'u2' }))
    expect([e.kind, e.reason, queueEndingText(e)]).toEqual(['replaced', null, 'Replaced by the agent'])
    const ws = { status: 'cancelled', ended_by: 'agent', agent_ending: 'replaced', replaced_by: 'payout-v2' }
    expect(workspaceEndingText(ws)).toBe('Replaced by the agent with payout-v2')
  })

  it('a person\'s cancel never reads as the agent\'s', () => {
    const e = queueEnding(row({ disposed_by: 'person', disposed_by_email: 'op@example.com',
      agent_ending: null, disposition_reason: 'done it myself' }))
    expect(e.kind).toBe('cancelled')
    expect(e.reason).toBeNull()
  })
})
