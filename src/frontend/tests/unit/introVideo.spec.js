import { describe, it, expect } from 'vitest'
import { INTRO_SCENES, INTRO_TOTAL_MS, introFrame, introSceneStart } from '@/components/onboarding/introVideo'

describe('the intro', () => {
  it('stays short', () => {
    expect(INTRO_TOTAL_MS).toBeLessThanOrEqual(90000)
  })

  it('covers what an agent is, a team, a company, what Trinity adds, control and the next step', () => {
    expect(INTRO_SCENES.map((s) => s.id)).toEqual(['agent', 'support', 'company', 'platform', 'charge', 'yours'])
    const platform = INTRO_SCENES.find((s) => s.id === 'platform').items.map((i) => i.title)
    expect(platform).toEqual(['They talk to each other', 'Schedules', 'Permission controls', 'Full logs'])
  })

  it('makes no numeric claims, which nobody has checked', () => {
    const text = JSON.stringify(INTRO_SCENES.map(({ title, caption, items }) => ({ title, caption, items })))
    expect(text).not.toMatch(/\d/)
  })

  it('starts on the first scene with one item showing', () => {
    expect(introFrame(0)).toMatchObject({ index: 0, shown: 1, ended: false })
  })

  it('brings the items in, and has them all showing before the scene ends', () => {
    const first = INTRO_SCENES[0]
    expect(introFrame(first.ms - 1).shown).toBe(first.items.length)
  })

  it('moves to the next scene on time, and each scene can be jumped to', () => {
    expect(introFrame(INTRO_SCENES[0].ms).index).toBe(1)
    INTRO_SCENES.forEach((_, i) => expect(introFrame(introSceneStart(i)).index).toBe(i))
  })

  it('ends after the last scene, and bad input is the beginning', () => {
    expect(introFrame(INTRO_TOTAL_MS)).toMatchObject({ index: INTRO_SCENES.length - 1, ended: true })
    expect(introFrame(-5).index).toBe(0)
    expect(introFrame(NaN).index).toBe(0)
  })
})
