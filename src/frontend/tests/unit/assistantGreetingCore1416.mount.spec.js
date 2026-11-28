// @vitest-environment jsdom
/**
 * trinity-enterprise#1416 (second fix round, trinity-pm): the assistant's hello
 * names both agents the instance came with — the Chief of Staff and the Company
 * Brain — whenever they arrive.
 *
 * The seeding race from the live walk on 14bf38d: the first-run read lands while
 * the seed pass is still creating the core agents, so it lists only the Brain
 * (`core_agents: ['cornelius']`, and `seeded_agents` without `corbin`). The
 * hello was written from that one read and said "Your Company Brain (cornelius)
 * is already here." When the Chief of Staff arrived it was left out, or — read
 * from the fleet but not from the stale first-run answer — counted as an agent
 * of the person's own.
 *
 * Mounted (#2918) on the ent#1408 harness: the real AssistantWidget, assistant,
 * first-run and network stores; the instance answering through a mocked api.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const BRAIN = 'cornelius'
const CHIEF = 'corbin'
let present      // the agents the instance has right now, besides trinity-system
let firstRunReads

vi.mock('@/api', () => {
  const respond = (method, url) => {
    if (url === '/api/enterprise/assistant/status') {
      return { available: true, contacted: true, reachable: true, plan: 'free', email: null, allowance: null,
        trial_terms: { version: 't0', url: '', accepted: 't0' }, profile_known: false }
    }
    if (url === '/api/onboarding/first-run') {
      firstRunReads += 1
      // The backend lists only the seeded agents present when it is asked.
      const seeded = [CHIEF, BRAIN].filter((n) => present.includes(n))
      return { first_run: true, seeded_agents: seeded, own_agent_count: 0, brain_agent: present.includes(BRAIN) ? BRAIN : null,
        core_agents: seeded, fresh_install: true }
    }
    if (url === '/api/agents') {
      return [{ name: 'trinity-system', status: 'running', is_system: true }, ...present.map((name) => ({ name, status: 'running' }))]
    }
    if (url === '/api/agents/autonomy-status') return {}
    if (url === '/api/ops/schedules') return { schedules: [] }
    return {}
  }
  const client = {
    get: vi.fn(async (url, config) => ({ data: respond('get', url, config) })),
    post: vi.fn(async (url) => ({ data: respond('post', url) })),
    put: vi.fn(async (url) => ({ data: respond('put', url) })),
    delete: vi.fn(async (url) => ({ data: respond('delete', url) })),
  }
  return { default: client }
})
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/', params: {}, query: {} }), useRouter: () => ({ push: async () => {} }) }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => ({ fetchAgents: async () => {}, agents: [] }) }))

import AssistantWidget from '@/components/assistant/AssistantWidget.vue'
import { useNetworkStore } from '@/stores/network'
import { useAssistantStore } from '@/stores/assistant'

const BOTH = 'Your Chief of Staff (corbin) and Company Brain (cornelius) are already here.'

describe('the hello names both core agents, whenever they arrive (ent#1416)', () => {
  let wrapper
  const hello = () => wrapper.findAll('.bubble').map((b) => b.text())
  const openPanel = async () => {
    await wrapper.get('[data-testid="assistant-launcher"]').trigger('click')
    await flushPromises()
  }
  // The roster the dashboard keeps current from the socket (agent_created).
  const arrive = async (name) => {
    present = [...present, name]
    useNetworkStore().agents = [{ name: 'trinity-system', is_system: true }, ...present.map((n) => ({ name: n, status: 'running' }))]
    await flushPromises()
  }

  beforeEach(() => {
    present = [BRAIN]
    firstRunReads = 0
    localStorage.clear()
    sessionStorage.clear()
    setActivePinia(createPinia())
    wrapper = mount(AssistantWidget, {
      attachTo: document.body,
      global: { stubs: {
        ChatBubble: { props: ['role', 'content'], template: '<div class="bubble" :data-role="role">{{ content }}</div>' },
        HelpChatWidget: { template: '<div />' },
      } },
    })
  })
  afterEach(() => wrapper.unmount())

  it('the Chief of Staff created after the hello was written is named in it', async () => {
    await openPanel()
    expect(hello()).toHaveLength(1)
    expect(hello()[0]).toContain('Your Company Brain (cornelius) is already here.')

    await arrive(CHIEF)
    expect(hello()).toHaveLength(1)
    expect(hello()[0]).toContain(BOTH)
    expect(hello()[0]).not.toMatch(/You have \d+ agent/)
    expect(useAssistantStore().opening).toContain(BOTH)   // what the assistant is told it said
  })

  it('a Chief of Staff present at open but missing from the earlier read is named, never counted as theirs', async () => {
    await flushPromises()            // the mount-time read: only the Brain existed
    expect(firstRunReads).toBe(1)
    present = [BRAIN, CHIEF]          // created before the panel opens, no socket yet
    await openPanel()
    expect(hello()).toHaveLength(1)
    expect(hello()[0]).toContain(BOTH)
    expect(hello()[0]).not.toMatch(/You have \d+ agent/)
  })

  it('the read that came before either core agent existed: both are named once they arrive', async () => {
    present = []
    await openPanel()
    expect(hello()[0]).not.toContain('already here')
    await arrive(BRAIN)
    await arrive(CHIEF)
    expect(hello()).toHaveLength(1)
    expect(hello()[0]).toContain(BOTH)
  })

  it('once the person has answered, the conversation is left as it is', async () => {
    await openPanel()
    useAssistantStore().messages.push({ id: 'u1', role: 'user', content: 'I run a bakery' })
    await arrive(CHIEF)
    expect(useAssistantStore().messages[0].content).toContain('Your Company Brain (cornelius) is already here.')
  })
})
