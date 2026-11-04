/**
 * The intro's scenes and timing, kept out of the component so a test can reach them.
 * The company it shows is how Ability AI runs on Trinity, as described by the team.
 * No numbers: only claims someone at the company can stand behind.
 */
export const INTRO_SCENES = Object.freeze([
  Object.freeze({
    id: 'agent',
    ms: 10000,
    title: 'What is an AI agent?',
    caption: "Not a chatbot you have to keep asking. An agent has a job, the tools to do it, and a memory. It keeps working, and it gets better at the job.",
    items: Object.freeze([
      Object.freeze({ title: 'A job', detail: 'One thing it is responsible for' }),
      Object.freeze({ title: 'Tools', detail: 'The systems it works in' }),
      Object.freeze({ title: 'Memory', detail: 'What it has learned about you' }),
    ]),
    layout: 'row',
  }),
  Object.freeze({
    id: 'support',
    ms: 15000,
    title: 'Take one team: support',
    caption: 'It answers from your knowledge base, hands the hard ones to a person, then learns from what that person said so the next answer is better.',
    items: Object.freeze([
      Object.freeze({ title: 'A question comes in', detail: 'From a customer' }),
      Object.freeze({ title: 'It answers', detail: 'From your knowledge base' }),
      Object.freeze({ title: 'It escalates', detail: 'The hard ones go to a person' }),
      Object.freeze({ title: 'It fills the gap', detail: 'The knowledge base improves' }),
    ]),
    layout: 'flow',
  }),
  Object.freeze({
    id: 'company',
    ms: 22000,
    title: 'Now a whole company',
    caption: "This is how we run our own company on Trinity. Teams of agents share one brain, and each keeps doing its job day after day.",
    items: Object.freeze([
      Object.freeze({ title: 'The brain: Cornelius', detail: 'Holds what the company knows, for every other agent' }),
      Object.freeze({ title: 'Marketing', detail: 'Researches, writes and posts, then reviews each campaign and improves it' }),
      Object.freeze({ title: 'Sales', detail: 'Finds leads, sends messages and keeps the CRM clean' }),
      Object.freeze({ title: 'Support', detail: 'Answers questions, escalates, and fills gaps in the knowledge base' }),
    ]),
    layout: 'grid',
  }),
  Object.freeze({
    id: 'platform',
    ms: 16000,
    title: 'Built to run for real',
    caption: "Agents aren't left to run loose. Trinity gives them what a real team has: a way to work together, a routine, clear limits and a record.",
    items: Object.freeze([
      Object.freeze({ title: 'They talk to each other', detail: 'One agent hands work to the next' }),
      Object.freeze({ title: 'Schedules', detail: 'Jobs run on time, without being asked' }),
      Object.freeze({ title: 'Permission controls', detail: 'Each agent reaches only what it should' }),
      Object.freeze({ title: 'Full logs', detail: 'See everything an agent did, and why' }),
    ]),
    layout: 'grid',
  }),
  Object.freeze({
    id: 'charge',
    ms: 12000,
    title: 'You stay in charge',
    caption: 'Agents do the work and ask before anything risky. The question lands in your Inbox, and nothing goes ahead until you say so.',
    items: Object.freeze([
      Object.freeze({ title: 'An agent asks', detail: '"Can I send this?"' }),
      Object.freeze({ title: 'Your Inbox', detail: 'One place for every question' }),
      Object.freeze({ title: 'You decide', detail: 'Approve it or change it' }),
    ]),
    layout: 'flow',
  }),
  Object.freeze({
    id: 'yours',
    ms: 5000,
    title: "Now let's build yours",
    caption: 'It starts with one small agent, doing one real job at your company.',
    items: Object.freeze([]),
    layout: 'empty',
  }),
])

export const INTRO_TOTAL_MS = INTRO_SCENES.reduce((sum, s) => sum + s.ms, 0)

/** Which scene is showing, and how many of its items have appeared so far. */
export function introFrame(elapsedMs) {
  const elapsed = Math.max(0, Number(elapsedMs) || 0)
  let start = 0
  for (let index = 0; index < INTRO_SCENES.length; index += 1) {
    const scene = INTRO_SCENES[index]
    if (elapsed < start + scene.ms) {
      const into = elapsed - start
      const count = scene.items.length
      // The items arrive over the first two thirds, so the last one can be read.
      const shown = count ? Math.min(count, Math.floor(into / ((scene.ms * 2) / 3 / count)) + 1) : 0
      return { index, scene, shown, ended: false }
    }
    start += scene.ms
  }
  const last = INTRO_SCENES.length - 1
  return { index: last, scene: INTRO_SCENES[last], shown: INTRO_SCENES[last].items.length, ended: true }
}

/** The elapsed time at which a scene begins, for the Next and Back controls. */
export function introSceneStart(index) {
  return INTRO_SCENES.slice(0, Math.max(0, index)).reduce((sum, s) => sum + s.ms, 0)
}
