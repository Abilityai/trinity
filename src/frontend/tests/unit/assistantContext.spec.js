import { describe, it, expect } from 'vitest'
import { buildContext, buildFleet, describePage } from '@/utils/assistantContext'

describe('assistant context: where the person is', () => {
  it('names a known page', () => {
    expect(describePage({ path: '/', params: {}, query: {} })).toEqual({ label: 'Dashboard', agent: null })
    expect(describePage({ path: '/settings/keys', params: {}, query: {} }).label).toBe('Settings')
  })

  it('names the agent in view and its tab', () => {
    expect(describePage({ path: '/agents/scout', params: { name: 'scout' }, query: { tab: 'schedules' } }))
      .toEqual({ label: 'Agent page for scout, schedules tab', agent: 'scout' })
  })

  it('falls back to the path for a page it does not know', () => {
    expect(describePage({ path: '/somewhere', params: {}, query: {} }).label).toBe('Page /somewhere')
  })
})

describe('assistant context: the fleet', () => {
  const agents = [
    { name: 'trinity-system', status: 'running', is_system: true },
    { name: 'scout', status: 'running' },
    { name: 'sage', status: 'stopped' },
  ]
  const schedules = [
    { id: 's1', agent_name: 'scout', name: 'Daily', enabled: true, cron_expression: '0 9 * * *' },
    { id: 's2', agent_name: 'other', name: 'X', enabled: true },
  ]

  it('leaves out the system agent and joins schedules by agent', () => {
    const fleet = buildFleet(agents, { scout: { autonomy_enabled: true } }, schedules)
    expect(fleet).toEqual([
      { name: 'scout', status: 'running', autonomy: true, schedules: [{ id: 's1', name: 'Daily', enabled: true, cron: '0 9 * * *' }] },
      { name: 'sage', status: 'stopped', autonomy: null, schedules: [] },
    ])
  })
})

describe('assistant context: what goes with a message', () => {
  it('sends the page and each agent by name and status, and nothing else about them', () => {
    const fleet = [{ name: 'scout', status: 'running', autonomy: true, schedules: [{ id: 's1', name: 'Daily' }] }]
    expect(buildContext({ path: '/', params: {}, query: {} }, fleet))
      .toEqual({ page: 'Dashboard', fleet: [{ name: 'scout', status: 'running' }] })
  })

  it('sends an empty fleet as empty, not missing', () => {
    expect(buildContext({ path: '/agents', params: {}, query: {} }, [])).toEqual({ page: 'Agents list', fleet: [] })
  })
})
