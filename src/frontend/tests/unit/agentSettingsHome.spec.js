/**
 * The agent Settings home — the rail's gate, the deep-link resolver and the
 * restart registry (trinity-enterprise#887, #888, #890, #891; trinity#966).
 *
 * All three are pure modules imported and executed here, never matched out of
 * an SFC's text (#2918).
 */
import { describe, expect, it, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { buildTabs, TAB_ALIASES } from '../../src/utils/agentTabs'
import {
  SETTINGS_SECTIONS, LEGACY_TAB_SECTIONS, settingsSections, resolveSettingsSection, filterSections,
} from '../../src/utils/agentSettings'
import { useAgentRestartStore } from '../../src/stores/agentRestart'

const OWNER = {
  isSystem: false, hasDashboardFlag: true, brainOrbVisible: true,
  canShare: true, a2aVisible: true, gitSync: true,
}
const ids = (flags) => buildTabs(flags).map((t) => t.id)

describe('the strip after the move (ent#887/#888/#891)', () => {
  it('has no Info, Credentials, Folders or Access tab any more', () => {
    for (const gone of ['info', 'credentials', 'folders', 'access']) {
      expect(ids(OWNER)).not.toContain(gone)
    }
  })

  it('puts Settings last on the strip', () => {
    const shown = ids(OWNER)
    expect(shown[shown.length - 1]).toBe('settings')
  })

  it('keeps Settings owner-only', () => {
    expect(ids({ ...OWNER, canShare: false })).not.toContain('settings')
  })

  it('gives the system agent a Settings tab (General and Guardrails live there)', () => {
    expect(ids({ ...OWNER, isSystem: true })).toContain('settings')
  })
})

describe('old ?tab= links keep landing (nothing bookmarked breaks)', () => {
  const cases = { info: 'general', guardrails: 'guardrails', credentials: 'storage', folders: 'storage', access: 'people' }
  for (const [tab, section] of Object.entries(cases)) {
    it(`?tab=${tab} opens Settings › ${section}`, () => {
      expect(TAB_ALIASES[tab]).toBe('settings')
      expect(resolveSettingsSection({ tab })).toBe(section)
    })
  }

  it('every legacy section names a pane that exists', () => {
    const paneIds = SETTINGS_SECTIONS.map((s) => s.id)
    for (const section of Object.values(LEGACY_TAB_SECTIONS)) expect(paneIds).toContain(section)
  })

  it('an explicit section wins over the legacy tab', () => {
    expect(resolveSettingsSection({ tab: 'settings', section: 'storage' })).toBe('storage')
  })

  it('a bare ?tab=settings opens General', () => {
    expect(resolveSettingsSection({ tab: 'settings' })).toBe('general')
  })

  it('a section this viewer cannot see falls back to the first pane, never a blank one', () => {
    const system = settingsSections({ isSystem: true })
    expect(resolveSettingsSection({ section: 'people' }, system)).toBe('general')
    expect(resolveSettingsSection({ section: 'nonsense' })).toBe('general')
  })
})

describe('which panes a viewer gets', () => {
  it('an owner gets every landed pane, General first', () => {
    const shown = settingsSections().map((s) => s.id)
    expect(shown[0]).toBe('general')
    expect(shown).toEqual(['general', 'guardrails', 'people', 'channels', 'integrations', 'storage'])
  })

  it('the system agent gets General, Guardrails and Integrations only', () => {
    expect(settingsSections({ isSystem: true }).map((s) => s.id)).toEqual(['general', 'guardrails', 'integrations'])
  })

  it('every pane answers one question', () => {
    for (const s of SETTINGS_SECTIONS) expect(s.question.length).toBeGreaterThan(0)
  })
})

describe('"Find a setting" filters the rail', () => {
  const ids = (q) => filterSections(SETTINGS_SECTIONS, q).map((s) => s.id)

  it('finds a control by a word that is not in the pane name', () => {
    expect(ids('timeout')).toEqual(['general'])
    expect(ids('credentials')).toEqual(['storage'])
  })

  it('is case-insensitive and ignores surrounding space', () => {
    expect(ids('  GUARD ')).toEqual(['guardrails'])
  })

  it('keeps every pane for an empty query', () => {
    expect(ids('')).toHaveLength(SETTINGS_SECTIONS.length)
  })

  it('returns nothing for a word no pane holds', () => {
    expect(ids('zzz-no-such-setting')).toEqual([])
  })
})

describe('the restart registry behind the one banner', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('lists each waiting change once, in the order it was reported', () => {
    const s = useAgentRestartStore()
    s.report('atlas', 'file-sharing', 'File sharing', true)
    s.report('atlas', 'folders', 'Shared folders', true)
    s.report('atlas', 'file-sharing', 'File sharing', true)
    expect(s.pendingFor('atlas').map((p) => p.label)).toEqual(['File sharing', 'Shared folders'])
  })

  it('retracts a row that is back in step', () => {
    const s = useAgentRestartStore()
    s.report('atlas', 'folders', 'Shared folders', true)
    s.report('atlas', 'folders', 'Shared folders', false)
    expect(s.pendingFor('atlas')).toEqual([])
  })

  it('keeps agents apart', () => {
    const s = useAgentRestartStore()
    s.report('atlas', 'resources', 'Memory and CPU', true)
    expect(s.pendingFor('ivy')).toEqual([])
  })

  it('a restart clears the list and moves the epoch so rows re-read', () => {
    const s = useAgentRestartStore()
    s.report('atlas', 'resources', 'Memory and CPU', true)
    const before = s.epochFor('atlas')
    s.restarted('atlas')
    expect(s.pendingFor('atlas')).toEqual([])
    expect(s.epochFor('atlas')).toBe(before + 1)
  })
})
