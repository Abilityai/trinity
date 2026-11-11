// @vitest-environment jsdom
/**
 * Workspace Build: the first time one of a person's agents does a job another of their
 * agents asked for, the page says so and offers to show it: a walk from the Dashboard to
 * that agent's Tasks, where the job is listed. Read from the real jobs, never from the
 * guide's word, and offered once.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalBuildHandoff from '@/components/portal/builder/PortalBuildHandoff.vue'
import { handoffDue, handoffLink, latestHandoff, markHandoffSeen } from '@/components/portal/builder/handoff'
import { tasksWalk, walkFromQuery, walkLink, walkSteps } from '@/components/onboarding/showMeWalk'

const job = (over = {}) => ({ status: 'success', triggered_by: 'agent', source_agent_name: 'game-filler', started_at: '2026-11-10T20:12:18Z', ...over })
const signals = (jobs) => ({ own: ['game-filler', 'finance-agent', 'marketing-agent'], jobs })

describe('a job one of their agents did for another', () => {
  it('is the newest finished job an own agent asked an own agent for', () => {
    const found = latestHandoff(signals({
      'marketing-agent': [job(), job({ started_at: '2026-11-10T20:12:29Z' })],
      'finance-agent': [job({ started_at: '2026-11-10T19:00:00Z' })],
    }))
    expect(found).toEqual({ from: 'game-filler', to: 'marketing-agent', at: '2026-11-10T20:12:29Z' })
  })

  it('never counts the guide, a stranger, a job still running, or a job a person started', () => {
    expect(latestHandoff(signals({ 'game-filler': [job({ source_agent_name: 'atlas-1234' })] }))).toBe(null)
    expect(latestHandoff(signals({ 'someone-elses': [job()] }))).toBe(null)
    expect(latestHandoff(signals({ 'marketing-agent': [job({ status: 'running' })] }))).toBe(null)
    expect(latestHandoff(signals({ 'marketing-agent': [job({ triggered_by: 'chat', source_agent_name: null })] }))).toBe(null)
    expect(latestHandoff(signals({ 'game-filler': [job()] }))).toBe(null)
    expect(latestHandoff(null)).toBe(null)
  })

  it('is offered once: seen stays with their saved answers', () => {
    const handoff = { from: 'game-filler', to: 'marketing-agent', at: 'x' }
    expect(handoffDue({ handoff, intake: {} })).toBe(true)
    expect(handoffDue({ handoff: null, intake: {} })).toBe(false)
    const seen = markHandoffSeen({ answers: { a: 1 } })
    expect(seen).toEqual({ answers: { a: 1 }, handoffSeen: true })
    expect(handoffDue({ handoff, intake: seen })).toBe(false)
  })
})

describe('the walk to that job', () => {
  it('starts on the Dashboard and carries who asked', () => {
    expect(handoffLink({ from: 'game-filler', to: 'marketing-agent' }))
      .toBe('/?walk=tasks&walk_agent=marketing-agent&walk_from=game-filler')
    expect(walkLink('tasks', 'marketing-agent', { from: 'marketing-agent' })).toBe('/?walk=tasks&walk_agent=marketing-agent')
    expect(walkLink('tasks', 'marketing-agent', { from: 'bad name!' })).toBe('/?walk=tasks&walk_agent=marketing-agent')
    expect(walkFromQuery({ walk: 'tasks', walk_agent: 'marketing-agent', walk_from: 'game-filler' }))
      .toMatchObject({ key: 'tasks', agent: 'marketing-agent', from: 'game-filler' })
    // Only the tasks walk carries it.
    expect(walkLink('schedules', 'marketing-agent', { from: 'game-filler' })).toBe('/?walk=schedules&walk_agent=marketing-agent')
  })

  it('ends on the list, saying which rows are the jobs the other agent asked for', () => {
    const steps = tasksWalk({ agent: 'marketing-agent', label: 'Marketing Agent', from: 'game-filler', fromLabel: 'Game Filler' })
    expect(steps.map((s) => s.id)).toEqual(['agent', 'tab', 'finished'])
    const end = steps[2].body
    expect(end).toContain('tagged "agent"')
    expect(end).toContain('Game Filler asked Marketing Agent')
    // Without one, the walk is the plain look at what an agent did.
    expect(tasksWalk({ agent: 'marketing-agent', label: 'Marketing Agent' })[2].body).not.toContain('tagged')
    const walked = walkSteps({ key: 'tasks', agent: 'marketing-agent', from: 'game-filler' }, { agent: 'marketing-agent', label: 'Marketing Agent', fromLabel: 'Game Filler' })
    expect(walked[2].body).toContain('Game Filler asked Marketing Agent')
  })
})

describe('the note on the Build page', () => {
  const props = { handoff: { from: 'game-filler', to: 'marketing-agent', at: 'x' }, fromLabel: 'Game Filler', toLabel: 'Marketing Agent' }

  it('says who did a job for whom and links to the walk in a new tab', () => {
    const w = mount(PortalBuildHandoff, { props })
    expect(w.text()).toContain('Marketing Agent just did a job for Game Filler')
    const link = w.get('[data-testid="build-handoff-show-me"]')
    expect(link.attributes('href')).toBe('/?walk=tasks&walk_agent=marketing-agent&walk_from=game-filler')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
  })

  it('tells the page when Show me is opened, with the link, and when it is put away', async () => {
    const w = mount(PortalBuildHandoff, { props })
    await w.get('[data-testid="build-handoff-show-me"]').trigger('click')
    expect(w.emitted('shown')[0][1]).toBe('/?walk=tasks&walk_agent=marketing-agent&walk_from=game-filler')
    await w.get('[data-testid="build-handoff-dismiss"]').trigger('click')
    expect(w.emitted('dismiss')).toHaveLength(1)
  })
})
