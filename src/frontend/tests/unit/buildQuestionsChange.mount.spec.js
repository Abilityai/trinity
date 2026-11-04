// @vitest-environment jsdom
/**
 * A saved answer in the setup panel can be changed: someone who typed their website into
 * the company question has to be able to put it right. Changing what the research is
 * about, once it has started, starts it again with the new answers.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BuildQuestions from '@/components/onboarding/steps/BuildQuestions.vue'
import { answerIntake, changeAnswer, emptyIntake, markResearchStarted } from '@/components/portal/builder/portalBuild'

const answer = (intake, pairs) => Object.entries(pairs).reduce((i, [id, v]) => answerIntake(i, id, v), intake)
const you = answer(emptyIntake(), { role: 'Office manager', software: 'none', agents: 'tried', plans: 'not_yet' })
const company = markResearchStarted(answer(emptyIntake(), { company: 'https://acme-football.example', website: 'acme-football.example' }), 'exec-1')

// Mounted the way the panel uses it: every update comes back as the new intake.
function mountQuestions(group, intake) {
  const w = mount(BuildQuestions, { props: { group, intake } })
  const feed = async () => {
    const last = w.emitted('update-intake')?.at(-1)?.[0]
    if (last) await w.setProps({ intake: last })
  }
  return { w, feed }
}

describe('changing a saved answer in the setup panel', () => {
  it('a typed answer reopens with what was saved, and saving puts the new one in', async () => {
    const { w } = mountQuestions('company', company)
    await w.get('[data-testid="first-run-change-company"]').trigger('click')
    const input = w.get('[data-testid="first-run-question-input"] input, input[data-testid="first-run-question-input"]')
    expect(input.element.value).toBe('https://acme-football.example')
    await input.setValue('Acme Football, a youth football club')
    await w.get('form').trigger('submit')
    expect(w.emitted('update-intake').at(-1)[0].answers.company).toBe('Acme Football, a youth football club')
  })

  it('a choice answer reopens its choices, and picking another one changes it', async () => {
    const { w } = mountQuestions('you', you)
    await w.get('[data-testid="first-run-change-agents"]').trigger('click')
    await w.get('[data-testid="first-run-choices-agents"]').findAll('button').find((b) => b.text() === 'I build them all the time').trigger('click')
    expect(w.emitted('update-intake').at(-1)[0].answers.agents).toBe('built')
  })

  it('Cancel keeps the saved answer and changes nothing', async () => {
    const { w } = mountQuestions('company', company)
    await w.get('[data-testid="first-run-change-company"]').trigger('click')
    await w.get('[data-testid="first-run-change-cancel"]').trigger('click')
    expect(w.emitted('update-intake')).toBeUndefined()
    expect(w.get('[data-testid="first-run-answer-company"]').text()).toBe('https://acme-football.example')
  })

  it('after changing the company, the step is told again that everything is answered, so the research restarts', async () => {
    const { w, feed } = mountQuestions('company', company)
    const before = (w.emitted('all-done') || []).length
    await w.get('[data-testid="first-run-change-company"]').trigger('click')
    await w.get('input').setValue('Acme Football, a youth football club')
    await w.get('form').trigger('submit')
    await feed()
    expect(w.emitted('update-intake').at(-1)[0].research).toBeUndefined()
    expect((w.emitted('all-done') || []).length).toBe(before + 1)
  })
})

describe('changeAnswer', () => {
  it('a new company or website after the research started forgets that research, so it runs again', () => {
    expect(changeAnswer(company, 'company', 'Acme Football').research).toBeUndefined()
    expect(changeAnswer(company, 'website', 'acme.example').research).toBeUndefined()
  })

  it('the same answer again leaves the research alone', () => {
    expect(changeAnswer(company, 'company', 'https://acme-football.example').research.executionId).toBe('exec-1')
  })

  it('answers about the person never touch the research', () => {
    const both = markResearchStarted(answer(you, { company: 'Acme' }), 'exec-2')
    expect(changeAnswer(both, 'role', 'CEO').research.executionId).toBe('exec-2')
    expect(changeAnswer(both, 'role', 'CEO').answers.role).toBe('CEO')
  })
})
