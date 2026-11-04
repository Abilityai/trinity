import { describe, it, expect } from 'vitest'
import { newerIntake } from '@/stores/buildIntake'
import { BUILD_SCHEMA, chooseAudit, chooseFirstAgent, emptyIntake, firstMessagePending, forgetHandover, intakeMessage, markIntakeSent, normalizeState, skipIntake } from '@/components/portal/builder/portalBuild'
import { tourCardPosition, tourSteps } from '@/components/portal/builder/portalBuildTour'

const answers = { role: 'CTO', software: 'dev', agents: 'built', company: 'Acme' }
const done = { ...emptyIntake(), answers, done: true }

describe('the answers follow the account', () => {
  it('uses the account copy when there is nothing newer here', () => {
    const server = { answers, savedAt: 5 }
    expect(newerIntake(emptyIntake(), server).answers.company).toBe('Acme')
    expect(newerIntake({ ...emptyIntake(), savedAt: 5 }, server).answers.company).toBe('Acme')
  })

  it('keeps this browser copy when it is newer, or the account has none', () => {
    const local = { ...emptyIntake(), answers: { role: 'CFO' }, savedAt: 9 }
    expect(newerIntake(local, { answers, savedAt: 5 })).toBe(local)
    expect(newerIntake(local, undefined)).toBe(local)
  })
})

describe('what the guide is told', () => {
  it('asks what they have in mind when they start from scratch', () => {
    expect(intakeMessage(chooseFirstAgent(done, ''))).toMatch(/First agent: from scratch\. Ask me what I have in mind/)
  })

  it('passes on the idea they picked', () => {
    expect(intakeMessage(chooseFirstAgent(done, 'drafts reports'))).toMatch(/First agent: I'd like one that drafts reports\./)
  })

  it('still says a plain skip is a test build', () => {
    expect(intakeMessage(chooseAudit(done, 'skip'))).toMatch(/build any agent as a test/)
  })

  it('names the sign-in email when there is one', () => {
    expect(intakeMessage(done, [], [], 'a@example.com')).toContain('My sign-in email: a@example.com.')
    expect(intakeMessage(skipIntake(emptyIntake()), [], [], 'a@example.com')).toContain('My sign-in email: a@example.com.')
    expect(intakeMessage(done)).not.toContain('sign-in email')
  })

  it('tells the guide about a choice made after it already had their answers', () => {
    const state = { phase: 'chat' }
    const sent = markIntakeSent(done)
    expect(firstMessagePending(sent, state)).toBe(false)
    expect(firstMessagePending(chooseAudit(sent, 'start'), state)).toBe(true)
    expect(firstMessagePending(chooseFirstAgent(sent, ''), state)).toBe(true)
    expect(firstMessagePending(markIntakeSent(chooseFirstAgent(sent, '')), state)).toBe(false)
  })
})

describe('no more handover: the guide builds the agent itself', () => {
  it('forgets what the old handover left in saved answers', () => {
    const old = { ...done, buildChat: 'builder', handoffSent: 'h1', handoffBefore: ['x'], handoffReported: false }
    const now = forgetHandover(old)
    expect(now.answers).toEqual(answers)
    for (const field of ['buildChat', 'handoffSent', 'handoffBefore', 'handoffReported']) expect(now).not.toHaveProperty(field)
    expect(forgetHandover(done)).toBe(done)
  })

  it('no longer reads a brief for another agent off the guide', () => {
    expect(normalizeState({ schema: BUILD_SCHEMA, handoff: { id: 'a-1', brief: '/build-agent x' } })).not.toHaveProperty('handoff')
  })
})

describe('the Workspace tour', () => {
  it('covers agents, the chat, the company page, the Inbox and help, by name', () => {
    const steps = tourSteps('Atlas')
    expect(steps.map((s) => s.target)).toEqual(['agents', 'chat', 'company', 'inbox', 'help'])
    expect(steps[0].body).toContain('Atlas')
    expect(steps.map((s) => s.body).join(' ')).not.toContain('Agent Builder')
    expect(steps.find((s) => s.target === 'chat').body).toContain('Atlas builds it with you, right here')
  })

  it('keeps its card on screen wherever the part is', () => {
    const screen = { width: 1000, height: 700 }
    const card = { width: 340, height: 200 }
    const px = (v) => parseInt(v, 10)
    for (const rect of [
      { left: 0, top: 0, width: 260, height: 700 },
      { left: 400, top: 0, width: 600, height: 700 },
      { left: 0, top: 650, width: 1000, height: 40 },
    ]) {
      const p = tourCardPosition(rect, card, screen)
      expect(px(p.left)).toBeGreaterThanOrEqual(16)
      expect(px(p.left) + card.width).toBeLessThanOrEqual(screen.width)
      expect(px(p.top) + card.height).toBeLessThanOrEqual(screen.height)
    }
  })

  it('sits beside a part when there is room', () => {
    const p = tourCardPosition({ left: 0, top: 100, width: 260, height: 40 }, { width: 340, height: 200 }, { width: 1000, height: 700 })
    expect(p.left).toBe('274px')
    expect(p.top).toBe('100px')
  })
})
