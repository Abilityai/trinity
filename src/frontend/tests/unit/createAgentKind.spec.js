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
// #3164: the modal reads whether a personal GitHub token is saved (presence only).
vi.mock('../../src/stores/auth', () => ({ useAuthStore: () => ({ fetchGithubPatStatus: async () => false }) }))

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

  // PR #3022 review: every list template is a catalog entry, and the backend
  // makes a catalog template pull-only whatever kind is asked for
  // (`_apply_agent_kind_default`, catalog_template=True). Offering "An agent"
  // there only promised a working branch that could never happen.
  it('is not asked for a catalog GitHub template, which is always pull-only', async () => {
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'Helper')
    expect(picker(wrapper).exists()).toBe(false)
    const payload = await submit(wrapper)
    expect(payload).toMatchObject({ template: 'github:acme/helper' })
    expect(payload).not.toHaveProperty('kind')
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

  it('hands git_mode to the post-create step for a GitHub template picked from the list', async () => {
    // Always pull-only (a catalog template): the modal must not close before
    // the notice that says so renders.
    const gitMode = { kind: 'agent', source_mode: true, reason: 'no GitHub token: pull-only' }
    createAgent.mockResolvedValue({ name: 'my-agent', git_mode: gitMode })
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'Helper')
    await submit(wrapper)
    const step = wrapper.findComponent('[data-testid="validation-step"]')
    expect(step.exists()).toBe(true)
    expect(step.props('gitMode')).toEqual(gitMode)
    expect(wrapper.emitted('close')).toBeUndefined()
  })

  it('still closes straight away for a local template', async () => {
    const wrapper = await mountModal()
    await pickTemplate(wrapper, 'Scout')
    await submit(wrapper)
    expect(wrapper.find('[data-testid="validation-step"]').exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})

describe('GitModeNotice', () => {
  it('says plainly when an agent was created pull-only, with the reason', () => {
    const w = mount(GitModeNotice, { props: { gitMode: { kind: 'agent', source_mode: true, pushes: false, reason: 'no GitHub token: pull-only' } } })
    expect(w.text()).toContain('Created pull-only')
    expect(w.text()).toContain('no GitHub token: pull-only')
  })

  it('names an agent with its own branch, and a deployment', () => {
    const agent = mount(GitModeNotice, { props: { gitMode: { kind: 'agent', source_mode: false, pushes: true, reason: 'the token can push: working branch' } } })
    expect(agent.text()).toContain('its own branch')
    const dep = mount(GitModeNotice, { props: { gitMode: { kind: 'deployment', source_mode: true, pushes: false, reason: 'a deployment of a codebase: pull-only' } } })
    expect(dep.text()).toContain('A deployment')
    expect(dep.text()).not.toContain('Created pull-only')
  })

  // PR #3022 review: fork-to-own is source_mode=true (it tracks its fork's
  // default branch) but PUSHES there — deciding "pull-only" from source_mode
  // told every fork-to-own user their work would not be kept.
  it('does not call a fork-to-own agent pull-only: it pushes to its own repo', () => {
    const w = mount(GitModeNotice, { props: { gitMode: { kind: 'agent', source_mode: true, pushes: true, reason: 'fork-to-own: the agent owns its fork' } } })
    expect(w.text()).not.toContain('Created pull-only')
    expect(w.text()).not.toContain('never pushes')
    expect(w.text()).toContain('owns its repository')
    expect(w.find('[data-testid="git-mode-notice"]').classes().join(' ')).not.toContain('status-warning')
  })

  // PR #3022 review nit: without `pushes` (an older backend) the binding is
  // unknown; guessing from source_mode is the misread this PR fixed.
  it('stays neutral when the response does not say whether the agent pushes', () => {
    const w = mount(GitModeNotice, { props: { gitMode: { kind: 'agent', source_mode: true, reason: 'fork-to-own: the agent owns its fork' } } })
    expect(w.text()).not.toContain('Created pull-only')
    expect(w.text()).not.toContain('never pushes')
    expect(w.text()).toContain('fork-to-own: the agent owns its fork')
    expect(w.find('[data-testid="git-mode-notice"]').classes().join(' ')).not.toContain('status-warning')
  })
})

describe('GitBindingBadge', () => {
  it('names the binding from whether the agent pushes, and renders nothing when it is unknown', () => {
    expect(mount(GitBindingBadge, { props: { pushes: true, sourceMode: false } }).text()).toBe('Agent · own branch')
    expect(mount(GitBindingBadge, { props: { pushes: false, sourceMode: true } }).text()).toBe('Pull-only')
    expect(mount(GitBindingBadge, { props: { pushes: null, sourceMode: true } }).find('[data-testid="git-binding-badge"]').exists()).toBe(false)
  })

  it('names a fork-to-own agent as owning its repo, not pull-only', () => {
    const w = mount(GitBindingBadge, { props: { pushes: true, sourceMode: true } })
    expect(w.text()).toBe('Agent · own repo')
    expect(w.attributes('title')).not.toContain('never pushes')
  })

  it('says pull-only is about auto-sync, not that a manual push is impossible', () => {
    const w = mount(GitBindingBadge, { props: { pushes: false, sourceMode: true } })
    expect(w.attributes('title')).toContain('auto-sync')
    expect(w.attributes('title')).not.toContain('never pushes')
  })
})
