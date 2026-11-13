// @vitest-environment jsdom
/**
 * #4499 — the help widget's two channels post to a vendor-operated host, so
 * the operator's run-time switches decide them (`GET /api/version` → `help`).
 * Before, the Ask tab had no switch and the report tabs followed a BUILD
 * argument that the published images had already compiled in as on.
 *
 * A channel shows only for a literal `true`. Until the answer arrives, when it
 * fails, and on a backend that sends no `help`, nothing is shown.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const get = vi.fn()
vi.mock('@/api', () => ({ default: { get: (...a) => get(...a) } }))

const LAUNCHER = '[aria-label="Open help chat"]'
const PANEL = '[role="dialog"][aria-label="Help chat"]'

let wrapper
async function mountWidget(version) {
  vi.resetModules()
  get.mockReset()
  if (version instanceof Error) get.mockRejectedValue(version)
  else get.mockResolvedValue({ data: version })
  const { default: HelpChatWidget } = await import('../../src/components/HelpChatWidget.vue')
  wrapper = mount(HelpChatWidget, { attachTo: document.body })
  await flushPromises()
  return wrapper
}
const tabs = () => wrapper.findAll('[role="tab"]').map((t) => t.text())
async function open() {
  await wrapper.find(LAUNCHER).trigger('click')
  await flushPromises()
}

beforeEach(() => { localStorage.clear(); global.fetch = vi.fn() })
afterEach(() => wrapper?.unmount())

describe('#4499 the help widget follows the run-time switches', () => {
  it('shows all four tabs when both channels are on', async () => {
    await mountWidget({ version: '1', help: { docs_qa: true, bug_reporting: true } })
    await open()
    expect(tabs()).toEqual(['Ask', 'Bug', 'Feature', 'Feedback'])
  })

  it('has no Ask tab when the docs service is switched off, and opens on a report tab', async () => {
    await mountWidget({ version: '1', help: { docs_qa: false, bug_reporting: true } })
    await open()
    expect(tabs()).toEqual(['Bug', 'Feature', 'Feedback'])
    expect(wrapper.find('[role="tab"][aria-selected="true"]').text()).toBe('Bug')
  })

  it('is the Ask panel alone, with no tab row, when reporting is switched off', async () => {
    await mountWidget({ version: '1', help: { docs_qa: true, bug_reporting: false } })
    await open()
    expect(wrapper.find(PANEL).exists()).toBe(true)
    expect(tabs()).toEqual([])
  })

  it('is not rendered at all when both are off', async () => {
    await mountWidget({ version: '1', help: { docs_qa: false, bug_reporting: false } })
    expect(wrapper.find(LAUNCHER).exists()).toBe(false)
    expect(wrapper.find(PANEL).exists()).toBe(false)
  })

  it.each([
    ['a backend that sends no help block', { version: '1' }],
    ['a value that is not a literal true', { version: '1', help: { docs_qa: 'true', bug_reporting: 1 } }],
    ['a version read that fails', new Error('network')],
  ])('is not rendered for %s', async (_label, version) => {
    await mountWidget(version)
    expect(wrapper.find(LAUNCHER).exists()).toBe(false)
    expect(global.fetch).not.toHaveBeenCalled()
  })
})
