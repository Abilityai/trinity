// @vitest-environment jsdom
/**
 * Show me on the real page: one control lit up with a short note. Tabs and panels draw
 * late, so it waits a little for the control; if it never appears, a note says so
 * instead of lighting up nothing.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ShowMeSpotlight from '@/components/onboarding/ShowMeSpotlight.vue'

const wait = (ms) => new Promise((done) => setTimeout(done, ms))
const settle = async (ms = 80) => { await wait(ms); await flushPromises() }

function control(target) {
  const el = document.createElement('div')
  el.setAttribute('data-tour', target)
  el.getBoundingClientRect = () => ({ left: 10, top: 10, width: 200, height: 40, right: 210, bottom: 50 })
  document.body.appendChild(el)
  return el
}

afterEach(() => { document.body.innerHTML = '' })

describe('Show me', () => {
  it('lights up the one control with its note and only Got it', async () => {
    control('showme-permissions')
    const w = mount(ShowMeSpotlight, { attachTo: document.body, props: { showKey: 'permissions', pollMs: 10, giveUpMs: 200 } })
    await settle()
    expect(document.querySelector('#build-tour-title').textContent).toBe('Who this agent can call')
    expect(document.querySelector('[data-testid="build-tour-next"]').textContent.trim()).toBe('Got it')
    expect(document.querySelector('[data-testid="build-tour-skip"]')).toBe(null)
    expect(document.querySelector('[data-testid="build-tour-progress"]')).toBe(null)
    document.querySelector('[data-testid="build-tour-next"]').click()
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })

  it('waits for a control that draws late', async () => {
    const w = mount(ShowMeSpotlight, { attachTo: document.body, props: { showKey: 'skills', pollMs: 10, giveUpMs: 300 } })
    await settle(30)
    control('showme-skills')
    await settle()
    expect(document.querySelector('#build-tour-title').textContent).toBe("This agent's skills")
    w.unmount()
  })

  it('says so when the control is not on the page, and never lights up nothing', async () => {
    const w = mount(ShowMeSpotlight, { attachTo: document.body, props: { showKey: 'schedules', pollMs: 10, giveUpMs: 50 } })
    await settle(120)
    expect(document.querySelector('[data-testid="build-tour"]')).toBe(null)
    expect(document.querySelector('[data-testid="show-me-missing"]').textContent).toContain("Couldn't find that on this page. It may be under another tab, or only the agent's owner can see it.")
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })

  it('a place it does not know closes straight away', async () => {
    const w = mount(ShowMeSpotlight, { attachTo: document.body, props: { showKey: 'nowhere', pollMs: 10, giveUpMs: 50 } })
    await settle(20)
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })
})
