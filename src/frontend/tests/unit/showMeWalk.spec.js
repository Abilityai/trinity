/**
 * Show me as a walk: for an agent's own keys, the page doesn't jump to the spot. It starts
 * on the Dashboard and lights up each thing to click, in order, so the person learns the
 * way: the agent, its Settings, Credentials, each key's box, then Save.
 */
import { describe, expect, it } from 'vitest'
import {
  SHOW_ME_WALKS, credentialsWalk, keySteps, permissionSteps, pickRows, scheduleSteps, stepDone, walkFromQuery, walkKeys,
  walkLink, walkSteps, withoutWalk,
} from '../../src/components/onboarding/showMeWalk'
import { showMeLink } from '../../src/components/onboarding/showMe'

const route = (path, query = {}) => ({ path, query })

describe('the walk link', () => {
  it('starts on the Dashboard and names the agent and the keys', () => {
    expect(walkLink('credentials', 'game-promoter', ['SUPABASE_URL', 'SUPABASE_KEY']))
      .toBe('/?walk=credentials&walk_agent=game-promoter&walk_keys=SUPABASE_URL%2CSUPABASE_KEY')
    expect(walkLink('credentials', 'game-promoter')).toBe('/?walk=credentials&walk_agent=game-promoter')
  })

  it('is what Show me gives for credentials', () => {
    expect(showMeLink('credentials', 'game-promoter', ['SUPABASE_URL'])).toBe('/?walk=credentials&walk_agent=game-promoter&walk_keys=SUPABASE_URL')
    expect(showMeLink('credentials')).toBe(null)
  })

  it('refuses anything that is not a walk or not an agent name', () => {
    expect(walkLink('public-links', 'game-promoter')).toBe(null)
    expect(walkLink('credentials', '')).toBe(null)
    expect(walkLink('credentials', '"><img src=x>')).toBe(null)
    expect(walkLink('credentials', '../settings')).toBe(null)
  })
})

describe('key names', () => {
  it('keeps only names an .env could hold, once each, at most ten', () => {
    expect(walkKeys('SUPABASE_URL, SUPABASE_KEY SUPABASE_URL')).toEqual(['SUPABASE_URL', 'SUPABASE_KEY'])
    expect(walkKeys(['A_KEY', 'not a key', '1BAD', 'x"]', 'ok_too'])).toEqual(['A_KEY', 'ok_too'])
    expect(walkKeys(Array.from({ length: 12 }, (_, i) => `K${i}`))).toHaveLength(10)
    expect(walkKeys(undefined)).toEqual([])
    expect(walkKeys(42)).toEqual([])
  })
})

describe('reading the walk back from the address', () => {
  it('gives the walk, the agent and the keys', () => {
    expect(walkFromQuery({ walk: 'credentials', walk_agent: 'game-promoter', walk_keys: 'SUPABASE_URL,SUPABASE_KEY' }))
      .toEqual({ key: 'credentials', agent: 'game-promoter', keys: ['SUPABASE_URL', 'SUPABASE_KEY'] })
    expect(walkFromQuery({ walk: 'credentials', walk_agent: 'game-promoter' }))
      .toEqual({ key: 'credentials', agent: 'game-promoter', keys: [] })
  })

  it('ignores a walk it does not know, a bad agent name, or none', () => {
    expect(walkFromQuery({ walk: 'delete-everything', walk_agent: 'game-promoter' })).toBe(null)
    expect(walkFromQuery({ walk: 'credentials', walk_agent: '<b>' })).toBe(null)
    expect(walkFromQuery({ walk: ['credentials'], walk_agent: 'game-promoter' })).toBe(null)
    expect(walkFromQuery({})).toBe(null)
    expect(walkFromQuery(undefined)).toBe(null)
  })

  it('takes itself out of the address and leaves the rest', () => {
    expect(withoutWalk({ walk: 'credentials', walk_agent: 'a', walk_keys: 'B', view: 'list' })).toEqual({ view: 'list' })
  })
})

describe('the credentials walk', () => {
  const steps = credentialsWalk({ agent: 'game-promoter', label: 'Game Promoter' })

  it('goes Dashboard card, Settings, Credentials, the keys, then a last word', () => {
    expect(steps.map((s) => s.id)).toEqual(['agent', 'settings', 'credentials', 'stopped', 'keys', 'finished'])
    expect(steps[0].target).toBe('[data-walk-agent="game-promoter"]')
    expect(steps[0].body).toBe('This is the Dashboard: every agent you have is on it. Click Game Promoter to open it.')
    expect(steps[1].target).toBe('[data-walk="settings-tab"]')
    expect(steps[2].target).toBe('[data-testid="settings-rail"] [data-section="credentials"]')
    const ready = '[data-walk="credentials-checklist"][data-walk-ready="true"]'
    expect(steps[3]).toMatchObject({ target: '[data-testid="credentials-stopped"]', optional: true, until: 'gone', waitFor: ready })
    expect(steps[4]).toMatchObject({ expand: true, waitFor: ready, title: 'One moment', body: 'Checking what Game Promoter needs.' })
    expect(steps[5].target).toBe(null)
    expect(steps[5].body).toBe("Game Promoter has its keys. Go back to your chat with Atlas and tell it you're done. It checks they're set, without ever seeing them.")
  })

  it('offers a way on when the thing to click is not on screen', () => {
    expect(steps[0].fallback).toEqual({ label: 'Open Game Promoter', to: '/agents/game-promoter' })
    expect(steps[1].fallback).toEqual({ label: 'Open its Settings', to: '/agents/game-promoter?tab=settings' })
    expect(steps[2].fallback).toEqual({ label: 'Open Credentials', to: '/agents/game-promoter?tab=settings&section=credentials' })
  })

  it('never says MCP, deploy or uses a long dash', () => {
    const words = steps.map((s) => `${s.title} ${s.body}`).join(' ')
    expect(words).not.toMatch(/MCP|deploy|—/)
  })
})

describe('when a step is already done', () => {
  const [agent, settings, credentials, stopped] = credentialsWalk({ agent: 'game-promoter', label: 'Game Promoter' })

  it('the agent step is done once its page is open', () => {
    expect(stepDone(agent, route('/'), 'game-promoter')).toBe(false)
    expect(stepDone(agent, route('/agents/game-promoter'), 'game-promoter')).toBe(true)
    expect(stepDone(agent, route('/agents/game-promoter-2'), 'game-promoter')).toBe(false)
  })

  it('Settings and Credentials are done from the address, old links included', () => {
    expect(stepDone(settings, route('/agents/game-promoter', { tab: 'overview' }), 'game-promoter')).toBe(false)
    expect(stepDone(settings, route('/agents/game-promoter', { tab: 'settings' }), 'game-promoter')).toBe(true)
    expect(stepDone(settings, route('/agents/game-promoter', { tab: 'credentials' }), 'game-promoter')).toBe(true)
    expect(stepDone(settings, route('/agents/writer', { tab: 'settings' }), 'game-promoter')).toBe(false)
    expect(stepDone(credentials, route('/agents/game-promoter', { tab: 'settings' }), 'game-promoter')).toBe(false)
    expect(stepDone(credentials, route('/agents/game-promoter', { tab: 'settings', section: 'credentials' }), 'game-promoter')).toBe(true)
    expect(stepDone(credentials, route('/agents/game-promoter', { tab: 'credentials' }), 'game-promoter')).toBe(true)
  })

  it('the later steps are never done from the address alone', () => {
    expect(stepDone(stopped, route('/agents/game-promoter', { tab: 'settings', section: 'credentials' }), 'game-promoter')).toBe(false)
  })
})

describe('which keys to light up', () => {
  const rows = [
    { name: 'SUPABASE_URL', status: 'missing', required: 'true' },
    { name: 'SUPABASE_KEY', status: 'missing', required: 'true' },
    { name: 'WHATSAPP_TOKEN', status: 'missing', required: 'false' },
    { name: 'DONE_ALREADY', status: 'set', required: 'true' },
  ]

  it('the ones Atlas named, if it named any and they are not set', () => {
    expect(pickRows(rows, ['WHATSAPP_TOKEN', 'DONE_ALREADY'])).toEqual({ mode: 'rows', names: ['WHATSAPP_TOKEN'] })
  })

  it('otherwise every required key that is missing, then any missing one', () => {
    expect(pickRows(rows, [])).toEqual({ mode: 'rows', names: ['SUPABASE_URL', 'SUPABASE_KEY'] })
    expect(pickRows(rows.slice(2), [])).toEqual({ mode: 'rows', names: ['WHATSAPP_TOKEN'] })
  })

  it('says so when everything asked for is already set', () => {
    expect(pickRows(rows, ['DONE_ALREADY'])).toEqual({ mode: 'all-set', names: [] })
    expect(pickRows([rows[3]], [])).toEqual({ mode: 'all-set', names: [] })
  })

  it('falls back to pasting when the agent lists none of them', () => {
    expect(pickRows([], ['SUPABASE_URL'])).toEqual({ mode: 'paste', names: ['SUPABASE_URL'] })
    expect(pickRows(rows, ['NOT_DECLARED'])).toEqual({ mode: 'paste', names: ['NOT_DECLARED'] })
    expect(pickRows([], [])).toEqual({ mode: 'paste', names: [] })
  })
})

describe('the key steps', () => {
  it('lights each box in turn, then Save, which waits until the page says it saved', () => {
    const steps = keySteps({ mode: 'rows', names: ['SUPABASE_URL', 'SUPABASE_KEY'] }, { agent: 'game-promoter', label: 'Game Promoter' })
    expect(steps.map((s) => s.id)).toEqual(['key-SUPABASE_URL', 'key-SUPABASE_KEY', 'save'])
    expect(steps[0]).toMatchObject({ target: '[data-walk-credential="SUPABASE_URL"]', title: 'SUPABASE_URL', next: true })
    expect(steps[0].body).toBe('Paste its value in this box. The lines above it say what it is and where to get it. Never paste it in a chat.')
    expect(steps[1].body).toBe('Then this one, the same way.')
    expect(steps[2]).toMatchObject({ target: '[data-walk="credentials-save"]', until: '[data-walk="credentials-saved"]' })
    expect(steps[2].body).toBe('Click Save credentials. They go into Game Promoter only, and it uses them from its next job.')
    expect(steps[2].fallback).toEqual({ label: 'Open Credentials', to: '/agents/game-promoter?tab=settings&section=credentials' })
  })

  it('uses the paste box when there are no boxes to fill', () => {
    const steps = keySteps({ mode: 'paste', names: ['SUPABASE_URL', 'SUPABASE_KEY'] }, { agent: 'game-promoter', label: 'Game Promoter' })
    expect(steps.map((s) => s.id)).toEqual(['paste', 'save'])
    expect(steps[0]).toMatchObject({ target: '[data-walk="quick-inject"]', next: true })
    expect(steps[0].body).toBe('Put each one on its own line, as NAME=value: SUPABASE_URL, SUPABASE_KEY. Never paste them in a chat.')
    expect(steps[1]).toMatchObject({ target: '[data-walk="quick-inject-save"]', until: '[data-walk="credentials-saved"]' })
  })

  it('says when there is nothing left to add', () => {
    const steps = keySteps({ mode: 'all-set', names: [] }, { agent: 'game-promoter', label: 'Game Promoter' })
    expect(steps).toHaveLength(1)
    expect(steps[0]).toMatchObject({ id: 'all-set', target: '[data-walk="credentials-checklist"]', next: true })
    expect(steps[0].body).toBe('Everything Game Promoter needs here is already set.')
  })
})

// Show me for Levels 4 to 7 walks too: sharing an agent with someone,
// letting one agent call another, and turning a schedule on.
describe('the walks for sharing, calling and schedules', () => {
  it('are walks, like credentials', () => {
    expect(SHOW_ME_WALKS).toEqual(['credentials', 'people', 'permissions', 'schedules'])
    expect(walkLink('people', 'scout')).toBe('/?walk=people&walk_agent=scout')
  })

  it('carry the agent to call and the schedule, and nothing that is not a name or an id', () => {
    expect(walkLink('permissions', 'scout', { to: 'writer' })).toBe('/?walk=permissions&walk_agent=scout&walk_to=writer')
    expect(walkLink('schedules', 'scout', { schedule: 'Ab_9-x' })).toBe('/?walk=schedules&walk_agent=scout&walk_schedule=Ab_9-x')
    expect(walkLink('permissions', 'scout', { to: '"><img src=x>' })).toBe('/?walk=permissions&walk_agent=scout')
    expect(walkLink('permissions', 'scout', { to: 'scout' })).toBe('/?walk=permissions&walk_agent=scout')
    expect(walkLink('schedules', 'scout', { schedule: 'a b"]' })).toBe('/?walk=schedules&walk_agent=scout')
    expect(walkLink('schedules', 'scout', { schedule: 'x'.repeat(65) })).toBe('/?walk=schedules&walk_agent=scout')
  })

  it('carry key names only on the credentials walk', () => {
    expect(walkLink('people', 'scout', ['SUPABASE_URL'])).toBe('/?walk=people&walk_agent=scout')
    expect(walkLink('credentials', 'scout', { keys: 'A_KEY' })).toBe('/?walk=credentials&walk_agent=scout&walk_keys=A_KEY')
  })

  it('read back what the link carried, and only that', () => {
    expect(walkFromQuery({ walk: 'permissions', walk_agent: 'scout', walk_to: 'writer' }))
      .toEqual({ key: 'permissions', agent: 'scout', keys: [], to: 'writer' })
    expect(walkFromQuery({ walk: 'schedules', walk_agent: 'scout', walk_schedule: 'abc' }))
      .toEqual({ key: 'schedules', agent: 'scout', keys: [], schedule: 'abc' })
    expect(walkFromQuery({ walk: 'permissions', walk_agent: 'scout', walk_to: '<b>', walk_schedule: ['x'] }))
      .toEqual({ key: 'permissions', agent: 'scout', keys: [] })
    expect(withoutWalk({ walk: 'x', walk_agent: 'a', walk_keys: 'K', walk_to: 'b', walk_schedule: 'c', tab: 'chat' })).toEqual({ tab: 'chat' })
  })

  it('give the credentials walk exactly as before', () => {
    const who = { agent: 'game-promoter', label: 'Game Promoter' }
    expect(walkSteps({ key: 'credentials', agent: 'game-promoter', keys: [] }, who)).toEqual(credentialsWalk(who))
  })

  it('never say MCP, deploy or use a long dash', () => {
    const who = { agent: 'scout', label: 'Scout', toLabel: 'Writer' }
    const all = [
      ...walkSteps({ key: 'people', agent: 'scout' }, who),
      ...walkSteps({ key: 'permissions', agent: 'scout', to: 'writer' }, who),
      ...walkSteps({ key: 'schedules', agent: 'scout' }, who),
      ...permissionSteps([{ name: 'writer', checked: 'false' }], 'writer', who),
      ...permissionSteps([{ name: 'writer', checked: 'true' }], 'writer', who),
      ...permissionSteps([], '', who),
      ...scheduleSteps([], '', who),
      ...scheduleSteps([{ id: 's1', enabled: 'true', name: 'Daily digest' }], 's1', who),
      ...scheduleSteps([{ id: 's1', enabled: 'false', name: 'Daily digest' }, { id: 's2', enabled: 'false', name: 'Weekly' }], '', who),
    ]
    expect(all.map((s) => `${s.title} ${s.body}`).join(' ')).not.toMatch(/MCP|deploy|—/)
  })
})

describe('the sharing walk', () => {
  const steps = walkSteps({ key: 'people', agent: 'scout' }, { agent: 'scout', label: 'Scout' })

  it('goes Dashboard card, Settings, People, the email box, then a last word', () => {
    expect(steps.map((s) => s.id)).toEqual(['agent', 'settings', 'people', 'add', 'finished'])
    expect(steps[0].target).toBe('[data-walk-agent="scout"]')
    expect(steps[1]).toMatchObject({ target: '[data-walk="settings-tab"]', at: { tab: 'settings' } })
    expect(steps[1].body).toBe('Who can use Scout is set under Settings, at the end of these tabs. Click it.')
    expect(steps[2]).toMatchObject({ target: '[data-testid="settings-rail"] [data-section="people"]', at: { tab: 'settings', section: 'people' } })
    expect(steps[2].fallback).toEqual({ label: 'Open People', to: '/agents/scout?tab=settings&section=people' })
    expect(steps[3]).toMatchObject({ target: '[data-walk="people-form"]', until: '[data-walk="people-added"]' })
    expect(steps[3].body).toBe('Type the email of the person who should use Scout, then click Add person.')
    expect(steps[4]).toMatchObject({ target: null, end: true })
    expect(steps[4].body).toBe("Scout is shared with them. They find it in their Workspace when they sign in with that email. Go back to your chat with Atlas and tell it you're done.")
  })

  it('is done from the address up to People, and never later', () => {
    expect(stepDone(steps[1], route('/agents/scout', { tab: 'settings' }), 'scout')).toBe(true)
    expect(stepDone(steps[2], route('/agents/scout', { tab: 'settings', section: 'credentials' }), 'scout')).toBe(false)
    expect(stepDone(steps[2], route('/agents/scout', { tab: 'settings', section: 'people' }), 'scout')).toBe(true)
    expect(stepDone(steps[2], route('/agents/writer', { tab: 'settings', section: 'people' }), 'scout')).toBe(false)
    expect(stepDone(steps[3], route('/agents/scout', { tab: 'settings', section: 'people' }), 'scout')).toBe(false)
  })
})

describe('the calling walk', () => {
  const who = { agent: 'scout', label: 'Scout', toLabel: 'Writer' }
  const steps = walkSteps({ key: 'permissions', agent: 'scout', to: 'writer' }, who)

  it('goes Dashboard card, Permissions (under More if it is there), the agents, then a last word', () => {
    expect(steps.map((s) => s.id)).toEqual(['agent', 'tab', 'rows', 'finished'])
    expect(steps[1]).toMatchObject({ target: '[data-testid="agent-tabs"] [data-tab="permissions"]', at: { tab: 'permissions' } })
    expect(steps[1].body).toBe('Which agents Scout may call is set under Permissions. Click it.')
    expect(steps[1].alt).toEqual({ target: '[data-testid="agent-tabs"] [data-overflow-trigger]', body: "It's under More. Click More, then Permissions." })
    expect(steps[1].fallback).toEqual({ label: 'Open Permissions', to: '/agents/scout?tab=permissions' })
    expect(steps[2]).toMatchObject({ expand: 'permissions', waitFor: '[data-walk="permissions-list"], [data-walk="permissions-none"]' })
    expect(steps[2].fallback).toEqual({ label: 'Open Permissions', to: '/agents/scout?tab=permissions' })
    expect(steps[3].body).toBe("Scout may now call Writer. Go back to your chat with Atlas and tell it you're done.")
    expect(walkSteps({ key: 'permissions', agent: 'scout' }, { agent: 'scout', label: 'Scout' })[3].body)
      .toBe("Scout may now call the agents you ticked. Go back to your chat with Atlas and tell it you're done.")
  })

  it('is done from the address once Permissions is open', () => {
    expect(stepDone(steps[1], route('/agents/scout', { tab: 'overview' }), 'scout')).toBe(false)
    expect(stepDone(steps[1], route('/agents/scout', { tab: 'permissions' }), 'scout')).toBe(true)
  })

  it('lights the agent to tick, then Save, which waits until the page says it saved', () => {
    const rows = [{ name: 'writer', checked: 'false' }, { name: 'sage', checked: 'true' }]
    const tick = permissionSteps(rows, 'writer', who)
    expect(tick.map((s) => s.id)).toEqual(['tick', 'save'])
    expect(tick[0]).toMatchObject({
      target: '[data-walk-permission="writer"]', until: '[data-walk-permission="writer"][data-walk-checked="true"]', title: 'Tick Writer',
    })
    expect(tick[0].body).toBe('Tick Writer, so Scout may call it.')
    expect(tick[1]).toMatchObject({ target: '[data-walk="permissions-save"]', until: '[data-walk="permissions-saved"]' })
    expect(tick[1].idle).toEqual({
      when: '[data-walk="permissions-save"][data-walk-idle="true"]',
      title: 'Nothing to save',
      body: "Nothing has changed here yet. Tick an agent first, or press Done if it's already how you want it.",
    })
    expect(tick[1].body).toBe('Click Save Permissions. Scout can call it from its next job.')
  })

  it('says so when it may already call that agent, instead of waiting for a Save that cannot happen', () => {
    const steps = permissionSteps([{ name: 'writer', checked: 'true' }], 'writer', who)
    expect(steps).toHaveLength(1)
    expect(steps[0]).toMatchObject({ id: 'already', target: '[data-walk-permission="writer"]', next: true, title: 'Already allowed' })
    expect(steps[0].body).toBe('Scout may already call Writer.')
  })

  it('lights the whole list when no agent was named, or the one named is not in it', () => {
    for (const to of ['', 'nobody']) {
      const steps = permissionSteps([{ name: 'writer', checked: 'false' }], to, { agent: 'scout', label: 'Scout' })
      expect(steps.map((s) => s.id)).toEqual(['pick', 'save'])
      expect(steps[0]).toMatchObject({ target: '[data-walk="permissions-list"]', next: true })
      expect(steps[0].body).toBe('Tick each agent Scout may call.')
    }
  })

  it('ends there when there is no other agent to call', () => {
    const steps = permissionSteps([], 'writer', who)
    expect(steps).toHaveLength(1)
    expect(steps[0]).toMatchObject({ id: 'none', target: '[data-walk="permissions-none"]', end: true })
    expect(steps[0].body).toBe("Scout can only call your other agents, and you don't have another one yet. Ask Atlas to build the next one with you.")
  })
})

describe('the schedules walk', () => {
  const who = { agent: 'scout', label: 'Scout' }
  const steps = walkSteps({ key: 'schedules', agent: 'scout', schedule: 's1' }, who)
  const digest = { id: 's1', enabled: 'false', name: 'Daily digest' }

  it('goes Dashboard card, Schedules (under More if it is there), the switch, Autonomy, then a last word', () => {
    expect(steps.map((s) => s.id)).toEqual(['agent', 'tab', 'rows'])
    const after = scheduleSteps([digest], 's1', who)
    expect(after.map((s) => s.id)).toEqual(['switch', 'autonomy', 'finished'])
    expect(steps[1]).toMatchObject({ target: '[data-testid="agent-tabs"] [data-tab="schedules"]', at: { tab: 'schedules' } })
    expect(steps[1].body).toBe('When Scout works on its own is set under Schedules. Click it.')
    expect(steps[1].alt.body).toBe("It's under More. Click More, then Schedules.")
    expect(steps[2]).toMatchObject({ expand: 'schedules', waitFor: '[data-tour="showme-schedules"]' })
    expect(steps[2].fallback).toEqual({ label: 'Open Schedules', to: '/agents/scout?tab=schedules' })
    expect(after[1]).toMatchObject({
      target: '[data-walk="autonomy-enable"]', optional: true, until: 'gone', waitFor: '[data-walk-autonomy]', title: 'Turn on Autonomy',
    })
    expect(after[1].body).toBe("Autonomy is Scout's switch for working on its own. While it's off, none of its schedules run. Click Enable autonomy.")
    expect(after[2]).toMatchObject({ end: true })
    expect(after[2].body).toBe("Scout now runs Daily digest on its own, at the next time shown on its row. Go back to your chat with Atlas and tell it you're done.")
  })

  it('lights the switch of the schedule named, and waits until its row says it is on', () => {
    const steps = scheduleSteps([digest, { id: 's2', enabled: 'false', name: 'Weekly' }], 's1', who)
    expect(steps).toHaveLength(3)
    expect(steps[0]).toMatchObject({
      id: 'switch',
      target: '[data-walk-schedule="s1"] [data-walk="schedule-toggle"]',
      until: '[data-walk-schedule="s1"][data-walk-enabled="true"]',
      title: 'Turn on Daily digest',
    })
    expect(steps[0].body).toBe('Click its switch, on the right of its row. The row then says Active.')
  })

  it('uses the only schedule when none was named', () => {
    expect(scheduleSteps([digest], '', who)[0].target).toBe('[data-walk-schedule="s1"] [data-walk="schedule-toggle"]')
  })

  it('says so when that schedule is already on, and still checks Autonomy', () => {
    const steps = scheduleSteps([{ ...digest, enabled: 'true' }], 's1', who)
    expect(steps.map((s) => s.id)).toEqual(['already', 'autonomy', 'finished'])
    expect(steps[0]).toMatchObject({ id: 'already', target: '[data-walk-schedule="s1"]', next: true, title: 'Already on' })
    expect(steps[0].body).toBe('Daily digest is already on.')
  })

  it('lights the list when there are several and none was named, or the one named is not there', () => {
    const rows = [digest, { id: 's2', enabled: 'false', name: 'Weekly' }]
    for (const id of ['', 'gone']) {
      const steps = scheduleSteps(rows, id, who)
      expect(steps.map((s) => s.id)).toEqual(['pick', 'autonomy', 'finished'])
      expect(steps[0]).toMatchObject({ id: 'pick', target: '[data-tour="showme-schedules"]', next: true })
      expect(steps[0].body).toBe('Each schedule has its own switch, on the right of its row. Click the switch of the one to turn on, then Next.')
      // Nothing says which one they turned on, so the last word claims nothing it can't know.
      expect(steps[2].body).toBe("Any of Scout's schedules that are on run at the time shown on their row, while Autonomy is on. Go back to your chat with Atlas and tell it you're done.")
    }
  })

  it('never swaps a named schedule that is gone for the only one left', () => {
    const steps = scheduleSteps([{ id: 's2', enabled: 'false', name: 'Weekly' }], 's1', who)
    expect(steps[0].id).toBe('pick')
  })

  it('ends there when the agent has no schedule yet', () => {
    const steps = scheduleSteps([], 's1', who)
    expect(steps.map((s) => s.id)).toEqual(['none'])
    expect(steps).toHaveLength(1)
    expect(steps[0]).toMatchObject({ id: 'none', target: '[data-tour="showme-schedules"]', end: true })
    expect(steps[0].body).toBe('Scout has no schedule yet. Ask Atlas to plan one with you.')
  })
})
