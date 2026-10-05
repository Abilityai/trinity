// @vitest-environment jsdom
/**
 * ent#621 (follow-up) — the rail's shortcut tips panel, mounted.
 *
 * A small panel pinned to the bottom of the OPEN rail: four headline chords
 * and a button that opens the full shortcuts dialog. The collapsed strip
 * carries the same door as one icon; the mobile sheet carries neither. It is
 * dismissible, and the rail only REPORTS the dismissal — the shell owns the
 * memory (`workspaceKeymap.mount.spec.js`).
 *
 * Mounted through `PortalRail`, not alone: where it sits (after the body, in
 * the open form only) is the rail's decision and half of what is promised.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import PortalRail from '@/components/portal/PortalRail.vue'
import { keyTipRows, keyHint, hostPlatform } from '@/components/portal/portalKeymap'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const P = hostPlatform()
const TABS = [
  { id: 'work', label: 'Work', signal: 'work' },
  { id: 'info', label: 'Info', signal: 'info' },
]
const mountRail = (props = {}) => mount(PortalRail, {
  attachTo: document.body,
  props: { tabs: TABS, activeTab: 'work', open: true, keyTips: true, participants: ['scout'], ...props },
})
const panel = (w) => w.find('[data-testid="ws-key-tips"]')

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
})

describe('ent#621 (follow-up) — the tips panel in the open rail', () => {
  it('lists the four headline chords, read from the map', () => {
    const w = mountRail()
    expect(panel(w).exists()).toBe(true)
    const rows = panel(w).findAll('[data-ws-key-tip]')
    const want = keyTipRows(P)
    expect(rows.map((r) => r.attributes('data-ws-key-tip'))).toEqual(want.map((r) => r.id))
    for (const [i, row] of rows.entries()) {
      expect(row.text()).toContain(want[i].label)
      expect(row.find('kbd').text()).toBe(want[i].keys)
    }
    // The search row is the chord the sidebar field shows, not a second spelling.
    expect(panel(w).text()).toContain(keyHint('search-focus', P))
    w.unmount()
  })

  it('sits AFTER the body, so it is the bottom of the rail and never over its content', () => {
    const w = mountRail()
    const body = w.find('[data-testid="portal-rail-body"]').element
    const tips = panel(w).element
    expect(body.compareDocumentPosition(tips) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // A sibling of the scroll area, not a child: it does not scroll away.
    expect(body.contains(tips)).toBe(false)
    w.unmount()
  })

  it('its button asks the shell for the full list', async () => {
    const w = mountRail()
    await panel(w).find('[data-testid="ws-key-tips-all"]').trigger('click')
    expect(w.emitted('open-keys')).toHaveLength(1)
    w.unmount()
  })

  it('its close reports the dismissal and names what it does', async () => {
    const w = mountRail()
    const close = panel(w).find('[data-testid="ws-key-tips-close"]')
    expect(close.attributes('aria-label')).toBe('Hide shortcut tips')
    await close.trigger('click')
    expect(w.emitted('dismiss-key-tips')).toHaveLength(1)
    w.unmount()
  })

  it('is gone once the shell says it was dismissed', () => {
    const w = mountRail({ keyTips: false })
    expect(panel(w).exists()).toBe(false)
    w.unmount()
  })
})

describe('ent#621 (follow-up) — the other two rail forms', () => {
  it('collapsed: one icon at the foot of the strip opens the list', async () => {
    const w = mountRail({ open: false })
    expect(panel(w).exists()).toBe(false)          // 48px holds no panel
    const btn = w.find('[data-testid="portal-rail-keys"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('title')).toContain(keyHint('key-list', P))
    await btn.trigger('click')
    expect(w.emitted('open-keys')).toHaveLength(1)
    w.unmount()
  })

  it('collapsed and dismissed: the icon goes with the panel', () => {
    const w = mountRail({ open: false, keyTips: false })
    expect(w.find('[data-testid="portal-rail-keys"]').exists()).toBe(false)
    w.unmount()
  })

  it('the mobile sheet shows neither — there is no keyboard to hint at', () => {
    const w = mountRail({ sheet: true })
    expect(panel(w).exists()).toBe(false)
    expect(w.find('[data-testid="portal-rail-keys"]').exists()).toBe(false)
    w.unmount()
  })
})
