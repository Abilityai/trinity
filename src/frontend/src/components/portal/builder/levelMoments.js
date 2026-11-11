/**
 * Level moments (core-agent AC-17): the page shows a person their levels when their first
 * build starts, lights a new agent in the sidebar, and lights the Levels card each time a
 * level ticks. These are the rules and the words; `PortalBuild.vue` shows them with the
 * same spotlight as the Workspace tour.
 */
import { nextLevel } from './levels'

const STARTED = ['plan', 'build']

/** The levels are shown once, when the first build starts and no level is reached yet. */
export function introDue({ levels, stage, intake }) {
  if (!Array.isArray(levels) || !levels.length) return false
  if (intake?.levelsIntroSeen) return false
  if (!STARTED.includes(stage)) return false
  return !levels.some((l) => l.id === 1 && l.reached)
}

export function markLevelsIntroSeen(intake) {
  return { ...intake, levelsIntroSeen: true }
}

export const INTRO_STEPS = Object.freeze([
  Object.freeze({
    target: 'levels',
    company: true,
    title: 'Your levels',
    body: "We'll take you through these steps to learn Trinity, and to build and manage your agents. Each one ticks here when you've done it.",
  }),
])

const joinIds = (ids) => (ids.length === 1 ? String(ids[0]) : `${ids.slice(0, -1).join(', ')} and ${ids[ids.length - 1]}`)

/**
 * What lights up when levels tick: the new agent in the sidebar first when Level 1 is one
 * of them and the page knows which agent it is, then the Levels card.
 */
export function reachedSteps({ reached, levels, agent = null }) {
  const ids = (Array.isArray(reached) ? reached : []).filter(Number.isInteger).sort((a, b) => a - b)
  if (!ids.length) return []
  const steps = []
  if (ids.includes(1) && agent?.name) {
    steps.push({
      selector: `[data-tour-agent="${agent.name}"]`,
      title: `${agent.label || agent.name} is yours`,
      body: 'Your agents live here: click one any time to talk to it.',
    })
  }
  const named = ids.length === 1 ? levels.find((l) => l.id === ids[0]) : null
  const next = nextLevel(levels)
  steps.push({
    target: 'levels',
    title: named ? `Level ${named.id} done: ${named.name}` : `Levels ${joinIds(ids)} done`,
    body: next ? `Next, Level ${next.id}: ${next.name}.` : "That's every level. You're certified to get started.",
    company: true,
  })
  return steps
}

// A level's name never has a full stop in it, so each sentence is one level or the next one.
// One or more groups of reached levels, each with its next level (lines the page joined).
const WHOLE = /^(?:(?:Level reached: Level \d+, [^.\n]+\.\s*)+(?:(?:Next: Level \d+, [^.\n]+\.|That was the last level\.)\s*)?)+$/
const REACHED_ID = /Level reached: Level (\d+),/g

/**
 * The page's line to the guide ("Level reached: ...", then the next one), shown in the chat
 * as a short note rather than as something the person said, also in the one-line form a
 * Working card shows. Anything else: null.
 */
export function levelNote(text, guideName = 'Your guide') {
  if (typeof text !== 'string') return null
  const flat = text.trim()
  if (!flat || !WHOLE.test(flat)) return null
  const ids = [...flat.matchAll(REACHED_ID)].map((m) => Number(m[1]))
  return `${ids.length === 1 ? 'Level' : 'Levels'} ${joinIds(ids)} reached. ${guideName} was told.`
}

const LATER = /^Level \d+, ([^.:\n]+): left for later\.\s*(?:Next: Level \d+, [^.\n]+\.|That was the last level\.)$/

/** The page's "left for later" line to the guide, as a short note. Anything else: null. */
export function laterNote(text, guideName = 'Your guide') {
  const name = typeof text === 'string' ? text.trim().match(LATER)?.[1] : null
  return name ? `${name} left for later. ${guideName} was told.` : null
}

export const MOMENT_RETRIES = 3

/**
 * A moment closed with nothing on screen to light (the Levels card hadn't drawn yet, or
 * the page was mid-change) comes back, up to `MOMENT_RETRIES` times, rather than counting
 * as seen: nobody saw it. Null once it has been tried enough, or for any other close.
 */
export function retryDropped(moment, reason, max = MOMENT_RETRIES) {
  if (reason !== 'dropped' || !moment) return null
  const tries = (Number(moment.tries) || 0) + 1
  return tries > max ? null : { ...moment, tries }
}
