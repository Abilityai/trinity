import { describe, it, expect } from 'vitest'
import { MAX_HISTORY_TURNS, buildFleet, buildPrompt, describePage } from '@/utils/assistantContext'

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
    { name: 'trinity-assistant', status: 'running' },
    { name: 'trinity-system', status: 'running', is_system: true },
    { name: 'scout', status: 'running' },
    { name: 'sage', status: 'stopped' },
  ]
  const schedules = [
    { id: 's1', agent_name: 'scout', name: 'Daily', enabled: true, cron_expression: '0 9 * * *' },
    { id: 's2', agent_name: 'other', name: 'X', enabled: true },
  ]

  it('leaves out the assistant itself and the system agent, and joins schedules by agent', () => {
    const fleet = buildFleet(agents, { scout: { autonomy_enabled: true } }, schedules, 'trinity-assistant')
    expect(fleet).toEqual([
      { name: 'scout', status: 'running', autonomy: true, schedules: [{ id: 's1', name: 'Daily', enabled: true, cron: '0 9 * * *' }] },
      { name: 'sage', status: 'stopped', autonomy: null, schedules: [] },
    ])
  })
})

describe('assistant context: the prompt for one turn', () => {
  const page = { label: 'Dashboard', agent: null }

  it('says there are no agents rather than leaving the fleet blank', () => {
    const prompt = buildPrompt({ page, fleet: [], history: [], message: 'hi' })
    expect(prompt).toBe('Page: Dashboard\n\nFleet:\n(no agents yet)\n\nCurrent message:\nhi')
  })

  it('carries state, schedule ids and earlier turns, with the message last', () => {
    const fleet = [{ name: 'scout', status: 'running', autonomy: false, schedules: [{ id: 's1', name: 'Daily', enabled: false, cron: '' }] }]
    const prompt = buildPrompt({
      page, fleet, message: 'turn it on',
      history: [{ role: 'user', content: 'hello' }, { role: 'assistant', content: 'Hi.' }, { role: 'system', content: 'ignored' }],
    })
    expect(prompt).toContain('- scout: running, autonomy off, schedules: "Daily" id=s1 off')
    expect(prompt).toContain('Conversation so far:\nPerson: hello\nYou: Hi.')
    expect(prompt).not.toContain('ignored')
    expect(prompt.endsWith('Current message:\nturn it on')).toBe(true)
  })

  it('keeps only the most recent turns', () => {
    const history = Array.from({ length: MAX_HISTORY_TURNS + 5 }, (_, i) => ({ role: 'user', content: `m${i}` }))
    const prompt = buildPrompt({ page, fleet: [], history, message: 'now' })
    expect(prompt).not.toContain('Person: m4\n')
    expect(prompt).toContain('Person: m5\n')
  })
})
