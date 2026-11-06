// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#925 — the Permissions panel's "not enforced
 * in-container" line.
 *
 * Peer skill grants are enforced inside the agent at skill load, by the
 * ent#752 hook. Where the agent has no intact hook (an old image, a runtime
 * without hooks), a peer that asks in prose is checked only by name, and the
 * panel must say so rather than let the grants read as enforced. Mounted, so
 * the predicate that decides when the line shows is executed, not read.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PeerSkillEnforcementLine from '../../src/components/PeerSkillEnforcementLine.vue'
import { useAgentsStore } from '../../src/stores/agents'

const PREFIX = 'Agents calling this agent are checked only for library skills they name: '
const LINES = {
  missing: 'its image has no in-agent skill check. Rebuild the base image and recreate the agent.',
  not_root_owned: "its in-agent skill check isn't protected. Recreate the agent to restore it.",
  writable: 'its in-agent skill check can be changed from inside it. Recreate the agent to restore it.',
  unsupported_runtime: "its runtime can't run an in-agent skill check.",
  predates: 'its image predates the in-agent skill check. Recreate the agent to enforce grants inside it.',
  something_new: "its in-agent skill check isn't intact. Recreate the agent to restore it.",
}

async function mountWith(answer, props = {}) {
  setActivePinia(createPinia())
  const agents = useAgentsStore()
  const get = typeof answer === 'function'
    ? vi.spyOn(agents, 'getAgentPeerSkillEnforcement').mockImplementation(answer)
    : vi.spyOn(agents, 'getAgentPeerSkillEnforcement').mockResolvedValue(answer)
  const wrapper = mount(PeerSkillEnforcementLine, { props: { agentName: 'finance', ...props } })
  await flushPromises()
  return { wrapper, get }
}

const lineOf = (wrapper) => wrapper.find('[data-testid="peer-skill-enforcement"]')

describe('PeerSkillEnforcementLine (mounted)', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('reads the agent it is mounted for', async () => {
    const { get } = await mountWith({ reachable: false, hook: null })
    expect(get).toHaveBeenCalledWith('finance')
  })

  it.each(Object.entries(LINES))('a reachable agent whose hook is %s gets its line', async (hook, text) => {
    const { wrapper } = await mountWith({ reachable: true, hook })
    const line = lineOf(wrapper)
    expect(line.exists()).toBe(true)
    expect(line.text()).toBe(PREFIX + text)
    expect(line.attributes('title')).toContain(`(${hook})`)
  })

  it.each([
    ['an intact hook', { reachable: true, hook: 'ok' }],
    ['an agent that did not answer (stopped)', { reachable: true, hook: 'unknown' }],
    ['no peer that could reach a library skill', { reachable: false, hook: null }],
    ['a viewer the answer is withheld from', { reachable: null, hook: null }],
    ['an answer that does not say reachable', { reachable: null, hook: 'missing' }],
  ])('%s shows no line', async (_label, answer) => {
    const { wrapper } = await mountWith(answer)
    expect(lineOf(wrapper).exists()).toBe(false)
  })

  it('a failed read is no answer, never an error line', async () => {
    const { wrapper } = await mountWith(() => Promise.reject(new Error('boom')))
    expect(lineOf(wrapper).exists()).toBe(false)
    expect(wrapper.text()).toBe('')
  })

  it('reads again when the agent changes, and drops the old answer at once', async () => {
    let resolveSecond
    const answers = [
      Promise.resolve({ reachable: true, hook: 'missing' }),
      new Promise((r) => { resolveSecond = r }),
    ]
    const { wrapper, get } = await mountWith(() => answers.shift())
    expect(lineOf(wrapper).exists()).toBe(true)
    await wrapper.setProps({ agentName: 'ops' })
    expect(lineOf(wrapper).exists()).toBe(false)        // finance's line never shows under ops
    resolveSecond({ reachable: true, hook: 'ok' })
    await flushPromises()
    expect(get).toHaveBeenLastCalledWith('ops')
    expect(lineOf(wrapper).exists()).toBe(false)
  })

  it('reads again when the agent starts or stops', async () => {
    const { wrapper, get } = await mountWith({ reachable: true, hook: 'unknown' }, { agentStatus: 'stopped' })
    get.mockResolvedValue({ reachable: true, hook: 'predates' })
    await wrapper.setProps({ agentStatus: 'running' })
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(2)
    expect(lineOf(wrapper).text()).toBe(PREFIX + LINES.predates)
  })
})
