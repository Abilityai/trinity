// @vitest-environment jsdom
/**
 * The level moments use the Workspace tour's spotlight: a step can name any part by a
 * selector (a sidebar agent row), and a short moment has no "Skip the tour".
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import PortalBuildTour from '@/components/portal/builder/PortalBuildTour.vue'

function onScreen(attr, value) {
  const el = document.createElement('div')
  el.setAttribute(attr, value)
  el.getBoundingClientRect = () => ({ left: 10, top: 10, width: 120, height: 32, right: 130, bottom: 42 })
  document.body.appendChild(el)
}
const settle = async () => { await new Promise((done) => setTimeout(done, 40)); await flushPromises() }

beforeEach(() => { document.body.innerHTML = '' })

describe('a level moment in the tour spotlight', () => {
  it('lights a part named by a selector, then one named by its tour name', async () => {
    onScreen('data-tour-agent', 'game-filler')
    onScreen('data-tour', 'levels')
    const steps = [
      { selector: '[data-tour-agent="game-filler"]', title: 'Game filler is yours', body: 'Your agents live here.' },
      { target: 'levels', title: 'Level 1 done: First agent', body: 'Next, Level 2: A new playbook.' },
    ]
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true, steps, minSteps: 1, skippable: false } })
    await settle()
    expect(document.querySelector('#build-tour-title').textContent).toBe('Game filler is yours')
    expect(document.querySelector('[data-testid="build-tour-skip"]')).toBeNull()
    document.querySelector('[data-testid="build-tour-next"]').click()
    await flushPromises()
    expect(document.querySelector('#build-tour-title').textContent).toBe('Level 1 done: First agent')
    expect(document.querySelector('[data-testid="build-tour-next"]').textContent.trim()).toBe('Got it')
    w.unmount()
  })

  it('a sidebar row that is not on screen (a phone) is left out, and the card still shows', async () => {
    onScreen('data-tour', 'levels')
    const steps = [
      { selector: '[data-tour-agent="game-filler"]', title: 'Game filler is yours', body: '' },
      { target: 'levels', title: 'Level 1 done: First agent', body: '' },
    ]
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true, steps, minSteps: 1, skippable: false } })
    await settle()
    expect(document.querySelector('#build-tour-title').textContent).toBe('Level 1 done: First agent')
    expect(w.emitted('close')).toBeUndefined()
    w.unmount()
  })

  it('reopened for the next moment, it never shows the last one', async () => {
    onScreen('data-tour', 'levels')
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true, steps: [{ target: 'levels', title: 'Level 3 done: Connected', body: '' }], minSteps: 1, skippable: false } })
    await settle()
    await w.setProps({ open: false })
    await w.setProps({ open: true, steps: [{ target: 'levels', title: 'Level 4 done: In control', body: '' }] })
    expect(document.querySelector('#build-tour-title')?.textContent).not.toBe('Level 3 done: Connected')
    await settle()
    expect(document.querySelector('#build-tour-title').textContent).toBe('Level 4 done: In control')
    w.unmount()
  })

  it('the Workspace tour still offers Skip', async () => {
    onScreen('data-tour', 'agents')
    onScreen('data-tour', 'chat')
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true } })
    await settle()
    expect(document.querySelector('[data-testid="build-tour-skip"]')).not.toBeNull()
    w.unmount()
  })
})
