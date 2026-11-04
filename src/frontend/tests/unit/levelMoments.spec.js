/**
 * Level moments: the levels shown at the start of a first build, the new agent lit in
 * the sidebar, each level ticking on the Levels card, and the guide's line as a note.
 */
import { describe, expect, it } from 'vitest'
import { LEVELS, levelMessage } from '@/components/portal/builder/levels'
import { introDue, levelNote, markLevelsIntroSeen, reachedSteps } from '@/components/portal/builder/levelMoments'

const levels = (reached = []) => LEVELS.map((l) => ({ ...l, reached: reached.includes(l.id), skipped: false }))

describe('introDue', () => {
  it('is due once the first build starts, with no agent yet', () => {
    expect(introDue({ levels: levels(), stage: 'plan', intake: {} })).toBe(true)
    expect(introDue({ levels: levels(), stage: 'build', intake: {} })).toBe(true)
  })
  it('waits for the build to start', () => {
    expect(introDue({ levels: levels(), stage: 'profile', intake: {} })).toBe(false)
    expect(introDue({ levels: levels(), stage: 'audit', intake: {} })).toBe(false)
  })
  it('is not due before the levels are read, once seen, or with Level 1 already reached', () => {
    expect(introDue({ levels: [], stage: 'plan', intake: {} })).toBe(false)
    expect(introDue({ levels: levels(), stage: 'plan', intake: { levelsIntroSeen: true } })).toBe(false)
    expect(introDue({ levels: levels([1]), stage: 'build', intake: {} })).toBe(false)
  })
  it('is remembered on the saved answers', () => {
    expect(markLevelsIntroSeen({ sent: true })).toEqual({ sent: true, levelsIntroSeen: true })
  })
})

describe('reachedSteps', () => {
  it('lights the new agent in the sidebar, then the Levels card with the tick and the next level', () => {
    const steps = reachedSteps({ reached: [1], levels: levels([1]), agent: { name: 'game-filler', label: 'Game filler' } })
    expect(steps).toEqual([
      { selector: '[data-tour-agent="game-filler"]', title: 'Game filler is yours', body: 'Your agents live here: click one any time to talk to it.' },
      { target: 'levels', title: 'Level 1 done: First agent', body: 'Next, Level 2: A new playbook.', company: true },
    ])
  })
  it('lights only the Levels card for any other level', () => {
    const steps = reachedSteps({ reached: [3], levels: levels([1, 2, 3]), agent: { name: 'game-filler', label: 'Game filler' } })
    expect(steps).toEqual([{ target: 'levels', title: 'Level 3 done: Connected', body: 'Next, Level 4: In control.', company: true }])
  })
  it('names several levels reached at once, and says when none is left', () => {
    expect(reachedSteps({ reached: [4, 5], levels: levels([1, 2, 3, 4, 5]) })[0]).toMatchObject({ title: 'Levels 4 and 5 done', body: 'Next, Level 6: A small team.' })
    expect(reachedSteps({ reached: [1, 2, 3], levels: levels([1, 2, 3]) }).at(-1).title).toBe('Levels 1, 2 and 3 done')
    expect(reachedSteps({ reached: [7], levels: levels([1, 2, 3, 4, 5, 6, 7]) })[0].body).toBe("That's every level.")
  })
  it('skips the sidebar step when no new agent is known', () => {
    expect(reachedSteps({ reached: [1], levels: levels([1]) })).toHaveLength(1)
  })
  it('has nothing to show for nothing reached', () => {
    expect(reachedSteps({ reached: [], levels: levels() })).toEqual([])
  })
})

describe('levelNote', () => {
  it("turns the page's line to the guide into a short note", () => {
    expect(levelNote(levelMessage(levels([1]).filter((l) => l.id === 1), levels([1])), 'Atlas')).toBe('Level 1 reached. Atlas was told.')
    const two = levels([1, 2])
    expect(levelNote(levelMessage(two.filter((l) => l.reached), two), 'Atlas')).toBe('Levels 1 and 2 reached. Atlas was told.')
    const all = levels([1, 2, 3, 4, 5, 6, 7])
    expect(levelNote(levelMessage(all.filter((l) => l.id === 7), all), 'Atlas')).toBe('Level 7 reached. Atlas was told.')
  })
  it('reads the one-line form a Working card shows too', () => {
    expect(levelNote('Level reached: Level 1, First agent. Next: Level 2, A new playbook.', 'Atlas')).toBe('Level 1 reached. Atlas was told.')
    expect(levelNote('Level reached: Level 1, First agent. Level reached: Level 2, A new playbook. Next: Level 3, Connected.', 'Atlas')).toBe('Levels 1 and 2 reached. Atlas was told.')
  })
  it('reads two level lines the page joined into one message', () => {
    const joined = 'Level reached: Level 3, Connected.\nNext: Level 4, In control.\nLevel reached: Level 4, In control.\nNext: Level 5, On a schedule.'
    expect(levelNote(joined, 'Atlas')).toBe('Levels 3 and 4 reached. Atlas was told.')
  })
  it('leaves anything else as it is', () => {
    expect(levelNote('Level reached: yes please', 'Atlas')).toBeNull()
    expect(levelNote('I changed the setting.\nLevel reached: Level 4, In control.\nNext: Level 5, On a schedule.', 'Atlas')).toBeNull()
    expect(levelNote('', 'Atlas')).toBeNull()
    expect(levelNote(null, 'Atlas')).toBeNull()
  })
})
