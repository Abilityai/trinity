// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import StepAudit from '@/components/onboarding/steps/StepAudit.vue'
import StepAgent from '@/components/onboarding/steps/StepAgent.vue'
import StepAboutYou from '@/components/onboarding/steps/StepAboutYou.vue'
import BuildQuestions from '@/components/onboarding/steps/BuildQuestions.vue'
import MeetGuideDialog from '@/components/onboarding/MeetGuideDialog.vue'
import { answerIntake, emptyIntake } from '@/components/portal/builder/portalBuild'

vi.mock('@/components/CreateAgentModal.vue', () => ({ default: { template: '<div data-testid="create-modal" />' } }))

const intake = emptyIntake()
const stubs = { FirstRunStepHeader: { template: '<div />' }, Teleport: true }

beforeEach(() => setActivePinia(createPinia()))

describe('starting the AI audit', () => {
  it('leaves setup for the chat straight away', async () => {
    const w = mount(StepAudit, { props: { ctx: { hasEmail: true, buildIntake: intake } }, global: { stubs } })
    await w.find('[data-testid="first-run-audit-start"]').trigger('click')
    // It says who they're about to meet first, and nothing has happened yet.
    expect(w.findComponent(MeetGuideDialog).props('visible')).toBe(true)
    expect(w.emitted('complete')).toBeUndefined()
    w.findComponent(MeetGuideDialog).vm.$emit('confirm')
    expect(w.emitted('update-intake')[0][0].audit).toBe('start')
    expect(w.emitted('complete')[0][0]).toEqual({ next: '/workspace/build', finish: true })
  })

  it('goes back to the audit once started, without restarting it', async () => {
    const w = mount(StepAudit, { props: { ctx: { hasEmail: true, buildIntake: { ...intake, audit: 'start' } } }, global: { stubs } })
    expect(w.text()).toContain('Go to your audit')
    await w.find('[data-testid="first-run-audit-start"]').trigger('click')
    w.findComponent(MeetGuideDialog).vm.$emit('confirm')
    expect(w.emitted('update-intake')).toBeUndefined()
    expect(w.emitted('complete')[0][0].finish).toBe(true)
  })
})

describe('the audit step follows what they told us', () => {
  const told = (answers) => ({ ...intake, answers: { role: 'CEO', software: 'none', agents: 'tried', company: 'Acme', ...answers }, done: true })

  it('makes talking it through with Atlas the main button for someone who knows exactly', async () => {
    const w = mount(StepAudit, { props: { ctx: { hasEmail: true, buildIntake: told({ plans: 'exact' }) } }, global: { stubs } })
    const main = w.find('[data-testid="first-run-audit-agent"]')
    expect(main.text()).toBe('Talk it through with Atlas')
    expect(w.find('[data-testid="first-run-audit-start"]').exists()).toBe(true)
    await main.trigger('click')
    w.findComponent(MeetGuideDialog).vm.$emit('confirm')
    expect(w.emitted('update-intake')[0][0].firstAgent).toEqual({ idea: '' })
    expect(w.emitted('complete')[0][0]).toEqual({ next: '/workspace/build', finish: true })
  })

  it('offers experienced people a short talk about their work instead', async () => {
    const w = mount(StepAudit, { props: { ctx: { hasEmail: true, buildIntake: told({ agents: 'built' }) } }, global: { stubs } })
    const quiet = w.find('[data-testid="first-run-audit-profile"]')
    expect(quiet.text()).toBe('Or just tell Atlas about your work (about 2 minutes)')
    await quiet.trigger('click')
    w.findComponent(MeetGuideDialog).vm.$emit('confirm')
    expect(w.emitted('update-intake')[0][0].profileChat).toBe(true)
    expect(w.emitted('complete')[0][0]).toEqual({ next: '/workspace/build', finish: true })
  })

  it('keeps it to the audit for everyone else', () => {
    const w = mount(StepAudit, { props: { ctx: { hasEmail: true, buildIntake: told({ agents: 'tried' }) } }, global: { stubs } })
    expect(w.find('[data-testid="first-run-audit-profile"]').exists()).toBe(false)
    expect(w.find('[data-testid="first-run-audit-agent"]').exists()).toBe(false)
  })
})

describe('the first agent', () => {
  const admin = { isAdmin: true, hasEmail: true, firstRun: true, buildIntake: { ...intake, audit: 'skip' } }

  it('offers nothing to pick after the audit, which recommends it', () => {
    const w = mount(StepAgent, { props: { ctx: { ...admin, buildIntake: { ...intake, audit: 'start' } } }, global: { stubs } })
    expect(w.find('[data-testid="first-run-agent-to-guide"]').exists()).toBe(true)
    expect(w.find('[data-testid="first-run-idea-research"]').exists()).toBe(false)
    expect(w.find('[data-testid="first-run-agent-scratch-start"]').exists()).toBe(false)
  })

  it('starts from scratch in the chat with the guide, not a form', async () => {
    const w = mount(StepAgent, { props: { ctx: admin }, global: { stubs } })
    await w.find('[data-testid="first-run-agent-scratch-start"]').trigger('click')
    w.findComponent(MeetGuideDialog).vm.$emit('confirm')
    expect(w.emitted('update-intake')[0][0].firstAgent).toEqual({ idea: '' })
    expect(w.emitted('complete')[0][0]).toEqual({ next: '/workspace/build', finish: true })
    expect(w.find('[data-testid="create-modal"]').exists()).toBe(false)
  })

  it('takes a starting point to the guide as an idea', async () => {
    const w = mount(StepAgent, { props: { ctx: admin }, global: { stubs } })
    await w.find('[data-testid="first-run-idea-writing"]').trigger('click')
    w.findComponent(MeetGuideDialog).vm.$emit('confirm')
    expect(w.emitted('update-intake')[0][0].firstAgent.idea).toMatch(/drafts reports/)
    expect(w.emitted('complete')[0][0].finish).toBe(true)
  })

  it('leaves the form for anyone without a guide', () => {
    const w = mount(StepAgent, { props: { ctx: { firstRun: true, buildIntake: intake } }, global: { stubs } })
    expect(w.find('[data-testid="first-run-purpose-blank"]').exists()).toBe(true)
    expect(w.find('[data-testid="first-run-agent-scratch-start"]').exists()).toBe(false)
  })
})

describe('the pop-up before the chat', () => {
  it('says who they meet, what happens and that it needs time to get ready', () => {
    const w = mount(MeetGuideDialog, { props: { visible: true, purpose: 'audit' }, global: { stubs: { ConfirmDialog: { props: ['title', 'message'], template: '<div>{{ title }} {{ message }}</div>' } } } })
    expect(w.text()).toContain("You're about to meet Atlas")
    expect(w.text()).toContain('first Trinity agent')
    expect(w.text()).toContain('a minute or two')
  })

  it('for the profile, says it is a short chat about their work', () => {
    const ConfirmDialog = { props: ['message', 'confirmText'], template: '<div>{{ message }} | {{ confirmText }}</div>' }
    const w = mount(MeetGuideDialog, { props: { visible: true, purpose: 'profile' }, global: { stubs: { ConfirmDialog } } })
    expect(w.text()).toContain('asks about your work, so Trinity knows what your job involves')
    expect(w.text()).toContain('about 2 minutes')
    expect(w.text()).toContain('| Go to Atlas')
  })
})

describe('without a sign-in email', () => {
  it("can't start the audit, and says what to do", () => {
    const w = mount(StepAudit, { props: { ctx: { hasEmail: false, buildIntake: intake } }, global: { stubs } })
    expect(w.find('[data-testid="first-run-audit-start"]').element.disabled).toBe(true)
    expect(w.text()).toContain('Add your sign-in email under About you first')
  })

  it("can't start a chat with the guide about a first agent", () => {
    const w = mount(StepAgent, { props: { ctx: { isAdmin: true, hasEmail: false, firstRun: true, buildIntake: { ...intake, audit: 'skip' } } }, global: { stubs } })
    expect(w.find('[data-testid="first-run-agent-needs-email"]').exists()).toBe(true)
    expect(w.find('[data-testid="first-run-agent-scratch-start"]').element.disabled).toBe(true)
    expect(w.find('[data-testid="first-run-idea-research"]').element.disabled).toBe(true)
  })
})

describe('who this is', () => {
  it('shows the sign-in email the answers are linked to, and lets them change it', async () => {
    const w = mount(StepAboutYou, { props: { ctx: { hasEmail: true, userEmail: 'a@example.com', buildIntake: intake } }, global: { stubs } })
    expect(w.find('[data-testid="first-run-identity-email"]').text()).toBe('a@example.com')
    await w.find('[data-testid="first-run-identity-change"]').trigger('click')
    expect(w.find('[data-testid="first-run-email-input"]').exists()).toBe(true)
  })

  it('asks for one when the account has none', () => {
    const w = mount(StepAboutYou, { props: { ctx: { hasEmail: false, buildIntake: intake } }, global: { stubs } })
    expect(w.text()).toContain('What email will you sign in with?')
    expect(w.find('[data-testid="first-run-identity-keep"]').exists()).toBe(false)
  })
})

describe("what's an agent, in the setup panel", () => {
  const answered = (agents) => ['role:CEO', 'software:none', `agents:${agents}`]
    .reduce((i, pair) => answerIntake(i, ...pair.split(':')), emptyIntake())

  it('shows a newcomer what an agent is before the next question', async () => {
    const w = mount(BuildQuestions, { props: { group: 'you', intake: answered('none') } })
    expect(w.find('[data-testid="what-is-an-agent"]').exists()).toBe(true)
    expect(w.find('[data-testid="first-run-choices-plans"]').exists()).toBe(false)
    await w.find('[data-testid="what-is-an-agent-done"]').trigger('click')
    const updates = w.emitted('update-intake') || []
    expect(updates.at(-1)[0].agentExplained).toBe(true)
  })

  it('goes straight on to what to build for someone who has built agents', () => {
    const w = mount(BuildQuestions, { props: { group: 'you', intake: answered('tried') } })
    expect(w.find('[data-testid="what-is-an-agent"]').exists()).toBe(false)
    expect(w.find('[data-testid="first-run-choices-plans"]').exists()).toBe(true)
  })
})
