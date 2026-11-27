// @vitest-environment jsdom
/**
 * #3447 — `ChannelConfigRow` rendered `<ChannelConfigDialog>` without importing
 * it. Vue could not resolve the tag, warned once per row, and rendered the
 * slotted channel panel INLINE inside the row's flex layout: no dialog, no
 * backdrop, no close.
 *
 * Mounted, because the defect is exactly what a source read cannot see — the
 * template was right and the component simply was not there at runtime.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { h } from 'vue'

vi.mock('../../src/api', () => ({ default: { get: vi.fn() } }))

import api from '../../src/api'
import ChannelConfigRow from '../../src/components/ChannelConfigRow.vue'

let wrapper
let warn
beforeEach(() => {
  api.get.mockReset()
  api.get.mockResolvedValue({ data: { connected: false } })
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => {
  wrapper?.unmount()
  warn.mockRestore()
  document.body.innerHTML = ''
})

async function mountRow() {
  wrapper = mount(ChannelConfigRow, {
    props: {
      title: 'Slack',
      agentName: 'alpha',
      statusUrl: '/api/agents/alpha/slack/channel',
      deriveStatus: (d) => ({ connected: !!d.connected }),
    },
    slots: { default: () => h('form', { 'data-testid': 'channel-panel' }, 'panel') },
    attachTo: document.body,
  })
  await flushPromises()
}

const dialog = () => document.body.querySelector('[role="dialog"][aria-label="Slack"]')
const unresolved = () => warn.mock.calls.filter((c) => String(c[0]).includes('Failed to resolve component'))

describe('#3447 ChannelConfigRow opens its config in a dialog', () => {
  it('renders no panel and no dialog until Configure is clicked', async () => {
    await mountRow()
    expect(dialog()).toBeNull()
    expect(document.body.querySelector('[data-testid="channel-panel"]')).toBeNull()
  })

  it('Configure opens a modal dialog holding the channel panel — not inline in the row', async () => {
    await mountRow()
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(dialog()).not.toBeNull()
    expect(dialog().getAttribute('aria-modal')).toBe('true')
    const panel = document.body.querySelector('[data-testid="channel-panel"]')
    expect(dialog().contains(panel)).toBe(true)
    // The row itself must not have grown the form.
    expect(wrapper.element.contains(panel)).toBe(false)
  })

  it('resolves every component it renders', async () => {
    await mountRow()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(unresolved()).toEqual([])
  })

  it('closing the dialog removes it and refetches the row status', async () => {
    await mountRow()
    expect(api.get).toHaveBeenCalledTimes(1)
    await wrapper.get('button').trigger('click')
    await flushPromises()

    dialog().querySelector('[aria-label="Close"]').click()
    await flushPromises()

    expect(dialog()).toBeNull()
    expect(api.get).toHaveBeenCalledTimes(2)
  })
})

// trinity-enterprise#340: a channel that fails its check must not read "Not connected".
describe('what the row says about its channel', () => {
  it.each([[500], [503]])('a %s says it could not check, and Retry asks again', async (code) => {
    api.get.mockRejectedValueOnce(Object.assign(new Error('x'), { response: { status: code } }))
    await mountRow()
    expect(wrapper.text()).toContain("Couldn't check")
    expect(wrapper.text()).not.toContain('Not connected')
    expect(wrapper.emitted('status').at(-1)[0]).toEqual({ connected: null })
    api.get.mockResolvedValueOnce({ data: { connected: true } })
    await wrapper.findAll('button').find((b) => b.text() === 'Retry').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Connected')
    expect(wrapper.emitted('status').at(-1)[0]).toEqual({ connected: true })
  })

  it('no response at all is also not an answer', async () => {
    api.get.mockRejectedValueOnce(new Error('network'))
    await mountRow()
    expect(wrapper.text()).toContain("Couldn't check")
  })

  it('a 4xx is an answer: not connected', async () => {
    api.get.mockRejectedValueOnce(Object.assign(new Error('x'), { response: { status: 404 } }))
    await mountRow()
    expect(wrapper.text()).toContain('Not connected')
    expect(wrapper.text()).not.toContain("Couldn't check")
    expect(wrapper.emitted('status').at(-1)[0]).toEqual({ connected: false })
  })

  it('opens from outside, and says when it closes', async () => {
    await mountRow()
    await wrapper.setProps({ open: true })
    await flushPromises()
    expect(dialog()).not.toBeNull()
    document.body.querySelector('[aria-label="Close"]').click()
    await flushPromises()
    expect(dialog()).toBeNull()
    expect(wrapper.emitted('closed')).toHaveLength(1)
  })
})
