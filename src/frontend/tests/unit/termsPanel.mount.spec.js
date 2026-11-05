// @vitest-environment jsdom
/**
 * trinity-enterprise#1107 — the terms editor on Settings › Access, and the
 * terms page's way back after declining.
 *
 * Mounted against a stubbed `api`, so what is read is the rendered panel, the
 * requests it sends and what it refuses to send:
 *  - the current title/text/version load into the editor, with the state badge;
 *  - an emptied text is refused by name next to the field and the save is not sent;
 *  - a changed text is saved through `PUT /api/terms` and the record reloads;
 *  - restore-default is confirmed first, then sent;
 *  - the switch posts `required` on its own;
 *  - the acceptance list shows account, version, time, and the total.
 * And on the terms page: declining signs out and the copy says how to come back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }))
vi.mock('axios', () => ({ default: ax }))

import TermsPanel from '@/components/settings/TermsPanel.vue'
import TermsAcceptance from '@/views/TermsAcceptance.vue'
import { useAuthStore } from '@/stores/auth'
import { termsClientError, TERMS_TEXT_MAX_BYTES } from '@/utils/termsEditor'

const CURRENT = () => ({
  required: true, version: 'default-1', title: 'Terms of use', text: '## Terms\n\nBe kind.',
  is_default: true, accepted: true,
})
const ACCEPTANCES = () => [
  { id: 1, user_id: 7, email: 'lee@example.com', terms_version: 'default-1', accepted_at: '2026-11-01T09:00:00Z', door: null },
  { id: 2, user_id: 8, email: null, terms_version: 'default-1', accepted_at: '2026-11-02T09:00:00Z', door: 'inv_2' },
]

function stubApi(current = CURRENT()) {
  api.get.mockImplementation((url) => {
    if (url === '/api/terms/current') return Promise.resolve({ data: current })
    if (url === '/api/terms/acceptances') return Promise.resolve({ data: ACCEPTANCES() })
    return Promise.reject(new Error(url))
  })
  api.put.mockImplementation((url, body) => Promise.resolve({
    data: { ...current, ...('text' in body ? { text: body.text, is_default: false, version: '2026-11-04T10:00:00Z' } : {}),
            ...('title' in body && body.title != null ? { title: body.title } : {}),
            ...('required' in body ? { required: body.required } : {}) },
  }))
  api.post.mockImplementation((url) => (url === '/api/terms/restore-default'
    ? Promise.resolve({ data: CURRENT() })
    : Promise.reject(new Error(url))))
}

async function mountPanel() {
  const wrapper = mount(TermsPanel, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of [api.get, api.post, api.put, ax.post]) f.mockReset()
  stubApi()
})

describe('TermsPanel — the editor', () => {
  it('loads the current text, version and state into the editor', async () => {
    const w = await mountPanel()
    expect(w.find('[data-testid="terms-panel-state"]').text()).toContain('shipped default')
    expect(w.find('[data-testid="terms-panel-version"]').text()).toContain('default-1')
    expect(w.find('textarea').element.value).toBe('## Terms\n\nBe kind.')
    expect(w.find('[data-testid="terms-panel-save"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="terms-panel-restore"]').attributes('disabled')).toBeDefined()
  })

  it('refuses an emptied text by name and does not send it', async () => {
    const w = await mountPanel()
    await w.find('textarea').setValue('   ')
    expect(w.text()).toContain('The text is empty.')
    expect(w.find('[data-testid="terms-panel-save"]').attributes('disabled')).toBeDefined()
    await w.find('[data-testid="terms-panel-save"]').trigger('click')
    expect(api.put).not.toHaveBeenCalled()
  })

  it('saves a changed text as a new version and reloads the record', async () => {
    const w = await mountPanel()
    api.get.mockClear()
    await w.find('textarea').setValue('## Ours\n\nNew rules.')
    expect(w.find('[data-testid="terms-panel-save"]').attributes('disabled')).toBeUndefined()
    await w.find('[data-testid="terms-panel-save"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/terms', { title: 'Terms of use', text: '## Ours\n\nNew rules.' })
    expect(w.find('[data-testid="terms-panel-version"]').text()).toContain('2026-11-04T10:00:00Z')
    expect(w.find('[data-testid="terms-panel-state"]').text()).toContain('custom text')
    expect(w.find('[data-testid="terms-panel-saved"]').text()).toContain('new version')
    expect(api.get).toHaveBeenCalledWith('/api/terms/acceptances')
  })

  it('restores the default only after the confirmation', async () => {
    stubApi({ ...CURRENT(), is_default: false, text: 'Custom.', version: '2026-11-04T10:00:00Z' })
    const w = await mountPanel()
    await w.find('[data-testid="terms-panel-restore"]').trigger('click')
    await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    const confirm = document.body.querySelector('[data-testid="confirm-dialog-confirm"], button.confirm, [data-confirm]')
      || [...document.body.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Restore the default' && !w.find('[data-testid="terms-panel-restore"]').element.isSameNode(b))
    expect(confirm).toBeTruthy()
    confirm.click()
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/terms/restore-default')
    expect(w.find('[data-testid="terms-panel-state"]').text()).toContain('shipped default')
    w.unmount()
  })

  it('keeps an unsaved draft when the switch is toggled', async () => {
    // Merge-train finding on ent#1107: `watch(terms, adopt)` re-adopted the
    // server's text on every store update, so a switch save while a draft
    // was being written wiped the draft.
    const w = await mountPanel()
    await w.find('textarea').setValue('## Draft in progress')
    expect(w.find('[data-testid="terms-panel-discard"]').exists()).toBe(true)
    await w.find('[data-testid="terms-panel-required"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/terms', { required: false })
    expect(w.find('textarea').element.value).toBe('## Draft in progress')
    expect(w.find('[data-testid="terms-panel-save"]').attributes('disabled')).toBeUndefined()
  })

  it('puts the toggle back when the switch save fails', async () => {
    const w = await mountPanel()
    api.put.mockRejectedValueOnce({ response: { status: 500, data: { detail: { message: 'Saving failed.' } } } })
    await w.find('[data-testid="terms-panel-required"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/terms', { required: false })
    expect(w.find('[data-testid="terms-panel-required"]').attributes('aria-checked')).toBe('true')
    expect(w.text()).toContain('Saving failed.')
  })

  it('posts the switch on its own', async () => {
    const w = await mountPanel()
    await w.find('[data-testid="terms-panel-required"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/terms', { required: false })
  })

  it('lists who accepted which version, with the total', async () => {
    const w = await mountPanel()
    const rows = w.findAll('[data-testid="terms-panel-acceptance"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('lee@example.com')
    expect(rows[0].text()).toContain('default-1')
    expect(rows[1].text()).toContain('user #8')
    expect(w.find('[data-testid="terms-panel-acceptances-total"]').text()).toBe('2 acceptances')
  })
})

describe('termsClientError — the named refusals', () => {
  it('names empty, oversized and over-long inputs, in that order', () => {
    expect(termsClientError({ title: 'x', text: '' }).field).toBe('text')
    expect(termsClientError({ title: 'x', text: 'y'.repeat(TERMS_TEXT_MAX_BYTES + 1) }).message).toContain('over 200 KB')
    expect(termsClientError({ title: 't'.repeat(201), text: 'ok' }).field).toBe('title')
    expect(termsClientError({ title: 'Terms', text: 'ok' })).toBeNull()
  })
})

describe('TermsAcceptance — declining', () => {
  it('signs out and says how to come back', async () => {
    const Blank = { template: '<div />' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/terms', component: TermsAcceptance },
        { path: '/login', component: Blank },
        { path: '/', component: Blank },
      ],
    })
    await router.push('/terms')
    await router.isReady()
    const auth = useAuthStore()
    auth.user = { username: 'admin', email: 'admin@example.com', role: 'admin' }
    auth.isAuthenticated = true
    auth.token = 'tok'
    const logout = vi.spyOn(auth, 'logout').mockResolvedValue()
    stubApi({ ...CURRENT(), accepted: false })

    const w = mount(TermsAcceptance, { global: { plugins: [router] } })
    await flushPromises()
    expect(w.find('[data-testid="terms-title"]').text()).toBe('Terms of use')
    expect(w.text()).toContain('sign in again and accept the terms')
    await w.find('[data-testid="terms-decline"]').trigger('click')
    await flushPromises()
    expect(logout).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.path).toBe('/login')
  })
})
