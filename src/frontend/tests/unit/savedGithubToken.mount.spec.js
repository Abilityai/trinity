// @vitest-environment jsdom
/**
 * #3164 — fork-to-own and repo binding use the user's SAVED personal GitHub
 * token instead of asking for it again.
 *
 * Mounted (the #2918 rule): the field decides whether a credential rides a
 * store write, so the proof is the real payload reaching the store.
 *  - with a saved token the form says so, sends NO `github_pat`, and a typed
 *    token is an explicit override that does ride the payload;
 *  - without one the field is required and nothing is sent;
 *  - the browser only ever learns presence (the store mock returns a boolean).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const createAgent = vi.fn()
const bindAgentToOwnRepo = vi.fn()
const saved = { value: true }
vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({ createAgent, bindAgentToOwnRepo, getBindToOwnRepoStatus: vi.fn() }),
}))
vi.mock('../../src/stores/auth', () => ({
  useAuthStore: () => ({ fetchGithubPatStatus: async () => saved.value }),
}))
const TEMPLATES = [
  { id: 'github:acme/brain', source: 'github', fork_to_own: 'required', display_name: 'Brain' },
]
vi.mock('../../src/api', () => ({ default: { get: vi.fn(async () => ({ data: TEMPLATES })) } }))

// eslint-disable-next-line import/first
import SavedGithubTokenField from '../../src/components/SavedGithubTokenField.vue'
// eslint-disable-next-line import/first
import CreateAgentModal from '../../src/components/CreateAgentModal.vue'
// eslint-disable-next-line import/first
import BindRepoPanel from '../../src/components/BindRepoPanel.vue'

beforeEach(() => {
  vi.clearAllMocks()
  saved.value = true
  createAgent.mockResolvedValue({ name: 'my-agent', git_mode: null })
  bindAgentToOwnRepo.mockResolvedValue({ github_repo: 'alice/brain' })
})

describe('the token field', () => {
  it('names the saved token, is not required, and an override is explicit', async () => {
    const w = mount(SavedGithubTokenField, { props: { hasSaved: true, inputId: 't', modelValue: '' } })
    expect(w.find('[data-testid="github-token-saved"]').exists()).toBe(true)
    expect(w.find('[data-testid="github-token-input"]').exists()).toBe(false)
    expect(w.text()).not.toContain('*')
    await w.find('[data-testid="github-token-toggle"]').trigger('click')
    expect(w.find('[data-testid="github-token-input"]').exists()).toBe(true)
    expect(w.text()).toContain('*')
    await w.setProps({ modelValue: 'ghp_typed' })
    await w.find('[data-testid="github-token-toggle"]').trigger('click')       // back to saved
    expect(w.emitted('update:modelValue').at(-1)).toEqual([''])                // the override is dropped
  })

  it('is required when nothing is saved', () => {
    const w = mount(SavedGithubTokenField, { props: { hasSaved: false, inputId: 't' } })
    expect(w.find('[data-testid="github-token-input"]').exists()).toBe(true)
    expect(w.find('[data-testid="github-token-toggle"]').exists()).toBe(false)
    expect(w.text()).toContain('*')
  })
})

async function forkForm() {
  const w = mount(CreateAgentModal, {
    global: { stubs: { ImportValidationStep: { template: '<div />' } } },
  })
  await flushPromises()
  await w.find('input[type="text"]').setValue('my-agent')
  await w.findAll('div.cursor-pointer').find((d) => d.text().includes('Brain')).trigger('click')
  await w.find('input[placeholder="your-github-username/my-agent-brain"]').setValue('alice/brain')
  return w
}

async function submit(w) {
  await w.find('form').trigger('submit')
  await flushPromises()
  return createAgent.mock.calls.at(-1)?.[0]
}

describe('fork-to-own at create', () => {
  it('with a saved token sends no github_pat', async () => {
    const payload = await submit(await forkForm())
    expect(payload.fork_to_own).toEqual({ destination_repo: 'alice/brain', private: true })
  })

  it('without a saved token, an empty field stops the submit', async () => {
    saved.value = false
    const w = await forkForm()
    expect(await submit(w)).toBeUndefined()
    expect(w.text()).toContain('Settings → GitHub token')
  })

  it('a typed override rides the payload', async () => {
    const w = await forkForm()
    await w.find('[data-testid="github-token-toggle"]').trigger('click')
    await w.find('[data-testid="github-token-input"]').setValue('ghp_override')
    expect((await submit(w)).fork_to_own.github_pat).toBe('ghp_override')
  })

  it('an override left EMPTY stops the submit rather than sending the saved token', async () => {
    // #3164 review: the parent could not see the override, so an empty one
    // read as "use my saved token".
    const w = await forkForm()
    await w.find('[data-testid="github-token-toggle"]').trigger('click')
    expect(await submit(w)).toBeUndefined()
  })
})

describe('binding an agent to a repo you own', () => {
  async function bindForm() {
    const w = mount(BindRepoPanel, { props: { agentName: 'bot', agentStatus: 'running' } })
    await w.findAll('button').find((b) => /bind|own repo|repository/i.test(b.text())).trigger('click')
    await flushPromises()
    await w.find('input[type="text"]').setValue('alice/brain')
    return w
  }

  it('with a saved token sends no github_pat', async () => {
    const w = await bindForm()
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(bindAgentToOwnRepo).toHaveBeenCalledWith('bot', { destination_repo: 'alice/brain', private: true })
  })

  it('without a saved token, an empty field sends nothing and says where to save one', async () => {
    saved.value = false
    const w = await bindForm()
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(bindAgentToOwnRepo).not.toHaveBeenCalled()
    expect(w.text()).toContain('Settings → GitHub token')
  })

  it('an override left EMPTY sends nothing — never the broad saved token', async () => {
    const w = await bindForm()
    await w.find('[data-testid="github-token-toggle"]').trigger('click')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(bindAgentToOwnRepo).not.toHaveBeenCalled()
  })
})
