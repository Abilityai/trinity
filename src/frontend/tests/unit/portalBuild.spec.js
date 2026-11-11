import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { describe, expect, it } from 'vitest'
import {
  BUILD_CANVAS_ID,
  BUILD_GUIDE_AGENT,
  BUILD_MANIFEST,
  BUILD_SCHEMA,
  BUILD_STAGES,
  INTAKE_QUESTIONS,
  answerIntake,
  buildAvailableFor,
  opensBuild,
  buildPhase,
  auditEmphasis,
  auditIntro,
  auditLead,
  auditSkipDestination,
  clearResearch,
  markResearchStarted,
  researchExecutionId,
  researchFailureMessage,
  researchMessage,
  researchView,
  companyFromProfile,
  companyToPublish,
  rebindToGuide,
  keepGuideCard,
  shouldStartResearch,
  chooseAudit,
  chooseProfileChat,
  finishIntake,
  finishProfileAction,
  firstMessagePending,
  intakeGroupDone,
  markIntakeSent,
  onboardingStartStep,
  onboardingSteps,
  reopenIntake,
  shouldOpenOnboarding,
  canvasToState,
  credentialsLinkFor,
  deployFailureMessage,
  emptyIntake,
  experienceRoute,
  guideCannotCreate,
  guideNameFor,
  isGuideName,
  isBuildCrew,
  myGuideCard,
  buildManifest,
  instanceAgentsFor,
  intakeMessage,
  liveAgentFor,
  loadIntake,
  markAgentExplained,
  needsAgentExplainer,
  normalizeState,
  saveIntake,
  skipIntake,
  stageIndex,
  stepState,
  startingLevel,
  worldView,
} from '../../src/components/portal/builder/portalBuild'

function memoryStorage() {
  const m = new Map()
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    _map: m,
  }
}

const finished = () => {
  let i = emptyIntake()
  i = answerIntake(i, 'role', 'Head of Operations')
  i = answerIntake(i, 'software', 'none')
  i = answerIntake(i, 'agents', 'none')
  i = answerIntake(i, 'company', 'Acme Freight')
  return i
}

describe('startingLevel: where a person starts', () => {
  it('no software and no agent experience starts with the tiny example', () => {
    expect(startingLevel({ software: 'none', agents: 'none' })).toBe('newcomer')
    expect(startingLevel({ software: 'some', agents: 'none' })).toBe('newcomer')
  })

  it('having tried agents, or being a developer, starts at the audit', () => {
    expect(startingLevel({ software: 'none', agents: 'tried' })).toBe('practitioner')
    expect(startingLevel({ software: 'developer', agents: 'none' })).toBe('practitioner')
  })

  it('having built agents starts at the deep end', () => {
    expect(startingLevel({ software: 'none', agents: 'built' })).toBe('builder')
    expect(startingLevel({ software: 'developer', agents: 'built' })).toBe('builder')
  })

  it('unknown answers never promote anyone', () => {
    expect(startingLevel({})).toBe('newcomer')
    expect(startingLevel({ software: 'wizard', agents: 'guru' })).toBe('newcomer')
  })
})

describe('the intake', () => {
  it('asks about the person first, then the company', () => {
    expect(INTAKE_QUESTIONS.map((q) => q.id)).toEqual(['role', 'software', 'agents', 'plans', 'company', 'website'])
    expect(INTAKE_QUESTIONS.filter((q) => q.group === 'you').map((q) => q.id)).toEqual(['role', 'software', 'agents', 'plans'])
  })

  it('moves to the next unanswered question and finishes after the last that counts', () => {
    let i = emptyIntake()
    expect(i.current).toBe('role')
    i = answerIntake(i, 'role', '  Ops  ')
    expect(i.answers.role).toBe('Ops')
    expect(i.current).toBe('software')
    expect(i.done).toBe(false)
    i = answerIntake(i, 'software', 'none')
    i = answerIntake(i, 'agents', 'none')
    expect(intakeGroupDone(i, 'you')).toBe(true)
    expect(intakeGroupDone(i, 'company')).toBe(false)
    expect(i.current).toBe('company')
    i = answerIntake(i, 'company', 'Acme Freight')
    expect(i.current).toBe(null)
    expect(i.done).toBe(true)
    expect(i.skipped).toBe(false)
  })

  it('the website is optional, and goes to the guide when given', () => {
    const i = answerIntake(finished(), 'website', ' acmefreight.com ')
    expect(i.done).toBe(true)
    expect(intakeMessage(i)).toContain('Company website: acmefreight.com.')
    expect(intakeMessage(finished())).not.toContain('Company website')
  })

  it('someone who skipped can still answer one more, and stays incomplete until the rest', () => {
    let i = skipIntake(answerIntake(emptyIntake(), 'role', 'Ops'))
    i = answerIntake(i, 'company', 'Acme')
    expect(i).toMatchObject({ done: true, skipped: true })
    i = answerIntake(answerIntake(i, 'software', 'none'), 'agents', 'none')
    expect(i).toMatchObject({ done: true, skipped: false })
  })

  it('refuses a blank answer and a choice that was not offered', () => {
    const start = emptyIntake()
    expect(answerIntake(start, 'role', '   ')).toEqual(start)
    const atSoftware = answerIntake(answerIntake(start, 'company', 'A'), 'role', 'B')
    expect(answerIntake(atSoftware, 'software', 'wizard')).toEqual(atSoftware)
  })

  it('bounds free text so the first message cannot overrun the composer limit', () => {
    const i = answerIntake(emptyIntake(), 'role', 'x'.repeat(5000))
    expect(i.answers.role.length).toBeLessThanOrEqual(200)
    expect(intakeMessage(finished()).length).toBeLessThan(8000)
  })

  it('skipping leaves an incomplete profile and keeps what was already answered', () => {
    const i = skipIntake(answerIntake(emptyIntake(), 'company', 'Acme'))
    expect(i.done).toBe(true)
    expect(i.skipped).toBe(true)
    expect(i.answers.company).toBe('Acme')
  })

  it('round-trips per person and survives broken or foreign storage', () => {
    const s = memoryStorage()
    saveIntake(s, 'a@example.com', finished())
    expect(loadIntake(s, 'a@example.com').answers.company).toBe('Acme Freight')
    expect(loadIntake(s, 'b@example.com')).toEqual(emptyIntake())
    s.setItem([...s._map.keys()][0], '{not json')
    expect(loadIntake(s, 'a@example.com')).toEqual(emptyIntake())
    expect(loadIntake(null, 'a@example.com')).toEqual(emptyIntake())
    expect(() => saveIntake(null, 'a@example.com', finished())).not.toThrow()
    const thrower = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('blocked') } }
    expect(loadIntake(thrower, 'a@example.com')).toEqual(emptyIntake())
    expect(() => saveIntake(thrower, 'a@example.com', finished())).not.toThrow()
  })

  it('the first message states the profile and the starting point in plain words', () => {
    const text = intakeMessage(finished())
    expect(text).toContain('Acme Freight')
    expect(text).toContain('Head of Operations')
    expect(text).toMatch(/level: newcomer/i)
  })

  it('tells the guide what is already on the instance, which it cannot see itself', () => {
    const roster = [{ name: 'finance-close', display_label: 'Finance Close' }, { name: 'support' }]
    expect(intakeMessage(finished(), roster)).toContain('Agents already on this instance: Finance Close, support.')
    expect(intakeMessage(skipIntake(emptyIntake()), roster)).toContain('Finance Close')
    expect(intakeMessage(finished(), [])).toContain('no other agents of mine')
  })

  it('tells the guide about the audit, so it knows how to start', () => {
    expect(intakeMessage(chooseAudit(finished(), 'start'))).toMatch(/AI audit: yes/)
    expect(intakeMessage(finished())).toMatch(/AI audit: yes/)
    expect(intakeMessage(chooseAudit(finished(), 'skip'))).toMatch(/AI audit: skipped.*test/)
  })

  it('tells the guide which agents are only samples', () => {
    const roster = [{ name: 'cornelius', display_label: 'Cornelius' }, { name: 'finance-close' }]
    const text = intakeMessage(finished(), roster, ['cornelius'])
    expect(text).toContain('Agents already on this instance: finance-close.')
    expect(text).toContain('Sample agents that came with Trinity: Cornelius.')
    expect(intakeMessage(finished(), [{ name: 'cornelius' }], ['cornelius'])).toContain('no other agents of mine')
  })

  it('a skipped intake says so, keeping whatever they did answer', () => {
    const text = intakeMessage(skipIntake(answerIntake(emptyIntake(), 'role', 'CTO')))
    expect(text).toMatch(/skipped the setup questions/)
    expect(text).toContain('My role: CTO.')
    expect(text).toMatch(/AI audit: skipped/)
    expect(text).not.toContain('not given')
    expect(intakeMessage(chooseAudit(skipIntake(emptyIntake()), 'start'))).toMatch(/AI audit: yes/)
  })
})

describe('routes by experience', () => {
  it('labels the agent answers the way people describe themselves, keeping the saved ids', () => {
    const agents = INTAKE_QUESTIONS.find((q) => q.id === 'agents')
    expect(agents.choices.map((c) => [c.id, c.label])).toEqual([
      ['none', "I've never built one"], ['tried', "I've built a few"], ['built', 'I build them all the time'],
    ])
  })

  it('asks whether they know what to build, right after the agents question, without requiring it', () => {
    const ids = INTAKE_QUESTIONS.map((q) => q.id)
    expect(ids.indexOf('plans')).toBe(ids.indexOf('agents') + 1)
    const plans = INTAKE_QUESTIONS.find((q) => q.id === 'plans')
    expect(plans).toMatchObject({ group: 'you', optional: true, prompt: "Do you know what you'd like to build first?" })
    expect(plans.choices.map((c) => [c.id, c.label])).toEqual([
      ['not_yet', 'Not yet'], ['rough', 'I have a rough idea'], ['exact', 'I know exactly'],
    ])
  })

  it('picks the route from the agents answer alone', () => {
    expect(experienceRoute({ agents: 'none', software: 'developer' })).toBe('newcomer')
    expect(experienceRoute({ agents: 'tried' })).toBe('standard')
    expect(experienceRoute({ agents: 'built', software: 'none' })).toBe('experienced')
  })

  it('a missing or unknown answer takes the gentlest route', () => {
    expect(experienceRoute({})).toBe('newcomer')
    expect(experienceRoute(undefined)).toBe('newcomer')
    expect(experienceRoute({ agents: 'guru' })).toBe('newcomer')
  })

  it('someone who finished before the new question existed is not sent back to it', () => {
    const before = finished()
    expect(before.done).toBe(true)
    expect(intakeGroupDone(before, 'you')).toBe(true)
    expect(onboardingStartStep(before)).toBe('audit')
  })

  it('an answer saved under the old labels reads with the new ones', () => {
    const old = { ...finished(), answers: { ...finished().answers, agents: 'tried' } }
    expect(intakeMessage(old)).toContain("AI agent experience: I've built a few.")
  })

  it('tells the guide whether they know what to build, only when they said', () => {
    expect(intakeMessage(answerIntake(finished(), 'plans', 'exact'))).toContain('What to build first: I know exactly.')
    expect(intakeMessage(finished())).not.toContain('What to build first')
  })
})

describe('the audit step', () => {
  const withPlans = (plans, agents = 'tried') => ({ ...finished(), answers: { ...finished().answers, agents, plans } })

  it('pushes the audit unless they know exactly what to build', () => {
    expect(auditEmphasis(withPlans('not_yet'))).toBe('audit')
    expect(auditEmphasis(withPlans('rough'))).toBe('audit')
    expect(auditEmphasis(finished())).toBe('audit')
    expect(auditEmphasis(withPlans('exact'))).toBe('agent')
  })

  it('still recommends the audit to someone who knows what they want', () => {
    expect(auditLead(withPlans('exact'))).toBe("You know what you'd like to build, so Atlas can plan it with you now. The AI audit is still recommended: it looks across your team's work for where an agent saves the most time, and takes about 10 minutes.")
    expect(auditLead(withPlans('not_yet'))).toBe(auditIntro(withPlans('not_yet')))
  })

  it('skipping it sends most people to Atlas, and experienced people to the dashboard', () => {
    expect(auditSkipDestination(withPlans('rough', 'tried')).next).toBe('/workspace/build')
    const experienced = auditSkipDestination(withPlans('rough', 'built'))
    // Explicit, so arriving from the Workspace doesn't turn it into the Build page.
    expect(experienced.next).toBe('/')
    expect(experienced.label).toBe("Finishing setup shows you round Trinity's dashboard and its special features. Atlas is in the Workspace whenever you want it.")
  })
})

describe('canvasToState: reading the guide agent\'s canvas', () => {
  const canvas = (data, kind = 'json') => ({
    id: BUILD_CANVAS_ID,
    blocks: [{ id: 'note', kind: 'markdown', markdown: 'hi' }, { id: 'state', kind, payload: data }],
  })

  it('reads the state block', () => {
    const s = canvasToState(canvas({ schema: BUILD_SCHEMA, stage: 'audit', company: { name: 'Acme' } }))
    expect(s.stage).toBe('audit')
    expect(s.company.name).toBe('Acme')
  })

  it('answers null for no canvas, no state block, or another schema', () => {
    expect(canvasToState(null)).toBe(null)
    expect(canvasToState({ blocks: [] })).toBe(null)
    expect(canvasToState(canvas({ schema: 'other/v9', stage: 'audit' }))).toBe(null)
    expect(canvasToState(canvas('a string'))).toBe(null)
  })
})

describe('normalizeState: an agent wrote this, so nothing is trusted', () => {
  it('falls back on an unknown stage and unknown statuses', () => {
    const s = normalizeState({
      schema: BUILD_SCHEMA,
      stage: 'launch-the-rockets',
      agents: [{ id: 'a', name: 'A', status: 'exploding', skills: [{ name: 's', status: 'odd' }], connections: [{ platform: 'p', status: 'odd' }] }],
    })
    expect(s.stage).toBe('profile')
    expect(s.agents[0].status).toBe('designing')
    expect(s.agents[0].skills[0].status).toBe('planned')
    expect(s.agents[0].connections[0].status).toBe('needed')
  })

  it("keeps that they moved on from Level 3 only when the guide wrote exactly true", () => {
    expect(normalizeState({ schema: BUILD_SCHEMA, keys_not_needed: true }).keys_not_needed).toBe(true)
    for (const odd of ['true', 1, undefined, {}]) expect(normalizeState({ schema: BUILD_SCHEMA, keys_not_needed: odd }).keys_not_needed).toBe(false)
  })

  it('drops malformed rows instead of throwing', () => {
    const s = normalizeState({
      schema: BUILD_SCHEMA,
      departments: [null, 3, { name: 'Sales', opportunities: ['nope', { title: 'Lead triage' }] }],
      data_sources: 'many',
      agents: [{ name: '' }, { name: 'Good' }],
    })
    expect(s.departments).toHaveLength(1)
    expect(s.departments[0].opportunities).toHaveLength(1)
    expect(s.data_sources).toEqual([])
    expect(s.agents.map((a) => a.name)).toEqual(['Good'])
  })

  it('bounds what it keeps', () => {
    const s = normalizeState({
      schema: BUILD_SCHEMA,
      agents: Array.from({ length: 500 }, (_, i) => ({ name: `a${i}` })),
      context_md: 'x'.repeat(500000),
    })
    expect(s.agents.length).toBeLessThanOrEqual(50)
    expect(s.context_md.length).toBeLessThanOrEqual(60000)
  })

  it('keeps variable NAMES and refuses anything shaped like a value', () => {
    const s = normalizeState({
      schema: BUILD_SCHEMA,
      agents: [{ name: 'A', variables: ['HUBSPOT_TOKEN', 'HUBSPOT_TOKEN=abc123', 'sk-live abc', 7] }],
    })
    expect(s.agents[0].variables).toEqual(['HUBSPOT_TOKEN'])
  })

  it('a schedule is off unless the agent says it is on', () => {
    const s = normalizeState({
      schema: BUILD_SCHEMA,
      agents: [{ name: 'A', schedules: [{ name: 'daily' }, { name: 'weekly', enabled: true }] }],
    })
    expect(s.agents[0].schedules.map((x) => x.enabled)).toEqual([false, true])
  })
})

describe('orchestrators and keys', () => {
  it('keeps which agents an orchestrator runs', () => {
    const s = normalizeState({ schema: BUILD_SCHEMA, agents: [{ name: 'Ops lead', kind: 'orchestrator', manages: ['Email triage', 3, ''] }] })
    expect(s.agents[0].kind).toBe('orchestrator')
    expect(s.agents[0].manages).toEqual(['Email triage'])
    expect(normalizeState({ schema: BUILD_SCHEMA, agents: [{ name: 'A' }] }).agents[0].manages).toEqual([])
  })

  it('links to where the values go, escaping the name', () => {
    expect(credentialsLinkFor('a b')).toBe('/agents/a%20b?tab=credentials')
  })
})

describe('worldView: what the right column shows', () => {
  it('fills from the intake before the agent has written anything', () => {
    const v = worldView(null, finished())
    expect(v.company.name).toBe('Acme Freight')
    expect(v.person.role).toBe('Head of Operations')
    expect(v.level).toBe('newcomer')
    expect(v.stage).toBe('profile')
    expect(v.profileComplete).toBe(false)
    expect(v.profileIncomplete).toBe(false)
  })

  it('shows answers while the intake is still going', () => {
    const v = worldView(null, answerIntake(emptyIntake(), 'company', 'Acme'))
    expect(v.company.name).toBe('Acme')
    expect(v.level).toBe(null)
  })

  it('marks a skipped profile incomplete, with no level', () => {
    const v = worldView(null, skipIntake(emptyIntake()))
    expect(v.profileComplete).toBe(false)
    expect(v.level).toBe(null)
  })

  it('nudges about the audit only after a skip, and not during the audit', () => {
    expect(worldView(null, skipIntake(emptyIntake())).auditNudge).toBe(true)
    expect(worldView(null, chooseAudit(finished(), 'skip')).auditNudge).toBe(true)
    expect(worldView(null, chooseAudit(finished(), 'start')).auditNudge).toBe(false)
    expect(worldView(null, finished()).auditNudge).toBe(false)
    const doing = normalizeState({ schema: BUILD_SCHEMA, audit: { status: 'in_progress' } })
    expect(worldView(doing, skipIntake(emptyIntake())).auditNudge).toBe(false)
    const done = normalizeState({ schema: BUILD_SCHEMA, audit: { status: 'done' } })
    expect(worldView(done, skipIntake(emptyIntake())).auditNudge).toBe(false)
  })

  it('the agent\'s state wins over the intake', () => {
    const state = normalizeState({
      schema: BUILD_SCHEMA,
      stage: 'build',
      company: { name: 'Acme Freight GmbH' },
      level: 'builder',
      profile_complete: true,
    })
    const v = worldView(state, skipIntake(emptyIntake()))
    expect(v.company.name).toBe('Acme Freight GmbH')
    expect(v.stage).toBe('build')
    expect(v.level).toBe('builder')
    expect(v.profileComplete).toBe(true)
  })

  it('finishing the audit later completes the profile even if the state omits the flag', () => {
    const state = normalizeState({ schema: BUILD_SCHEMA, audit: { status: 'done' } })
    expect(worldView(state, skipIntake(emptyIntake())).profileComplete).toBe(true)
  })

  it('without an intake or a state it says nothing is known yet', () => {
    const v = worldView(null, emptyIntake())
    expect(v.company.name).toBe('')
    expect(v.agents).toEqual([])
  })
})

describe('the profile: Trinity knows who they are and what the company does', () => {
  const state = (over = {}) => normalizeState({ schema: BUILD_SCHEMA, ...over })

  it('is complete once the guide says so, or once the audit is done', () => {
    expect(worldView(state({ profile_complete: true }), finished()).profileComplete).toBe(true)
    expect(worldView(state({ audit: { status: 'done' } }), finished()).profileComplete).toBe(true)
  })

  it('answering the questions is no longer enough on its own, but it is no alarm: the guide fills the rest in as they talk', () => {
    const v = worldView(state(), finished())
    expect(v.profileComplete).toBe(false)
    // Red is for questions left unanswered. With all of them answered the profile is in
    // progress with the guide, said calmly, with one offer to tell it about their work.
    expect(v.profileIncomplete).toBe(false)
    expect(v.profileNote).toBe('talk')
    expect(stepState('profile', v)).toBe('filling')
    // Once they have asked for that talk, the offer is not made again.
    const asked = worldView(state(), chooseProfileChat(finished()))
    expect(asked.profileIncomplete).toBe(false)
    expect(asked.profileNote).toBe('filling')
    expect(chooseProfileChat(finished()).profileAsked).toBe(true)
  })

  it('says nothing about the profile once it is complete, or while nothing is open', () => {
    expect(worldView(state({ profile_complete: true }), finished()).profileNote).toBe('')
    expect(worldView(state({ audit: { status: 'in_progress' } }), finished()).profileNote).toBe('')
    expect(worldView(null, finished()).profileNote).toBe('')
    // Questions skipped: that is the red one, not a note.
    expect(worldView(null, skipIntake(emptyIntake())).profileNote).toBe('')
  })

  it('is never red while the audit is running, or before the guide has written anything', () => {
    expect(worldView(state({ audit: { status: 'in_progress' } }), finished()).profileIncomplete).toBe(false)
    expect(worldView(null, finished()).profileIncomplete).toBe(false)
  })

  it('a skipped intake is red straight away, as before', () => {
    expect(worldView(null, skipIntake(emptyIntake())).profileIncomplete).toBe(true)
  })

  it('red opens the questions while one is unanswered, otherwise the chat with Atlas', () => {
    expect(finishProfileAction(skipIntake(emptyIntake()))).toBe('questions')
    expect(finishProfileAction(finished())).toBe('atlas')
  })

  it('asking Atlas to finish the profile tells it so, once, and keeps a started audit', () => {
    const asked = chooseProfileChat({ ...finished(), audit: 'start' })
    expect(asked.audit).toBe('start')
    expect(firstMessagePending(asked, state())).toBe(true)
    expect(intakeMessage(asked)).toContain("Profile: I'd like to tell you about my work first, so you know what my job involves.")
    expect(intakeMessage(markIntakeSent(asked))).not.toContain('Profile:')
    expect(chooseProfileChat(finished()).audit).toBe('skip')
  })

  it('never says they want a test agent when what they asked for is the talk about their work', () => {
    const text = intakeMessage(chooseProfileChat(finished()))
    expect(text).toContain('AI audit: skipped for now.')
    expect(text).not.toContain('build any agent as a test')
  })

  it('once the guide has their answers, asking for the talk sends just that', () => {
    const text = intakeMessage(chooseProfileChat(markIntakeSent(finished())))
    expect(text).toBe("Profile: I'd like to tell you about my work first, so you know what my job involves.")
  })
})

describe("the guide's template and the profile", () => {
  const guide = readFileSync(fileURLToPath(new URL('../../../../config/agent-templates/build-guide/CLAUDE.md', import.meta.url)), 'utf8')
  const talk = readFileSync(fileURLToPath(new URL('../../../../config/agent-templates/build-guide/.claude/skills/about-your-work/SKILL.md', import.meta.url)), 'utf8')

  it('the state it copies starts with the profile not complete', () => {
    expect(guide).toContain('"profile_complete": false')
    expect(guide).not.toContain('"profile_complete": true')
  })

  it('research that could not run does not keep the profile incomplete for good', () => {
    expect(guide).toMatch(/research couldn't run/)
    expect(talk).toMatch(/research couldn't run/)
  })
})

describe('liveAgentFor: "live" is checked against the roster, not believed', () => {
  const roster = [{ name: 'support-triage', availability: 'ready' }]

  it('finds the agent when it really exists', () => {
    expect(liveAgentFor({ live_name: 'support-triage' }, roster)?.name).toBe('support-triage')
  })

  it('answers null when the claimed agent is not on the roster', () => {
    expect(liveAgentFor({ live_name: 'ghost' }, roster)).toBe(null)
    expect(liveAgentFor({}, roster)).toBe(null)
    expect(liveAgentFor({ live_name: 'support-triage' }, null)).toBe(null)
  })
})

describe('instanceAgentsFor: the world is the person\'s real instance', () => {
  const roster = [
    { name: 'build-guide' },
    { name: 'agent-builder' },
    { name: 'support-triage' },
    { name: 'finance-close', display_label: 'Finance Close', availability: 'stopped' },
  ]

  it('lists what already runs on the instance, without the build crew itself', () => {
    expect(instanceAgentsFor(roster, []).map((a) => a.name)).toEqual(['support-triage', 'finance-close'])
  })

  it('does not list an agent twice when the guide already reports it', () => {
    const reported = [{ id: 'x', name: 'Support triage', live_name: 'support-triage' }]
    expect(instanceAgentsFor(roster, reported).map((a) => a.name)).toEqual(['finance-close'])
  })

  it('tolerates a missing roster', () => {
    expect(instanceAgentsFor(null, [])).toEqual([])
  })
})

describe('one Atlas per person', () => {
  it('names a new Atlas from the username, without personal data, and steps past a held name', () => {
    const name = guideNameFor('person@example.com')
    expect(name).toMatch(/^atlas-[0-9a-f]{8}$/)
    expect(guideNameFor('person@example.com')).toBe(name)
    expect(guideNameFor(' Person@Example.com ')).toBe(name)
    expect(guideNameFor('other@example.com')).not.toBe(name)
    expect(guideNameFor('person@example.com', [name])).toBe(`${name}-2`)
    expect(guideNameFor('person@example.com', [name, `${name}-2`])).toBe(`${name}-3`)
  })

  it('knows guide names and the old crew', () => {
    expect(isGuideName('build-guide')).toBe(true)
    expect(isGuideName('atlas-0a1b2c3d')).toBe(true)
    expect(isGuideName('atlas-0a1b2c3d-2')).toBe(true)
    expect(isGuideName('atlas-helper')).toBe(false)
    expect(isGuideName('atlas-0a1b2c3d-10')).toBe(false)
    expect(isBuildCrew('agent-builder')).toBe(true)
    expect(isBuildCrew('atlas-0a1b2c3d')).toBe(true)
    expect(isBuildCrew('researcher')).toBe(false)
  })

  it('finds only the Atlas this person owns', () => {
    const roster = [
      { name: 'atlas-11111111', owned: false, owner: 'colleague@example.com' },
      { name: 'build-guide', owned: false },
      { name: 'atlas-22222222', owned: true },
    ]
    expect(myGuideCard(roster).name).toBe('atlas-22222222')
    expect(myGuideCard(roster.slice(0, 2))).toBe(null)
    expect(myGuideCard(null)).toBe(null)
    // The original guide stays its owner's.
    expect(myGuideCard([{ name: 'atlas-22222222', owned: true }, { name: 'build-guide', owned: true }]).name).toBe('build-guide')
  })

  it('deploys one Atlas, with the grant to create agents, and no Agent Builder', () => {
    const yaml = buildManifest('atlas-22222222')
    expect(yaml).toContain('  atlas-22222222:\n    template: local:build-guide')
    expect(yaml).toMatch(/atlas-22222222:[\s\S]*grants:\s*\n\s*- agents\.manage/)
    expect(yaml).not.toContain('agent-builder')
    expect(yaml).toContain('preset: none')
    expect(yaml).toContain('naming: bare')
  })

  it('keeps the manifest file and the built-in copy the same', () => {
    const shipped = readFileSync(
      fileURLToPath(new URL('../../../../config/manifests/workspace-build.yaml', import.meta.url)),
      'utf8',
    )
    expect(buildManifest(BUILD_GUIDE_AGENT)).toBe(shipped)
    expect(BUILD_MANIFEST).toBe(shipped)
  })

  it("leaves every guide and the old Agent Builder out of the person's agents", () => {
    const roster = [{ name: 'atlas-22222222' }, { name: 'agent-builder' }, { name: 'build-guide' }, { name: 'researcher' }]
    expect(instanceAgentsFor(roster, []).map((a) => a.name)).toEqual(['researcher'])
  })

  it('says the guide cannot create agents only when the instance says so', () => {
    expect(guideCannotCreate({ grants: [{ capability: 'agents.manage', granted: false }] })).toBe(true)
    expect(guideCannotCreate({ grants: [{ capability: 'agents.manage', granted: true }] })).toBe(false)
    expect(guideCannotCreate({ grants: [{ capability: 'skills.manage', granted: false }] })).toBe(false)
    expect(guideCannotCreate({})).toBe(false)
    expect(guideCannotCreate(null)).toBe(false)
  })
})

describe('keys left for later', () => {
  it('is read from the world as a plain yes or no', () => {
    expect(normalizeState({ schema: BUILD_SCHEMA, keys_later: true }).keys_later).toBe(true)
    expect(normalizeState({ schema: BUILD_SCHEMA, keys_later: 'yes' }).keys_later).toBe(false)
    expect(normalizeState({ schema: BUILD_SCHEMA }).keys_later).toBe(false)
  })
})

describe('buildAvailableFor: who gets the door', () => {
  it('is for people who can make agents', () => {
    expect(buildAvailableFor({ isPlatformSession: true, workspaceOnly: false })).toBe(true)
  })

  it('is closed to external clients and Workspace-only members', () => {
    expect(buildAvailableFor({ isPlatformSession: false, workspaceOnly: false })).toBe(false)
    expect(buildAvailableFor({ isPlatformSession: true, workspaceOnly: true })).toBe(false)
    expect(buildAvailableFor({})).toBe(false)
  })

  it('is closed to an operator, who has the Dashboard but may not create agents', () => {
    const on = { isPlatformSession: true, workspaceOnly: false }
    expect(buildAvailableFor({ ...on, role: 'operator' })).toBe(false)
    expect(buildAvailableFor({ ...on, role: 'creator' })).toBe(true)
    expect(buildAvailableFor({ ...on, role: 'admin' })).toBe(true)
    // A role still loading closes nothing: the server refuses whatever it must.
    expect(buildAvailableFor({ ...on, role: undefined })).toBe(true)
    expect(buildAvailableFor({ ...on, role: '' })).toBe(true)
  })
})

describe('buildPhase: what the left column shows', () => {
  const ready = {
    rosterLoaded: true, rosterError: '', hasGuide: true,
    claude: { loaded: true, failed: false, configured: true },
    canvasRead: true, threadsLoaded: true, intakeDone: false,
  }

  it('waits for the roster', () => {
    expect(buildPhase({ ...ready, rosterLoaded: false })).toBe('loading')
  })

  it('says so when the roster failed, rather than offering setup', () => {
    expect(buildPhase({ ...ready, rosterError: 'boom' })).toBe('failed')
    expect(buildPhase({ ...ready, rosterLoaded: false, rosterError: 'boom' })).toBe('failed')
  })

  it('asks the questions first, because they need neither Claude nor the guide', () => {
    const noClaude = { loaded: true, failed: false, configured: false }
    expect(buildPhase({ ...ready, claude: noClaude })).toBe('intake')
    expect(buildPhase({ ...ready, hasGuide: false })).toBe('intake')
    expect(buildPhase({ ...ready, hasGuide: false, claude: noClaude })).toBe('intake')
  })

  it('then asks for Claude: without it no agent can answer', () => {
    const noClaude = { loaded: true, failed: false, configured: false }
    expect(buildPhase({ ...ready, intakeDone: true, claude: noClaude })).toBe('connect')
    expect(buildPhase({ ...ready, intakeDone: true, hasGuide: false, claude: noClaude })).toBe('connect')
  })

  it('does not read a failed flags read as "Claude is not connected"', () => {
    expect(buildPhase({ ...ready, intakeDone: true, claude: { loaded: true, failed: true, configured: false } })).toBe('chat')
  })

  it('waits for the flags rather than flashing the wrong step', () => {
    expect(buildPhase({ ...ready, claude: { loaded: false, failed: false, configured: false } })).toBe('loading')
  })

  it('offers setup once the questions are done and the guide is missing', () => {
    expect(buildPhase({ ...ready, intakeDone: true, hasGuide: false })).toBe('setup')
  })

  it('does not wait on a canvas that cannot exist yet', () => {
    expect(buildPhase({ ...ready, hasGuide: false, canvasRead: false })).toBe('intake')
  })

  it('waits for the canvas and the chat list, then asks or chats', () => {
    expect(buildPhase({ ...ready, canvasRead: false })).toBe('loading')
    expect(buildPhase({ ...ready, threadsLoaded: false })).toBe('loading')
    expect(buildPhase(ready)).toBe('intake')
    expect(buildPhase({ ...ready, intakeDone: true })).toBe('chat')
  })

  it('someone the guide already knows goes straight to connecting, without new questions', () => {
    const noClaude = { loaded: true, failed: false, configured: false }
    expect(buildPhase({ ...ready, intakeDone: true, claude: noClaude })).toBe('connect')
  })
})

describe('shouldOpenOnboarding: when the window opens by itself', () => {
  const fresh = {
    buildAvailable: true,
    threads: [],
    threadsTrusted: true,
    roster: [{ name: 'build-guide' }, { name: 'agent-builder' }],
    intake: { done: false },
  }

  it('opens for someone with nothing here yet', () => {
    expect(shouldOpenOnboarding(fresh)).toBe(true)
    expect(shouldOpenOnboarding({ ...fresh, roster: [] })).toBe(true)
  })

  it('stays shut for anyone who cannot build', () => {
    expect(shouldOpenOnboarding({ ...fresh, buildAvailable: false })).toBe(false)
  })

  it('stays shut for anyone with a chat', () => {
    expect(shouldOpenOnboarding({ ...fresh, threads: [{ id: 's1' }] })).toBe(false)
  })

  it('does not count the empty chat Trinity makes when an agent is opened', () => {
    expect(shouldOpenOnboarding({ ...fresh, threads: [{ id: 's1', message_count: 0 }] })).toBe(true)
    expect(shouldOpenOnboarding({ ...fresh, threads: [{ id: 's1', message_count: 3 }] })).toBe(false)
  })

  it('still opens on a fresh install, whose sample agents nobody here built', () => {
    const roster = [...fresh.roster, { name: 'cornelius' }, { name: 'acme-scout' }]
    expect(shouldOpenOnboarding({ ...fresh, roster, seededAgents: ['cornelius', 'acme-scout'] })).toBe(true)
    expect(shouldOpenOnboarding({ ...fresh, roster: [...roster, { name: 'finance-close' }], seededAgents: ['cornelius', 'acme-scout'] })).toBe(false)
  })

  it('stays shut if it cannot tell which agents are samples', () => {
    expect(shouldOpenOnboarding({ ...fresh, roster: [...fresh.roster, { name: 'cornelius' }] })).toBe(false)
  })

  it('stays shut on an instance with its own agents: that person is not new', () => {
    expect(shouldOpenOnboarding({ ...fresh, roster: [...fresh.roster, { name: 'finance-close' }] })).toBe(false)
  })

  it('does not come back after finishing or skipping', () => {
    expect(shouldOpenOnboarding({ ...fresh, intake: finishIntake(emptyIntake()) })).toBe(false)
    expect(shouldOpenOnboarding({ ...fresh, intake: finishIntake(skipIntake(emptyIntake())) })).toBe(false)
  })

  it('does not guess from a chat list that failed to load', () => {
    expect(shouldOpenOnboarding({ ...fresh, threadsTrusted: false })).toBe(false)
  })
})

describe('the onboarding window', () => {
  const QUESTIONS = ['role', 'software', 'agents', 'plans', 'company', 'website']

  it('has the welcome, the questions and the audit, and setup only if needed', () => {
    expect(onboardingSteps()).toEqual(['welcome', ...QUESTIONS, 'audit'])
  })

  it('connects Claude and sets up the guide before the company, which starts the research', () => {
    expect(onboardingSteps({ claudeMissing: true, guideMissing: true })).toEqual(
      ['welcome', 'role', 'software', 'agents', 'plans', 'connect', 'setup', 'company', 'website', 'audit'],
    )
  })

  it('explains what an agent is right after the agents question, for a newcomer', () => {
    expect(onboardingSteps({ explain: true })).toEqual(['welcome', 'role', 'software', 'agents', 'explain', 'plans', 'company', 'website', 'audit'])
    expect(onboardingSteps({ current: 'explain' })).toContain('explain')
    expect(onboardingSteps()).not.toContain('explain')
  })

  it('the explainer is for someone who has never built an agent, once', () => {
    expect(needsAgentExplainer(finished())).toBe(true)
    expect(needsAgentExplainer(markAgentExplained(finished()))).toBe(false)
    expect(needsAgentExplainer({ ...finished(), answers: { ...finished().answers, agents: 'tried' } })).toBe(false)
    expect(needsAgentExplainer({ ...finished(), answers: { ...finished().answers, agents: 'built' } })).toBe(false)
    expect(needsAgentExplainer(emptyIntake())).toBe(false)
    expect(needsAgentExplainer(null)).toBe(false)
  })

  it('keeps a setup step while someone is on it, so finishing it does not renumber the steps', () => {
    expect(onboardingSteps({ claudeMissing: false, current: 'connect' })).toContain('connect')
    expect(onboardingSteps({ guideMissing: false, current: 'setup' })).toContain('setup')
  })

  it('opens on the welcome, or where they had got to', () => {
    expect(onboardingStartStep(emptyIntake())).toBe('welcome')
    expect(onboardingStartStep(answerIntake(emptyIntake(), 'role', 'Ops'))).toBe('software')
    expect(onboardingStartStep(finished())).toBe('audit')
  })

  it('introduces the audit from who they are and how much they know', () => {
    expect(auditIntro(finished())).toContain("You told us you're Head of Operations.")
    expect(auditIntro(finished())).toMatch(/don't need any technical knowledge/)
    let i = emptyIntake()
    i = answerIntake(i, 'role', 'CTO')
    i = answerIntake(i, 'software', 'developer')
    i = answerIntake(i, 'agents', 'built')
    expect(auditIntro(i)).toMatch(/move quickly/)
    expect(auditIntro(i)).not.toMatch(/technical knowledge/)
    expect(auditIntro(emptyIntake())).not.toContain("You told us")
  })

  it('choosing the audit only takes the two real answers', () => {
    expect(chooseAudit(emptyIntake(), 'start').audit).toBe('start')
    expect(chooseAudit(emptyIntake(), 'skip').audit).toBe('skip')
    expect(chooseAudit(emptyIntake(), 'maybe').audit).toBe(null)
  })
})

describe('the company research', () => {
  const started = markResearchStarted(finished(), 'exec-1')

  it('asks for the company and its site, or says there is none', () => {
    expect(researchMessage(answerIntake(finished(), 'website', 'acme.com'))).toBe('/company-research Company: Acme Freight. Website: acme.com.')
    expect(researchMessage(finished())).toBe('/company-research Company: Acme Freight. No website given.')
  })

  it('starts once, and only once there is a company', () => {
    expect(shouldStartResearch(emptyIntake())).toBe(false)
    expect(shouldStartResearch(finished())).toBe(true)
    expect(shouldStartResearch(started)).toBe(false)
    expect(shouldStartResearch(clearResearch(started))).toBe(true)
  })

  it('reads the id from either spelling of the task route', () => {
    expect(researchExecutionId({ execution_id: 'a' })).toBe('a')
    expect(researchExecutionId({ task_execution_id: 'b' })).toBe('b')
    expect(researchExecutionId({})).toBe('')
  })

  it('tells the guide it is running, so it knows to look for it', () => {
    expect(intakeMessage(started)).toContain('Company research: running in the background.')
    expect(intakeMessage(finished())).not.toContain('Company research')
  })

  it('a start that never came back stops claiming to run', () => {
    const marked = markResearchStarted(finished(), '', 1000)
    expect(researchView({ intake: marked, now: 2000 }).status).toBe('running')
    expect(researchView({ intake: marked, now: 1000 + 3 * 60 * 1000 }).status).toBe('failed')
  })

  it('is nothing until it has started', () => {
    expect(researchView({ intake: finished() }).status).toBe('none')
  })

  it('is running until the job says otherwise, including when the job cannot be read', () => {
    expect(researchView({ intake: started }).status).toBe('running')
    expect(researchView({ intake: started, execution: { status: 'running' } }).status).toBe('running')
  })

  it('the report wins over whatever the job says, even in another browser', () => {
    const canvas = { blocks: [{ id: 'report', kind: 'markdown', markdown: '# Acme' }] }
    expect(researchView({ intake: started, canvas, execution: { status: 'failed' } })).toMatchObject({ status: 'done', report: '# Acme' })
    expect(researchView({ intake: emptyIntake(), canvas }).status).toBe('done')
  })

  it('reads the report where the canvas route puts it, under payload', () => {
    const canvas = { blocks: [{ id: 'report', kind: 'markdown', title: null, slot: null, payload: { markdown: '# Acme' } }] }
    expect(researchView({ intake: started, canvas })).toMatchObject({ status: 'done', report: '# Acme' })
  })

  it("a colleague's research, kept on the instance, counts as done here", () => {
    const shared = companyFromProfile(emptyIntake(), { name: 'Acme Freight', website: 'acme.example', research_md: '# Acme', context_md: 'ctx' }, 1000)
    expect(researchView({ intake: shared, profile: { research_md: '# Acme' } })).toMatchObject({ status: 'done', report: '# Acme' })
    expect(researchView({ intake: shared }).status).toBe('done')
  })

  it('a job that failed says what to do, and one that ended with no report is not a success', () => {
    const failed = researchView({ intake: started, execution: { status: 'failed', error: 'Authentication failure: Not logged in' } })
    expect(failed.status).toBe('failed')
    expect(failed.error).toMatch(/Claude isn't answering/)
    expect(researchView({ intake: started, execution: { status: 'success' } }).status).toBe('failed')
  })

  it('never shows Trinity\'s raw error', () => {
    expect(researchFailureMessage('Traceback: secret path')).not.toContain('Traceback')
  })
})

describe('a skipped profile', () => {
  it('shows red until it is finished', () => {
    expect(worldView(null, emptyIntake()).profileIncomplete).toBe(false)
    expect(worldView(null, answerIntake(emptyIntake(), 'company', 'Acme')).profileIncomplete).toBe(false)
    expect(worldView(null, skipIntake(emptyIntake())).profileIncomplete).toBe(true)
    expect(worldView(null, finished()).profileIncomplete).toBe(false)
  })

  it('draws the stepper in red, whatever stage the build has reached', () => {
    const v = worldView(null, skipIntake(emptyIntake()))
    expect(stepState('profile', v)).toBe('incomplete')
    expect(stepState('profile', { ...v, stage: 'build' })).toBe('incomplete')
    expect(stepState('profile', worldView(null, finished()))).toBe('current')
  })

  it('reopens at the first unanswered question, keeping what was answered', () => {
    const i = reopenIntake(finishIntake(skipIntake(answerIntake(emptyIntake(), 'role', 'CTO'))))
    expect(i).toMatchObject({ done: false, skipped: false, finished: false, current: 'software' })
    expect(i.answers.role).toBe('CTO')
  })

  it('finishing clears the skip', () => {
    let i = reopenIntake(skipIntake(emptyIntake()))
    for (const [q, a] of [['role', 'Ops'], ['software', 'none'], ['agents', 'none'], ['company', 'Acme']]) i = answerIntake(i, q, a)
    expect(i).toMatchObject({ done: true, skipped: false })
  })
})

describe('firstMessagePending: what the guide has not been told', () => {
  const state = normalizeState({ schema: BUILD_SCHEMA })

  it('nothing until the questions are done', () => {
    expect(firstMessagePending(emptyIntake(), null)).toBe(false)
  })

  it('the answers, or the skip, once, when it has not started', () => {
    expect(firstMessagePending(finished(), null)).toBe(true)
    expect(firstMessagePending(skipIntake(emptyIntake()), null)).toBe(true)
    expect(firstMessagePending(markIntakeSent(finished()), null)).toBe(false)
    expect(firstMessagePending(finished(), state)).toBe(false)
  })

  it('the answers again when someone who skipped comes back and finishes', () => {
    let i = reopenIntake(markIntakeSent(skipIntake(emptyIntake())))
    expect(firstMessagePending(i, state)).toBe(false)
    for (const [q, a] of [['role', 'Ops'], ['software', 'none'], ['agents', 'none'], ['company', 'Acme']]) i = answerIntake(i, q, a)
    expect(firstMessagePending(i, state)).toBe(true)
    expect(firstMessagePending(markIntakeSent(i), state)).toBe(false)
  })

  it('nothing new when they come back and skip again', () => {
    const i = skipIntake(reopenIntake(markIntakeSent(skipIntake(emptyIntake()))))
    expect(firstMessagePending(i, state)).toBe(false)
  })
})

describe('deployFailureMessage: a failed setup says what to do', () => {
  it('explains a name Trinity still reserves', () => {
    const error = { response: { status: 500, data: { failed: [{ name: 'build-guide', reason: 'Agent already exists' }] } } }
    expect(deployFailureMessage({ error })).toMatch(/still holds the name build-guide/)
  })

  it('names per-agent failures, including on a successful response', () => {
    const result = { status: 'partial', failed: [{ name: 'agent-builder', reason: 'clone failed' }] }
    expect(deployFailureMessage({ result })).toContain('agent-builder: clone failed')
  })

  it('says nothing when everything worked', () => {
    expect(deployFailureMessage({ result: { failed: [] } })).toBe('')
  })

  it('tells someone without the role to ask an administrator', () => {
    expect(deployFailureMessage({ error: { response: { status: 403, data: {} } } })).toMatch(/administrator/)
  })

  it('falls back to the error itself', () => {
    expect(deployFailureMessage({ error: { message: 'Network Error' } })).toContain('Network Error')
  })
})

describe('stages', () => {
  it('runs profile, audit, plan, build, live, and ranks them', () => {
    expect(BUILD_STAGES.map((s) => s.id)).toEqual(['profile', 'audit', 'plan', 'build', 'live'])
    expect(stageIndex('build')).toBeGreaterThan(stageIndex('audit'))
    expect(stageIndex('nonsense')).toBe(0)
  })

  it('never ticks an audit that did not happen', () => {
    const built = { stage: 'build', audit: { status: 'not_started' } }
    expect(stepState('profile', built)).toBe('done')
    expect(stepState('audit', built)).toBe('not-done')
    expect(stepState('build', built)).toBe('current')
    expect(stepState('live', built)).toBe('upcoming')
    expect(stepState('audit', { stage: 'plan', audit: { status: 'done' } })).toBe('done')
  })

  it('names the guide agent once', () => {
    expect(BUILD_GUIDE_AGENT).toBe('build-guide')
  })
})

describe('the company, once per instance', () => {
  const profile = { name: 'Acme Freight', website: 'acme.example', research_md: '# Acme', context_md: 'Acme moves freight.' }

  it("starts a colleague's answers from what is known, and marks the research as theirs too", () => {
    const seeded = companyFromProfile(emptyIntake(), profile, 1000)
    expect(seeded.answers).toEqual({ company: 'Acme Freight', website: 'acme.example' })
    expect(seeded.research).toEqual({ started: true, shared: true, executionId: '', at: 1000 })
    expect(shouldStartResearch(seeded)).toBe(false)
  })

  it('never overwrites answers someone already gave', () => {
    const mine = answerIntake(emptyIntake(), 'company', 'Mine Ltd')
    expect(companyFromProfile(mine, profile)).toBe(mine)
    expect(companyFromProfile(emptyIntake(), null).answers).toEqual({})
    expect(companyFromProfile(emptyIntake(), { name: '' }).answers).toEqual({})
  })

  it('tells the guide the research is already in its folder', () => {
    const seeded = companyFromProfile({ ...emptyIntake(), answers: { role: 'CEO', software: 'none', agents: 'tried' } }, profile, 1000)
    expect(intakeMessage(seeded)).toContain('Company research: shared by a colleague, already in company-research.md.')
    expect(intakeMessage(seeded)).not.toContain('running in the background')
  })
})

describe('final review: a guide whose name Trinity changed, and answers for another guide', () => {
  it("accepts the name Trinity gives when it adds _2 to a taken one", () => {
    expect(isGuideName('atlas-0a1b2c3d_2')).toBe(true)
    expect(isGuideName('atlas-0a1b2c3d-2_3')).toBe(true)
    expect(isGuideName('build-guide_2')).toBe(true)
    expect(isBuildCrew('atlas-0a1b2c3d_2')).toBe(true)
    expect(isGuideName('atlas-0a1b2c3d_x')).toBe(false)
  })

  it('remembers which guide was told, and tells a new one again', () => {
    const sent = markIntakeSent({ ...emptyIntake(), answers: { company: 'Acme' }, done: true }, 'atlas-22222222')
    expect(sent.sentTo).toBe('atlas-22222222')
    expect(rebindToGuide(sent, 'atlas-22222222')).toBe(sent)
    const moved = rebindToGuide(sent, 'atlas-33333333')
    expect(moved.sent).toBe(false)
    expect(moved).not.toHaveProperty('sentTo')
  })

  it('answers sent before guides were per person were sent to build-guide', () => {
    const old = { ...emptyIntake(), sent: true, research: { started: true, executionId: 'e1', at: 1 } }
    expect(rebindToGuide(old, 'build-guide')).toBe(old)
    const moved = rebindToGuide(old, 'atlas-22222222')
    expect(moved.sent).toBe(false)
    expect(moved).not.toHaveProperty('research')
  })

  it("keeps the guide's card when the roster brings the same one again, and takes a changed one", () => {
    const card = { name: 'atlas-11111111', display_label: 'Atlas', avatar_url: null }
    expect(keepGuideCard(card, { ...card })).toBe(card)
    const pictured = { ...card, avatar_url: '/avatars/atlas.webp' }
    expect(keepGuideCard(card, pictured)).toBe(pictured)
    expect(keepGuideCard(null, card)).toBe(card)
    expect(keepGuideCard(card, null)).toBe(null)
  })

  it('a guide set up again under the same name, with no page and no chat of theirs, is told again', () => {
    // Atlas's address comes from the person, so the guide set up after a deleted one has its name.
    const now = Date.parse('2026-11-06T10:30:00Z')
    const yesterday = now - 20 * 60 * 60 * 1000
    const sent = markIntakeSent({ ...emptyIntake(), answers: { company: 'Acme' }, done: true, research: { started: true, executionId: 'e1', at: yesterday, guide: 'atlas-22222222' } }, 'atlas-22222222')
    expect(rebindToGuide(sent, 'atlas-22222222', { fresh: false, now })).toBe(sent)
    const again = rebindToGuide(sent, 'atlas-22222222', { fresh: true, now })
    expect(again.sent).toBe(false)
    expect(again).not.toHaveProperty('sentTo')
    // Yesterday's research went with the guide before, so this one runs its own.
    expect(again).not.toHaveProperty('research')
    // Research from minutes ago was started in this guide, and may still be running.
    const recent = { ...sent, research: { ...sent.research, at: now - 5 * 60 * 1000 } }
    const resent = rebindToGuide(recent, 'atlas-22222222', { fresh: true, now })
    expect(resent.sent).toBe(false)
    expect(resent.research).toEqual(recent.research)
    // Answers that never went anywhere have nothing to undo, nor has research started for them.
    const unsent = { ...emptyIntake(), answers: { company: 'Acme' }, done: true, research: { started: true, executionId: 'e1', at: yesterday, guide: 'atlas-22222222' } }
    expect(rebindToGuide(unsent, 'atlas-22222222', { fresh: true, now })).toBe(unsent)
  })

  it("a colleague's shared research stays, but is copied again into the new guide", () => {
    const shared = { ...emptyIntake(), sent: true, sentTo: 'atlas-a', research: { started: true, shared: true, copied: true, at: 1 } }
    expect(rebindToGuide(shared, 'atlas-b').research).toEqual({ started: true, shared: true, copied: false, at: 1 })
  })
})

describe('final review: publishing the company profile', () => {
  const profile = { name: 'Acme Freight', website: 'acme.example', research_md: '# Acme', context_md: 'Acme moves freight.' }

  it("never blanks a colleague's text, and keeps theirs where this person has none", () => {
    expect(companyToPublish({ answers: { company: 'Acme Freight', website: 'acme.example' }, report: '', context: '', profile })).toBe(null)
    expect(companyToPublish({ answers: { company: 'Acme Freight' }, report: '', context: 'New context.', profile }))
      .toEqual({ name: 'Acme Freight', website: 'acme.example', research_md: '# Acme', context_md: 'New context.' })
  })

  it('publishes a first profile, and nothing without a company name', () => {
    expect(companyToPublish({ answers: { company: 'Acme' }, report: '# Acme', context: '', profile: null }))
      .toEqual({ name: 'Acme', website: '', research_md: '# Acme', context_md: '' })
    expect(companyToPublish({ answers: {}, report: '# Acme', context: '', profile: null })).toBe(null)
  })
})


describe('the guide has one page', () => {
  const threads = [
    { id: 'main-1', agent_name: 'atlas-1a2b3c4d', is_main: true },
    { id: 'side-1', agent_name: 'atlas-1a2b3c4d', is_main: false },
    { id: 'other-1', agent_name: 'game-filler', is_main: true },
    { id: 'room-1', agent_name: 'atlas-1a2b3c4d', is_main: true, is_room: true },
  ]
  const on = { buildAvailable: true, guideName: 'atlas-1a2b3c4d', threads }

  it("their own guide's row, and its main chat, open the Build page", () => {
    expect(opensBuild({ ...on, agentName: 'atlas-1a2b3c4d' })).toBe(true)
    expect(opensBuild({ ...on, sessionId: 'main-1' })).toBe(true)
    expect(opensBuild({ ...on, threads: [{ session_id: 'main-1', agent_name: 'atlas-1a2b3c4d', is_main: true }], sessionId: 'main-1' })).toBe(true)
  })

  it('every other agent and chat opens as before', () => {
    expect(opensBuild({ ...on, agentName: 'game-filler' })).toBe(false)
    expect(opensBuild({ ...on, sessionId: 'other-1' })).toBe(false)
    // Another chat of the guide's isn't the one the Build page shows, so it stays reachable.
    expect(opensBuild({ ...on, sessionId: 'side-1' })).toBe(false)
    expect(opensBuild({ ...on, sessionId: 'room-1' })).toBe(false)
    expect(opensBuild({ ...on, sessionId: 'unknown' })).toBe(false)
    expect(opensBuild({ ...on })).toBe(false)
  })

  it('never for someone who cannot build, or who has no guide', () => {
    expect(opensBuild({ ...on, buildAvailable: false, agentName: 'atlas-1a2b3c4d' })).toBe(false)
    expect(opensBuild({ ...on, guideName: '', agentName: '' })).toBe(false)
    expect(opensBuild({ ...on, guideName: null, sessionId: 'main-1' })).toBe(false)
    expect(opensBuild()).toBe(false)
  })
})
