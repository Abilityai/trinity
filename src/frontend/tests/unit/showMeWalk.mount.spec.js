// @vitest-environment jsdom
/**
 * The walk on the real pages: it starts from the address, lights up one thing at a time,
 * lets only that thing be clicked, and moves on when the page shows it was done.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import ShowMeWalk from '@/components/onboarding/ShowMeWalk.vue'
import { useNetworkStore } from '@/stores/network'

const Blank = { template: '<div />' }

function place(attrs, rect = { left: 100, top: 100, width: 160, height: 32 }) {
  const el = document.createElement('div')
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v)
  el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height })
  el.scrollIntoView = () => {}
  document.body.appendChild(el)
  return el
}
const row = (name, status = 'missing', required = 'true') => place({ 'data-walk-credential': name, 'data-walk-status': status, 'data-walk-required': required })
const checklist = () => place({ 'data-walk': 'credentials-checklist', 'data-walk-ready': 'true' })

const wait = async (ms = 40) => { await new Promise((done) => setTimeout(done, ms)); await flushPromises() }
const card = () => document.querySelector('[data-testid="show-me-walk"]')
const title = () => document.querySelector('#show-me-walk-title')?.textContent.trim()
const click = async (testid) => { document.querySelector(`[data-testid="${testid}"]`).click(); await wait() }

let router
let wrapper
async function start(path) {
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Blank }, { path: '/agents/:name', component: Blank }] })
  await router.push(path)
  await router.isReady()
  wrapper = mount(ShowMeWalk, { attachTo: document.body, global: { plugins: [router] }, props: { tickMs: 10, giveUpMs: 150 } })
  await wait()
}

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  useNetworkStore().agents = [{ name: 'game-promoter', display_label: 'Game Promoter' }]
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('the credentials walk, on the pages', () => {
  it('starts from the address, takes itself out of it, and lights up the agent on the Dashboard', async () => {
    place({ 'data-walk-agent': 'game-promoter' })
    await start('/?walk=credentials&walk_agent=game-promoter&walk_keys=SUPABASE_URL&view=list')
    expect(title()).toBe('Your agents live here')
    expect(card().textContent).toContain('Click Game Promoter to open it.')
    expect(router.currentRoute.value.query).toEqual({ view: 'list' })
    expect(document.querySelector('[data-testid="show-me-walk-spot"]')).not.toBe(null)
    // Everything but the lit-up part is covered, so only it can be clicked.
    expect(document.querySelectorAll('[data-testid="show-me-walk-block"]')).toHaveLength(4)
  })

  it('moves on as the agent, its Settings and Credentials open, then lights the key Atlas named', async () => {
    place({ 'data-walk-agent': 'game-promoter' })
    await start('/?walk=credentials&walk_agent=game-promoter&walk_keys=SUPABASE_URL')
    document.body.querySelector('[data-walk-agent]').remove()
    place({ 'data-walk': 'settings-tab' })
    await router.push('/agents/game-promoter')
    await wait()
    expect(title()).toBe('Its settings')
    const rail = place({ 'data-testid': 'settings-rail' })
    const item = document.createElement('button')
    item.setAttribute('data-section', 'credentials')
    item.getBoundingClientRect = () => ({ left: 20, top: 200, width: 200, height: 40, right: 220, bottom: 240 })
    item.scrollIntoView = () => {}
    rail.appendChild(item)
    await router.push('/agents/game-promoter?tab=settings')
    await wait()
    expect(title()).toBe('Credentials')
    checklist()
    row('SUPABASE_URL')
    row('SUPABASE_KEY')
    await router.push('/agents/game-promoter?tab=settings&section=credentials')
    await wait()
    expect(title()).toBe('SUPABASE_URL')
  })

  it('goes from box to box with Next, and Save waits until the page says it saved', async () => {
    checklist()
    row('SUPABASE_URL')
    row('SUPABASE_KEY')
    row('WHATSAPP_TOKEN', 'missing', 'false')
    place({ 'data-walk': 'credentials-save' })
    await start('/agents/game-promoter?tab=settings&section=credentials&walk=credentials&walk_agent=game-promoter')
    expect(title()).toBe('SUPABASE_URL')
    await click('show-me-walk-next')
    expect(title()).toBe('SUPABASE_KEY')
    await click('show-me-walk-next')
    expect(title()).toBe('Save them')
    await wait(60)
    expect(title()).toBe('Save them')
    place({ 'data-walk': 'credentials-saved' })
    await wait()
    expect(title()).toBe("That's it")
    expect(card().textContent).toContain('Go back to your chat with Atlas')
    await click('show-me-walk-done')
    expect(card()).toBe(null)
  })

  it('asks for a stopped agent to be started first, and goes on once it is', async () => {
    checklist()
    row('SUPABASE_URL')
    const note = place({ 'data-testid': 'credentials-stopped' })
    await start('/agents/game-promoter?tab=settings&section=credentials&walk=credentials&walk_agent=game-promoter')
    expect(title()).toBe('Start Game Promoter first')
    note.remove()
    await wait()
    expect(title()).toBe('SUPABASE_URL')
  })

  it("offers a way on when it can't find the thing to click", async () => {
    await start('/?walk=credentials&walk_agent=game-promoter')
    expect(title()).toBe('Your agents live here')
    await wait(200)
    expect(card().textContent).toContain("Can't see it on this page.")
    expect(document.querySelector('[data-testid="show-me-walk-spot"]')).toBe(null)
    await click('show-me-walk-fallback')
    expect(router.currentRoute.value.path).toBe('/agents/game-promoter')
  })

  it('a part that is off screen counts as not found', async () => {
    place({ 'data-walk-agent': 'game-promoter' }, { left: 3000, top: 100, width: 160, height: 32 })
    await start('/?walk=credentials&walk_agent=game-promoter')
    await wait(200)
    expect(document.querySelector('[data-testid="show-me-walk-spot"]')).toBe(null)
    expect(document.querySelector('[data-testid="show-me-walk-fallback"]').textContent.trim()).toBe('Open Game Promoter')
  })

  it('Stop ends it, and so does Escape', async () => {
    place({ 'data-walk-agent': 'game-promoter' })
    await start('/?walk=credentials&walk_agent=game-promoter')
    await click('show-me-walk-stop')
    expect(card()).toBe(null)
    await router.push('/?walk=credentials&walk_agent=game-promoter')
    await wait()
    expect(card()).not.toBe(null)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wait()
    expect(card()).toBe(null)
  })

  it('does nothing for a walk it does not know', async () => {
    place({ 'data-walk-agent': 'game-promoter' })
    await start('/?walk=delete-everything&walk_agent=game-promoter')
    expect(card()).toBe(null)
  })
})

// The walks for sharing, calling and schedules, on the same engine.
function inside(parent, attrs, rect = { left: 300, top: 60, width: 90, height: 32 }) {
  const el = document.createElement('button')
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v)
  el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height })
  el.scrollIntoView = () => {}
  parent.appendChild(el)
  return el
}
const body = () => document.querySelector('[data-testid="show-me-walk-body"]')?.textContent.trim()
const has = (testid) => Boolean(document.querySelector(`[data-testid="${testid}"]`))

describe('the calling walk, on the pages', () => {
  beforeEach(() => {
    useNetworkStore().agents = [{ name: 'scout', display_label: 'Scout' }, { name: 'writer', display_label: 'Writer' }]
  })

  it('finds Permissions under More, then in the list, then lights the agent to tick and Save', async () => {
    const tabs = place({ 'data-testid': 'agent-tabs' })
    inside(tabs, { 'data-overflow-trigger': '' })
    await start('/agents/scout?tab=overview&walk=permissions&walk_agent=scout&walk_to=writer')
    expect(title()).toBe('Permissions')
    expect(body()).toBe("It's under More. Click More, then Permissions.")
    inside(tabs, { 'data-tab': 'permissions' }, { left: 300, top: 100, width: 160, height: 32 })
    await wait()
    expect(body()).toBe('Which agents Scout may call is set under Permissions. Click it.')

    const list = place({ 'data-walk': 'permissions-list' })
    const writer = inside(list, { 'data-walk-permission': 'writer', 'data-walk-checked': 'false' })
    inside(list, { 'data-walk-permission': 'sage', 'data-walk-checked': 'false' })
    place({ 'data-walk': 'permissions-save' })
    await router.push('/agents/scout?tab=permissions')
    await wait()
    expect(title()).toBe('Tick Writer')
    expect(has('show-me-walk-next')).toBe(false)
    writer.setAttribute('data-walk-checked', 'true')
    await wait()
    expect(title()).toBe('Save them')
    place({ 'data-walk': 'permissions-saved' })
    await wait()
    expect(title()).toBe("That's it")
    expect(body()).toBe("Scout may now call Writer. Go back to your chat with Atlas and tell it you're done.")
    await click('show-me-walk-done')
    expect(card()).toBe(null)
  })

  it('says so when it may already call that agent', async () => {
    const list = place({ 'data-walk': 'permissions-list' })
    inside(list, { 'data-walk-permission': 'writer', 'data-walk-checked': 'true' })
    await start('/agents/scout?tab=permissions&walk=permissions&walk_agent=scout&walk_to=writer')
    expect(title()).toBe('Already allowed')
    expect(body()).toBe('Scout may already call Writer.')
    await click('show-me-walk-next')
    expect(title()).toBe("That's it")
  })

  it('ends there, with Done, when there is no other agent', async () => {
    place({ 'data-walk': 'permissions-none' })
    await start('/agents/scout?tab=permissions&walk=permissions&walk_agent=scout&walk_to=writer')
    expect(title()).toBe('No other agent yet')
    expect(has('show-me-walk-next')).toBe(false)
    expect(has('show-me-walk-stop')).toBe(false)
    await click('show-me-walk-done')
    expect(card()).toBe(null)
  })
})

describe('the schedules walk, on the pages', () => {
  beforeEach(() => { useNetworkStore().agents = [{ name: 'scout', display_label: 'Scout' }] })

  function schedules(autonomy, rows = [{ id: 's1', enabled: 'false', name: 'Daily digest' }]) {
    const panel = place({ 'data-tour': 'showme-schedules', 'data-walk-autonomy': autonomy })
    return rows.map((r) => {
      const el = inside(panel, { 'data-walk-schedule': r.id, 'data-walk-enabled': r.enabled, 'data-walk-schedule-name': r.name }, { left: 40, top: 200, width: 600, height: 60 })
      inside(el, { 'data-walk': 'schedule-toggle' }, { left: 600, top: 210, width: 24, height: 24 })
      return el
    })
  }

  it('lights the switch, then Enable autonomy while it is off, then the last word', async () => {
    const [digest] = schedules('off')
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout&walk_schedule=s1')
    expect(title()).toBe('Turn on Daily digest')
    digest.setAttribute('data-walk-enabled', 'true')
    const button = place({ 'data-walk': 'autonomy-enable' })
    await wait()
    expect(title()).toBe('Turn on Autonomy')
    button.remove()
    await wait()
    expect(title()).toBe("That's it")
  })

  it('skips Autonomy when it is already on', async () => {
    const [digest] = schedules('on')
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout&walk_schedule=s1')
    digest.setAttribute('data-walk-enabled', 'true')
    await wait()
    expect(title()).toBe("That's it")
  })

  it('still lights Enable autonomy when the schedule was already on', async () => {
    schedules('off', [{ id: 's1', enabled: 'true', name: 'Daily digest' }])
    place({ 'data-walk': 'autonomy-enable' })
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout&walk_schedule=s1')
    expect(title()).toBe('Already on')
    await click('show-me-walk-next')
    expect(title()).toBe('Turn on Autonomy')
  })

  it('ends there when the agent has no schedule', async () => {
    place({ 'data-tour': 'showme-schedules', 'data-walk-autonomy': 'off' })
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout')
    expect(title()).toBe('No schedule yet')
    expect(body()).toBe('Scout has no schedule yet. Ask Atlas to plan one with you.')
  })
})

describe('the sharing walk, on the pages', () => {
  it('lights the email box and goes on once the page says the person was added', async () => {
    useNetworkStore().agents = [{ name: 'scout', display_label: 'Scout' }]
    place({ 'data-walk': 'people-form' })
    await start('/agents/scout?tab=settings&section=people&walk=people&walk_agent=scout')
    expect(title()).toBe('Their email')
    await wait(60)
    expect(title()).toBe('Their email')
    place({ 'data-walk': 'people-added' })
    await wait()
    expect(title()).toBe("That's it")
    expect(body()).toContain('They find it in their Workspace')
  })
})

// Final review (4 Nov): rows below the fold, a banner scrolled away, and a Save with nothing to save.
describe('final review: what is off screen, and nothing to save', () => {
  beforeEach(() => {
    useNetworkStore().agents = [{ name: 'scout', display_label: 'Scout' }, { name: 'writer', display_label: 'Writer' }]
  })

  it('scrolls to a row below the fold instead of saying it cannot see it', async () => {
    const panel = place({ 'data-tour': 'showme-schedules', 'data-walk-autonomy': 'off' })
    const row = inside(panel, { 'data-walk-schedule': 's4', 'data-walk-enabled': 'false', 'data-walk-schedule-name': 'Weekly' }, { left: 40, top: 2000, width: 600, height: 60 })
    const toggle = inside(row, { 'data-walk': 'schedule-toggle' }, { left: 600, top: 2010, width: 24, height: 24 })
    let scrolled = 0
    toggle.scrollIntoView = () => { scrolled += 1 }
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout&walk_schedule=s4')
    await wait(200)
    expect(title()).toBe('Turn on Weekly')
    expect(has('show-me-walk-lost')).toBe(false)
    expect(has('show-me-walk-spot')).toBe(true)
    expect(scrolled).toBe(1)
  })

  it('never skips Enable autonomy just because the banner is scrolled out of view', async () => {
    const panel = place({ 'data-tour': 'showme-schedules', 'data-walk-autonomy': 'off' })
    const row = inside(panel, { 'data-walk-schedule': 's1', 'data-walk-enabled': 'true', 'data-walk-schedule-name': 'Daily digest' }, { left: 40, top: 200, width: 600, height: 60 })
    inside(row, { 'data-walk': 'schedule-toggle' }, { left: 600, top: 210, width: 24, height: 24 })
    place({ 'data-walk': 'autonomy-enable' }, { left: 40, top: -600, width: 160, height: 36 })
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout&walk_schedule=s1')
    expect(title()).toBe('Already on')
    await click('show-me-walk-next')
    await wait(200)
    expect(title()).toBe('Turn on Autonomy')
  })

  it('says there is nothing to save, with Done, rather than waiting on a Save that is greyed out', async () => {
    const list = place({ 'data-walk': 'permissions-list' })
    inside(list, { 'data-walk-permission': 'writer', 'data-walk-checked': 'false' })
    const save = place({ 'data-walk': 'permissions-save', 'data-walk-idle': 'true' })
    await start('/agents/scout?tab=permissions&walk=permissions&walk_agent=scout')
    expect(title()).toBe('Tick the ones it may call')
    await click('show-me-walk-next')
    expect(title()).toBe('Nothing to save')
    expect(body()).toBe("Nothing has changed here yet. Tick an agent first, or press Done if it's already how you want it.")
    expect(has('show-me-walk-next')).toBe(false)
    expect(has('show-me-walk-done')).toBe(true)
    save.setAttribute('data-walk-idle', 'false')
    await wait()
    expect(title()).toBe('Save them')
  })
})

// The small issues from the final review.
describe('final review, small issues', () => {
  beforeEach(() => {
    useNetworkStore().agents = [{ name: 'scout', display_label: 'Scout' }, { name: 'writer', display_label: 'Writer' }]
  })

  it('a list that never shows is not read as empty: it offers a way on instead', async () => {
    await start('/agents/scout?tab=permissions&walk=permissions&walk_agent=scout&walk_to=writer')
    await wait(220)
    expect(title()).not.toBe('No other agent yet')
    expect(has('show-me-walk-lost')).toBe(true)
    expect(document.querySelector('[data-testid="show-me-walk-fallback"]').textContent.trim()).toBe('Open Permissions')
  })

  it('the same for schedules that never load', async () => {
    await start('/agents/scout?tab=schedules&walk=schedules&walk_agent=scout&walk_schedule=s1')
    await wait(220)
    expect(title()).not.toBe('No schedule yet')
    expect(has('show-me-walk-lost')).toBe(true)
  })

  it('Escape closes an open More menu without stopping the walk', async () => {
    const tabs = place({ 'data-testid': 'agent-tabs' })
    inside(tabs, { 'data-overflow-trigger': '' })
    const menu = inside(tabs, { 'data-overflow-menu': '' }, { left: 300, top: 100, width: 160, height: 120 })
    await start('/agents/scout?tab=overview&walk=permissions&walk_agent=scout')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wait()
    expect(card()).not.toBe(null)
    menu.remove()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wait()
    expect(card()).toBe(null)
  })
})

// The first step asks for a click on the lit agent, and a first tester looked for a way on
// instead. A step that only opens something has Next, which clicks the lit part for them.
describe('Next on a step that only opens something', () => {
  it('clicks the lit agent for them, and the walk moves on as if they had', async () => {
    const agent = place({ 'data-walk-agent': 'game-promoter' })
    agent.addEventListener('click', () => router.push('/agents/game-promoter'))
    await start('/?walk=credentials&walk_agent=game-promoter&walk_keys=SUPABASE_URL')
    expect(title()).toBe('Your agents live here')
    const settings = place({ 'data-walk': 'settings-tab' })
    const tabButton = document.createElement('button')
    let opened = 0
    tabButton.addEventListener('click', () => { opened += 1 })
    settings.appendChild(tabButton)
    await click('show-me-walk-press')
    expect(router.currentRoute.value.path).toBe('/agents/game-promoter')
    expect(title()).toBe('Its settings')
    // A wrapper that isn't a button itself: the button inside it is the one pressed.
    await click('show-me-walk-press')
    expect(opened).toBe(1)
  })

  it('is not offered where the step changes something they choose', async () => {
    place({ 'data-walk': 'people-form' })
    await start('/agents/game-promoter?tab=settings&section=people&walk=people&walk_agent=game-promoter')
    expect(title()).toBe('Their email')
    expect(document.querySelector('[data-testid="show-me-walk-press"]')).toBeNull()
  })
})
