import { describe, it, expect } from 'vitest'
import { starterGroups } from '@/utils/assistantStarters'

const labels = (groups) => groups.map((g) => [g.heading, g.items.map((i) => i.label)])
const FLEET = [{ name: 'scout', status: 'stopped' }, { name: 'sage', status: 'running' }]

describe('what the assistant offers on its empty screen', () => {
  it('with no agents, it is about the first one', () => {
    const groups = starterGroups({ label: 'Dashboard', agent: null }, [])
    expect(groups.map((g) => g.heading)).toEqual(['Start here', 'Find your way'])
    expect(groups[0].items[0].label).toBe('Build my first agent')
    expect(groups.flatMap((g) => g.items).some((i) => i.fill)).toBe(false)
  })

  it('with a fleet, it shows looking, doing and building, and names a running agent in what it starts', () => {
    const groups = starterGroups({ label: 'Dashboard', agent: null }, FLEET)
    expect(groups.map((g) => g.heading)).toEqual(['Check', 'Do', 'Build'])
    const ask = groups[1].items.find((i) => i.label === 'Ask an agent…')
    expect(ask).toMatchObject({ fill: true, text: 'Ask sage ' })
  })

  it('on an agent page, that agent comes first and by name', () => {
    const groups = starterGroups({ label: 'Agent page for scout', agent: 'scout' }, FLEET)
    expect(labels(groups)[0]).toEqual(['This agent', ['What does scout do?', 'How has scout been running?', 'Give scout a test run', 'Change when it runs…']])
    expect(groups.map((g) => g.heading)).toEqual(['This agent', 'Check', 'Build'])
  })

  it('an agent page for an agent it cannot see is treated as any other page', () => {
    expect(starterGroups({ label: 'Agent page for ghost', agent: 'ghost' }, FLEET).map((g) => g.heading)).toEqual(['Check', 'Do', 'Build'])
  })
})
