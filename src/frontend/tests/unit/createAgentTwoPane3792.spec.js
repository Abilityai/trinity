// @vitest-environment jsdom
/**
 * #3792: the Create Agent dialog is a fixed-size two-pane picker.
 *
 * Mounted (#2918): the name and ID decide the create payload (a store write),
 * so the proof is what reaches `createAgent`.
 *  - every source lives in one left-hand list; picking one changes only the
 *    right pane, and the panel keeps one fixed, viewport-clamped size
 *  - one Name field: the ID follows it, can be overridden, and the typed name
 *    rides along as the display name (dropped over the backend's 120 cap)
 *  - search narrows the templates; a failed catalog load still leaves Blank
 *    and GitHub import usable; Esc closes
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, DOMWrapper } from '@vue/test-utils'

const createAgent = vi.fn()
const apiGet = vi.fn()
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => ({ createAgent }) }))
vi.mock('../../src/stores/auth', () => ({ useAuthStore: () => ({ fetchGithubPatStatus: async () => false }) }))
vi.mock('../../src/api', () => ({ default: { get: (...a) => apiGet(...a) } }))

const TEMPLATES = [
  { id: 'github:acme/brain', source: 'github', fork_to_own: 'required', display_name: 'Brain', tagline: 'Second brain' },
  { id: 'local:sage', source: 'local', display_name: 'Sage - Strategic Advisor', description: 'Strategic advisor that synthesizes research' },
  { id: 'local:scout', source: 'local', display_name: 'Scout - Market Research Analyst', description: 'Market research specialist' },
  { id: 'github:acme/helper', source: 'github', display_name: 'Helper', github_repo: 'acme/helper' },
]

// eslint-disable-next-line import/first
import CreateAgentModal from '../../src/components/CreateAgentModal.vue'

// BaseModal teleports to <body>: query the document, not the wrapper.
const dom = () => new DOMWrapper(document.body)
const panel = () => dom().get('[role="dialog"] > div')
const options = () => dom().findAll('[data-testid="source-option"]')
const pick = async (text) => options().find((b) => b.text().includes(text)).trigger('click')
const detail = () => dom().get('[data-testid="source-detail"]')
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

describe('#3792 layout', () => {
  it('lists every source in one list, grouped, Blank selected by default', async () => {
    await open()
    expect(options().map((o) => o.text())).toEqual([
      'Blank agent', 'Import a GitHub repository',
      'Brain', 'Sage - Strategic Advisor', 'Scout - Market Research Analyst', 'Helper',
    ])
    const list = dom().get('[data-testid="source-list"]').text()
    for (const heading of ['Start', 'Featured', 'Local templates', 'GitHub templates']) expect(list).toContain(heading)
    expect(options()[0].attributes('aria-pressed')).toBe('true')
    expect(detail().text()).toContain('Blank agent')
  })

  it('keeps one fixed, viewport-clamped panel size whatever is selected', async () => {
    await open()
    const before = panel().classes()
    expect(before).toContain('h-[min(600px,calc(100dvh-2rem))]')
    expect(before).toContain('overflow-hidden')
    for (const source of ['Import a GitHub repository', 'Brain', 'Sage', 'Helper', 'Blank agent']) {
      await pick(source)
      expect(panel().classes()).toEqual(before)
    }
    // Neither pane can scroll sideways.
    expect(dom().get('[data-testid="source-list"]').classes()).toContain('overflow-x-hidden')
    expect(detail().classes()).toContain('overflow-x-hidden')
  })

  it('shows the selection in the right pane only', async () => {
    await open()
    await pick('Sage')
    expect(detail().text()).toContain('Sage - Strategic Advisor')
    expect(detail().text()).toContain('Local template')
    expect(options().find((o) => o.text().includes('Sage')).attributes('aria-pressed')).toBe('true')
    await pick('Brain')
    expect(detail().text()).toContain('Your own copy')
    expect(detail().find('input[placeholder="your-github-username/my-agent-brain"]').exists()).toBe(true)
  })

  it('names the runtime read-only: Claude Code for a blank agent, the template otherwise', async () => {
    await open()
    expect(dom().get('[data-testid="runtime-row"]').text()).toBe('Claude Code')
    await pick('Sage')
    expect(dom().get('[data-testid="runtime-row"]').text()).toContain('Set by the template')
    expect(dom().find('[data-testid="runtime-row"] select, [data-testid="runtime-row"] input').exists()).toBe(false)
  })
})

describe('#3792 one name', () => {
  it('sends the typed name as the display name and derives the ID from it', async () => {
    await open()
    await dom().get('#agent-name').setValue('Marketing Assistant')
    expect(dom().get('#agent-name-preview code').text()).toBe('marketing-assistant')
    expect(await submit()).toEqual({ name: 'Marketing Assistant', display_label: 'Marketing Assistant' })
  })

  it('sends no display name when the name already reads as the ID', async () => {
    await open()
    await dom().get('#agent-name').setValue('my-agent')
    expect(await submit()).toEqual({ name: 'my-agent' })
  })

  it('lets the ID be overridden, and Use name goes back to following the name', async () => {
    await open()
    await dom().get('#agent-name').setValue('Marketing Assistant')
    await dom().findAll('button').find((b) => b.text() === 'Edit').trigger('click')
    const idField = dom().get('#agent-id')
    expect(idField.element.value).toBe('marketing-assistant')
    await idField.setValue('Mkt Bot')
    expect(await submit()).toEqual({ name: 'Mkt Bot', display_label: 'Marketing Assistant' })

    await dom().findAll('button').find((b) => b.text() === 'Use name').trigger('click')
    expect(dom().get('#agent-name-preview code').text()).toBe('marketing-assistant')
  })

  it('refuses an override that sanitizes to nothing, with a named error', async () => {
    await open()
    await dom().get('#agent-name').setValue('Marketing Assistant')
    await dom().findAll('button').find((b) => b.text() === 'Edit').trigger('click')
    await dom().get('#agent-id').setValue('!!!')
    expect(dom().get('button[type="submit"]').element.disabled).toBe(true)
    expect(dom().get('[role="alert"]').text()).toContain('Enter an ID')
    await submit()
    expect(createAgent).not.toHaveBeenCalled()
  })

  it('drops a display name over the 120-character cap instead of failing the create', async () => {
    await open()
    const long = 'A'.repeat(121)
    await dom().get('#agent-name').setValue(long)
    expect(await submit()).toEqual({ name: long })
  })
})

describe('#3792 search, failure and keyboard', () => {
  it('narrows the templates and says when nothing matches, keeping the Start options', async () => {
    await open()
    await dom().get('#template-search').setValue('market')
    expect(options().map((o) => o.text())).toEqual(['Blank agent', 'Import a GitHub repository', 'Scout - Market Research Analyst'])
    await dom().get('#template-search').setValue('zzz')
    expect(options()).toHaveLength(2)
    expect(dom().get('[data-testid="source-list"]').text()).toContain('No templates match')
  })

  it('a failed catalog load says so and still creates a blank agent', async () => {
    apiGet.mockRejectedValue(new Error('Network Error'))
    await open()
    expect(dom().find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(dom().get('[data-testid="source-list"]').text()).not.toContain('No templates match')
    await dom().get('#agent-name').setValue('solo')
    expect(await submit()).toEqual({ name: 'solo' })
  })

  it('Esc closes the dialog', async () => {
    await open()
    await dom().get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
