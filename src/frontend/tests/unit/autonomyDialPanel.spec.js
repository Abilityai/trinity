// @vitest-environment jsdom
/**
 * trinity-enterprise#641 — the instance autonomy level, mounted.
 *
 * The level is a CEILING, and the two things an admin fears about a ceiling
 * are both false here: raising it does not promote anything, and lowering it
 * does not destroy what a seat earned. The panel has to say so, because an
 * operator who believes either will not touch the control.
 *
 * It also has to render the one fact that changes behaviour — whether a level
 * permits unprompted work at all — on the option itself, rather than leaving
 * it to be inferred from the ordering.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import api from '@/api'
import AutonomyDialPanel from '@/components/settings/AutonomyDialPanel.vue'

const LEVELS = [
  { level: 'L0', label: 'Continuity — companions absent or on-request only', allows_unprompted: false },
  { level: 'L1', label: 'Companion — on-request; brief on for ready seats', allows_unprompted: false },
  { level: 'L2', label: 'Delegated classes — unprompted on graduated ask classes; guard-capped', allows_unprompted: true },
  { level: 'L3', label: 'Load-bearing judgment — unprompted broadly; human gates on consequence', allows_unprompted: true },
]
const DIAL = { level: 'L1', label: LEVELS[1].label, allows_unprompted: false, levels: LEVELS, unprompted_from: 'L2', rule_version: '2026-09-23' }

async function mountPanel(dial = DIAL) {
  api.get.mockResolvedValue({ data: dial })
  const w = mount(AutonomyDialPanel)
  await flushPromises()
  return w
}

beforeEach(() => { vi.clearAllMocks() })

describe('the instance autonomy level', () => {
  it('renders one option per level and marks the ones that permit unprompted work', async () => {
    const w = await mountPanel()
    for (const lv of LEVELS) {
      expect(w.find(`[data-testid="autonomy-dial-option-${lv.level}"]`).exists()).toBe(true)
    }
    const l1 = w.find('[data-testid="autonomy-dial-option-L1"]').text()
    const l2 = w.find('[data-testid="autonomy-dial-option-L2"]').text()
    expect(l1).toContain('always asks first')
    expect(l2).toContain('unprompted possible')
    // the label is the canon wording, not a restatement
    expect(l2).toContain('Delegated classes')
  })

  it('says raising the level does not promote and lowering it does not destroy', async () => {
    // Both halves, because an operator who believes either one is false will
    // not move the control, and the asymmetry is the whole reason the earned
    // state is stored and the level is ANDed at read time.
    const t = (await mountPanel()).text()
    expect(t).toMatch(/never promotes/i)
    expect(t).toMatch(/takes nothing away|comes back exactly as it was/i)
  })

  it('PUTs the chosen level and adopts the response without emptying the options', async () => {
    const w = await mountPanel()
    // the PUT answer deliberately carries no `levels` — a replace would blank
    // the list on the first successful save (the ent#375 merge lesson).
    api.put.mockResolvedValue({ data: { level: 'L2', label: LEVELS[2].label, allows_unprompted: true } })
    await w.find('[data-testid="autonomy-dial-option-L2"] input').setValue('L2')
    await w.find('[data-testid="autonomy-dial-save"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/settings/autonomy-dial', { level: 'L2' })
    expect(w.find('[data-testid="autonomy-dial-current"]').text()).toBe('L2')
    expect(w.findAll('[data-testid^="autonomy-dial-option-"]').length).toBe(4)
  })

  it('save is inert until the choice actually differs', async () => {
    const w = await mountPanel()
    expect(w.find('[data-testid="autonomy-dial-save"]').attributes('disabled')).toBeDefined()
    await w.find('[data-testid="autonomy-dial-option-L3"] input').setValue('L3')
    expect(w.find('[data-testid="autonomy-dial-save"]').attributes('disabled')).toBeUndefined()
  })

  it('a load failure offers a retry; a save failure keeps the form and names the refusal', async () => {
    // Three different states an operator must be able to tell apart: could not
    // read it, could not write it, and wrote it. Collapsed, the first two both
    // read as "you may not set this".
    api.get.mockRejectedValueOnce({ response: { data: { detail: 'boom' } } })
    const w = mount(AutonomyDialPanel)
    await flushPromises()
    expect(w.text()).toContain('boom')
    expect(w.text()).toContain('Retry')

    api.get.mockResolvedValue({ data: DIAL })
    await w.find('button').trigger('click')
    await flushPromises()
    api.put.mockRejectedValue({ response: { data: { detail: { message: 'level must be one of L0, L1, L2, L3' } } } })
    await w.find('[data-testid="autonomy-dial-option-L2"] input').setValue('L2')
    await w.find('[data-testid="autonomy-dial-save"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('level must be one of')
    expect(w.findAll('[data-testid^="autonomy-dial-option-"]').length).toBe(4)
  })
})
