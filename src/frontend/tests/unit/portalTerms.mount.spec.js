// @vitest-environment jsdom
/**
 * trinity-enterprise#1107 ruling A(1) — the terms screen a Workspace portal
 * session accepts on its OWN token, inside the Workspace.
 *
 * `PortalTerms.vue` is mounted against the real `portalHttp` client with a
 * stub adapter, so what is read is the rendered page, the requests it sends —
 * each carrying the portal bearer — and what it emits:
 *  - the title, version and rendered text are shown; the one accept action
 *    stays disabled until the box is ticked, and sends the version shown;
 *  - a successful accept emits `accepted` (the shell re-bootstraps) and leaves
 *    the refusal for that reload to clear (#4515);
 *  - a stale version reloads the text, says so, and asks for a fresh tick;
 *  - a closed invitation offers no accept, only the way out;
 *  - declining emits `decline` — the shell signs the session out.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

import { portalHttp, useClientPortalStore } from '@/stores/clientPortal'
import PortalTerms from '@/components/portal/PortalTerms.vue'

const CURRENT = { required: true, version: '2026-11', title: 'House rules', text: '## Terms\n\nBe kind.', accepted: false, is_default: false }
const calls = []

function serve(handlers) {
  portalHttp.defaults.adapter = async (config) => {
    calls.push({ method: (config.method || 'get').toLowerCase(), url: config.url, auth: config.headers?.Authorization, data: config.data ? JSON.parse(config.data) : null })
    const h = handlers[`${(config.method || 'get').toLowerCase()} ${config.url}`]
    const { status, data } = typeof h === 'function' ? h() : h
    if (status < 400) return { status, data, headers: {}, config, statusText: 'OK' }
    const error = new Error(`HTTP ${status}`)
    error.config = config
    error.response = { status, data, headers: {}, config }
    throw error
  }
}

let store
beforeEach(() => {
  localStorage.clear()
  calls.length = 0
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'portal-tok'
  store.termsRefusal = 'terms_required'
})
afterEach(() => { delete portalHttp.defaults.adapter })

async function mountScreen(handlers = {}) {
  serve({ 'get /api/terms/current': { status: 200, data: CURRENT }, ...handlers })
  const w = mount(PortalTerms)
  await flushPromises()
  return w
}
const box = (w) => w.find('[data-testid="portal-terms-agree"]')
const acceptBtn = (w) => w.find('[data-testid="portal-terms-accept"]')

describe('PortalTerms — reading and accepting on the portal token', () => {
  it('reads the terms with the portal bearer and shows title, version and text', async () => {
    const w = await mountScreen()
    expect(calls[0]).toMatchObject({ method: 'get', url: '/api/terms/current', auth: 'Bearer portal-tok' })
    expect(w.find('[data-testid="portal-terms-title"]').text()).toBe('House rules')
    expect(w.find('[data-testid="portal-terms-version"]').text()).toContain('2026-11')
    expect(w.find('[data-testid="portal-terms-text"]').html()).toContain('Be kind.')
  })

  it('keeps the one accept action disabled until the box is ticked', async () => {
    const w = await mountScreen()
    expect(acceptBtn(w).attributes('disabled')).toBeDefined()
    await box(w).setValue(true)
    expect(acceptBtn(w).attributes('disabled')).toBeUndefined()
    expect(w.findAll('[data-testid="portal-terms-accept"]')).toHaveLength(1)
  })

  it('accepts the version it showed on the portal bearer and emits accepted while still mounted', async () => {
    const w = await mountScreen({ 'post /api/terms/accept': { status: 200, data: { accepted: true, created: true, version: '2026-11', accepted_at: 'x' } } })
    await box(w).setValue(true)
    await acceptBtn(w).trigger('click')
    await flushPromises()
    expect(calls[1]).toMatchObject({ method: 'post', url: '/api/terms/accept', auth: 'Bearer portal-tok', data: { version: '2026-11' } })
    // #4515: the refusal stays set — it is what keeps this screen mounted in
    // `Portal.vue`, so clearing it here would unmount the screen before the
    // emit and drop it. The shell's re-bootstrap clears it with the roster.
    expect(store.termsRefusal).toBe('terms_required')
    expect(w.emitted('accepted')).toHaveLength(1)
  })

  it('a stale version reloads the text, says so, and asks for a fresh tick', async () => {
    let served = 0
    const w = await mountScreen({
      'get /api/terms/current': () => ({ status: 200, data: served++ ? { ...CURRENT, version: '2026-12', text: 'New text.' } : CURRENT }),
      'post /api/terms/accept': { status: 409, data: { detail: { code: 'version_stale', message: 'changed', version: '2026-12' } } },
    })
    await box(w).setValue(true)
    await acceptBtn(w).trigger('click')
    await flushPromises()
    expect(w.emitted('accepted')).toBeUndefined()
    expect(w.find('[data-testid="portal-terms-stale"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-terms-version"]').text()).toContain('2026-12')
    expect(w.find('[data-testid="portal-terms-text"]').html()).toContain('New text.')
    expect(box(w).element.checked).toBe(false)
    expect(acceptBtn(w).attributes('disabled')).toBeDefined()
  })

  it('a failed accept is shown beside the action; nothing is emitted', async () => {
    const w = await mountScreen({ 'post /api/terms/accept': { status: 500, data: { detail: 'boom' } } })
    await box(w).setValue(true)
    await acceptBtn(w).trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(true)
    expect(w.emitted('accepted')).toBeUndefined()
    expect(store.termsRefusal).toBe('terms_required')
  })
})

describe('PortalTerms — ways out', () => {
  it('declining emits decline and sends nothing', async () => {
    const w = await mountScreen()
    await w.find('[data-testid="portal-terms-decline"]').trigger('click')
    expect(w.emitted('decline')).toHaveLength(1)
    expect(calls.filter((c) => c.method === 'post')).toHaveLength(0)
  })

  it('a closed invitation offers no accept, only sign-out', async () => {
    store.termsRefusal = 'invite_revoked'
    const w = await mountScreen()
    expect(w.find('[data-testid="portal-terms-closed"]').exists()).toBe(true)
    expect(acceptBtn(w).exists()).toBe(false)
    expect(w.find('[data-testid="portal-terms-decline"]').exists()).toBe(true)
    expect(calls).toHaveLength(0)
  })

  it('a failed read offers retry and sign-out, never an accept', async () => {
    const w = await mountScreen({ 'get /api/terms/current': { status: 500, data: { detail: 'down' } } })
    expect(w.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(acceptBtn(w).exists()).toBe(false)
    expect(w.find('[data-testid="portal-terms-decline"]').exists()).toBe(true)
  })
})
