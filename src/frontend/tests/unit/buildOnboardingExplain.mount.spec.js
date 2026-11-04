// @vitest-environment jsdom
/**
 * The Workspace onboarding window (for people who can build but aren't admins): right
 * after "I've never built one" comes "What's an agent?". The answer reaches the window's
 * props a tick after it is given, so this is only provable mounted, with a parent that
 * owns the answers the way Portal.vue does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import PortalBuildOnboarding from '@/components/portal/builder/PortalBuildOnboarding.vue'
import { answerIntake, emptyIntake, markAgentExplained } from '@/components/portal/builder/portalBuild'

vi.mock('@/stores/sessions', () => ({
  useSessionsStore: () => ({
    featureFlagsLoaded: true,
    featureFlagsFailed: false,
    claudeAuthConfigured: true,
    loadFeatureFlags: async () => {},
  }),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ principalId: 'person@example.com' }) }))
vi.mock('@/stores/clientPortal', () => ({
  useClientPortalStore: () => ({ deployBuildSystem: async () => ({}), fetchRoster: async () => {} }),
}))

const atAgents = () => answerIntake(answerIntake(emptyIntake(), 'role', 'CEO'), 'software', 'none')

function openWindow() {
  const intake = ref(atAgents())
  const Host = defineComponent({
    setup: () => () => h(PortalBuildOnboarding, {
      open: true,
      intake: intake.value,
      roster: [{ name: 'build-guide', owned: true }],
      onAnswer: (id, value) => { intake.value = answerIntake(intake.value, id, value) },
      onExplained: () => { intake.value = markAgentExplained(intake.value) },
    }),
  })
  return { wrapper: mount(Host, { attachTo: document.body }), intake }
}

const find = (testid) => document.querySelector(`[data-testid="${testid}"]`)
const choose = async (label) => {
  const button = [...document.querySelectorAll('[data-testid="build-onboarding-choices"] button')].find((b) => b.textContent.trim() === label)
  button.click()
  await flushPromises()
}

beforeEach(() => { document.body.innerHTML = '' })

describe("the onboarding window: what's an agent", () => {
  it('comes right after "I\'ve never built one", then the next question', async () => {
    const { wrapper, intake } = openWindow()
    await flushPromises()
    expect(find('build-onboarding-agents')).not.toBe(null)
    await choose("I've never built one")
    expect(find('what-is-an-agent')).not.toBe(null)
    expect(find('build-onboarding-progress').textContent.trim()).toBe('Step 5 of 9')
    find('what-is-an-agent-done').click()
    await flushPromises()
    expect(intake.value.agentExplained).toBe(true)
    expect(find('build-onboarding-plans')).not.toBe(null)
    wrapper.unmount()
  })

  it('is not shown to someone who has built a few', async () => {
    const { wrapper } = openWindow()
    await flushPromises()
    await choose("I've built a few")
    expect(find('what-is-an-agent')).toBe(null)
    expect(find('build-onboarding-plans')).not.toBe(null)
    // Optional, but it has its own answers: the website's "We don't have one" isn't one.
    expect(find('build-onboarding-no-website')).toBe(null)
    wrapper.unmount()
  })
})

describe('the onboarding window: the audit step', () => {
  const at = (plans) => {
    let i = answerIntake(answerIntake(answerIntake(emptyIntake(), 'role', 'CEO'), 'software', 'none'), 'agents', 'tried')
    i = answerIntake(i, 'company', 'Acme')
    return plans ? answerIntake(i, 'plans', plans) : i
  }
  const mountAt = (intake) => mount(PortalBuildOnboarding, { attachTo: document.body, props: { open: true, intake, roster: [{ name: 'build-guide', owned: true }] } })

  it('someone who knows exactly talks it through with Atlas first, and can still start the audit', async () => {
    const w = mountAt(at('exact'))
    await flushPromises()
    expect(find('build-onboarding-audit-agent').textContent.trim()).toBe('Talk it through with Atlas')
    expect(find('build-onboarding-audit-start')).not.toBe(null)
    find('build-onboarding-audit-agent').click()
    expect(w.emitted('finish')[0]).toEqual(['agent'])
    w.unmount()
  })

  it('everyone else is offered the audit first', async () => {
    const w = mountAt(at('rough'))
    await flushPromises()
    expect(find('build-onboarding-audit-agent')).toBe(null)
    expect(find('build-onboarding-audit-skip')).not.toBe(null)
    w.unmount()
  })
})
