// @vitest-environment jsdom
/**
 * The feature tour reuses the Workspace tour's component with its own stops. A stop
 * whose part isn't on screen (a nav link collapsed into "N more", or the whole nav on a
 * phone) is left out, and with fewer than two left there is no tour at all.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import PortalBuildTour from '@/components/portal/builder/PortalBuildTour.vue'
import { featureTourSteps } from '@/components/onboarding/featureTour'

function onScreen(...targets) {
  for (const target of targets) {
    const el = document.createElement('div')
    el.setAttribute('data-tour', target)
    el.getBoundingClientRect = () => ({ left: 10, top: 10, width: 120, height: 32, right: 130, bottom: 42 })
    document.body.appendChild(el)
  }
}

// The tour waits a frame before picking its stops, so parts that measure themselves settle.
const settle = async () => { await new Promise((done) => setTimeout(done, 40)); await flushPromises() }

beforeEach(() => { document.body.innerHTML = '' })

describe('the feature tour, mounted', () => {
  it('shows only the stops whose part is on screen', async () => {
    onScreen('view-modes', 'nav-operations', 'nav-settings')
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true, steps: featureTourSteps() } })
    await settle()
    expect(document.querySelector('[data-testid="build-tour-progress"]').textContent.trim()).toBe('1 of 3')
    expect(document.querySelector('#build-tour-title').textContent).toBe('Your agents')
    expect(w.emitted('close')).toBeUndefined()
    w.unmount()
  })

  it('a part that leaves the screen after the tour started is skipped, not pointed at', async () => {
    onScreen('view-modes', 'nav-operations', 'nav-settings')
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true, steps: featureTourSteps() } })
    await settle()
    // The nav measures itself after the tour picked its stops, and folds Operations away.
    document.querySelector('[data-tour="nav-operations"]').getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 })
    document.querySelector('[data-testid="build-tour-next"]').click()
    await flushPromises()
    expect(document.querySelector('#build-tour-title').textContent).toBe('Settings')
    expect(document.querySelector('[data-testid="build-tour-progress"]').textContent.trim()).toBe('2 of 2')
    w.unmount()
  })

  it('with only one part on screen there is no tour, and it closes straight away', async () => {
    onScreen('view-modes')
    const w = mount(PortalBuildTour, { attachTo: document.body, props: { open: true, steps: featureTourSteps() } })
    await settle()
    // The parent closes it on this event, in the same tick.
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })
})
