// @vitest-environment jsdom
/**
 * trinity-enterprise#539 — Select all / Deselect all in "Assign shared skills",
 * and the confirm before a save drops approval requirements, MOUNTED.
 *
 * The rules are executed in skillAssignDraft.spec.js. This proves the wiring a
 * rule test cannot see: what each button does to the real draft, what Save then
 * SENDS (a checkbox assertion cannot see the assignment the dialog does not
 * list, or the set's locked row), when Save asks first, and where keyboard
 * focus goes. Only `@/api` is mocked; the stores and the component are real.
 *
 * The dialog renders through the REAL Teleport, into `document.body`, and is
 * queried there. Stubbed inline, every patch re-created the dialog's nodes, so
 * a button that a click disables was REPLACED rather than disabled in place,
 * and the focus tests passed for the wrong reason (review of this PR).
 *
 * Two harness facts shape the assertions: VTU's `trigger` does nothing on a
 * disabled element, so a disabled state is read off `element.disabled`, never
 * "proven" by a click that goes nowhere; and jsdom leaves focus on a control
 * that becomes disabled, so focus is asserted on its exact target.
 *
 * The controls render only inside this dialog, which only the owner or an
 * admin can open: `skillsTab.mount.spec.js` ("a viewer is offered no
 * assignment controls") holds that.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises, enableAutoUnmount, DOMWrapper } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', isAuthenticated: true }),
}))

import SkillAssignModal from '../../src/components/skills/SkillAssignModal.vue'
import { useSkillsStore } from '../../src/stores/skills'
import { useSkillGatesStore } from '../../src/stores/skillGates'
import { BULK_TITLES, selectAllTitle, deselectAllTitle } from '../../src/utils/skillAssignDraft'

enableAutoUnmount(afterEach)

const AGENT = 'agent-x'
const LIBRARY = [
  { name: 'plain-on', description: 'held' },
  { name: 'plain-off', description: 'not held' },
  { name: 'dep-on', description: 'retired, held', deprecated: true, superseded_by: 'plain-on' },
  { name: 'dep-off', description: 'retired, not held', deprecated: true, superseded_by: null },
  { name: 'locked', description: 'held only through the set kit' },
  { name: 'both', description: 'held on its own and through kit' },
]
const row = (skill_name, over = {}) => ({ skill_name, individual: true, via_sets: [], delivery_status: null, ...over })
// `gone` is assigned on its own, but its skill has left the library: the
// dialog has no row for it.
const HELD = [
  row('plain-on'), row('dep-on'), row('both', { via_sets: ['kit'] }),
  row('locked', { individual: false, via_sets: ['kit'] }), row('gone'),
]
const SAVED_TICKS = ['both', 'dep-on', 'plain-on']
const gate = (skill_name) => ({ skill_name, approver: 'primary', approver_reachable: true, origin: 'set' })

let state
function respond(over = {}) {
  state = {
    library: LIBRARY, assigned: HELD, status: { configured: true }, sets: [],
    gates: [], gateMapFails: false, ...over,
  }
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') return Promise.resolve({ data: state.status })
    if (url === '/api/skills/library') return Promise.resolve({ data: state.library })
    if (url === `/api/agents/${AGENT}/skills`) return Promise.resolve({ data: state.assigned })
    if (url === `/api/agents/${AGENT}/skill-sets`) return Promise.resolve({ data: state.sets })
    if (url === `/api/agents/${AGENT}/skill-gates`) {
      if (state.gateMapFails) return Promise.reject({ response: { status: 500, data: { detail: 'down' } } })
      const answer = () => ({ data: { agent_name: AGENT, gates: state.gates, approvers: [] } })
      return state.gateMapHold ? state.gateMapHold.then(answer) : Promise.resolve(answer())
    }
    return Promise.resolve({ data: [] })
  })
}

// The tab loads both stores; the dialog reads them.
async function mountModal({ load = true } = {}) {
  if (load) {
    await useSkillsStore().load(AGENT)
    await useSkillGatesStore().load(AGENT)
  }
  const w = mount(SkillAssignModal, {
    props: { modelValue: true },
    attachTo: document.body,
    global: { stubs: { SkillContractChips: true } },
  })
  await flushPromises(); await nextTick()
  return w
}

// Teleported: the dialog lives in <body>, not under the wrapper.
const page = () => new DOMWrapper(document.body)
const tid = (id) => page().find(`[data-testid="${id}"]`)
const selectAll = () => tid('skill-assign-select-all')
const deselectAll = () => tid('skill-assign-deselect-all')
const save = () => tid('skill-assign-save')
const reset = () => tid('skill-assign-reset')
const close = () => tid('skill-assign-close')
const box = (name) => page().find(`[data-testid="skill-assign-list"] input[type="checkbox"][value="${name}"]`)
const ticked = () => page().findAll('[data-testid="skill-assign-list"] input[type="checkbox"][value]')
  .filter((b) => b.element.checked).map((b) => b.element.value).sort()
const sent = () => [...api.put.mock.calls.at(-1)[1].skills].sort()
const writes = () => api.put.mock.calls.length + api.post.mock.calls.length + api.delete.mock.calls.length
const gateReads = () => api.get.mock.calls.filter(([url]) => url === `/api/agents/${AGENT}/skill-gates`).length

async function settle() {
  await flushPromises(); await nextTick(); await flushPromises()
}
async function click(wrapper) {
  await wrapper.trigger('click')
  await settle()
}

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of Object.values(api)) f.mockReset()
  respond()
  api.put.mockResolvedValue({ data: { delivery: null } })
})

describe('Select all', () => {
  it('ticks what it may, keeps every tick it found, and writes nothing', async () => {
    await mountModal()
    expect(ticked()).toEqual(SAVED_TICKS)
    expect(save().element.disabled).toBe(true)

    await click(selectAll())

    expect(ticked()).toEqual(['both', 'dep-on', 'plain-off', 'plain-on'])
    expect(box('dep-off').element.checked).toBe(false)              // deprecated and not held: left to the operator
    expect(tid('skill-locked-locked').element.checked).toBe(true)   // the set's row is still ticked…
    expect(tid('skill-locked-locked').element.disabled).toBe(true)  // …and still locked
    expect(save().element.disabled).toBe(false)                     // dirty, as after a manual tick
    expect(reset().exists()).toBe(true)
    expect(writes()).toBe(0)
  })

  it('then Save sends the additions, the held deprecated skill and the unlisted assignment, never the set\'s row', async () => {
    const w = await mountModal()
    await click(selectAll())
    await click(save())

    expect(api.put).toHaveBeenCalledTimes(1)
    expect(api.put.mock.calls[0][0]).toBe(`/api/agents/${AGENT}/skills`)
    expect(sent()).toEqual(['both', 'dep-on', 'gone', 'plain-off', 'plain-on'])
    expect(w.emitted('saved')).toHaveLength(1)                     // the same notice path as any save
  })
})

describe('Deselect all', () => {
  it('unticks every listed skill it may, deprecated included, and writes nothing', async () => {
    await mountModal()
    await click(deselectAll())

    expect(ticked()).toEqual([])
    expect(tid('skill-locked-locked').element.checked).toBe(true)   // the set's row stays, locked
    expect(save().element.disabled).toBe(false)
    expect(writes()).toBe(0)
  })

  it('then Save keeps the assignment the dialog does not list', async () => {
    await mountModal()
    await click(deselectAll())
    await click(save())

    expect(tid('confirm-dialog').exists()).toBe(false)              // nothing removed carries a requirement
    expect(sent()).toEqual(['gone'])                               // `both` stays through its set, server-side
  })

  it('with nothing unlisted, Save persists an empty list', async () => {
    respond({ assigned: [row('plain-on'), row('plain-off')] })
    await mountModal()
    await click(deselectAll())
    await click(save())
    expect(sent()).toEqual([])
  })
})

describe('Reset still restores the saved set', () => {
  it.each(['select', 'deselect'])('after %s all', async (verb) => {
    await mountModal()
    await click(verb === 'select' ? selectAll() : deselectAll())
    expect(ticked()).not.toEqual(SAVED_TICKS)

    await click(reset())
    expect(ticked()).toEqual(SAVED_TICKS)
    expect(save().element.disabled).toBe(true)
  })
})

describe('disabled, with the reason on the button', () => {
  it('both wait for a library that has not loaded', async () => {
    await mountModal({ load: false })
    for (const b of [selectAll(), deselectAll()]) {
      expect(b.element.disabled).toBe(true)
      expect(b.attributes('title')).toBe(BULK_TITLES.loading)
    }
  })

  it.each([
    ['unconfigured', { status: { configured: false } }, BULK_TITLES.unconfigured],
    ['empty', { library: [] }, BULK_TITLES.empty],
  ])('Select all names a library that is %s', async (_, over, title) => {
    respond(over)
    await mountModal()
    expect(selectAll().element.disabled).toBe(true)
    expect(selectAll().attributes('title')).toBe(title)
  })

  it('enabled, each title counts what the click will change', async () => {
    await mountModal()
    expect(selectAll().attributes('title')).toBe(selectAllTitle(1))    // plain-off
    expect(deselectAll().attributes('title')).toBe(deselectAllTitle(3)) // plain-on, dep-on, both
  })

  it('both wait while a save is in flight, then come back', async () => {
    let finish
    api.put.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    await mountModal()
    await click(selectAll())
    await click(save())

    for (const b of [selectAll(), deselectAll()]) {
      expect(b.element.disabled).toBe(true)
      expect(b.attributes('title')).toBe(BULK_TITLES.saving)
    }
    finish({ data: { delivery: null } })
    await settle()
    expect(selectAll().element.disabled).toBe(false)
    expect(deselectAll().element.disabled).toBe(false)
  })
})

describe('keyboard: focus never falls out of the dialog', () => {
  it('the dialog opens on the filter (ent#660); Deselect all is marked so it never does', async () => {
    await mountModal()
    expect(document.activeElement).toBe(tid('skill-assign-filter').element)
    expect(deselectAll().attributes()).toHaveProperty('data-destructive')
  })

  it('with nothing to tick, it still opens on the filter, not on Deselect all', async () => {
    respond({ library: [LIBRARY[0]], assigned: [row('plain-on')] })
    await mountModal()
    expect(selectAll().element.disabled).toBe(true)
    expect(document.activeElement).toBe(tid('skill-assign-filter').element)
  })

  it('a row that vanishes under focus gives focus back to the dialog (Safari keeps it on the row across a mouse pick)', async () => {
    // `other-*` keeps both bulk buttons enabled: the row leaving is the only change.
    respond({
      library: [...LIBRARY, { name: 'other-on', description: 'x' }, { name: 'other-off', description: 'x' }],
      assigned: [...HELD, row('other-on')],
    })
    await mountModal()
    box('plain-on').element.focus()
    await settle()
    await tid('skill-assign-filter').setValue('other')           // moves no focus, like that mouse pick
    await settle()
    expect(box('plain-on').exists()).toBe(false)
    expect(selectAll().element.disabled).toBe(false)
    expect(deselectAll().element.disabled).toBe(false)
    expect(document.activeElement).toBe(close().element)
  })

  it('a button that disables itself hands focus to the other one', async () => {
    await mountModal()
    const selectEl = selectAll().element
    selectEl.focus()
    await click(selectAll())
    expect(selectAll().element).toBe(selectEl)                     // disabled in place, not re-created
    expect(selectEl.disabled).toBe(true)
    expect(document.activeElement).toBe(deselectAll().element)

    await click(deselectAll())
    expect(deselectAll().element.disabled).toBe(true)
    expect(document.activeElement).toBe(selectAll().element)
  })

  it('…and to Close when the other one has nothing to do', async () => {
    respond({ library: [LIBRARY[2]], assigned: [row('dep-on')] })    // a held deprecated skill, nothing else
    await mountModal()
    deselectAll().element.focus()
    await click(deselectAll())
    expect(selectAll().element.disabled).toBe(true)
    expect(document.activeElement).toBe(close().element)
  })

  it('a manual tick that leaves Select all nothing to do moves focus off it', async () => {
    await mountModal()
    selectAll().element.focus()                                    // a mouse click on a box leaves it there (Safari)
    await box('plain-off').setValue(true)
    await settle()
    expect(selectAll().element.disabled).toBe(true)
    expect(document.activeElement).toBe(deselectAll().element)
  })

  it('Reset hands focus to Close rather than dropping it with the button', async () => {
    await mountModal()
    await click(selectAll())
    reset().element.focus()
    await click(reset())
    expect(reset().exists()).toBe(false)
    expect(document.activeElement).toBe(close().element)
  })

  it('Save hands focus to Close while it saves', async () => {
    api.put.mockImplementation(() => new Promise(() => {}))         // never answers: the save stays in flight
    await mountModal()
    await click(selectAll())
    save().element.focus()
    await click(save())
    expect(save().element.disabled).toBe(true)
    expect(document.activeElement).toBe(close().element)
  })
})

describe('Save asks first when it would drop an approval requirement', () => {
  it('names the skill, opens on Cancel, and sends nothing until confirmed', async () => {
    respond({ gates: [gate('plain-on')] })
    await mountModal()
    await click(deselectAll())
    await click(save())

    expect(tid('confirm-dialog').exists()).toBe(true)
    expect(tid('confirm-dialog-title').text()).toBe('Unassign /plain-on?')
    expect(tid('confirm-dialog-message').text())
      .toBe('/plain-on needs approval. Saving will remove it from this agent, along with its approval requirement.')
    expect(document.activeElement).toBe(tid('confirm-dialog-cancel').element)
    expect(api.put).not.toHaveBeenCalled()

    await click(tid('confirm-dialog-cancel'))
    expect(tid('confirm-dialog').exists()).toBe(false)
    expect(ticked()).toEqual([])                                    // the draft is kept
    expect(api.put).not.toHaveBeenCalled()

    await click(save())
    await click(tid('confirm-dialog-confirm'))
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(sent()).toEqual(['gone'])
  })

  it('decides on a fresh gate map, not the one read when the tab opened', async () => {
    await mountModal()                                              // read: nothing gated
    state.gates = [gate('plain-on')]                                // gated since, elsewhere
    const before = gateReads()
    await box('plain-on').setValue(false)
    await click(save())
    expect(gateReads()).toBe(before + 1)
    expect(tid('confirm-dialog').exists()).toBe(true)
    expect(api.put).not.toHaveBeenCalled()
  })

  it('a second click while the map is re-read is the same click', async () => {
    await mountModal()
    let release
    state.gateMapHold = new Promise((resolve) => { release = resolve })
    await box('plain-on').setValue(false)
    await save().trigger('click')
    await save().trigger('click')
    release()
    await settle()
    expect(api.put).toHaveBeenCalledTimes(1)
  })

  it('a dialog closed while the map is re-read neither saves nor asks', async () => {
    const w = await mountModal()
    let release
    state.gateMapHold = new Promise((resolve) => { release = resolve })
    await box('plain-on').setValue(false)
    await save().trigger('click')
    await w.setProps({ modelValue: false })
    release()
    await settle()
    expect(api.put).not.toHaveBeenCalled()
  })

  it('reads nothing extra for a save that only adds', async () => {
    await mountModal()
    const before = gateReads()
    await click(selectAll())
    await click(save())
    expect(gateReads()).toBe(before)
    expect(api.put).toHaveBeenCalledTimes(1)
  })

  it('asks for a manual untick too: it is the same consequence', async () => {
    respond({ gates: [gate('plain-on')] })
    await mountModal()
    await box('plain-on').setValue(false)
    await click(save())
    expect(tid('confirm-dialog').exists()).toBe(true)
    expect(api.put).not.toHaveBeenCalled()
  })

  it('does not ask when the set still holds the gated skill', async () => {
    respond({ gates: [gate('both')] })
    await mountModal()
    await box('both').setValue(false)
    await click(save())
    expect(tid('confirm-dialog').exists()).toBe(false)
    expect(api.put).toHaveBeenCalledTimes(1)
  })

  it('does ask for it when another held set is unresolved: the server then keeps only set-only rows', async () => {
    respond({
      gates: [gate('both')],
      sets: [{ name: 'kit', status: 'ok' }, { name: 'gone-set', status: 'unresolved', reason: 'missing_upstream' }],
    })
    await mountModal()
    await box('both').setValue(false)
    await click(save())
    expect(tid('confirm-dialog-title').text()).toBe('Unassign /both?')
    expect(api.put).not.toHaveBeenCalled()
  })

  it('asks for any removal when the approval map cannot be read', async () => {
    await mountModal()                                              // read fine when the tab opened…
    state.gateMapFails = true                                       // …and fails now
    await box('dep-on').setValue(false)
    await click(save())
    expect(tid('confirm-dialog-message').text()).toContain("couldn't be read")
    expect(api.put).not.toHaveBeenCalled()
  })

  it('confirming keeps focus in the dialog while the save runs', async () => {
    respond({ gates: [gate('plain-on')] })
    api.put.mockImplementation(() => new Promise(() => {}))
    await mountModal()
    await box('plain-on').setValue(false)
    save().element.focus()
    await click(save())
    await click(tid('confirm-dialog-confirm'))
    expect(tid('confirm-dialog').exists()).toBe(false)
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(close().element)
  })
})

// ---- trinity-enterprise#660 + trinity#3448: find a skill, see what Save would do ----

const type = async (text) => { await tid('skill-assign-filter').setValue(text); await settle() }
const choose = async (id, value) => { await tid(id).setValue(value); await settle() }
const listed = () => page().findAll('[data-testid="skill-assign-list"] input[type="checkbox"]')
  .map((b) => b.attributes('value') || b.attributes('data-testid').replace('skill-locked-', ''))
const mark = (name) => tid(`skill-pending-${name}`)
const pending = () => tid('skill-assign-pending').text()
const options = (id) => tid(id).findAll('option').map((o) => o.text())
const overlay = () => page().find('[role="dialog"]')

const TAGGED = [
  { name: 'team-one', description: 'x', source_id: 'src_a', source_name: 'Team', category: 'project-management' },
  { name: 'team-two', description: 'x', source_id: 'src_a', source_name: 'Team', category: null },
  { name: 'vendor-one', description: 'x', source_id: 'src_b', source_name: 'Vendor', category: 'research-and-analysis' },
]

describe('the filter narrows the list and both bulk buttons', () => {
  it('states the total and what is shown; the unfiltered wording is today\'s', async () => {
    await mountModal()
    expect(tid('skill-assign-count').text()).toBe('6 skills in the library')
    await type('plain')
    expect(listed()).toEqual(['plain-on', 'plain-off'])
    expect(tid('skill-assign-count').text()).toBe('6 in the library · 2 shown')
  })

  it('matches the description too: one rule with the Skills tab', async () => {
    await mountModal()
    await type('RETIRED')
    expect(listed()).toEqual(['dep-on', 'dep-off'])
  })

  it('Select all ticks only what is shown', async () => {
    respond({ library: [...LIBRARY, { name: 'other-off', description: 'also not held' }] })
    await mountModal()
    await type('plain')
    await click(selectAll())
    await type('')
    expect(box('plain-off').element.checked).toBe(true)
    expect(box('other-off').element.checked).toBe(false)
  })

  it('Deselect all unticks only what is shown', async () => {
    await mountModal()
    await type('plain')
    await click(deselectAll())
    await type('')
    expect(ticked()).toEqual(['both', 'dep-on'])
  })

  it('with nothing shown, both are disabled and say why, and the list says so', async () => {
    await mountModal()
    await type('no such skill')
    expect(listed()).toEqual([])
    expect(tid('skill-assign-no-match').text()).toBe('No skill matches the filter.')
    expect(selectAll().element.disabled).toBe(true)
    expect(selectAll().attributes('title')).toBe(BULK_TITLES.noneListed)
    expect(deselectAll().element.disabled).toBe(true)
    expect(deselectAll().attributes('title')).toBe(BULK_TITLES.noneListed)
  })

  it('an empty library says that, not that a filter found nothing', async () => {
    respond({ library: [], assigned: [] })
    await mountModal()
    expect(tid('skill-assign-no-match').text()).toBe('The library has no skills yet.')
  })

  it('a new query starts the list at the first row', async () => {
    await mountModal()
    const list = tid('skill-assign-list').element
    list.scrollTop = 200
    await type('plain')
    expect(list.scrollTop).toBe(0)
  })
})

describe('source and category', () => {
  it('the source filter hides with one source, and shows with two', async () => {
    respond({ library: TAGGED.slice(0, 2), assigned: [] })
    await mountModal()
    expect(tid('skill-assign-source').exists()).toBe(false)
  })

  it('the category filter hides when no skill has one', async () => {
    respond({ library: LIBRARY })
    await mountModal()
    expect(tid('skill-assign-category').exists()).toBe(false)
  })

  it('offers what the skills carry, readable, and narrows by it', async () => {
    respond({ library: TAGGED, assigned: [] })
    await mountModal()
    expect(options('skill-assign-source')).toEqual(['All sources', 'Team', 'Vendor'])
    expect(options('skill-assign-category')).toEqual(['All categories', 'Project management', 'Research and analysis'])

    await choose('skill-assign-source', 'src_a')
    expect(listed()).toEqual(['team-one', 'team-two'])
    await choose('skill-assign-category', 'project-management')
    expect(listed()).toEqual(['team-one'])
    expect(tid('skill-assign-count').text()).toBe('3 in the library · 1 shown')
  })

  it('a skill with no category appears only under All categories', async () => {
    respond({ library: TAGGED, assigned: [] })
    await mountModal()
    await choose('skill-assign-category', 'research-and-analysis')
    expect(listed()).toEqual(['vendor-one'])
    await choose('skill-assign-category', '')
    expect(listed()).toEqual(['team-one', 'team-two', 'vendor-one'])
  })

  it('a choice the list no longer offers clears itself: a hidden control never keeps narrowing', async () => {
    respond({ library: TAGGED, assigned: [] })
    await mountModal()
    await choose('skill-assign-source', 'src_b')
    expect(listed()).toEqual(['vendor-one'])

    useSkillsStore().library = TAGGED.slice(0, 2)                  // a sync dropped that source
    await settle()

    expect(tid('skill-assign-source').exists()).toBe(false)
    expect(listed()).toEqual(['team-one', 'team-two'])
    expect(tid('skill-assign-count').text()).toBe('2 skills in the library')
  })
})

describe('a filter never narrows what Save does', () => {
  it('a ticked skill hidden by the filter stays in the draft, the count and the save', async () => {
    await mountModal()
    await box('plain-off').setValue(true)
    await type('retired')
    expect(box('plain-off').exists()).toBe(false)
    expect(pending()).toBe('1 to add')

    await click(save())

    expect(sent()).toEqual(['both', 'dep-on', 'gone', 'plain-off', 'plain-on'])
  })

  it('an unticked skill hidden by the filter stays out of the save', async () => {
    await mountModal()
    await box('plain-on').setValue(false)
    await type('retired')
    expect(pending()).toBe('1 to remove')
    await click(save())
    expect(sent()).toEqual(['both', 'dep-on', 'gone'])
  })

  it('a hidden removal still asks for the approval confirm', async () => {
    respond({ gates: [gate('plain-on')] })
    await mountModal()
    await box('plain-on').setValue(false)
    await type('retired')
    expect(box('plain-on').exists()).toBe(false)

    await click(save())

    expect(tid('confirm-dialog-title').text()).toBe('Unassign /plain-on?')
    expect(api.put).not.toHaveBeenCalled()
  })

  it('a filter on its own changes nothing: Save stays disabled', async () => {
    await mountModal()
    await type('plain')
    expect(save().element.disabled).toBe(true)
    expect(pending()).toBe('')
  })
})

describe('pending changes are marked before Save', () => {
  it('rows and the count say to add and to remove; a row that changes nothing has no mark', async () => {
    await mountModal()
    await box('plain-off').setValue(true)
    await box('plain-on').setValue(false)

    expect(mark('plain-off').text()).toBe('to add')
    expect(mark('plain-on').text()).toBe('to remove')
    expect(mark('dep-on').exists()).toBe(false)
    expect(pending()).toBe('1 to add, 1 to remove')
  })

  it('Reset clears the marks and the count', async () => {
    await mountModal()
    await box('plain-off').setValue(true)
    await box('plain-on').setValue(false)

    await click(reset())

    expect(mark('plain-off').exists()).toBe(false)
    expect(mark('plain-on').exists()).toBe(false)
    expect(pending()).toBe('')
  })

  it('a save clears them too', async () => {
    await mountModal()
    await box('plain-off').setValue(true)
    respond({ assigned: [...HELD, row('plain-off')] })
    await click(save())
    expect(mark('plain-off').exists()).toBe(false)
    expect(pending()).toBe('')
  })

  it('unticking a skill a set also holds says it stays, and names the set', async () => {
    await mountModal()
    await box('both').setValue(false)

    expect(mark('both').text()).toBe('kept by set')
    expect(mark('both').attributes('title')).toBe('Stays on this agent through kit. Saving removes only the direct assignment.')
    expect(pending()).toBe('1 kept by a set')
    expect(save().element.disabled).toBe(false)
  })

  it('while a held set is unresolved the server may drop it, so it reads as a removal', async () => {
    respond({ sets: [{ name: 'kit', status: 'unresolved' }] })
    await mountModal()
    await box('both').setValue(false)
    expect(mark('both').text()).toBe('to remove')
    expect(pending()).toBe('1 to remove')
  })

  it('a set\'s locked row never carries a mark', async () => {
    await mountModal()
    await click(selectAll())
    await click(deselectAll())
    expect(mark('locked').exists()).toBe(false)
  })

  it('the count is announced', async () => {
    await mountModal()
    expect(tid('skill-assign-pending').attributes('aria-live')).toBe('polite')
  })
})

describe('the dialog and its filter', () => {
  it('each opening starts unfiltered', async () => {
    const w = await mountModal()
    await type('plain')
    await w.setProps({ modelValue: false })
    await settle()
    await w.setProps({ modelValue: true })
    await settle()
    expect(tid('skill-assign-filter').element.value).toBe('')
    expect(listed()).toHaveLength(6)
  })

  it('Esc with text in the filter clears it and keeps the dialog; Esc again closes', async () => {
    const w = await mountModal()
    await box('plain-off').setValue(true)
    await type('plain')

    await tid('skill-assign-filter').trigger('keydown', { key: 'Escape' })
    await settle()
    expect(tid('skill-assign-filter').element.value).toBe('')
    expect(w.emitted('update:modelValue')).toBeUndefined()
    expect(box('plain-off').element.checked).toBe(true)            // the draft is untouched

    await tid('skill-assign-filter').trigger('keydown', { key: 'Escape' })
    await settle()
    expect(w.emitted('update:modelValue')).toEqual([[false]])
  })

  it('a backdrop click closes a clean dialog, not one holding unsaved changes', async () => {
    const w = await mountModal()
    await box('plain-off').setValue(true)
    await overlay().trigger('click')
    await settle()
    expect(w.emitted('update:modelValue')).toBeUndefined()

    await box('plain-off').setValue(false)
    await overlay().trigger('click')
    await settle()
    expect(w.emitted('update:modelValue')).toEqual([[false]])
  })

  it('the list has one fixed height, inside a panel capped to the window (the bound; geometry is the eyeball)', async () => {
    await mountModal()
    const list = tid('skill-assign-list')
    expect(list.classes()).toEqual(expect.arrayContaining(['h-[50vh]', 'min-h-40', 'overflow-y-auto']))
    expect([...list.element.parentElement.classList])
      .toEqual(expect.arrayContaining(['flex', 'flex-col', 'max-h-[calc(100dvh-2rem)]', 'overflow-y-auto']))
  })
})

describe('a failed save', () => {
  it('keeps the bulk draft and says why', async () => {
    api.put.mockRejectedValue({ response: { data: { detail: 'Could not reach the agent' } } })
    await mountModal()
    await click(selectAll())
    await click(save())

    expect(ticked()).toContain('plain-off')
    expect(document.body.textContent).toContain('Could not reach the agent')
    expect(save().element.disabled).toBe(false)
  })
})
