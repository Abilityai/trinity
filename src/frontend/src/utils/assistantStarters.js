/**
 * What the assistant's empty screen offers: a few things it can do, grouped,
 * chosen for where the person is and what they have.
 *
 * An item with `fill` is the start of a sentence the person finishes (it goes
 * into the box); one without is sent as it stands.
 */
const ask = (label) => ({ label, text: label })
const start = (label, text) => ({ label, text, fill: true })

const BUILD = Object.freeze({
  heading: 'Build',
  items: [
    ask('Build an agent with me'),
    ask('What could agents do for my business?'),
    ask('Show me the ready-made agents'),
  ],
})

// Asked first, of someone the assistant knows nothing about: one click, and
// what it suggests from then on fits them.
const ABOUT = Object.freeze({
  heading: 'First, what describes you best?',
  items: [
    { label: 'Founder or owner', text: "I'm a founder or business owner." },
    { label: 'Operations', text: 'I work in operations.' },
    { label: 'Engineering', text: "I'm an engineer." },
    { label: 'Sales or marketing', text: 'I work in sales or marketing.' },
    { label: 'Just exploring', text: "I'm just exploring what Trinity can do." },
  ],
})

const TELL = Object.freeze({
  heading: 'Tell the Trinity team',
  items: [
    start('Report a problem…', "Something isn't working: "),
    start('Suggest an idea…', "Here's an idea for Trinity: "),
  ],
})

/**
 * @param {{ label: string, agent: string|null }} page - describePage(route)
 * @param {Array<{ name: string, status?: string }>} fleet
 * @param {{ known?: boolean }} person - whether the assistant already knows who it is talking to
 */
export function starterGroups(page, fleet, person = {}) {
  const agents = (fleet || []).filter((a) => a?.name)
  const here = page?.agent && agents.some((a) => a.name === page.agent) ? page.agent : null
  const about = person.known ? [] : [ABOUT]

  if (!agents.length) {
    return [
      ...about,
      { heading: 'Start here', items: [ask('Build my first agent'), ...BUILD.items.slice(1)] },
      { heading: 'Find your way', items: [ask('How does Trinity work?'), ask('What do I still need to set up?')] },
      TELL,
    ]
  }

  const groups = [...about]
  if (here) {
    groups.push({
      heading: 'This agent',
      items: [
        ask(`What does ${here} do?`),
        ask(`How has ${here} been running?`),
        ask(`Give ${here} a test run`),
        start('Change when it runs…', `Change ${here}'s schedule to `),
      ],
    })
  }
  groups.push({
    heading: 'Check',
    items: [
      ask('How are my agents doing?'),
      ask('What failed recently, and why?'),
      ask('What did my agents cost this week?'),
      ask('Is everything set up correctly?'),
    ].slice(0, here ? 2 : 4),
  })
  if (!here) {
    const first = (agents.find((a) => a.status === 'running') || agents[0]).name
    groups.push({
      heading: 'Do',
      items: [
        ask('What runs on a schedule?'),
        start('Schedule an agent…', `Every weekday at 9, have ${first} `),
        start('Ask an agent…', `Ask ${first} `),
        start('Share an agent…', `Share ${first} with `),
      ],
    })
  }
  groups.push(here ? { ...BUILD, items: BUILD.items.slice(0, 1) } : BUILD)
  groups.push(TELL)
  return groups
}
