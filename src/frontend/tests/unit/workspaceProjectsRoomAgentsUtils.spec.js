/**
 * trinity-enterprise#1131 (PR-A) — which of a linked room's agents aren't on
 * the project: the pure rule the room header's chip and list are drawn from.
 */
import { describe, it, expect } from 'vitest'
import { roomAgentRows, agentStateLabel } from '@/components/portal/projects/projectsUtils'

const agent = (agent_name, state, extra = {}) => ({ agent_name, state, can_add: false, ...extra })

describe('roomAgentRows', () => {
  it('is null when the read carries no room block (a thread, a guest, a person who cannot view)', () => {
    expect(roomAgentRows(null)).toBeNull()
    expect(roomAgentRows({ id: 'prj_1', name: 'Q4' })).toBeNull()
  })

  it('has no chip when every room agent is on the project, or there are none', () => {
    expect(roomAgentRows({ room_agents: [agent('scout', 'active')], can: {} }).chip).toBeNull()
    expect(roomAgentRows({ room_agents: [], can: {} }).chip).toBeNull()
  })

  it('counts the agents not on it before the ones waiting on their owner', () => {
    const view = roomAgentRows({
      room_agents: [agent('scout', 'active'), agent('ledger', 'off'), agent('mine', 'declined'),
        agent('atlas', 'pending')],
      can: { manage_agents: false },
    })
    expect(view.chip).toEqual({ label: '2 not on it', variant: 'warning' })
    expect(roomAgentRows({ room_agents: [agent('atlas', 'pending')], can: {} }).chip)
      .toEqual({ label: '1 waiting on owner', variant: 'neutral' })
  })

  it('offers Add only where the server said the caller can add, and names why not', () => {
    const { rows, canManage } = roomAgentRows({
      room_agents: [agent('ledger', 'off', { can_add: true }), agent('mine', 'off', { reason: 'not_on_your_list' }),
        agent('scout', 'active')],
      can: { manage_agents: true },
    })
    expect(canManage).toBe(true)
    expect(rows.map((r) => [r.name, r.action, r.note])).toEqual([
      ['ledger', 'add', null], ['mine', null, 'Not on your list'], ['scout', null, null]])
  })

  it('never offers Add to someone who does not manage agents, whatever a row says', () => {
    const { rows } = roomAgentRows({ room_agents: [agent('ledger', 'off', { can_add: true })], can: {} })
    expect(rows[0].action).toBeNull()
  })

  it('labels every state, including an agent that has no row on the project', () => {
    const { rows } = roomAgentRows({
      room_agents: ['active', 'pending', 'declined', 'off'].map((s) => agent(s, s)), can: {},
    })
    expect(rows.map((r) => r.label)).toEqual(['Can work on it', 'Waiting for owner', 'Owner declined',
      'Not on the project'])
    expect(agentStateLabel('off')).toBe('Not on the project')
  })

  it('says the room could not be read rather than showing an empty list', () => {
    const view = roomAgentRows({ room_agents: null, degraded: ['room'], can: { manage_agents: true } })
    expect(view.degraded).toBe(true)
    expect(view.rows).toEqual([])
    expect(view.chip).toEqual({ label: "Couldn't read agents", variant: 'neutral' })
  })
})
