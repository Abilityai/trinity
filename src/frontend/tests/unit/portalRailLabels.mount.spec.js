// @vitest-environment jsdom
/**
 * #4347 — the collapsed rail tells its icons apart in words. Each tab button
 * carries its label under the icon (the strip widens 48px → 64px for it), the
 * signal dot's meaning is on the button's hover title and accessible name, and
 * the phone sheet — which has no hover — states each dot's meaning in a line
 * under its tab strip.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import PortalRail from '@/components/portal/PortalRail.vue'
import { RAIL_COLLAPSED } from '@/composables/useColumnResize'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const TABS = [
  { id: 'info', label: 'Info', icon: 'info' },
  { id: 'files', label: 'Files', icon: 'paperclip' },
  { id: 'work', label: 'Work', icon: 'bolt' },
]
const SIGNALS = { info: { updated: true, note: '2 suggestions' }, work: { live: 1, agents: ['scout'] } }
const mountRail = (props = {}) => mount(PortalRail, {
  attachTo: document.body,
  props: { tabs: TABS, activeTab: 'info', open: false, signals: SIGNALS, participants: ['scout'], ...props },
})

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
})

describe('#4347 — the collapsed rail labels its icons', () => {
  it('prints each tab\'s label under its icon', () => {
    const w = mountRail()
    for (const t of TABS) {
      const btn = w.get(`[data-testid="portal-rail-tab-${t.id}"]`)
      expect(btn.get('[data-rail-tab-label]').text()).toBe(t.label)
    }
    w.unmount()
  })

  it('is 64px wide, and the shell reserves the same width', () => {
    expect(RAIL_COLLAPSED).toBe(64)
    const w = mountRail()
    expect(w.get('aside').classes()).toContain('w-16')
    w.unmount()
  })

  it('says what a dot means on hover and to a screen reader', () => {
    const w = mountRail()
    const info = w.get('[data-testid="portal-rail-tab-info"]')
    expect(info.attributes('title')).toContain('Info · 2 suggestions')
    expect(info.attributes('aria-label')).toContain('2 suggestions')
    expect(w.get('[data-testid="portal-rail-tab-work"]').attributes('title')).toContain('Work · 1 running')
    // A tab with nothing to say has no meaning to state.
    expect(w.get('[data-testid="portal-rail-tab-files"]').attributes('title')).not.toContain('·  ·')
    w.unmount()
  })
})

describe('#4347 — the sheet states what its dots mean', () => {
  it('lists every signalled tab\'s meaning under the strip', () => {
    const w = mountRail({ sheet: true })
    const notes = w.get('[data-testid="portal-rail-signal-notes"]').text()
    expect(notes).toContain('Info · 2 suggestions')
    expect(notes).toContain('Work · 1 running')
    expect(notes).not.toContain('Files')
    w.unmount()
  })

  it('has no line when nothing carries a signal', () => {
    const w = mountRail({ sheet: true, signals: {} })
    expect(w.find('[data-testid="portal-rail-signal-notes"]').exists()).toBe(false)
    w.unmount()
  })
})
