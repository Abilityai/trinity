// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#1108 — the wake-on-ask-ending switch shows an
 * honest status ("Wakes when an ask ends: on/off") read off the server, for
 * an untouched agent (on by default) and for an owner's OFF. Mounted, not
 * regexed (trinity#2918): the status is a rendered predicate.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../src/api', () => ({ default: { get: (...a) => get(...a), put: (...a) => put(...a) } }))

// eslint-disable-next-line import/first
import ReliabilityPanel from '../../src/components/ReliabilityPanel.vue'

function answering(resumeEnabled) {
  get.mockImplementation(async (url) => {
    if (url.endsWith('/operator-resume')) return { data: { agent_name: 'a', enabled: resumeEnabled } }
    if (url.endsWith('/circuit-breaker')) return { data: { config: { enabled: false, global_enabled: true } } }
    throw new Error(`unexpected GET ${url}`)
  })
}

async function mountPanel() {
  const w = mount(ReliabilityPanel, { props: { agentName: 'a', embedded: true } })
  await flushPromises()
  return w
}

describe('ReliabilityPanel — wake on ask ending status (ent#1108)', () => {
  beforeEach(() => { get.mockReset(); put.mockReset() })

  it('an untouched agent reads "on" — the platform default', async () => {
    answering(true)
    const w = await mountPanel()
    expect(w.get('[data-testid="resume-status"]').text()).toBe('Wakes when an ask ends: on')
    const boxes = w.findAll('input[type="checkbox"]')
    expect(boxes[boxes.length - 1].element.checked).toBe(true)
  })

  it("an owner's OFF reads \"off\"", async () => {
    answering(false)
    const w = await mountPanel()
    expect(w.get('[data-testid="resume-status"]').text()).toBe('Wakes when an ask ends: off')
  })

  it('a GET that fails reads "unknown", never "off" — the setting is on by default', async () => {
    get.mockImplementation(async (url) => {
      if (url.endsWith('/operator-resume')) throw new Error('503')
      if (url.endsWith('/circuit-breaker')) return { data: { config: { enabled: false, global_enabled: true } } }
      throw new Error(`unexpected GET ${url}`)
    })
    const w = await mountPanel()
    expect(w.get('[data-testid="resume-status"]').text()).toBe('Wakes when an ask ends: unknown')
    const boxes = w.findAll('input[type="checkbox"]')
    const box = boxes[boxes.length - 1].element
    expect(box.checked).toBe(false)
    expect(box.disabled).toBe(true)
  })

  it('switching it off writes the owner\'s choice and the status follows the server', async () => {
    answering(true)
    put.mockResolvedValue({ data: { agent_name: 'a', enabled: false } })
    const w = await mountPanel()
    const boxes = w.findAll('input[type="checkbox"]')
    await boxes[boxes.length - 1].setValue(false)
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/agents/a/operator-resume', { enabled: false })
    expect(w.get('[data-testid="resume-status"]').text()).toBe('Wakes when an ask ends: off')
  })
})
