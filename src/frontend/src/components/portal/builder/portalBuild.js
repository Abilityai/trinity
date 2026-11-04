/**
 * Workspace Build: every decidable rule behind the Build page. Pure.
 *
 * The page is the ordinary Workspace conversation on the left, with a guide
 * agent, and the person's "world" on the right. It adds no endpoint and no
 * table. The agent reports what it is doing the way any agent reports anything
 * to a person, by writing a canvas (`set_canvas`, id `build`) whose `state`
 * block holds the document `normalizeState` reads, so the existing canvas route,
 * its audience rule and its size cap all apply.
 *
 * That document is written by an agent, so nothing in it is trusted: every field
 * is typed, bounded and given a fallback here, and "live" is checked against the
 * roster rather than believed (`liveAgentFor`).
 */

export const BUILD_GUIDE_AGENT = 'build-guide'
// What people call it. The address stays `build-guide`, which links and skills depend on.
export const BUILD_GUIDE_NAME = 'Atlas'
// The audit and the plans are the hardest thinking on this page.
export const BUILD_CREW_MODEL = 'claude-opus-5-5'
// Phase 1 deployed an Agent Builder beside the guide. Instances may still run it.
const LEGACY_BUILDER = 'agent-builder'
export const BUILD_CANVAS_ID = 'build'
export const BUILD_RESEARCH_CANVAS_ID = 'company-research'

// How to reach the team, in one place so that it's easy to change.
export const BUILD_SUPPORT = Object.freeze({
  email: 'support@ability.ai',
  docs: 'https://docs.ability.ai',
  workshops: 'https://www.ability.ai/workshops',
})

export function supportMailto() {
  return `mailto:${BUILD_SUPPORT.email}?subject=${encodeURIComponent('Help building agents on Trinity')}`
}
export const BUILD_STATE_BLOCK = 'state'
export const BUILD_SCHEMA = 'trinity-build/v1'
// Every person who can build has their own guide. The first one ever deployed kept the
// address `build-guide`; any other is `atlas-` and 8 hex digits of its owner's username.
// A deploy adds `_2` to a name a live agent already has, so that is a guide too.
const GUIDE_NAME_RE = /^(atlas-[0-9a-f]{8}(-[2-9])?|build-guide)(_\d+)?$/

/** A guide's address. */
export function isGuideName(name) {
  return GUIDE_NAME_RE.test(String(name || ''))
}

/** The agents behind the Build page, never part of the person's world. */
export function isBuildCrew(name) {
  return isGuideName(name) || name === LEGACY_BUILDER
}

// FNV-1a, 32 bits, over the UTF-8 bytes: stable, and no personal data in an address.
function fnv1a(text) {
  let hash = 0x811c9dc5
  for (const byte of new TextEncoder().encode(text)) {
    hash ^= byte
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

/**
 * The address for a new guide. A deleted agent's name stays held for a while, so
 * a held one steps on to -2, then -3, up to -9.
 */
export function guideNameFor(username, taken = []) {
  const base = `atlas-${fnv1a(String(username || '').trim().toLowerCase())}`
  const held = new Set(taken)
  if (!held.has(base)) return base
  for (let n = 2; n <= 9; n++) {
    if (!held.has(`${base}-${n}`)) return `${base}-${n}`
  }
  return base
}

/**
 * The person's own guide on their roster. Someone else's, even one shared with
 * them, is never it. The original `build-guide` stays its owner's.
 */
export function myGuideCard(roster) {
  const own = (Array.isArray(roster) ? roster : []).filter((a) => a?.owned === true && isGuideName(a.name))
  return own.find((a) => a.name === BUILD_GUIDE_AGENT) || own[0] || null
}

/**
 * What "Set up the guide" deploys: one guide, under the person's own address.
 * `buildManifest(BUILD_GUIDE_AGENT)` is byte-identical to
 * config/manifests/workspace-build.yaml (a unit test compares them): the frontend
 * bundle cannot read outside its own tree.
 */
export function buildManifest(guideName) {
  return `# Workspace Build: the guide behind the Build page.
# It talks to the person, and it builds the agents itself.
name: workspace-build
description: The guide behind the Workspace Build page
naming: bare
agents:
  ${guideName}:
    template: local:build-guide
    # Needed to create the agents it builds. It still asks the person to approve each one.
    grants:
      - agents.manage
permissions:
  preset: none
`
}

export const BUILD_MANIFEST = buildManifest(BUILD_GUIDE_AGENT)

export const BUILD_STAGES = Object.freeze([
  Object.freeze({ id: 'profile', label: 'Profile' }),
  Object.freeze({ id: 'audit', label: 'Audit' }),
  Object.freeze({ id: 'plan', label: 'Plan' }),
  Object.freeze({ id: 'build', label: 'Build' }),
  Object.freeze({ id: 'live', label: 'Live' }),
])

const STAGE_IDS = BUILD_STAGES.map((s) => s.id)
const LEVELS = ['newcomer', 'practitioner', 'builder']
const AGENT_STATUSES = ['designing', 'building', 'needs_you', 'ready', 'live', 'failed']
const AGENT_KINDS = ['example', 'agent', 'orchestrator']
const SKILL_STATUSES = ['planned', 'building', 'done']
const CONNECTION_STATUSES = ['needed', 'connected']
const AUDIT_STATUSES = ['not_started', 'in_progress', 'done']
const SIZE_LEVELS = ['small', 'medium', 'large']
const VALUE_LEVELS = ['low', 'medium', 'high']
const ACCESS_STATES = ['unknown', 'known', 'connected']

export const LEVEL_LABELS = Object.freeze({
  newcomer: 'New to this: your guide explains each step',
  practitioner: 'Some experience: your guide explains the Trinity parts',
  builder: 'Experienced builder: your guide moves quickly',
})

// ------------------------------------------------------------------ the intake

const FREE_TEXT_MAX = 200

export const INTAKE_QUESTIONS = Object.freeze([
  Object.freeze({
    id: 'role',
    group: 'you',
    prompt: "Let's start with you. What's your role at your company?",
    placeholder: 'Head of Marketing',
  }),
  Object.freeze({
    id: 'software',
    group: 'you',
    prompt: 'How much experience do you have with software development?',
    choices: Object.freeze([
      Object.freeze({ id: 'none', label: 'None' }),
      Object.freeze({ id: 'some', label: 'A little' }),
      Object.freeze({ id: 'developer', label: "I'm a developer" }),
    ]),
  }),
  Object.freeze({
    id: 'agents',
    group: 'you',
    prompt: 'And with AI agents?',
    choices: Object.freeze([
      Object.freeze({ id: 'none', label: "I've never built one" }),
      Object.freeze({ id: 'tried', label: "I've built a few" }),
      Object.freeze({ id: 'built', label: 'I build them all the time' }),
    ]),
  }),
  // Optional, so someone who finished before it existed is never sent back to it.
  Object.freeze({
    id: 'plans',
    group: 'you',
    optional: true,
    prompt: "Do you know what you'd like to build first?",
    choices: Object.freeze([
      Object.freeze({ id: 'not_yet', label: 'Not yet' }),
      Object.freeze({ id: 'rough', label: 'I have a rough idea' }),
      Object.freeze({ id: 'exact', label: 'I know exactly' }),
    ]),
  }),
  Object.freeze({
    id: 'company',
    group: 'company',
    prompt: "What's your company called, and what does it do?",
    placeholder: 'Acme Freight, a 40-person logistics company',
  }),
  Object.freeze({
    id: 'website',
    group: 'company',
    optional: true,
    prompt: "What's your company's website?",
    hint: 'Your guide reads it, so you can skip explaining the basics.',
    placeholder: 'acmefreight.com',
  }),
])

const QUESTION_IDS = INTAKE_QUESTIONS.map((q) => q.id)
// The website is a help, not part of a complete profile.
const REQUIRED_IDS = INTAKE_QUESTIONS.filter((q) => !q.optional).map((q) => q.id)

export function emptyIntake() {
  return { current: REQUIRED_IDS[0], answers: {}, done: false, skipped: false, sent: false, finished: false, audit: null }
}

/** Records an answer. A blank answer or a choice not offered changes nothing. */
export function answerIntake(intake, questionId, raw) {
  const question = INTAKE_QUESTIONS.find((q) => q.id === questionId)
  if (!question) return intake
  const value = typeof raw === 'string' ? raw.trim().slice(0, FREE_TEXT_MAX) : ''
  if (!value) return intake
  if (question.choices && !question.choices.some((c) => c.id === value)) return intake
  const answers = { ...intake.answers, [questionId]: value }
  const current = REQUIRED_IDS.find((id) => !answers[id]) || null
  const complete = current === null
  // Someone who skipped has left the questions, even if they answer one more later.
  return { ...intake, answers, current, done: complete || Boolean(intake.skipped), skipped: complete ? false : intake.skipped }
}

/**
 * An answer given or changed in the setup panel. The research is about the company and
 * its website, so a new one of those, once the research has started, forgets that
 * research and it runs again with the new answers.
 */
export function changeAnswer(intake, questionId, raw) {
  const next = answerIntake(intake, questionId, raw)
  const question = INTAKE_QUESTIONS.find((q) => q.id === questionId)
  const changed = next.answers?.[questionId] !== intake.answers?.[questionId]
  return changed && question?.group === 'company' && intake.research?.started ? clearResearch(next) : next
}

/** "Skip for now": the profile stays incomplete, and shows in red until it isn't. */
export function skipIntake(intake) {
  return { ...intake, current: null, done: true, skipped: true }
}

/** Whether a set of questions has been answered: 'you' or 'company'. */
export function intakeGroupDone(intake, group) {
  return INTAKE_QUESTIONS
    .filter((q) => q.group === group && !q.optional)
    .every((q) => Boolean(intake?.answers?.[q.id]))
}

/** The AI audit: 'start' or 'skip'. Skipping means building any agent as a test. */
export function chooseAudit(intake, choice) {
  if (choice === 'skip') return { ...intake, audit: choice }
  return choice === 'start' ? { ...intake, audit: choice, firstAgent: null, tellGuide: true } : intake
}

/**
 * No audit, and they'd rather talk it through with the guide: what they have in mind,
 * or `''` to start from scratch.
 */
export function chooseFirstAgent(intake, idea = '') {
  return { ...intake, audit: 'skip', firstAgent: { idea }, tellGuide: true }
}

/** They have been through onboarding, so the window doesn't open by itself again. */
export function finishIntake(intake) {
  return { ...intake, finished: true }
}

export function markIntakeSent(intake, guideName = '') {
  return { ...intake, sent: true, reopened: false, tellGuide: false, profileChat: false, ...(guideName ? { sentTo: guideName } : {}) }
}

/**
 * Answers sent to another guide (before guides were per person, that was build-guide)
 * haven't reached this one, so they go again, and its own research runs. A colleague's
 * shared research stays, and is copied into this guide too.
 */
export function rebindToGuide(intake, guideName) {
  if (!intake || !guideName) return intake
  const told = intake.sentTo || (intake.sent ? BUILD_GUIDE_AGENT : '')
  const research = intake.research
  // Research records its guide; older answers only say where they were sent.
  const researchedBy = research && !research.shared ? (research.guide || (told && told !== guideName ? told : '')) : ''
  const copiedTo = research?.shared && research.copied ? (research.copiedTo || told) : ''
  const stale = (told && told !== guideName) || (researchedBy && researchedBy !== guideName) || (copiedTo && copiedTo !== guideName)
  if (!stale) return intake
  const { sentTo: _told, research: _research, ...rest } = intake
  const next = { ...rest, sent: false }
  if (research?.shared) {
    const { copiedTo: _copiedTo, ...shared } = research
    next.research = { ...shared, copied: false }
  }
  return next
}

/**
 * They'd like to tell the guide about their work, which is what a complete profile
 * still lacks. A started audit carries on; otherwise it counts as skipped for now.
 */
export function chooseProfileChat(intake) {
  return { ...intake, profileChat: true, tellGuide: true, audit: intake.audit || 'skip' }
}

/** What the red profile opens: the questions while one is unanswered, otherwise the guide's chat. */
export function finishProfileAction(intake) {
  return REQUIRED_IDS.some((id) => !intake?.answers?.[id]) ? 'questions' : 'atlas'
}

/**
 * Back into the questions from the red profile, at the first one still unanswered.
 * If the guide was already told they skipped, it needs telling again when they finish.
 */
export function reopenIntake(intake) {
  const current = REQUIRED_IDS.find((id) => !intake.answers?.[id]) || REQUIRED_IDS[0]
  return { ...intake, current, done: false, skipped: false, finished: false, reopened: Boolean(intake.sent) }
}

/** Whether the guide has answers it hasn't been told yet. */
export function firstMessagePending(intake, state) {
  if (!intake?.done) return false
  // They chose something in setup after the guide already had their answers.
  if (intake.tellGuide) return true
  if (intake.reopened) return !intake.skipped
  return !intake.sent && !state
}

/**
 * The onboarding window's steps. The two setup steps only appear if they're needed,
 * and come before the company, because answering that starts the research.
 */
export function onboardingSteps({ claudeMissing = false, guideMissing = false, explain = false, current = '' } = {}) {
  const you = INTAKE_QUESTIONS.filter((q) => q.group === 'you').map((q) => q.id)
  const company = INTAKE_QUESTIONS.filter((q) => q.group === 'company').map((q) => q.id)
  // "What's an agent?" goes right after they said they've never built one.
  if (explain || current === 'explain') you.splice(you.indexOf('agents') + 1, 0, 'explain')
  return [
    'welcome',
    ...you,
    // Kept once reached, so finishing one doesn't renumber the steps under the person.
    ...(claudeMissing || current === 'connect' ? ['connect'] : []),
    ...(guideMissing || current === 'setup' ? ['setup'] : []),
    ...company,
    'audit',
  ]
}

/** Someone who has never built an agent sees what one is, once. */
export function needsAgentExplainer(intake) {
  return intake?.answers?.agents === 'none' && !intake.agentExplained
}

export function markAgentExplained(intake) {
  return { ...intake, agentExplained: true }
}

/** Where the window opens: the welcome, or where they had got to. */
export function onboardingStartStep(intake) {
  if (!intake?.answers || Object.keys(intake.answers).length === 0) return 'welcome'
  return REQUIRED_IDS.find((id) => !intake.answers[id]) || 'audit'
}

// ---------------------------------------------------------------- the research

/** What the guide is asked to research, as a job that runs beside the chat. */
export function researchMessage(intake) {
  const { company, website } = intake?.answers || {}
  return `/company-research Company: ${company}.${website ? ` Website: ${website}.` : ' No website given.'}`
}

/** Once per company, and only once there is a company to research. */
export function shouldStartResearch(intake) {
  return Boolean(intake?.answers?.company) && !intake?.research?.started
}

/**
 * A colleague already told Trinity about the company: its name, website and research
 * are kept on the instance, so this person's answers start from them and no second
 * research runs. Answers someone already gave are never overwritten.
 */
export function companyFromProfile(intake, profile, now = Date.now()) {
  const answers = intake?.answers || {}
  if (!profile?.name || answers.company || answers.website) return intake
  const seeded = { ...answers, company: profile.name, ...(profile.website ? { website: profile.website } : {}) }
  return { ...intake, answers: seeded, research: { started: true, shared: true, executionId: '', at: now } }
}

/**
 * What this person's guide adds to the company profile kept on the instance, or null
 * when it adds nothing. Text they don't have yet keeps the colleague's, so publishing
 * never blanks it, and an unchanged profile isn't sent again.
 */
export function companyToPublish({ answers = {}, stateName = '', report = '', context = '', profile = null } = {}) {
  const name = answers.company || stateName || profile?.name || ''
  if (!name) return null
  const doc = {
    name,
    website: answers.website || profile?.website || '',
    research_md: report || profile?.research_md || '',
    context_md: context || profile?.context_md || '',
  }
  if (!doc.research_md && !doc.context_md) return null
  const same = profile && ['name', 'website', 'research_md', 'context_md'].every((k) => (profile[k] || '') === doc[k])
  return same ? null : doc
}

/** Marked before the request goes, so that nothing else starts a second job. */
export function markResearchStarted(intake, executionId, now = Date.now(), guideName = '') {
  return { ...intake, research: { started: true, executionId: executionId || '', at: now, ...(guideName ? { guide: guideName } : {}) } }
}

/** Forgets a failed attempt, so that it can be tried again. */
export function clearResearch(intake) {
  const { research: _dropped, ...rest } = intake
  return rest
}

/** The execution id from the task route's answer, whichever spelling it used. */
export function researchExecutionId(response) {
  return response?.execution_id || response?.task_execution_id || ''
}

const RESEARCH_START_MS = 2 * 60 * 1000
const RESEARCH_FAILED = new Set(['failed', 'cancelled', 'canceled', 'timeout', 'timed_out', 'error'])

/**
 * What the page says about the research. The report is what counts, so it wins over
 * whatever the job's status says. A job that ended with no report didn't work.
 */
export function researchView({ intake, canvas = null, execution = null, profile = null, now = Date.now() } = {}) {
  const blocks = Array.isArray(canvas?.blocks) ? canvas.blocks : []
  // The canvas route puts a block's content under `payload`; older reads had it flat.
  const block = blocks.find((b) => b?.id === 'report' && b?.kind === 'markdown')
  const report = block?.payload?.markdown || block?.markdown || ''
  if (report) return { status: 'done', report, error: '' }
  // A colleague's research, kept on the instance (companyFromProfile).
  if (intake?.research?.shared) return { status: 'done', report: profile?.research_md || '', error: '' }
  if (!intake?.research?.started) return { status: 'none', report: '', error: '' }
  // Marked as starting, and the page closed before the request came back.
  if (!intake.research.executionId && now - (intake.research.at || 0) > RESEARCH_START_MS) {
    return { status: 'failed', report: '', error: researchFailureMessage() }
  }
  if (RESEARCH_FAILED.has(execution?.status)) return { status: 'failed', report: '', error: researchFailureMessage(execution?.error) }
  if (execution?.status === 'success' || execution?.status === 'completed') {
    return { status: 'failed', report: '', error: "The research finished without a report. Try it again." }
  }
  return { status: 'running', report: '', error: '' }
}

/** Trinity's error, in words that say what to do. This is the first real use of Claude. */
export function researchFailureMessage(error) {
  const text = String(error || '')
  if (/not logged in|authentication|subscription|api key|credit/i.test(text)) {
    return "Claude isn't answering on this instance, so the research couldn't run. Check the token you connected, then try again."
  }
  return "The research couldn't finish. Try it again, or carry on without it."
}

/** How the AI audit is introduced, from what they told us. */
export function auditIntro(intake) {
  const { role, software, agents } = intake?.answers || {}
  const who = role ? `You told us you're ${role}. ` : ''
  const base = `${who}Your guide will ask how your team works and which jobs take the most time by hand, then recommend the best first agent to build. Your company research keeps running while you talk. It takes about 10 minutes, and you can stop whenever you like.`
  const level = startingLevel({ software, agents })
  if (level === 'newcomer') return `${base} You don't need any technical knowledge: it explains everything as you go.`
  if (level === 'builder') return `${base} It knows you've built agents, so it will move quickly.`
  return base
}

/**
 * The audit step's main button: the audit, unless they know exactly what to build, in
 * which case it's talking that through with the guide. The audit stays recommended.
 */
export function auditEmphasis(intake) {
  return intake?.answers?.plans === 'exact' ? 'agent' : 'audit'
}

/** What the audit step says, from what they told us. */
export function auditLead(intake) {
  if (auditEmphasis(intake) === 'agent') {
    return `You know what you'd like to build, so ${BUILD_GUIDE_NAME} can plan it with you now. The AI audit is still recommended: it looks across your team's work for where an agent saves the most time, and takes about 10 minutes.`
  }
  return auditIntro(intake)
}

/**
 * Where skipping the audit in setup leads. Experienced people stay on the Dashboard,
 * which shows them round Trinity's features; everyone else goes to the guide.
 */
export function auditSkipDestination(intake) {
  if (experienceRoute(intake?.answers) === 'experienced') {
    return {
      // Explicit, so that arriving from the Workspace doesn't turn it into the Build page.
      next: '/',
      label: `Finishing setup shows you round Trinity's dashboard and its special features. ${BUILD_GUIDE_NAME} is in the Workspace whenever you want it.`,
    }
  }
  return {
    next: '/workspace/build',
    label: "Finishing setup opens your guide, so you can build any agent as a test. If you're not sure what to build, it will point you back to the audit.",
  }
}

/**
 * Where a person starts. Only what they actually said can move them up, so an
 * answer that is missing or unrecognised reads as the gentlest start.
 */
export function startingLevel({ software, agents } = {}) {
  if (agents === 'built') return 'builder'
  if (agents === 'tried' || software === 'developer') return 'practitioner'
  return 'newcomer'
}

/**
 * The route through onboarding, from the agents answer alone: 'newcomer', 'standard'
 * or 'experienced'. The software answer changes only the wording. A missing or
 * unrecognised answer takes the gentlest route.
 */
export function experienceRoute({ agents } = {}) {
  if (agents === 'built') return 'experienced'
  if (agents === 'tried') return 'standard'
  return 'newcomer'
}

const choiceLabel = (questionId, id) => INTAKE_QUESTIONS
  .find((q) => q.id === questionId)?.choices?.find((c) => c.id === id)?.label || 'not given'

/** The first message to the guide, in words the person can read in their chat. */
export function intakeMessage(intake, existingAgents = [], seededAgents = [], signInEmail = '') {
  const { company, website, role, software, agents, plans } = intake.answers
  const label = (a) => a?.display_label || a?.name
  const own = (existingAgents || []).filter((a) => !seededAgents.includes(a?.name))
  const samples = (existingAgents || []).filter((a) => seededAgents.includes(a?.name))
  const names = own.map(label).filter(Boolean)
  const existing = [
    names.length ? `Agents already on this instance: ${names.slice(0, 50).join(', ')}.` : 'There are no other agents of mine on this instance yet.',
    samples.length ? `Sample agents that came with Trinity: ${samples.map(label).filter(Boolean).slice(0, 50).join(', ')}.` : '',
  ].filter(Boolean).join('\n')
  const auditSkipped = intake.audit === 'skip' || (intake.skipped && intake.audit !== 'start')
  const idea = intake.firstAgent?.idea
  const auditLine = !auditSkipped
    ? "AI audit: yes, let's do it now."
    : intake.profileChat && !intake.firstAgent
      // Asking for the talk about their work is not choosing a test agent.
      ? 'AI audit: skipped for now.'
      : !intake.firstAgent
        ? "AI audit: skipped for now. I'd like to build any agent as a test."
        : idea
          ? `AI audit: skipped for now. First agent: I'd like one that ${idea}. Walk me through building it.`
          : "AI audit: skipped for now. First agent: from scratch. Ask me what I have in mind and walk me through building it."
  const emailLine = signInEmail ? `My sign-in email: ${signInEmail}.` : ''
  const researchLine = intake.research?.shared ? 'Company research: shared by a colleague, already in company-research.md.'
    : intake.research?.started ? 'Company research: running in the background.' : ''
  const plansLine = plans ? `What to build first: ${choiceLabel('plans', plans)}.` : ''
  const profileLine = intake.profileChat ? "Profile: I'd like to tell you about my work first, so you know what my job involves." : ''
  // The guide already has their answers, so asking for the talk is all this says.
  if (profileLine && intake.sent && !intake.reopened) return profileLine
  if (intake.skipped) {
    return [
      "I skipped the setup questions, so my profile isn't complete.",
      emailLine,
      role ? `My role: ${role}.` : '',
      software ? `Software development experience: ${choiceLabel('software', software)}.` : '',
      agents ? `AI agent experience: ${choiceLabel('agents', agents)}.` : '',
      plansLine,
      company ? `My company: ${company}.` : '',
      website ? `Company website: ${website}.` : '',
      researchLine,
      auditLine,
      profileLine,
      existing,
    ].filter(Boolean).join('\n')
  }
  return [
    emailLine,
    `My role: ${role}.`,
    `Software development experience: ${choiceLabel('software', software)}.`,
    `AI agent experience: ${choiceLabel('agents', agents)}.`,
    plansLine,
    `Starting level: ${startingLevel({ software, agents })}.`,
    `My company: ${company}.`,
    website ? `Company website: ${website}.` : '',
    researchLine,
    auditLine,
    profileLine,
    existing,
  ].filter(Boolean).join('\n')
}

const intakeKey = (email) => `trinity-workspace-onboarding:${String(email || '').toLowerCase()}`

export function loadIntake(storage, email) {
  try {
    const raw = storage?.getItem(intakeKey(email))
    if (!raw) return emptyIntake()
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || !parsed.answers) return emptyIntake()
    return forgetHandover({ ...emptyIntake(), ...parsed })
  } catch {
    return emptyIntake()
  }
}

export function saveIntake(storage, email, intake) {
  try {
    storage?.setItem(intakeKey(email), JSON.stringify(intake))
  } catch {
    // Private window or blocked storage: the page keeps working for this visit.
  }
}

// ------------------------------------------------------------ the agent's state

const text = (v, max = 300) => (typeof v === 'string' ? v.trim().slice(0, max) : '')
const oneOf = (v, allowed, fallback) => (allowed.includes(v) ? v : fallback)
const list = (v, max, map) => (Array.isArray(v) ? v : [])
  .map((row) => (row && typeof row === 'object' ? map(row) : null))
  .filter(Boolean)
  .slice(0, max)

// A credential is only ever named here. Anything that does not look like a
// bare variable name is refused, so a value an agent pastes by mistake is not
// drawn on a screen someone might be sharing.
const VARIABLE_NAME = /^[A-Z][A-Z0-9_]{0,63}$/

function normalizeAgent(row) {
  const name = text(row.name, 80)
  if (!name) return null
  return {
    id: text(row.id, 80) || name,
    name,
    department: text(row.department, 80),
    purpose: text(row.purpose, 500),
    kind: oneOf(row.kind, AGENT_KINDS, 'agent'),
    status: oneOf(row.status, AGENT_STATUSES, 'designing'),
    live_name: text(row.live_name, 80),
    manages: (Array.isArray(row.manages) ? row.manages : [])
      .map((m) => text(m, 80)).filter(Boolean).slice(0, 50),
    skills: list(row.skills, 30, (s) => {
      const skillName = text(s.name, 80)
      return skillName
        ? { name: skillName, purpose: text(s.purpose, 300), status: oneOf(s.status, SKILL_STATUSES, 'planned') }
        : null
    }),
    connections: list(row.connections, 30, (c) => {
      const platform = text(c.platform, 80)
      return platform
        ? { platform, why: text(c.why, 300), status: oneOf(c.status, CONNECTION_STATUSES, 'needed') }
        : null
    }),
    variables: (Array.isArray(row.variables) ? row.variables : [])
      .filter((v) => typeof v === 'string' && VARIABLE_NAME.test(v))
      .slice(0, 50),
    schedules: list(row.schedules, 20, (s) => {
      const scheduleName = text(s.name, 80)
      return scheduleName
        ? { name: scheduleName, when: text(s.when, 120), enabled: s.enabled === true }
        : null
    }),
  }
}

function normalizeDepartment(row) {
  const name = text(row.name, 80)
  if (!name) return null
  return {
    id: text(row.id, 80) || name,
    name,
    opportunities: list(row.opportunities, 30, (o) => {
      const title = text(o.title, 160)
      return title
        ? {
            title,
            why: text(o.why, 500),
            effort: oneOf(o.effort, SIZE_LEVELS, 'medium'),
            value: oneOf(o.value, VALUE_LEVELS, 'medium'),
            recommended: o.recommended === true,
          }
        : null
    }),
  }
}

export function normalizeState(raw) {
  if (!raw || typeof raw !== 'object' || raw.schema !== BUILD_SCHEMA) return null
  const company = raw.company && typeof raw.company === 'object' ? raw.company : {}
  const person = raw.person && typeof raw.person === 'object' ? raw.person : {}
  const audit = raw.audit && typeof raw.audit === 'object' ? raw.audit : {}
  const focus = raw.focus && typeof raw.focus === 'object' ? raw.focus : {}
  return {
    schema: BUILD_SCHEMA,
    stage: oneOf(raw.stage, STAGE_IDS, 'profile'),
    company: { name: text(company.name, FREE_TEXT_MAX), industry: text(company.industry, 120), size: text(company.size, 60) },
    person: { role: text(person.role, FREE_TEXT_MAX) },
    level: oneOf(raw.level, LEVELS, null),
    profile_complete: raw.profile_complete === true,
    audit: { status: oneOf(audit.status, AUDIT_STATUSES, 'not_started'), summary: text(audit.summary, 1000) },
    departments: list(raw.departments, 30, normalizeDepartment),
    data_sources: list(raw.data_sources, 60, (d) => {
      const name = text(d.name, 120)
      return name
        ? {
            name,
            department: text(d.department, 80),
            holds: text(d.holds, 300),
            format: text(d.format, 120),
            access: oneOf(d.access, ACCESS_STATES, 'unknown'),
          }
        : null
    }),
    context_md: text(raw.context_md, 60000),
    agents: list(raw.agents, 50, normalizeAgent),
    focus: { agent: text(focus.agent, 80), skill: text(focus.skill, 80) },
    next_step: text(raw.next_step, 300),
  }
}

/**
 * Phase 1 moved the chat to the Agent Builder to build. The guide builds now, so what
 * that left in saved answers is forgotten wherever answers are read.
 */
const HANDOVER_FIELDS = ['buildChat', 'handoffSent', 'handoffBefore', 'handoffReported']
export function forgetHandover(intake) {
  if (!intake || !HANDOVER_FIELDS.some((f) => f in intake)) return intake
  const rest = { ...intake }
  for (const field of HANDOVER_FIELDS) delete rest[field]
  return rest
}

/** A settings proposal the person has answered, so its card doesn't come back. */
export function markProposalHandled(intake, id) {
  const handled = Array.isArray(intake?.fleetHandled) ? intake.fleetHandled : []
  if (!id || handled.includes(id)) return intake
  return { ...intake, fleetHandled: [...handled, id].slice(-50) }
}

/** They've been shown round the Workspace. */
export function markTourSeen(intake) {
  return { ...intake, tourSeen: true }
}

/** The state document out of the guide's `build` canvas, or null if it has none. */
export function canvasToState(canvas) {
  const blocks = Array.isArray(canvas?.blocks) ? canvas.blocks : []
  const block = blocks.find((b) => b?.id === BUILD_STATE_BLOCK && b?.kind === 'json')
  return block ? normalizeState(block.payload) : null
}

// ------------------------------------------------------------------- the world

/**
 * What the right column draws: the agent's state where it has written one, and
 * the person's own answers until then, so the world starts filling in while they
 * are still answering, before any model has run.
 */
export function worldView(state, intake) {
  const answers = intake?.answers || {}
  const intakeLevel = intake?.done && !intake.skipped ? startingLevel(answers) : null
  // Trinity knows who they are and what the company does once the guide says so
  // (questions, research and a short talk about their work), or the audit is done.
  const profileComplete = Boolean(state && (state.profile_complete || state.audit.status === 'done'))
  return {
    stage: state?.stage || 'profile',
    company: {
      name: state?.company.name || answers.company || '',
      industry: state?.company.industry || '',
      size: state?.company.size || '',
    },
    person: { role: state?.person.role || answers.role || '' },
    level: state?.level || intakeLevel,
    profileComplete,
    // Red in the stepper. Not while they are still answering, not before the guide
    // has written anything, and not while the audit is running, which fills it in.
    profileIncomplete: !profileComplete
      && Boolean(intake?.skipped || (intake?.done && state && state.audit.status !== 'in_progress')),
    audit: state?.audit || { status: 'not_started', summary: '' },
    // After they chose to skip it, and not while they are in the middle of doing it.
    auditNudge: Boolean(intake?.skipped || intake?.audit === 'skip')
      && state?.audit.status !== 'in_progress' && state?.audit.status !== 'done',
    departments: state?.departments || [],
    dataSources: state?.data_sources || [],
    contextMd: state?.context_md || '',
    agents: state?.agents || [],
    focus: state?.focus || { agent: '', skill: '' },
    nextStep: state?.next_step || '',
  }
}

/** The real roster entry behind an agent the guide reports, or null. */
export function liveAgentFor(agent, roster) {
  if (!agent?.live_name || !Array.isArray(roster)) return null
  return roster.find((a) => a?.name === agent.live_name) || null
}

/**
 * What already runs on this instance and the guide hasn't reported. The world is
 * a view of the real instance, so these belong on it from the first visit.
 */
export function instanceAgentsFor(roster, reported) {
  const claimed = new Set((reported || []).map((a) => a.live_name).filter(Boolean))
  return (Array.isArray(roster) ? roster : [])
    .filter((a) => a?.name && !isBuildCrew(a.name) && !claimed.has(a.name))
}

/**
 * The door: people who can make agents. An external client or a Workspace-only
 * member would be driving agent creation on someone else's instance.
 */
export function buildAvailableFor({ isPlatformSession = false, workspaceOnly = false } = {}) {
  return Boolean(isPlatformSession && !workspaceOnly)
}

/**
 * Whether the onboarding window opens by itself: on the first visit of someone who can
 * build and has nothing here yet. Anyone with a chat, or agents of their own, keeps
 * their Workspace exactly as before, and a chat list that failed to load is no evidence
 * of being new. The Build page opens the window for anyone else who reaches it.
 */
export function shouldOpenOnboarding({ buildAvailable = false, threads = [], threadsTrusted = false, roster = [], intake = null, seededAgents = [] } = {}) {
  if (!buildAvailable || !threadsTrusted || intake?.finished) return false
  // Opening an agent makes an empty Main chat, which isn't anyone having talked to it.
  if (threads.some((t) => t?.message_count !== 0)) return false
  // A fresh install comes with sample agents, which nobody here chose to build.
  return instanceAgentsFor(roster, []).every((a) => seededAgents.includes(a.name))
}

/**
 * What the left column shows. The questions come first, and Claude next because,
 * without it, every agent turn fails. A failed read of the flags is not evidence that it is missing, so it
 * never blocks anyone (the first-run overlay makes the same distinction).
 */
export function buildPhase({
  rosterLoaded = false, rosterError = '', hasGuide = false,
  claude = {}, canvasRead = false, threadsLoaded = false, intakeDone = false,
} = {}) {
  if (rosterError) return 'failed'
  if (!rosterLoaded || !claude.loaded) return 'loading'
  // Without a guide there is no canvas to wait for, and someone it already knows
  // must not be asked the questions again while their canvas is on its way.
  if (hasGuide && (!canvasRead || !threadsLoaded)) return 'loading'
  // The questions come first: they run in the browser, so they need neither Claude
  // nor the guide, and they are what a newcomer expects to meet.
  if (!intakeDone) return 'intake'
  if (!claude.failed && !claude.configured) return 'connect'
  if (!hasGuide) return 'setup'
  return 'chat'
}

/**
 * How the stepper draws one step. Someone can reach Build without doing the audit,
 * by building the small example or by skipping, and a check mark on Audit would then
 * say something that didn't happen.
 */
export function stepState(stepId, world) {
  if (stepId === 'profile' && world?.profileIncomplete) return 'incomplete'
  const index = stageIndex(stepId)
  const current = stageIndex(world?.stage)
  if (index === current) return 'current'
  if (index > current) return 'upcoming'
  if (stepId === 'audit' && world?.audit?.status !== 'done') return 'not-done'
  return 'done'
}

export function stageIndex(stage) {
  const i = STAGE_IDS.indexOf(stage)
  return i < 0 ? 0 : i
}

export const STATUS_DISPLAY = Object.freeze({
  designing: Object.freeze({ label: 'Designing', variant: 'neutral' }),
  building: Object.freeze({ label: 'Building', variant: 'info' }),
  needs_you: Object.freeze({ label: 'Needs you', variant: 'urgent' }),
  ready: Object.freeze({ label: 'Ready to review', variant: 'success' }),
  live: Object.freeze({ label: 'Live', variant: 'success' }),
  failed: Object.freeze({ label: 'Failed', variant: 'danger' }),
})

/**
 * Whether the guide is known to lack the permission to create agents. Read from the
 * instance rather than remembered from the deploy, so it survives a reload and
 * clears once an admin turns it on. An unreadable answer is not a "no".
 */
export function guideCannotCreate(grantsResponse) {
  const grants = grantsResponse?.grants
  if (!Array.isArray(grants)) return false
  const manage = grants.find((g) => g?.capability === 'agents.manage')
  return manage?.granted === false
}

/**
 * Why setting up the guide failed, in words a person can act on. A deploy reports
 * per-agent failures in its body, sometimes on a 200, so those are read first.
 */
export function deployFailureMessage({ result = null, error = null } = {}) {
  const body = result || error?.response?.data || {}
  const failed = Array.isArray(body.failed) ? body.failed : []
  if (failed.some((f) => /already exists/i.test(f?.reason || ''))) {
    const names = failed.map((f) => f.name).filter(Boolean).join(' and ')
    return `Trinity still holds the name ${names}, usually because an agent with that name was deleted. An administrator can release it.`
  }
  if (failed.length) {
    return `Setting up the guide failed: ${failed.map((f) => `${f.name}: ${f.reason}`).join('; ')}. Try again, or ask an administrator.`
  }
  if (!error) return ''
  if (error.response?.status === 403) {
    return "Your account can't create agents on this instance. Ask an administrator for an account that can."
  }
  const detail = typeof body.detail === 'string' ? body.detail : error.message
  return `Setting up the guide failed: ${detail || 'unknown error'}. Try again, or ask an administrator.`
}

/** The agent's credentials on its page in the main app, for adding the values. */
export function credentialsLinkFor(agentName) {
  return `/agents/${encodeURIComponent(agentName)}?tab=credentials`
}

/** How often the page rereads the canvas: quickly during a turn, slowly after. */
export const BUILD_POLL_LIVE_MS = 4000
export const BUILD_POLL_IDLE_MS = 30000
