// @vitest-environment jsdom
/**
 * trinity-enterprise#625 — the Workspace theme switch.
 *
 * The first spec in this suite that MOUNTS a component. Every other portal
 * spec is pure rules + source-structure guards, because `vitest.config.js`
 * pins `environment: 'node'`; the AC here asks for a click that reaches
 * `setTheme`, and a regex over the SFC cannot prove a click (#2829's rule),
 * so this file opts into jsdom per-file and drives the real component with
 * `@vue/test-utils`. The pure rules and the placement guards keep the house
 * shape alongside.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  THEME_IDS,
  THEME_OPTIONS,
  normalizeTheme,
  resolvedTheme,
  themeSwitchAriaLabel,
  themeSwitchIcon,
  themeSwitchLabel,
} from '../../src/utils/themeSwitch'
import PortalThemeSwitch from '../../src/components/portal/PortalThemeSwitch.vue'
import ThemeChoice from '../../src/components/base/ThemeChoice.vue'
import { useThemeStore } from '../../src/stores/theme'

const src = (rel) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))

// jsdom has no matchMedia; the store consults it for `system`. Answer "dark".
function stubMatchMedia(matches) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }))
}

describe('themeSwitch rules', () => {
  it('offers exactly the three choices the store accepts, in display order', () => {
    expect(THEME_IDS).toEqual(['light', 'dark', 'system'])
    expect(THEME_OPTIONS.map((o) => o.label)).toEqual(['Light', 'Dark', 'System'])
  })

  it('an unknown choice reads as system (the store default)', () => {
    expect(normalizeTheme('sepia')).toBe('system')
    expect(normalizeTheme(undefined)).toBe('system')
    expect(normalizeTheme('dark')).toBe('dark')
  })

  it('system resolves against the OS answer; an explicit choice ignores it', () => {
    expect(resolvedTheme('system', true)).toBe('dark')
    expect(resolvedTheme('system', false)).toBe('light')
    expect(resolvedTheme('light', true)).toBe('light')
  })

  it('the label shows the resolved state honestly under system (AC #3)', () => {
    expect(themeSwitchLabel('system', true)).toBe('System · dark')
    expect(themeSwitchLabel('system', false)).toBe('System · light')
    expect(themeSwitchLabel('dark', false)).toBe('Dark')
    expect(themeSwitchAriaLabel('system', true)).toBe('Theme: System · dark')
  })

  it('the trigger icon follows the resolved theme, never the choice', () => {
    expect(themeSwitchIcon('system', true)).toBe('moon')
    expect(themeSwitchIcon('system', false)).toBe('sun')
    expect(themeSwitchIcon('light', true)).toBe('sun')
  })
})

describe('PortalThemeSwitch (mounted)', () => {
  let store
  beforeEach(() => {
    localStorage.clear()
    stubMatchMedia(true)
    setActivePinia(createPinia())
    store = useThemeStore()
    store.initTheme()
  })

  function mountSwitch() {
    return mount(PortalThemeSwitch, { attachTo: document.body })
  }

  it('renders with nothing stored as "System · dark" and names itself for a screen reader', () => {
    const w = mountSwitch()
    const trigger = w.get('[data-testid="portal-theme-switch"]')
    expect(store.theme).toBe('system')
    expect(trigger.text()).toBe('System · dark')
    expect(trigger.attributes('aria-label')).toBe('Theme: System · dark')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(w.find('[data-testid="portal-theme-menu"]').exists()).toBe(false)
    w.unmount()
  })

  it('opens to three radio options with the current one checked', async () => {
    const w = mountSwitch()
    await w.get('[data-testid="portal-theme-switch"]').trigger('click')
    const radios = w.findAll('[role="radio"]')
    expect(radios).toHaveLength(3)
    expect(radios.map((r) => r.attributes('data-theme-option'))).toEqual(['light', 'dark', 'system'])
    expect(radios.map((r) => r.attributes('aria-checked'))).toEqual(['false', 'false', 'true'])
    expect(w.get('[data-testid="portal-theme-switch"]').attributes('aria-expanded')).toBe('true')
    w.unmount()
  })

  it('clicking an option calls setTheme with that value, closes, and the trigger reflects it', async () => {
    const w = mountSwitch()
    const setTheme = vi.spyOn(store, 'setTheme')
    await w.get('[data-testid="portal-theme-switch"]').trigger('click')
    await w.get('[data-theme-option="dark"]').trigger('click')
    expect(setTheme).toHaveBeenCalledWith('dark')
    expect(store.theme).toBe('dark')
    // One store, one key: the platform NavBar reads the same value.
    expect(localStorage.getItem('trinity-theme')).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    await nextTick()
    expect(w.find('[data-testid="portal-theme-menu"]').exists()).toBe(false)
    expect(w.get('[data-testid="portal-theme-switch"]').text()).toBe('Dark')
    w.unmount()
  })

  it('a choice made elsewhere (the NavBar) is reflected here — no second store', async () => {
    const w = mountSwitch()
    store.setTheme('light')
    await nextTick()
    expect(w.get('[data-testid="portal-theme-switch"]').text()).toBe('Light')
    await w.get('[data-testid="portal-theme-switch"]').trigger('click')
    expect(w.get('[data-theme-option="light"]').attributes('aria-checked')).toBe('true')
    w.unmount()
  })

  it('Escape and a click outside close the menu', async () => {
    const w = mountSwitch()
    await w.get('[data-testid="portal-theme-switch"]').trigger('click')
    expect(w.find('[data-testid="portal-theme-menu"]').exists()).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(w.find('[data-testid="portal-theme-menu"]').exists()).toBe(false)

    await w.get('[data-testid="portal-theme-switch"]').trigger('click')
    document.body.click()
    await nextTick()
    expect(w.find('[data-testid="portal-theme-menu"]').exists()).toBe(false)
    w.unmount()
  })
})

describe('ThemeChoice (mounted)', () => {
  it('arrow keys move the selection like a native radio group', async () => {
    const w = mount(ThemeChoice, { props: { theme: 'light' } })
    await w.get('[role="radiogroup"]').trigger('keydown', { key: 'ArrowRight' })
    expect(w.emitted('select')).toEqual([['dark']])
    const w2 = mount(ThemeChoice, { props: { theme: 'light' } })
    await w2.get('[role="radiogroup"]').trigger('keydown', { key: 'ArrowLeft' })
    expect(w2.emitted('select')).toEqual([['system']])
  })

  it('only the checked option is in the tab order (roving tabindex)', () => {
    const w = mount(ThemeChoice, { props: { theme: 'dark' } })
    expect(w.findAll('[role="radio"]').map((r) => r.attributes('tabindex'))).toEqual(['-1', '0', '-1'])
  })
})

describe('placement and sharing (source guards)', () => {
  const CONV = src('../../src/components/portal/PortalConversation.vue')
  const ROOM = src('../../src/components/portal/PortalRoom.vue')
  const PORTAL = src('../../src/views/Portal.vue')
  const NAVBAR = src('../../src/components/NavBar.vue')
  const SWITCH = src('../../src/components/portal/PortalThemeSwitch.vue')

  it('both column headers end with the header-end slot, inside <header>', () => {
    for (const file of [CONV, ROOM]) {
      const header = file.slice(file.indexOf('<header'), file.indexOf('</header>'))
      expect(header).toContain('<slot name="header-end" />')
    }
  })

  it('the shell fills the slot on the conversation AND the room', () => {
    const fills = PORTAL.split('<template #header-end>').length - 1
    expect(fills).toBe(2)
    expect(PORTAL).toContain("import PortalThemeSwitch from '@/components/portal/PortalThemeSwitch.vue'")
  })

  it('the switch depends on the theme store only — nothing a client session lacks (AC #4)', () => {
    expect(SWITCH).toContain("from '@/stores/theme'")
    expect(SWITCH).not.toMatch(/stores\/(auth|agents|clientPortal)/)
  })

  it('the NavBar consumes the same ThemeChoice primitive — one picker, not two', () => {
    expect(NAVBAR).toContain("import ThemeChoice from './base/ThemeChoice.vue'")
    expect(NAVBAR).toContain('<ThemeChoice :theme="themeStore.theme"')
    expect(NAVBAR).not.toContain("@click=\"setTheme('light')\"")
  })
})
