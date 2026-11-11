// @vitest-environment jsdom
/**
 * Workspace Build: the way from the Workspace to the Dashboard. A link in the sidebar,
 * beside Build an agent (so only for people who can use the Dashboard), and the first
 * stop of a person's first Show me walk: the page lights that link before the walk opens.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalSidebarBuildLink from '@/components/portal/builder/PortalSidebarBuildLink.vue'
import { WALK_DOOR_SEEN, walkDoorDue, walkDoorSteps } from '@/components/portal/builder/portalBuildTour'

describe('the Dashboard link in the sidebar', () => {
  it('opens the Dashboard in a new tab, so the chat stays open behind it', () => {
    const link = mount(PortalSidebarBuildLink).get('[data-testid="sidebar-dashboard"]')
    expect(link.element.tagName).toBe('A')
    expect(link.attributes('href')).toBe('/')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    expect(link.attributes('data-tour')).toBe('dashboard')
    expect(link.text()).toContain('Dashboard')
  })
})

describe('one door to the guide', () => {
  const has = (w, id) => w.find(`[data-testid="${id}"]`).exists()

  it('shows Build an agent until their guide is in the list, then the guide is the door', () => {
    const before = mount(PortalSidebarBuildLink)
    expect(has(before, 'sidebar-build')).toBe(true)
    const after = mount(PortalSidebarBuildLink, { props: { guideShown: true, profileIncomplete: true } })
    expect(has(after, 'sidebar-build')).toBe(false)
    // The rest of the block stays: the Dashboard, a profile to finish, help.
    expect(has(after, 'sidebar-dashboard')).toBe(true)
    expect(has(after, 'sidebar-profile-incomplete')).toBe(true)
    expect(has(after, 'sidebar-get-help')).toBe(true)
  })

  it('gives someone who cannot build the Dashboard and help, and nothing about building', () => {
    const w = mount(PortalSidebarBuildLink, { props: { canBuild: false, profileIncomplete: true } })
    expect(has(w, 'sidebar-build')).toBe(false)
    expect(has(w, 'sidebar-profile-incomplete')).toBe(false)
    expect(has(w, 'sidebar-dashboard')).toBe(true)
    expect(has(w, 'sidebar-get-help')).toBe(true)
  })
})

describe('the profile line in the sidebar', () => {
  const has = (w, id) => w.find(`[data-testid="${id}"]`).exists()

  it('is red only for questions left unanswered', () => {
    const w = mount(PortalSidebarBuildLink, { props: { profileIncomplete: true } })
    expect(w.get('[data-testid="sidebar-profile-incomplete"]').text()).toContain('Profile incomplete')
    expect(has(w, 'sidebar-profile-note')).toBe(false)
  })

  it('offers the talk about their work once, calmly, then only says the guide is filling it in', async () => {
    const offer = mount(PortalSidebarBuildLink, { props: { profileNote: 'talk' } })
    const line = offer.get('[data-testid="sidebar-profile-note"]')
    expect(line.element.tagName).toBe('BUTTON')
    expect(line.text()).toBe('Profile: tell Atlas about your work')
    expect(line.classes().join(' ')).not.toMatch(/danger/)
    await line.trigger('click')
    expect(offer.emitted('finish-profile')).toHaveLength(1)
    expect(has(offer, 'sidebar-profile-incomplete')).toBe(false)

    const filling = mount(PortalSidebarBuildLink, { props: { profileNote: 'filling' } }).get('[data-testid="sidebar-profile-note"]')
    expect(filling.element.tagName).toBe('P')
    expect(filling.text()).toBe('Profile: Atlas fills it in as you talk')
  })
})

describe('the first stop of a first walk', () => {
  it('lights the Dashboard link and says it is the way there', () => {
    const steps = walkDoorSteps()
    expect(steps).toHaveLength(1)
    expect(steps[0].target).toBe('dashboard')
    expect(steps[0].title).toBe('This is how you get to the Dashboard')
    expect(steps[0].body).toMatch(/always here/)
    expect(steps[0].body).toMatch(/new tab/)
    expect(`${steps[0].title} ${steps[0].body}`).not.toMatch(/—/)
    expect(WALK_DOOR_SEEN).toBe('trinity-walk-door-seen')
  })

  it('is due once, when the link is on screen and nothing else has the page', () => {
    expect(walkDoorDue({ seen: false, linkVisible: true, busy: false })).toBe(true)
    // After the first time the card's link opens the walk straight away.
    expect(walkDoorDue({ seen: true, linkVisible: true, busy: false })).toBe(false)
    // A phone has no sidebar on screen: nothing to light, so the walk just opens.
    expect(walkDoorDue({ seen: false, linkVisible: false, busy: false })).toBe(false)
    expect(walkDoorDue({ seen: false, linkVisible: true, busy: true })).toBe(false)
    expect(walkDoorDue()).toBe(false)
  })
})
