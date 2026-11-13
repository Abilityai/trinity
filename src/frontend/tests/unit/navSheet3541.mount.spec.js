// @vitest-environment jsdom
/**
 * #3541 — below 640px the nav bar's link row is not displayed, so the phone
 * sheet is the only way to reach Library, Operations, Settings or Workspace.
 * Mounted, not grepped: the issue's acceptance criteria are about what is
 * rendered — every entitled link, the active page, the Operations badge.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import NavSheet from '../../src/components/NavSheet.vue'
import { buildNavLinks, menuBadge } from '../../src/utils/navLinks.js'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }],
})

const mounted = []
afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount()
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

async function render(links) {
  const w = mount(NavSheet, {
    props: { links },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  mounted.push(w)
  await nextTick()
  return w
}

const trigger = () => document.querySelector('[data-nav-sheet-trigger]')
const sheet = () => document.querySelector('[data-nav-sheet]')

async function tap(el) {
  el.click()
  await nextTick()
  await nextTick()
}

/** Render, then open the sheet the way a person does: tap the menu button. */
async function open(links) {
  const w = await render(links)
  await tap(trigger())
  return w
}

const items = () => [...document.querySelectorAll('[data-nav-sheet-item]')]
const text = (el) => el.textContent.replace(/\s+/g, ' ').trim()

describe('NavSheet (#3541)', () => {
  it('lists every entitled link, Enterprise included when entitled', async () => {
    await open(buildNavLinks({ path: '/', hasAnyEnterprise: true }))
    expect(items().map((el) => el.getAttribute('href'))).toEqual([
      '/', '/library', '/operations', '/settings', '/workspace', '/enterprise',
    ])
  })

  it('omits Enterprise on an OSS build', async () => {
    await open(buildNavLinks({ path: '/' }))
    expect(items().map(text)).not.toContain('Enterprise')
  })

  it('marks the current page with aria-current, and only that one', async () => {
    await open(buildNavLinks({ path: '/library' }))
    const current = items().filter((el) => el.getAttribute('aria-current') === 'page')
    expect(current.map(text)).toEqual(['Library'])
  })

  it('shows the Operations badge, critical colour when anything is critical', async () => {
    await open(buildNavLinks({ path: '/', opsCount: 7, opsCritical: true }))
    const ops = items().find((el) => el.getAttribute('href') === '/operations')
    const badge = ops.querySelector('.rounded-full')
    expect(badge.textContent.trim()).toBe('7')
    expect(badge.className).toContain('bg-status-danger-600')
  })

  it('keeps Workspace opening in a new tab, as the row does', async () => {
    await open(buildNavLinks({ path: '/' }))
    const ws = items().find((el) => el.getAttribute('href') === '/workspace')
    expect(ws.getAttribute('target')).toBe('_blank')
    expect(ws.getAttribute('rel')).toBe('noopener')
  })

  it('starts shut, and the menu button opens it', async () => {
    await render(buildNavLinks({ path: '/' }))
    expect(sheet()).toBeNull()
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
    await tap(trigger())
    expect(sheet()).not.toBeNull()
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
  })

  it('closes on a link tap, on the close button and on Escape', async () => {
    const links = buildNavLinks({ path: '/' })
    await open(links)
    await tap(items()[2])
    expect(sheet()).toBeNull()

    await tap(trigger())
    await tap(document.querySelector('[aria-label="Close navigation"]'))
    expect(sheet()).toBeNull()

    await tap(trigger())
    sheet().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(sheet()).toBeNull()
  })

  it('carries the Operations badge on the menu button while shut', async () => {
    await render(buildNavLinks({ path: '/', opsCount: 4 }))
    const badge = document.querySelector('[data-nav-sheet-badge]')
    expect(badge.textContent.trim()).toBe('4')
    expect(trigger().getAttribute('aria-label')).toBe('Open navigation, 4 pending in Operations')
  })

  it('shows no badge with nothing pending', async () => {
    await render(buildNavLinks({ path: '/' }))
    expect(document.querySelector('[data-nav-sheet-badge]')).toBeNull()
    expect(trigger().getAttribute('aria-label')).toBe('Open navigation')
  })
  it('is named by its heading', async () => {
    await open(buildNavLinks({ path: '/' }))
    const id = sheet().getAttribute('aria-labelledby')
    expect(document.getElementById(id).textContent.trim()).toBe('Navigation')
  })

  it('closes when the window widens past sm, releasing the scroll lock', async () => {
    let fire = null
    vi.stubGlobal('matchMedia', (query) => ({
      matches: false,
      media: query,
      addEventListener: (_t, fn) => { fire = fn },
      removeEventListener: () => { fire = null },
    }))
    try {
      await open(buildNavLinks({ path: '/' }))
      expect(sheet()).not.toBeNull()
      fire({ matches: true })
      await nextTick()
      await nextTick()
      expect(sheet()).toBeNull()
      expect(document.body.style.overflow).toBe('')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('menuBadge (#3541)', () => {
  it('is null with nothing pending', () => {
    expect(menuBadge(buildNavLinks({ path: '/' }))).toBeNull()
    expect(menuBadge()).toBeNull()
  })

  it('carries the Operations count and its criticality', () => {
    expect(menuBadge(buildNavLinks({ path: '/', opsCount: 3 }))).toEqual({ text: '3', critical: false })
    expect(menuBadge(buildNavLinks({ path: '/', opsCount: 250, opsCritical: true }))).toEqual({ text: '99+', critical: true })
  })
})
