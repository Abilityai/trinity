// @vitest-environment jsdom
/**
 * trinity-enterprise#704 — the create flow asks "what is this repository?"
 *
 * Mounted (the #2829 / #2918 rule): the question gates a store write (the
 * create payload), so the proof is a real selection reaching `createAgent`.
 *  - asked exactly on the paths that bind git: a GitHub template from the list,
 *    or a custom repo with the CLONE intent; never for blank/local, copy, fork
 *    or a featured fork-to-own template (those are "an agent in your own repo"
 *    by definition, or have no git link)
 *  - the answer rides the payload as `kind`; unasked, no `kind` is sent
 *  - the create response's git_mode reaches the post-create step, and the
 *    notice says it plainly when an agent was created pull-only
 *  - the Git panel badge names the binding
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const createAgent = vi.fn()
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => ({ createAgent }) }))

const TEMPLATES = [
  { id: 'github:acme/helper', source: 'github', display_name: 'Helper', github_repo: 'acme/helper' },
  { id: 'github:acme/brain', source: 'github', fork_to_own: 'required', display_name: 'Brain' },
  { id: 'local:scout', source: 'local', display_name: 'Scout' },
]
vi.mock('../../src/api', () => ({ default: { get: vi.fn(async () => ({ data: TEMPLATES })) } }))

// eslint-disable-next-line import/first
import CreateAgentModal from '../../src/components/CreateAgentModal.vue'
// eslint-disable-next-line import/first
import GitModeNotice from '../../src/components/GitModeNotice.vue'
// eslint-disable-next-line import/first
import GitBindingBadge from '../../src/components/GitBindingBadge.vue'

async function mountModal() {
  const wrapper = mount(CreateAgentModal, {
    global: { stubs: { ImportValidationStep: { props: ['agentName', 'importSnapshot', 'gitMode'], template: '<div data-testid="validation-step" />' } } },
  })
  await flushPromises()
  await wrapper.find('input[type="text"]').setValue('my-agent')
  return wrapper
}

const picker = (w) => w.find('[data-testid="agent-kind-picker"]')

async function pickTemplate(wrapper, text) {
  const card = wrapper.findAll('div.cursor-pointer').find((d) => d.text().includes(text))
  await card.trigger('click')
}

async function submit(wrapper) {
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  return createAgent.mock.calls.at(-1)?.[0]
}

beforeEach(() => {
  vi.clearAllMocks()
  createAgent.mockResolvedValue({ name: 'my-agent', git_mode: null })
})

describe('the create flow asks what the repository is', () => {
  it('is not asked for a blank or a local agent, and no kind is sent', async () => {
    const wrapper = await mountModal()
    expect(picker(wrapper).exists()).toBe(false)
    await pickTemplate(wrapper, 'Scout')
    expect(picker(wrapper).exists()).toBe(false)
    expect(await submit(wrapper)).not.toHaveProperty('kind')
  })

  it('is asked for a GitHub template, defaults to an agent, and sends the answer', async () => {
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'Helper')
    expect(picker(wrapper).exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-kind-agent"]').element.checked).toBe(true)
    await wrapper.find('[data-testid="agent-kind-deployment"]').setValue(true)
    const payload = await submit(wrapper)
    expect(payload).toMatchObject({ template: 'github:acme/helper', kind: 'deployment' })
  })

  it('is asked for a custom repo clone, and not for copy or fork', async () => {
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'GitHub Repository')
    expect(picker(wrapper).exists()).toBe(true)

    await wrapper.find('input[name="import-intent"][value="copy"]').setValue(true)
    expect(picker(wrapper).exists()).toBe(false)
    await wrapper.find('input[name="import-intent"][value="fork"]').setValue(true)
    expect(picker(wrapper).exists()).toBe(false)

    await wrapper.find('input[name="import-intent"][value="clone"]').setValue(true)
    await wrapper.find('input[placeholder^="owner/repo"]').setValue('acme/tool')
    const payload = await submit(wrapper)
    expect(payload).toMatchObject({ template: 'github:acme/tool', import_intent: 'clone', kind: 'agent' })
  })

  it('is not asked for a featured fork-to-own template (an agent in your own repo)', async () => {
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'Brain')
    expect(picker(wrapper).exists()).toBe(false)
  })

  it('hands the create response git_mode to the post-create step', async () => {
    const gitMode = { kind: 'agent', source_mode: true, reason: 'the token cannot push to acme/tool: pull-only.' }
    createAgent.mockResolvedValue({ name: 'my-agent', git_mode: gitMode })
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'GitHub Repository')
    await wrapper.find('input[placeholder^="owner/repo"]').setValue('acme/tool')
    await submit(wrapper)
    const step = wrapper.findComponent('[data-testid="validation-step"]')
    expect(step.exists()).toBe(true)
    expect(step.props('gitMode')).toEqual(gitMode)
  })
})

describe('GitModeNotice', () => {
  it('says plainly when an agent was created pull-only, with the reason', () => {
    const w = mount(GitModeNotice, { props: { gitMode: { kind: 'agent', source_mode: true, reason: 'no GitHub token: pull-only' } } })
    expect(w.text()).toContain('Created pull-only')
    expect(w.text()).toContain('no GitHub token: pull-only')
  })

  it('names an agent with its own branch, and a deployment', () => {
    const agent = mount(GitModeNotice, { props: { gitMode: { kind: 'agent', source_mode: false, reason: 'the token can push: working branch' } } })
    expect(agent.text()).toContain('its own branch')
    const dep = mount(GitModeNotice, { props: { gitMode: { kind: 'deployment', source_mode: true, reason: 'a deployment of a codebase: pull-only' } } })
    expect(dep.text()).toContain('A deployment')
    expect(dep.text()).not.toContain('Created pull-only')
  })
})

describe('GitBindingBadge', () => {
  it('names the binding, and renders nothing when it is unknown', () => {
    expect(mount(GitBindingBadge, { props: { sourceMode: false } }).text()).toBe('Agent · own branch')
    expect(mount(GitBindingBadge, { props: { sourceMode: true } }).text()).toBe('Pull-only')
    expect(mount(GitBindingBadge, { props: { sourceMode: null } }).find('[data-testid="git-binding-badge"]').exists()).toBe(false)
  })
})
