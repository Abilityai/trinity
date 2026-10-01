// @vitest-environment jsdom
/**
 * #3115 — an ask's agent-written markdown renders on every surface that answers
 * or reviews it: the operator queue card, resolved history, the Workspace asks
 * and /m. Mounted (#2918): the rendering IS the fix, so a regex over the SFC
 * would prove nothing.
 *
 *   - the question renders block markdown (bold, a list, inline code, a link
 *     hardened to target=_blank rel=noopener) and never shows raw `**`;
 *   - title, option labels and the given answer render inline only — a list
 *     or heading written there degrades to text, it never breaks the layout;
 *   - everything goes through the one sanitiser: a script or an onerror
 *     handler never reaches the DOM;
 *   - clicking a formatted option still sends the RAW option string;
 *   - a plain-text ask shows exactly its text.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'ops' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import AskMarkdown from '@/components/operator/AskMarkdown.vue'
import QueueCard from '@/components/operator/QueueCard.vue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import MobileAdmin from '@/views/MobileAdmin.vue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { useAuthStore } from '@/stores/auth'
import { http } from '@/utils/boundedHttp'

const QUESTION = [
  'Release the **Q4 budget**?',
  '',
  '- Campaigns: `3`',
  '- Owner: [runbook](https://example.com/runbook)',
].join('\n')
const EVIL = 'Hi <img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'
const OPTION = '**Approve** `v2`'

function expectRendered(el) {
  const html = el.innerHTML
  expect(el.querySelector('strong')?.textContent).toBe('Q4 budget')
  expect(el.querySelectorAll('li')).toHaveLength(2)
  expect(el.querySelector('code')?.textContent).toBe('3')
  const a = el.querySelector('a')
  expect(a.getAttribute('href')).toBe('https://example.com/runbook')
  expect(a.getAttribute('target')).toBe('_blank')
  expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  expect(html).not.toContain('**')
}

let wrapper
beforeEach(() => {
  document.body.innerHTML = ''
  delete window.__pwned
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('AskMarkdown — the one renderer', () => {
  it('block: a question renders as markdown', () => {
    wrapper = mount(AskMarkdown, { props: { text: QUESTION } })
    expectRendered(wrapper.element)
    expect(wrapper.classes()).toContain('prose')
  })

  it('inline: no block element, no class, even when one is written', () => {
    wrapper = mount(AskMarkdown, { props: { text: '# Title with **bold**\n- item <div class="fixed inset-0">x</div>', inline: true } })
    const el = wrapper.element
    expect(el.tagName).toBe('SPAN')
    expect(el.querySelector('strong')).not.toBeNull()
    for (const tag of ['h1', 'ul', 'li', 'div', 'p']) expect(el.querySelector(tag)).toBeNull()
    expect(el.querySelector('[class]')).toBeNull()
  })

  it.each([false, true])('sanitised (inline=%s): no script, no event handler', (inline) => {
    wrapper = mount(AskMarkdown, { props: { text: EVIL, inline }, attachTo: document.body })
    expect(wrapper.element.querySelector('script')).toBeNull()
    expect(wrapper.element.innerHTML).not.toContain('onerror')
    expect(window.__pwned).toBeUndefined()
  })

  it('plain text shows exactly its text', () => {
    wrapper = mount(AskMarkdown, { props: { text: 'Deploy the release now?' } })
    expect(wrapper.text()).toBe('Deploy the release now?')
    const inline = mount(AskMarkdown, { props: { text: 'Deploy the release now?', inline: true } })
    expect(inline.text()).toBe('Deploy the release now?')
    inline.unmount()
  })

  it('/m mode keeps the elements and drops the prose classes', () => {
    wrapper = mount(AskMarkdown, { props: { text: QUESTION, prose: false } })
    expectRendered(wrapper.element)
    expect(wrapper.classes()).not.toContain('prose')
  })
})

const ITEM = {
  id: 'q1', agent_name: 'agent-a', type: 'approval', status: 'pending', priority: 'high',
  title: 'Approve **the budget**', question: QUESTION, options: [OPTION, 'Reject'],
  context: {}, created_at: '2026-09-01T10:00:00Z',
}

describe('operator queue card', () => {
  it('renders the question, title and options; a click still sends the raw option', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useOperatorQueueStore()
    store.respondToItem = vi.fn()
    wrapper = mount(QueueCard, { props: { item: ITEM }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
    expect(wrapper.find('[data-testid="queue-card-title"] strong').text()).toBe('the budget')
    store.expandedItemId = 'q1'
    await nextTick()
    expectRendered(wrapper.find('[data-testid="queue-card-question"]').element)
    const option = wrapper.findAll('button').find((b) => b.find('strong').exists() && b.text().includes('Approve'))
    expect(option.find('code').text()).toBe('v2')
    expect(option.text()).not.toContain('**')
    await option.trigger('click')
    await wrapper.findAll('button').find((b) => b.text() === 'Send').trigger('click')
    expect(store.respondToItem).toHaveBeenCalledWith('q1', OPTION, '')
  })
})

describe('resolved history', () => {
  it('renders the title and the given answer inline', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    wrapper = mount(ResolvedCard, {
      props: { item: { ...ITEM, status: 'responded', response: OPTION, responded_at: '2026-09-01T11:00:00Z' } },
      global: { plugins: [pinia], stubs: { AgentAvatar: true } },
    })
    expect(wrapper.find('[data-testid="resolved-title"] strong').text()).toBe('the budget')
    const answer = wrapper.find('[data-testid="resolved-response"]')
    expect(answer.find('strong').text()).toBe('Approve')
    expect(answer.text()).not.toContain('**')
  })
})

describe('Workspace asks', () => {
  it('renders the question like the operator queue does, and answers with the raw option', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useClientPortalStore()
    store.asksAvailable = true
    store.asksLoaded = true
    store.asks = [{
      id: 'q1', agent_name: 'agent-a', kind: 'approval', title: ITEM.title, question: QUESTION,
      options: [OPTION, 'Reject'], status: 'pending', created_at: '2026-09-01T10:00:00Z',
    }]
    store.answerAsk = vi.fn().mockResolvedValue({ status: 'answered' })
    wrapper = mount(PortalAsks, { global: { plugins: [pinia] }, attachTo: document.body })
    await flushPromises()
    const q = wrapper.find('[data-testid="portal-ask-question-q1"]')
    expectRendered(q.element)
    // Same renderer and classes as the operator queue card: they look the same.
    expect(q.classes()).toEqual(expect.arrayContaining(['prose', 'prose-sm', 'max-w-none']))
    expect(wrapper.find('[data-testid="portal-ask-title-q1"] strong').text()).toBe('the budget')
    const option = wrapper.findAll('[data-testid="portal-ask-option-q1"]')[0]
    expect(option.find('strong').text()).toBe('Approve')
    await option.trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalled()
    expect(store.answerAsk.mock.calls[0][1].response).toBe(OPTION)
  })

  it('a plain question reads as before', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useClientPortalStore()
    store.asksAvailable = true
    store.asksLoaded = true
    store.asks = [{ id: 'q2', agent_name: 'a', kind: 'question', title: 'Budget', question: 'Is $20k fine?',
      status: 'pending', created_at: '2026-09-01T10:00:00Z' }]
    wrapper = mount(PortalAsks, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-ask-question-q2"]').text()).toBe('Is $20k fine?')
  })
})

describe('/m', () => {
  it('renders the question and options on the ops card', async () => {
    http.get.mockImplementation((url) => {
      if (url === '/api/operator-queue') return Promise.resolve({ data: { items: [ITEM], count: 1 } })
      if (url === '/api/notifications') return Promise.resolve({ data: { items: [], count: 0 } })
      return Promise.reject(new Error(`unexpected ${url}`))
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().isAuthenticated = true
    wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
    await flushPromises()
    const card = wrapper.find('[data-item-id="q1"]')
    expectRendered(card.find('[data-testid="queue-message"]').element)
    expect(card.find('[data-testid="queue-title"] strong').text()).toBe('the budget')
    const option = card.findAll('[data-testid="queue-option"]')[0]
    expect(option.find('code').text()).toBe('v2')
    expect(option.text()).not.toContain('**')
  })
})
