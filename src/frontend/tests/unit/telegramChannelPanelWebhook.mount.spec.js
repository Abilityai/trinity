// @vitest-environment jsdom
/**
 * trinity-enterprise#1189 — the Telegram panel's delivery warning.
 *  - after Verify, the warning follows Verify's answer (Telegram's own record),
 *    including when the stored webhook URL is still set;
 *  - no Public URL: admins get the Settings link, everyone else is told to ask
 *    an admin (Settings → General is admin-only);
 *  - Public URL set but registration failing: no "set the Public URL" advice;
 *  - a Verify result never outlives the bot it was about.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { ref } from 'vue'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const role = vi.hoisted(() => ({ admin: false }))
vi.mock('@/composables/useRole', () => ({ useRole: () => ({ isAdmin: ref(role.admin) }) }))

import TelegramChannelPanel from '@/components/TelegramChannelPanel.vue'

const AGENT = 'scout'
const BOUND = (over) => ({
  agent_name: AGENT, configured: true, bot_username: 'probe_bot', bot_link: 'https://t.me/probe_bot',
  webhook_url: null, public_url_set: false, group_count: 0, progress_indicator_enabled: true, ...over,
})

let binding
beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  role.admin = false
  binding = BOUND()
  api.get.mockImplementation((url) =>
    Promise.resolve({ data: url.endsWith('/groups') ? [] : binding }))
})

async function render() {
  const w = mount(TelegramChannelPanel, {
    props: { agentName: AGENT },
    global: { stubs: { VoiceChannelToggle: true, 'router-link': { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
  })
  await flushPromises()
  return w
}
const warning = (w) => w.find('[data-testid="telegram-webhook-warning"]')

async function verify(w, data, fresh) {
  api.post.mockResolvedValueOnce({ data })
  if (fresh) binding = fresh
  await w.findAll('button').find((b) => b.text() === 'Verify').trigger('click')
  await flushPromises()
}

describe('TelegramChannelPanel webhook warning', () => {
  it('no Public URL, non-admin: asks an admin, no dead Settings link', async () => {
    const w = await render()
    expect(warning(w).exists()).toBe(true)
    expect(warning(w).text()).toContain('Ask an admin')
    expect(warning(w).find('a[href^="/settings"]').exists()).toBe(false)
  })

  it('no Public URL, admin: links to Settings → General', async () => {
    role.admin = true
    const w = await render()
    expect(warning(w).find('a[href="/settings?tab=general"]').exists()).toBe(true)
  })

  it('shows Verify’s failure even when a webhook URL is still stored', async () => {
    binding = BOUND({ webhook_url: 'https://x.example.com/api/telegram/webhook/s', public_url_set: true })
    const w = await render()
    expect(warning(w).exists()).toBe(false)
    const reason = 'Telegram cannot deliver to https://x.example.com: certificate verify failed'
    await verify(w, { ok: false, webhook_registered: false, webhook_error: reason })
    expect(warning(w).exists()).toBe(true)
    expect(warning(w).text()).toContain(reason)
    expect(warning(w).text()).not.toContain('Set it under')
  })

  it('hides the warning when Verify says Telegram can deliver', async () => {
    binding = BOUND({ public_url_set: true })
    const w = await render()
    expect(warning(w).exists()).toBe(true)
    await verify(w, { ok: true, webhook_registered: true, webhook_error: null }, BOUND({ public_url_set: true }))
    expect(warning(w).exists()).toBe(false)
  })

  it('clears the Verify result on disconnect and on connect', async () => {
    const w = await render()
    await verify(w, { ok: false, webhook_registered: false, webhook_error: 'old bot reason' })
    expect(w.find('[data-testid="telegram-verify-result"]').exists()).toBe(true)

    api.delete.mockResolvedValueOnce({ data: { ok: true } })
    await w.findAll('button').find((b) => b.text() === 'Disconnect').trigger('click')
    await flushPromises()

    api.put.mockResolvedValueOnce({ data: BOUND({ bot_username: 'new_bot' }) })
    await w.find('input[type="password"]').setValue('1:abc')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledOnce()
    expect(w.find('[data-testid="telegram-verify-result"]').exists()).toBe(false)
    expect(w.text()).not.toContain('old bot reason')
  })
})
