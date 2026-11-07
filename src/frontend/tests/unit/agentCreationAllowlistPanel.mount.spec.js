// @vitest-environment jsdom
/**
 * Settings → Agents → Template sources for agent-created agents
 * (abilityai/trinity-enterprise#591), mounted.
 *
 * The list decides what every agent may build, so the tests drive the rendered
 * panel against a stubbed client and read what it shows and sends:
 *  - a succeeded fetch of an empty list is "Any source", a failed one LoadFailed;
 *  - the badge reads `bounded`, never the entry count: a damaged row that bounds
 *    agents to nothing must not say "Any source";
 *  - Save sends the trimmed, non-empty lines; a 422 maps each reason back to
 *    the TEXTAREA line it came from;
 *  - the impact list says allowed / refused in words, not hue alone;
 *  - an unreadable impact read says so instead of "none".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import AgentCreationAllowlistPanel from '@/components/settings/AgentCreationAllowlistPanel.vue'

const UNBOUNDED = {
  entries: [], bounded: false, stored_invalid: false, not_bundled: [],
  in_use: [{ source: 'github:acme/tools', agents: 2, allowed: true }],
}

let w
async function render(data = UNBOUNDED) {
  api.get.mockImplementation(async () => ({ data }))
  w = mount(AgentCreationAllowlistPanel, { attachTo: document.body })
  await flushPromises()
  return w
}
const $ = (id) => w.find(`[data-testid="${id}"]`)
const textarea = () => w.find('textarea')

beforeEach(() => vi.clearAllMocks())
afterEach(() => w?.unmount())

describe('AgentCreationAllowlistPanel', () => {
  it('shows the empty state only after a fetch that succeeded', async () => {
    await render()
    expect($('allowlist-badge').text()).toBe('Any source')
    expect($('allowlist-empty').text()).toContain('Any source: agents may build from any template they can reach.')
    expect(textarea().element.value).toBe('')
  })

  it('renders LoadFailed, never the empty copy, when the fetch fails', async () => {
    api.get.mockRejectedValue({ response: { data: { detail: 'boom' } } })
    w = mount(AgentCreationAllowlistPanel, { attachTo: document.body })
    await flushPromises()
    expect(w.text()).toContain("Couldn't load the template sources")
    expect($('allowlist-empty').exists()).toBe(false)
    expect($('allowlist-badge').exists()).toBe(false)
  })

  it('keys the badge on bounded: a damaged row is not "Any source"', async () => {
    await render({ ...UNBOUNDED, bounded: true, stored_invalid: true, entries: [] })
    expect($('allowlist-badge').text()).toBe('0 sources')
    expect($('allowlist-damaged').exists()).toBe(true)
    expect($('allowlist-empty').exists()).toBe(false)
  })

  it('lets the admin save a damaged list without editing it (the banner asks for a save)', async () => {
    await render({ ...UNBOUNDED, bounded: true, stored_invalid: true, entries: ['local:scout'] })
    expect($('allowlist-save').attributes('disabled')).toBeUndefined()
    expect($('allowlist-discard').attributes('disabled')).toBeDefined()
    api.put.mockResolvedValue({ data: { ...UNBOUNDED, bounded: true, entries: ['local:scout'] } })
    await $('allowlist-save').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/settings/agent-creation-allowlist', { entries: ['local:scout'] })
    expect($('allowlist-damaged').exists()).toBe(false)
  })

  it('pre-fills the saved list and saves the trimmed, non-empty lines', async () => {
    await render({ ...UNBOUNDED, bounded: true, entries: ['local:scout'] })
    expect(textarea().element.value).toBe('local:scout')
    expect($('allowlist-save').attributes('disabled')).toBeDefined()   // nothing edited yet

    api.put.mockResolvedValue({ data: { ...UNBOUNDED, bounded: true, entries: ['local:scout', 'github:acme/*'] } })
    await textarea().setValue('  local:scout  \n\n github:acme/* \n')
    await $('allowlist-save').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/settings/agent-creation-allowlist',
      { entries: ['local:scout', 'github:acme/*'] })
    expect($('allowlist-badge').text()).toBe('2 sources')
  })

  it('maps each refused entry back to its textarea line', async () => {
    await render()
    api.put.mockRejectedValue({
      response: { data: { detail: {
        code: 'invalid_template_sources',
        message: "Some entries aren't valid sources — nothing was saved.",
        errors: [{ index: 1, entry: 'github:acme', reason: 'Use github:<owner>/<repo> or github:<owner>/*, e.g. github:acme/*.' }],
      } } },
    })
    await textarea().setValue('local:scout\n\ngithub:acme')
    await $('allowlist-save').trigger('click')
    await flushPromises()
    expect(w.text()).toContain("Some entries aren't valid sources — nothing was saved.")
    expect($('allowlist-line-errors').text()).toBe(
      'Line 3 github:acme — Use github:<owner>/<repo> or github:<owner>/*, e.g. github:acme/*.')
    expect(textarea().element.value).toBe('local:scout\n\ngithub:acme')   // the draft is kept
    // Below the field it describes: a list of up to 100 reasons must not push
    // the field down (design-system principle 30).
    const order = textarea().element.compareDocumentPosition($('allowlist-line-errors').element)
    expect(order & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('clears the list by saving an empty field, and Discard restores the saved one', async () => {
    await render({ ...UNBOUNDED, bounded: true, entries: ['local:scout'] })
    await textarea().setValue('github:x/y')
    await $('allowlist-discard').trigger('click')
    expect(textarea().element.value).toBe('local:scout')

    api.put.mockResolvedValue({ data: UNBOUNDED })
    await textarea().setValue('')
    await $('allowlist-save').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/settings/agent-creation-allowlist', { entries: [] })
    expect($('allowlist-badge').text()).toBe('Any source')
  })

  it('says allowed or refused in words in the impact list', async () => {
    await render({
      ...UNBOUNDED, bounded: true, entries: ['local:scout'],
      in_use: [
        { source: 'github:evil/tools', agents: 1, allowed: false },
        { source: 'local:scout', agents: 3, allowed: true },
      ],
    })
    // One cell per child span (flex gap spaces them; the compiler drops the whitespace).
    const rows = $('allowlist-in-use').findAll('li').map((li) =>
      [...li.element.children].map((c) => c.textContent.replace(/\s+/g, ' ').trim()).join(' '))
    expect(rows).toEqual(['✗ refused github:evil/tools 1 agent', '✓ allowed local:scout 3 agents'])
    expect(w.text()).toContain('4 agents · checked against the saved list')
  })

  it('says it could not read the impact instead of claiming none', async () => {
    await render({ ...UNBOUNDED, in_use: null })
    expect($('allowlist-in-use-unknown').exists()).toBe(true)
    expect($('allowlist-in-use-none').exists()).toBe(false)
  })

  it('names listed bundled templates that the template list hides', async () => {
    await render({ ...UNBOUNDED, bounded: true, entries: ['local:default'], hidden: ['local:default'] })
    expect($('allowlist-hidden').text()).toContain('local:default')
    expect($('allowlist-hidden').text()).toContain('agents can still create from it')
    expect($('allowlist-not-bundled').exists()).toBe(false)
  })

  it('names listed local templates that are not bundled', async () => {
    await render({ ...UNBOUNDED, bounded: true, entries: ['local:uploaded'], not_bundled: ['local:uploaded'] })
    expect($('allowlist-not-bundled').text()).toContain('local:uploaded')
  })
})
