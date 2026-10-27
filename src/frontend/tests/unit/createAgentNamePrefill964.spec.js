// @vitest-environment jsdom
/**
 * trinity-enterprise#964: the Create Agent dialog prefills Name from the source.
 *
 * Mounted (#2918): the name decides the create payload (a store write), so the
 * proof is the Name field and what reaches `createAgent`.
 *  - Import a GitHub repository: the repo name, never the owner, from every
 *    accepted form (owner/repo, URL, `.git`), for every import intent
 *  - a GitHub template: its declared `agent_name`, else its repo name
 *  - a hand-typed Name survives a source change; a Name still equal to the
 *    last prefill is replaced; an ID override is kept
 *  - Blank and local templates keep an empty Name
 *  - a repo name that sanitizes to nothing leaves Name empty
 *  - the payload is unchanged
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, DOMWrapper } from '@vue/test-utils'

const createAgent = vi.fn()
const apiGet = vi.fn()
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => ({ createAgent }) }))
vi.mock('../../src/stores/auth', () => ({ useAuthStore: () => ({ fetchGithubPatStatus: async () => false }) }))
vi.mock('../../src/api', () => ({ default: { get: (...a) => apiGet(...a) } }))

const TEMPLATES = [
  { id: 'github:acme/brain', source: 'github', fork_to_own: 'required', display_name: 'Brain', tagline: 'Second brain', github_repo: 'acme/brain', agent_name: 'second-brain' },
  { id: 'local:sage', source: 'local', display_name: 'Sage - Strategic Advisor', description: 'Strategic advisor' },
  { id: 'github:acme/helper', source: 'github', display_name: 'Helper Bot', github_repo: 'acme/helper', agent_name: 'helper' },
  { id: 'github:acme/trinity-pm', source: 'github', display_name: 'Trinity PM', github_repo: 'acme/trinity-pm', agent_name: null },
]

// eslint-disable-next-line import/first
import CreateAgentModal from '../../src/components/CreateAgentModal.vue'

const dom = () => new DOMWrapper(document.body)
const options = () => dom().findAll('[data-testid="source-option"]')
const pick = async (text) => {
  await options().find((b) => b.text().includes(text)).trigger('click')
  await flushPromises()
}
const name = () => dom().get('#agent-name').element.value
const typeName = async (v) => { await dom().get('#agent-name').setValue(v); await flushPromises() }
const typeRepo = async (v) => { await dom().get('#github-repo').setValue(v); await flushPromises() }
async function submit() {
  await dom().get('form').trigger('submit')
  await flushPromises()
  return createAgent.mock.calls.at(-1)?.[0]
}

let wrapper
async function open(props = {}) {
  wrapper = mount(CreateAgentModal, { props, global: { stubs: { ImportValidationStep: true } } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  apiGet.mockResolvedValue({ data: TEMPLATES })
  createAgent.mockResolvedValue({ name: 'x' })
})
afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
})

describe('Import a GitHub repository', () => {
  it.each([
    ['owner/repo', 'Abilityai/trinity-pm'],
    ['https URL', 'https://github.com/Abilityai/trinity-pm'],
    ['.git suffix', 'https://github.com/Abilityai/trinity-pm.git'],
    ['trailing slash', 'https://github.com/Abilityai/trinity-pm/'],
  ])('prefills the repo name, never the owner (%s)', async (_label, input) => {
    await open()
    await pick('Import a GitHub repository')
    await typeRepo(input)
    expect(name()).toBe('trinity-pm')
    expect(dom().get('#agent-name-preview').text()).toContain('trinity-pm')
  })

  it('follows the repository while the person types it', async () => {
    await open()
    await pick('Import a GitHub repository')
    await typeRepo('acme/tri')
    expect(name()).toBe('tri')
    await typeRepo('acme/trinity')
    expect(name()).toBe('trinity')
    await typeRepo('acme/')
    expect(name()).toBe('')
  })

  it.each(['copy', 'fork'])('prefills for the %s intent too', async (intent) => {
    await open()
    await pick('Import a GitHub repository')
    await dom().get(`input[type="radio"][value="${intent}"]`).setValue(true)
    await typeRepo('acme/trinity-docs')
    expect(name()).toBe('trinity-docs')
  })

  it('leaves Name empty when the repo name sanitizes to nothing, with the usual submit error', async () => {
    await open()
    await pick('Import a GitHub repository')
    await typeRepo('acme/___')
    expect(name()).toBe('')
    await submit()
    expect(createAgent).not.toHaveBeenCalled()
    expect(dom().text()).toContain('at least one ASCII letter or number')
  })

  it('sends the same payload as a hand-typed name', async () => {
    await open()
    await pick('Import a GitHub repository')
    await typeRepo('Abilityai/trinity-pm')
    const payload = await submit()
    expect(payload).toEqual({ name: 'trinity-pm', template: 'github:Abilityai/trinity-pm', import_intent: 'clone', kind: 'agent' })
  })
})

describe('GitHub templates', () => {
  it("prefills the template's declared agent name", async () => {
    await open()
    await pick('Helper Bot')
    expect(name()).toBe('helper')
  })

  it('falls back to the repo name without the owner', async () => {
    await open()
    await pick('Trinity PM')
    expect(name()).toBe('trinity-pm')
  })

  it('prefills a featured (fork-to-own) template', async () => {
    await open()
    await pick('Brain')
    expect(name()).toBe('second-brain')
  })

  it('prefills a template preselected by the caller once the catalog loads', async () => {
    await open({ initialTemplate: 'github:acme/helper' })
    expect(name()).toBe('helper')
  })
})

describe('Blank and local templates', () => {
  it('Blank starts empty', async () => {
    await open()
    expect(name()).toBe('')
  })

  it('a local template keeps an empty Name', async () => {
    await open()
    await pick('Sage')
    expect(name()).toBe('')
  })

  it('switching from a prefilled source to Blank clears the prefill', async () => {
    await open()
    await pick('Helper Bot')
    await pick('Blank agent')
    expect(name()).toBe('')
  })
})

describe('never clobbers the person', () => {
  it('keeps a hand-typed Name when the source changes', async () => {
    await open()
    await pick('Helper Bot')
    await typeName('My Helper')
    await pick('Trinity PM')
    expect(name()).toBe('My Helper')
    await pick('Import a GitHub repository')
    await typeRepo('acme/other')
    expect(name()).toBe('My Helper')
  })

  it('keeps a Name typed before any source was picked', async () => {
    await open()
    await typeName('Marketing Assistant')
    await pick('Helper Bot')
    expect(name()).toBe('Marketing Assistant')
  })

  it('replaces a Name that still equals the previous prefill', async () => {
    await open()
    await pick('Helper Bot')
    await pick('Trinity PM')
    expect(name()).toBe('trinity-pm')
  })

  it('keeps a manually edited ID across source changes', async () => {
    await open()
    await pick('Helper Bot')
    await dom().findAll('button').find((b) => b.text() === 'Edit').trigger('click')
    await flushPromises()
    await dom().get('#agent-id').setValue('custom-id')
    await pick('Trinity PM')
    expect(dom().get('#agent-id').element.value).toBe('custom-id')
    const payload = await submit()
    expect(payload.name).toBe('custom-id')
    expect(payload.display_label).toBe('trinity-pm')
  })
})
