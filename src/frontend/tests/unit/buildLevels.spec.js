/**
 * The seven levels, worked out from what the page read on the instance.
 * Only the person's own agents count, and nothing their guide did by itself ticks a level.
 * Level 1 is having an agent; Level 2 a playbook added after the agent was built; Level 3
 * (keys) is skipped when none of their agents needs any.
 */
import { describe, expect, it } from 'vitest'
import {
  LEVELS,
  levelAgents,
  levelLine,
  levelMessage,
  levelsFrom,
  markLevelsSeen,
  newlyReached,
  nextLevel,
  intakeWithBase,
  levelsOf,
  playbookBase,
  withLevels,
} from '@/components/portal/builder/levels'

const reached = (signals, kept, base) => levelsFrom(signals, kept, base).filter((l) => l.reached).map((l) => l.id)
const job = (over = {}) => ({ status: 'success', triggered_by: 'chat', source_agent_name: null, started_at: '2026-11-03T10:00:00Z', completed_at: '2026-11-03T10:05:00Z', ...over })
// Level 1 is reached by any agent of theirs, so the other rules are read past it.
const beyond1 = (signals, kept, base) => reached(signals, kept, base).filter((id) => id !== 1)

describe('levels', () => {
  it('names the seven levels in order', () => {
    expect(LEVELS.map((l) => [l.id, l.name])).toEqual([
      [1, 'First agent'],
      [2, 'A new playbook'],
      [3, 'Connected'],
      [4, 'In control'],
      [5, 'On a schedule'],
      [6, 'A small team'],
      [7, 'Running on its own'],
    ])
    for (const level of LEVELS) {
      expect(level.means).toBeTruthy()
      expect(level.why).toBeTruthy()
      expect(level.prompt).toMatch(new RegExp(`^Help me reach Level ${level.id}: `))
      expect(`${level.means} ${level.why} ${level.prompt}`).not.toMatch(/—|MCP/)
    }
    expect(LEVELS[0].means).toBe('You have an agent of your own on Trinity.')
    expect(LEVELS[1].prompt).toBe('Help me reach Level 2: add a new playbook to one of my agents.')
    expect(LEVELS[2].prompt).toBe('Help me reach Level 3: show me how to add the keys my agent needs.')
  })

  it('counts only the person\'s own agents: no guide, no system agent, no samples, at most 20', () => {
    const roster = [
      { name: 'email-triage', owned: true },
      { name: 'colleague-agent', owned: false },
      { name: 'atlas-1a2b3c4d', owned: true },
      { name: 'build-guide', owned: true },
      { name: 'agent-builder', owned: true },
      { name: 'trinity-system', owned: true },
      { name: 'odd-system', owned: true, is_system: true },
      { name: 'cornelius', owned: true },
      { name: 'invoice-chaser', owned: true },
    ]
    expect(levelAgents(roster, ['cornelius'])).toEqual(['email-triage', 'invoice-chaser'])
    const many = Array.from({ length: 25 }, (_, i) => ({ name: `agent-${i}`, owned: true }))
    expect(levelAgents(many)).toHaveLength(20)
    expect(levelAgents(null)).toEqual([])
  })

  it('with no agent yet, no level is reached or skipped, and the next is Level 1', () => {
    const levels = levelsFrom({})
    expect(levels.every((l) => !l.reached && !l.skipped)).toBe(true)
    expect(nextLevel(levels).id).toBe(1)
  })

  it('Level 1 is having an agent of their own, whether or not it has run yet', () => {
    expect(reached({ own: ['game-promoter'] })).toEqual([1])
    expect(levelsFrom({ own: ['game-promoter', 'b'] })[0].evidence).toEqual(['game-promoter'])
    expect(reached({ own: [] })).toEqual([])
    // Deleting their only agent unticks it: it is read as it is now.
    expect(reached({ own: [] }, { 1: ['game-promoter'] })).toEqual([])
  })

  it('Level 2 is a playbook an agent of theirs has now that it did not have when the page first saw it', () => {
    const base = { a: ['daily-post', 'onboarding'] }
    expect(beyond1({ own: ['a'], playbooks: { a: ['daily-post', 'onboarding'] } }, {}, base)).toEqual([])
    expect(beyond1({ own: ['a'], playbooks: { a: ['daily-post', 'onboarding', 'weekly-report'] } }, {}, base)).toEqual([2])
    expect(levelsFrom({ own: ['a'], playbooks: { a: ['daily-post', 'weekly-report'] } }, {}, base)[1].evidence).toEqual(['a'])
    // Without a first look to compare with, or without knowing its playbooks now, nothing is new.
    expect(beyond1({ own: ['a'], playbooks: { a: ['weekly-report'] } }, {}, {})).toEqual([])
    expect(beyond1({ own: ['a'], playbooks: { a: null } }, {}, base)).toEqual([])
    // Only their own agents.
    expect(beyond1({ own: ['a'], playbooks: { b: ['new-one'] } }, {}, { b: [] })).toEqual([])
  })

  it('the first look at each agent is remembered once, and never replaced', () => {
    const signals = { own: ['a', 'b', 'c'], playbooks: { a: ['x', 'y'], b: null, c: [] } }
    expect(playbookBase({}, signals)).toEqual({ a: ['x', 'y'], c: [] })
    expect(playbookBase({ a: ['x'] }, signals)).toEqual({ a: ['x'], c: [] })
    // Nothing new to remember: no change at all.
    expect(playbookBase({ a: ['x'], c: [] }, signals)).toBe(null)
    expect(playbookBase(undefined, { own: [], playbooks: {} })).toBe(null)
    // Bounded: names are strings, at most 100 per agent.
    const long = Array.from({ length: 120 }, (_, i) => `p${i}`)
    expect(playbookBase({}, { own: ['a'], playbooks: { a: [...long, 7, null] } }).a).toHaveLength(100)

    // At the 20-agent read cap, at most 40 agents remembered: the oldest that are no longer theirs go first.
    const own = Array.from({ length: 20 }, (_, i) => `mine-${i}`)
    const old = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`gone-${i}`, []]))
    const kept = playbookBase(old, { own, playbooks: Object.fromEntries(own.map((n) => [n, ['x']])) })
    expect(Object.keys(kept)).toHaveLength(40)
    expect(kept['gone-0']).toBeUndefined()
    expect(kept['gone-10']).toEqual([])
    expect(own.every((n) => kept[n][0] === 'x')).toBe(true)
  })

  // Final review (4 Nov): a deleted agent built again under the same name gets a new first look.
  it('forgets an agent that is no longer theirs, unless the list may just be cut short', () => {
    expect(playbookBase({ a: ['x'], gone: ['y'] }, { own: ['a'], playbooks: { a: ['x'] } })).toEqual({ a: ['x'] })
    const twenty = Array.from({ length: 20 }, (_, i) => `a${i}`)
    const base = { gone: ['y'], ...Object.fromEntries(twenty.map((n) => [n, []])) }
    expect(playbookBase(base, { own: twenty, playbooks: {} })).toBe(null)
    // No agents read at all (a roster that failed): nothing is dropped.
    expect(playbookBase({ gone: ['y'] }, { own: [], playbooks: {} })).toBe(null)
  })

  it('Level 3 is every key one of their agents needs being set, and is skipped when none needs any', () => {
    expect(beyond1({ own: ['a', 'b'], keys: { a: 'all-set', b: 'missing' } })).toEqual([3])
    expect(beyond1({ own: ['a'], keys: { a: 'missing' } })).toEqual([])
    const none = levelsFrom({ own: ['a', 'b'], keys: { a: 'needs-none', b: 'needs-none' } })[2]
    expect(none).toMatchObject({ id: 3, reached: false, skipped: true })
    expect(levelsFrom({ own: ['a', 'b'], keys: { a: 'needs-none', b: 'missing' } })[2]).toMatchObject({ reached: false, skipped: false })
    // Not knowing is never "not needed".
    expect(levelsFrom({ own: ['a'], keys: { a: 'unknown' } })[2].skipped).toBe(false)
    expect(levelsFrom({ own: [], keys: {} })[2].skipped).toBe(false)
  })

  it('Level 4 is a share, or a permission to call another agent that is not Atlas or itself', () => {
    expect(beyond1({ own: ['a'], shares: { a: ['sam@example.com'] } })).toEqual([4])
    expect(beyond1({ own: ['a'], permissions: { a: ['b'] } })).toEqual([4])
    expect(beyond1({ own: ['a'], permissions: { a: ['a', 'build-guide', 'atlas-1a2b3c4d'] } })).toEqual([])
  })

  it('Level 5 is a scheduled run of one of their agents that worked, not one Atlas started', () => {
    expect(beyond1({ own: ['a'], jobs: { a: [job({ triggered_by: 'schedule' })] } })).toEqual([5])
    expect(beyond1({ own: ['a'], jobs: { a: [job({ triggered_by: 'manual' })] } })).toEqual([])
    expect(beyond1({ own: ['a'], jobs: { a: [job({ triggered_by: 'schedule', status: 'failed' })] } })).toEqual([])
    expect(beyond1({ own: ['a'], jobs: { a: [job({ triggered_by: 'schedule', source_agent_name: 'atlas-1a2b3c4d' })] } })).toEqual([])
  })

  it('Level 6 is three agents of theirs, one of which really called another of theirs', () => {
    const call = { from: 'a', to: 'b', at: '2026-11-03T10:01:00Z' }
    expect(beyond1({ own: ['a', 'b', 'c'], calls: [call] })).toEqual([6])
    expect(beyond1({ own: ['a', 'b'], calls: [call] })).toEqual([])
    expect(beyond1({ own: ['a', 'c', 'd'], calls: [call] })).toEqual([])
    expect(beyond1({ own: ['a', 'b', 'c'], calls: [{ from: 'a', to: 'a', at: call.at }] })).toEqual([])
    expect(beyond1({ own: ['a', 'b', 'c'], calls: [{ from: 'build-guide', to: 'a', at: call.at }] })).toEqual([])
  })

  it('Level 7 is one job that called two or more of their agents, and an approval they answered', () => {
    const own = ['a', 'b', 'c']
    const jobs = { a: [job({ triggered_by: 'schedule', started_at: '2026-11-03T10:00:00Z', completed_at: '2026-11-03T10:05:00Z' })] }
    const calls = [{ from: 'a', to: 'b', at: '2026-11-03T10:01:00Z' }, { from: 'a', to: 'c', at: '2026-11-03T10:03:00Z' }]
    expect(reached({ own, jobs, calls, answered: 1 })).toContain(7)
    const outside = calls.map((c) => ({ ...c, at: '2026-11-03T11:00:00Z' }))
    expect(reached({ own, jobs, calls: outside, answered: 1 })).not.toContain(7)
    const sameTarget = [calls[0], { ...calls[1], to: 'b' }]
    expect(reached({ own, jobs, calls: sameTarget, answered: 1 })).not.toContain(7)
    expect(reached({ own, jobs, calls, answered: 0 })).not.toContain(7)
  })

  it('a level built on history stays reached while the agents behind it are still theirs', () => {
    const kept = { 5: ['a'], 6: ['a', 'b'], 7: ['a', 'b', 'c'] }
    expect(beyond1({ own: ['a', 'b', 'c'] }, kept)).toEqual([5, 6, 7])
    // Deleting an agent behind a level unticks it.
    expect(beyond1({ own: ['b', 'c'] }, kept)).toEqual([])
    expect(beyond1({ own: ['a', 'b'] }, kept)).toEqual([5])
    // Having an agent, a playbook, keys and a share are read as they are now: never kept.
    expect(reached({ own: [] }, { 1: ['a'], 2: ['a'], 3: ['a'], 4: ['a'] })).toEqual([])
  })

  it('says which agents made a level true, so it can be kept', () => {
    const levels = levelsFrom({ own: ['a', 'b', 'c'], calls: [{ from: 'a', to: 'c', at: '2026-11-03T10:01:00Z' }] })
    expect(levels[0].evidence).toEqual(['a'])
    expect(levels[5].evidence).toEqual(['a', 'c'])
    expect(levels[2].evidence).toEqual([])
  })

  it('knows which levels are new, never a skipped one, and remembers only history levels with their agents', () => {
    const levels = levelsFrom({ own: ['a'], jobs: { a: [job({ triggered_by: 'schedule' })] }, shares: { a: ['sam@example.com'] }, keys: { a: 'needs-none' } })
    expect(newlyReached(levels, [1])).toEqual([4, 5])
    expect(newlyReached(levels, undefined)).toEqual([1, 4, 5])
    expect(newlyReached(levels, 'odd')).toEqual([1, 4, 5])
    expect(markLevelsSeen({ levelsSeen: [1] }, [4, 5, 1], levels)).toEqual({ levelsSeen: [1, 4, 5], levelsKept: { 5: ['a'] }, levelsVersion: 7 })
    expect(markLevelsSeen({}, [1], levels)).toEqual({ levelsSeen: [1], levelsKept: {}, levelsVersion: 7 })
  })

  it('tells the guide each level reached, and which comes next', () => {
    expect(levelLine(LEVELS[0])).toBe('Level reached: Level 1, First agent.')
    const levels = levelsFrom({ own: ['a'] })
    expect(levelMessage([levels[0]], levels)).toBe('Level reached: Level 1, First agent.\nNext: Level 2, A new playbook.')
    const all = LEVELS.map((l) => ({ ...l, reached: true, skipped: false }))
    expect(levelMessage([all[6]], all)).toBe('Level reached: Level 7, Running on its own.\nThat was the last level.')
  })
})

// Final review (4 Nov): answers saved by the six-level version, and a tab that comes back stale.
describe('saved levels', () => {
  it('moves answers saved with six levels to the seven-level numbers, once', () => {
    const old = { levelsSeen: [1, 2, 3, 6], levelsKept: { 1: ['a'], 4: ['b'], 5: ['b', 'c'], 6: ['b', 'c', 'd'] } }
    expect(levelsOf(old)).toEqual({ levelsSeen: [1, 3, 4, 7], levelsKept: { 5: ['b'], 6: ['b', 'c'], 7: ['b', 'c', 'd'] }, levelsBase: {}, levelsVersion: 7 })
    const now = { levelsSeen: [1, 2], levelsKept: { 5: ['b'] }, levelsBase: { a: ['x'] }, levelsVersion: 7 }
    expect(levelsOf(now)).toEqual(now)
    expect(levelsOf(undefined)).toEqual({ levelsSeen: [], levelsKept: {}, levelsBase: {}, levelsVersion: 7 })
    expect(withLevels({ answers: { role: 'x' }, levelsSeen: [2] }, { levelsBase: { a: [] } }))
      .toEqual({ answers: { role: 'x' }, levelsSeen: [3], levelsKept: {}, levelsBase: { a: [] }, levelsVersion: 7 })
  })

  const signals = { own: ['x', 'y'], playbooks: { x: ['p1', 'p2'], y: ['q1'] } }

  it('saves a new first look on the newest saved answers, never over another tab\'s', () => {
    const current = { savedAt: 1, levelsSeen: [1], answers: {} }
    const stored = { savedAt: 5, levelsSeen: [1, 2], fleetHandled: ['p9'], levelsBase: { x: ['p1'] }, levelsVersion: 7, answers: {} }
    const out = intakeWithBase(current, stored, {}, signals)
    expect(out.base).toEqual({ x: ['p1'], y: ['q1'] })
    expect(out.intake).toEqual({ ...stored, levelsKept: {}, levelsBase: { x: ['p1'], y: ['q1'] } })
  })

  it('takes another tab\'s first look over its own, and saves nothing when nothing is new', () => {
    const stored = { savedAt: 5, levelsBase: { x: ['p1'], y: ['q1'] }, levelsVersion: 7 }
    const out = intakeWithBase({ savedAt: 1 }, stored, { x: ['p1', 'p2'] }, signals)
    expect(out).toEqual({ base: { x: ['p1'], y: ['q1'] }, intake: null })
  })

  it('saves a first look again when a save dropped it', () => {
    const current = { savedAt: 3, levelsVersion: 7 }
    const out = intakeWithBase(current, current, { x: ['p1'], y: ['q1'] }, signals)
    expect(out.intake).toEqual({ savedAt: 3, levelsSeen: [], levelsKept: {}, levelsBase: { x: ['p1'], y: ['q1'] }, levelsVersion: 7 })
  })
})
