import { describe, expect, it } from 'vitest'
import { featureTourDue, featureTourSteps, markFeatureTourSeen } from '../../src/components/onboarding/featureTour'

describe('the feature tour on the Dashboard', () => {
  it('walks round the special features, each pointing at something on the screen', () => {
    expect(featureTourSteps().map((s) => s.target)).toEqual(['view-modes', 'nav-operations', 'nav-library', 'nav-workspace', 'nav-settings'])
  })

  it('is due once for experienced people who finished setup', () => {
    const done = { done: true, answers: { agents: 'built' } }
    expect(featureTourDue(done)).toBe(true)
    expect(featureTourDue(markFeatureTourSeen(done))).toBe(false)
    expect(featureTourDue({ ...done, answers: { agents: 'tried' } })).toBe(false)
    expect(featureTourDue({ ...done, done: false })).toBe(false)
    expect(featureTourDue(null)).toBe(false)
  })

  it('says nothing about editions or paid features', () => {
    expect(JSON.stringify(featureTourSteps())).not.toMatch(/paid|enterprise|upgrade|—/i)
  })
})
