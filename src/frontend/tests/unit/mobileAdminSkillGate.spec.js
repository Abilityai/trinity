// @vitest-environment jsdom
/**
 * trinity#3274 — `/m` chat: a request naming a gated skill is "waiting for
 * approval", not a 10-minute poll of `/executions/undefined`.
 *
 * `/task` answers 202 `pending_approval` with no execution. The chat shows the
 * server's message as a platform line, polls nothing, and keeps the held
 * request out of the history it sends with the next message (resent, the
 * command would ask for an approval on every later turn). A refusal is named.
 *
 * MOUNTED through `sendChatMessage`, with `boundedHttp` stubbed by URL (the
 * `mobileAdminNotAddressee.spec.js` harness).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'agents' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/utils/boundedHttp'
import { useAuthStore } from '@/stores/auth'
import MobileAdmin from '@/views/MobileAdmin.vue'

const NOTICE = 'Not run: the skill pay-invoice on fin needs approval before it can run. ' +
  'Request gate-abc is waiting for a decision.'
const REFUSED = 'The skill pay-invoice is not installed on fin.'

let wrapper

async function mountView(post) {
  http.get.mockImplementation(async (url) => {
    if (url === '/api/operator-queue') return { data: { items: [], count: 0 } }
    if (url === '/api/notifications') return { data: { items: [], count: 0 } }
    return { data: [] }
  })
  http.post.mockImplementation(post)
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
  await flushPromises()
  wrapper.vm.chatAgent = 'fin'
  return wrapper
}

async function send(w, text) {
  w.vm.chatInput = text
  await w.vm.sendChatMessage()
  await flushPromises()
}

const polled = () => http.get.mock.calls.map(c => c[0]).filter(u => /\/executions\//.test(u))

describe('/m chat — a gated skill', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('shows the notice as a platform line and polls nothing', async () => {
    const w = await mountView(async () => ({ status: 202, data: { status: 'pending_approval', message: NOTICE } }))
    await send(w, '/pay-invoice 100 EUR')
    expect(w.find('[data-testid="chat-system-line"]').text()).toContain(NOTICE)
    expect(polled()).toEqual([])
  })

  it('leaves the held request out of the next message\'s history', async () => {
    const w = await mountView(async () => ({ status: 202, data: { status: 'pending_approval', message: NOTICE } }))
    await send(w, '/pay-invoice 100 EUR')
    await send(w, 'what is on today?')
    const second = http.post.mock.calls[1][1]
    expect(second.message).not.toContain('/pay-invoice')
    expect(second.message).not.toContain(NOTICE)
  })

  it('names a refusal', async () => {
    const w = await mountView(async () => {
      throw Object.assign(new Error('Request failed with status code 409'), {
        response: { status: 409, headers: { 'x-trinity-error-code': 'gated_skill_not_installed' },
          data: { detail: { status: 'refused', code: 'gated_skill_not_installed', message: REFUSED } } },
      })
    })
    await send(w, '/pay-invoice 100 EUR')
    expect(w.text()).toContain(`Error: ${REFUSED}`)
  })
})
