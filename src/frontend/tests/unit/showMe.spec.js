/**
 * Show me: the real page where a setting lives, with that one control highlighted.
 */
import { describe, expect, it } from 'vitest'
import { SHOW_ME_TARGETS, showMeLink } from '../../src/components/onboarding/showMe'
import { SHOW_ME_KEYS } from '../../src/components/portal/builder/fleetChanges'
import { SHOW_ME_WALKS } from '../../src/components/onboarding/showMeWalk'

describe('showMeLink', () => {
  it('walks from the Dashboard for sharing, calling and schedules', () => {
    expect(showMeLink('permissions', 'writer')).toBe('/?walk=permissions&walk_agent=writer')
    expect(showMeLink('permissions', 'writer', { to: 'scout' })).toBe('/?walk=permissions&walk_agent=writer&walk_to=scout')
    expect(showMeLink('people', 'writer')).toBe('/?walk=people&walk_agent=writer')
    expect(showMeLink('schedules', 'writer', { schedule: 's1' })).toBe('/?walk=schedules&walk_agent=writer&walk_schedule=s1')
  })

  it('opens the right tab of the agent page, or Settings for the keys', () => {
    expect(showMeLink('public-links', 'writer')).toBe('/agents/writer?tab=sharing&showme=public-links')
    expect(showMeLink('skills', 'writer')).toBe('/agents/writer?tab=skills&showme=skills')
    expect(showMeLink('channels', 'writer')).toBe('/agents/writer?tab=sharing&showme=channels')
    expect(showMeLink('keys')).toBe('/settings?tab=integrations&showme=keys')
  })

  it('encodes the agent name and knows nothing else', () => {
    expect(showMeLink('skills', 'a b')).toBe('/agents/a%20b?tab=skills&showme=skills')
    expect(showMeLink('../x', 'writer')).toBe(null)
    expect(showMeLink('permissions')).toBe(null)
  })

  it('has a title and a note for every place Show me can go', () => {
    for (const key of SHOW_ME_KEYS.filter((k) => !SHOW_ME_WALKS.includes(k))) {
      expect(SHOW_ME_TARGETS[key].title).toBeTruthy()
      expect(SHOW_ME_TARGETS[key].body).toMatch(/\.$/)
    }
    expect(SHOW_ME_TARGETS.permissions.title).toBe('Who this agent can call')
    expect(SHOW_ME_KEYS.filter((k) => SHOW_ME_WALKS.includes(k))).toEqual(['permissions', 'people', 'schedules', 'credentials'])
  })
})
