/**
 * Atlas's template (config/agent-templates/build-guide): it builds the agent itself, in
 * the same chat, with the create-agent wizard. Nothing in it hands the chat to another
 * agent any more.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'fs'
import { join } from 'path'
import { fileURLToPath } from 'url'
import { describe, expect, it } from 'vitest'
import { LEVELS } from '@/components/portal/builder/levels'
import { WELCOME_STARTS } from '@/components/portal/builder/portalBuild'

const ROOT = fileURLToPath(new URL('../../../../config/agent-templates/build-guide/', import.meta.url))
const read = (path) => readFileSync(join(ROOT, path), 'utf8')
function allFiles(dir = ROOT) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? allFiles(path) : [path]
  })
}

describe("Atlas's template: it builds the agent itself", () => {
  it('tells a newcomer an agent can do several jobs, in the video\'s words, never that it has one', () => {
    const newcomer = read('CLAUDE.md').split('\n').find((l) => l.startsWith('| `newcomer`'))
    expect(newcomer).not.toMatch(/one job/i)
    expect(newcomer).toMatch(/several jobs/)
    expect(newcomer).toMatch(/your tools, instructions you give it once, and a memory/)
  })

  it('counts thinking work, like the business model, as something an agent helps with, never as a goal it rules out', () => {
    const playbook = read('.claude/skills/company-audit/PLAYBOOK.md')
    expect(playbook.startsWith('# The AI audit playbook')).toBe(true)
    expect(playbook).toMatch(/four kinds of opportunity/)
    expect(playbook).toMatch(/\*\*Thinking work:\*\*/)
    expect(playbook).toMatch(/business model/)
    expect(playbook).toMatch(/Never tell them an agent can't help with a goal/)
    expect(playbook).not.toMatch(/If it only frees up time rather than serving that goal directly, say so/)
  })

  it('knows the eight levels by the page\'s own names, answers a reached level, and helps with the next', () => {
    const doc = read('CLAUDE.md')
    const section = doc.slice(doc.indexOf('## Levels'), doc.indexOf('\n## ', doc.indexOf('## Levels') + 1))
    expect(section.length).toBeGreaterThan(100)
    for (const level of LEVELS) expect(section).toContain(`${level.id}. **${level.name}**`)
    expect(section).toMatch(/A line that starts `Level reached:`/)
    expect(section).toMatch(/`Next:`/)
    expect(section).toMatch(/When none of their agents needs a key, it is still a step/)
    expect(section).toMatch(/Show me `credentials`/)
    expect(section).toMatch(/the page checked/i)
    expect(section).toMatch(/Never say a level is reached/)
    expect(section).toMatch(/`Help me reach Level/)
    expect(section).toMatch(/\/build-agent/)
    expect(section).toMatch(/\/fleet/)
    expect(doc).toMatch(/\| `Level reached:` \.\.\. \| /)
    // The fast path for testing: a brief and "skip the audit" go straight to the Level 1 question, with no questions.
    const fast = doc.split('\n').find((l) => l.startsWith('| They give you a brief and ask to skip the audit'))
    expect(fast).toMatch(/no questions/)
    expect(fast).toMatch(/Level 1 question/)
    expect(fast).toMatch(/Skip the audit and `\/about-your-work`/)
  })

  it('installs the create-agent wizard and offers /build-agent, not /build-it', () => {
    const template = read('template.yaml')
    expect(template).toMatch(/installed:[\s\S]*- create-agent@abilityai/)
    expect(template).toMatch(/playbooks:[\s\S]*- name: build-agent\n/)
    expect(template).not.toMatch(/- name: build-it\n/)
    expect(existsSync(join(ROOT, '.claude/skills/build-it'))).toBe(false)
  })

  it('builds with the wizard, asks in this chat, and leaves every schedule off', () => {
    const skill = read('.claude/skills/build-agent/SKILL.md')
    expect(skill).toContain('/create-agent:custom')
    expect(skill).toContain('enabled: false')
    expect(skill).toContain('in this chat')
    // The wizard's own way of asking goes to the Inbox; here the person is in the chat.
    for (const line of skill.split('\n').filter((l) => l.includes('ask_operator'))) expect(line).toMatch(/[Nn]ever/)
    expect(skill).toContain('scripts/deploy-agent.sh')
  })

  // The first build walks up the levels. The agent first, then its first
  // playbook, then the others one at a time; later agents all at once.
  it('opens a first build by saying it will go step by step, where the levels are, and that Level 1 is asking for the agent', () => {
    const levels = read('CLAUDE.md').slice(read('CLAUDE.md').indexOf('## Levels'))
    expect(levels).toMatch(/When you start on a person's first agent/)
    expect(levels).toMatch(/Since this is your first agent, I'll take you through it step by step/)
    expect(levels).toMatch(/Level 1 is simply to have your agent/)
    expect(levels).toMatch(/Don't repeat it for later agents/)
    expect(read('.claude/skills/plan-agent/SKILL.md')).toMatch(/If this is their first agent .*the short paragraph about their levels/)
  })

  // Tester feedback, 5 Nov: the plan said "it needs your Supabase credentials" before the agent
  // existed, which read as "give them now". The plan is read back as the steps, and a
  // key is said to be asked for at its level, never before.
  it('reads a first plan back as the levels, with what it needs asked for at that level and not before', () => {
    const levels = read('CLAUDE.md').slice(read('CLAUDE.md').indexOf('## Levels'))
    expect(levels).toMatch(/asked for at its own level, never before/)
    expect(levels).toMatch(/I'll ask you for at that level, not before/)
    const fast = read('CLAUDE.md').split('\n').find((l) => l.startsWith('| They give you a brief and ask to skip the audit'))
    expect(fast).toMatch(/asked for at Level 3, not now/)
    const plan = read('.claude/skills/plan-agent/SKILL.md')
    expect(plan).toMatch(/read it back as the steps you'll take together, one level each/)
    expect(plan).toMatch(/Level 3, the keys it needs, named here and asked for then, in Trinity, never now/)
    expect(plan).toMatch(/\*\*Level 1, now\.\*\* I create Game Filler\. No keys, no jobs yet, nothing needed from you\./)
    expect(plan).toMatch(/I'll ask for those at that step, and you'll add them in Trinity, not here/)
  })

  it('builds a first agent one step at a time, its playbooks held back and added one by one', () => {
    const skill = read('.claude/skills/build-agent/SKILL.md')
    const section = skill.slice(skill.indexOf('## First agent: one step at a time'), skill.indexOf('\n## ', skill.indexOf('## First agent: one step at a time') + 1))
    expect(section.length).toBeGreaterThan(200)
    expect(section).toMatch(/fleet-state\.json/)
    expect(section).toMatch(/~\/builds\/<slug>\.held\//)
    // A first agent is created by the guide itself: the wizard's questions and approvals are Inbox cards nothing answers back.
    expect(section).toMatch(/without the wizard/)
    expect(section).toMatch(/deploy-agent\.sh/)
    expect(section).toMatch(/Never paste an archive/)
    expect(section).toMatch(/get_agent_info/)
    expect(section).toMatch(/End your turn there/)
    expect(section).toMatch(/what a playbook is/)
    expect(section).toMatch(/`chat_with_agent`/)
    expect(section).toMatch(/exactly as written/)
    expect(section).toMatch(/status: "planned"/)
    expect(section).toMatch(/status: "done"/)
    expect(section).toMatch(/offer to add the rest together/)
    expect(section).toMatch(/never add a playbook to it in the same turn you create it/i)
    expect(section).toMatch(/later agent, build it all at once/)
    // Atlas's rules agree, and its Levels help points at the held playbooks.
    const guide = read('CLAUDE.md')
    expect(guide.split('\n').find((l) => l.startsWith('2. **You find out what they want'))).toMatch(/first build/)
    expect(guide).toMatch(/- Level 1:.*one step at a time/)
    expect(guide).toMatch(/- Level 2:.*~\/builds\/<slug>\.held\//)
  })

  it("names the plan's keys in the agent's settings when it is created, so the page keeps Level 3 for them", () => {
    // The page shows Level 3 as not needed while Trinity lists no keys for any of their
    // agents. Named in the file the guide writes itself, they can't be skipped later on.
    const skill = read('.claude/skills/build-agent/SKILL.md')
    const settings = skill.split('\n').find((l) => l.startsWith('- `template.yaml`: `name: <slug>`'))
    expect(settings).toMatch(/when the plan lists keys, `credentials:` with their names under `env_file`.*`credential_setup:`/)
    expect(settings).toMatch(/names only/i)
    // Only a required key counts as needed: an optional one reads as no keys at all.
    expect(settings).toMatch(/`required: true`/)
    expect(settings).not.toMatch(/no `credentials:`/)
    // An example in the exact shape, `credential_setup:` beside `credentials:`, never inside.
    expect(skill).toMatch(/\ncredentials:\n {2}env_file:\n {4}- [A-Z_]+\n(?: {4}- [A-Z_]+\n)*credential_setup:\n {2}- name: [A-Z_]+\n {4}title: .+\n {4}description: .+\n {4}required: true\n/)
    expect(settings).toMatch(/top level.*never inside it/)
    const level3 = skill.split('\n').find((l) => l.startsWith('**Level 3: its keys.**'))
    expect(level3).toMatch(/named when it was created/)
    expect(level3).toMatch(/Show me `credentials`/)
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/- Level 1:.*the names of the keys it will need/)
    expect(guide).toMatch(/- Level 3:.*named when it was created/)
  })

  it('always takes them to Level 3, and moves on from it only on their yes when no agent needs a key', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/"keys_not_needed": false/)
    expect(guide.split('\n').find((l) => l.startsWith('3. **Connected**'))).toMatch(/still a step.*nothing to connect.*move on.*`keys_not_needed: true`/)
    expect(guide).toMatch(/- Level 3:.*Show me `credentials`.*needs no key.*`keys_not_needed: true`/)
    const level3 = read('.claude/skills/build-agent/SKILL.md').split('\n').find((l) => l.startsWith('**Level 3: its keys.**'))
    expect(level3).toMatch(/needs no key.*move on.*`keys_not_needed: true`.*Level 4/)
    expect(level3).not.toMatch(/skip this/)
  })

  it('ends a first agent by sending them to try it in its own chat, and never calls the levels finished', () => {
    const skill = read('.claude/skills/build-agent/SKILL.md')
    const last = skill.split('\n').find((l) => l.startsWith('**When its steps are done.**'))
    expect(last).toBeTruthy()
    // Where to click, then what to type: the route, not a description.
    expect(last).toMatch(/click \*\*<name>\*\* under Agents, on the left/)
    expect(last).toMatch(/type `\/<playbook>`/)
    expect(last).toMatch(/Never say "you're done"/)
    expect(last).toMatch(/eight levels/)
    expect(last).toMatch(/For example: "/)
    // No push to the next level: it waits for them, and more playbooks come before more agents.
    expect(last).toMatch(/nothing more they have to do now/)
    expect(last).toMatch(/like an employee/)
    expect(last).toMatch(/different person/)
    expect(last).not.toMatch(/Want to plan the next/)
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/- \*\*A new playbook or a new agent\*\*:.*like an employee.*different person/)
    expect(guide).not.toMatch(/Want to plan the next/)
    expect(guide).toMatch(/- Level 6:.*different person/)
    const level5 = skill.split('\n').find((l) => l.startsWith('**Level 5: on a schedule.**'))
    expect(level5).toMatch(/ticks Level 5 once the schedule and Autonomy are both on/)
    const doc = read('CLAUDE.md')
    expect(doc).toMatch(/5\. \*\*On a schedule\*\*: one of their agents has a schedule turned on, with Autonomy on/)
    expect(doc).not.toMatch(/five levels/i)
    expect(doc).toMatch(/`Level reached: Level 5` and it is their first agent.*click \*\*<name>\*\* under Agents, on the left.*type `\/<playbook>`/)
  })

  it('keeps its own tools out of the chat: it offers the walk in plain words, never "with /fleet"', () => {
    const doc = read('CLAUDE.md')
    const talk = doc.slice(doc.indexOf('## How you talk'), doc.indexOf('\n## ', doc.indexOf('## How you talk') + 1))
    expect(talk).toMatch(/- Your own tools stay out of the chat\./)
    expect(talk).toMatch(/never name one to them/)
    expect(talk).toMatch(/Not "Shall I walk you through it with \/fleet\?" but "Shall I walk you through it\?"/)
    const level3 = read('.claude/skills/build-agent/SKILL.md').split('\n').find((l) => l.startsWith('**Level 3: its keys.**'))
    expect(level3).toMatch(/"Shall I walk you through it\?"/)
    expect(level3).toMatch(/never name `\/fleet`/)
  })

  it("looks up each service's own guide, and gives the link and the steps before walking them to where the key goes", () => {
    const level3 = read('.claude/skills/build-agent/SKILL.md').split('\n').find((l) => l.startsWith('**Level 3: its keys.**'))
    // Looked up every time, on the service's own site: what it remembers is often out of date.
    expect(level3).toMatch(/where to find its value/)
    expect(level3).toMatch(/Look it up first, every time/)
    expect(level3).toMatch(/`WebSearch`.*its own site only.*`WebFetch`/)
    // One block per key: what it is, the steps in the page's own menu names, the link.
    expect(level3).toMatch(/one short block per key/)
    expect(level3).toMatch(/For example: "/)
    expect(level3).toMatch(/Settings, then API Keys/)
    expect(level3).toMatch(/\[Supabase: API keys\]\(https:\/\/supabase\.com\/docs\/guides\/api\/api-keys\)/)
    expect(level3).toMatch(/\[WhatsApp Cloud API: get started\]\(https:\/\/developers\.facebook\.com\/docs\/whatsapp\/cloud-api\/get-started\)/)
    // Never invented, never typed into the chat.
    expect(level3).toMatch(/don't invent a menu name/)
    expect(level3).toMatch(/never in this chat/)
    // Finding them comes before the offer of the walk.
    expect(level3.indexOf('where to find its value')).toBeLessThan(level3.indexOf('"Shall I walk you through it?"'))
    // A failed test brings the guide again, not a bare list of key names, and the page's line stays out of the chat.
    expect(level3).toMatch(/give that key's block again, looked up fresh, with the guide's link and its steps/)
    expect(level3).toMatch(/Don't quote `CONNECTIONS: OK` or `CONNECTIONS: FAILED` to them/)
    expect(read('CLAUDE.md')).toMatch(/- Level 3:.*where to find its value.*service's own help page.*Show me `credentials`/)
  })

  it('tests the connections when they come back, in the words the page looks for, and never says they work without the reply', async () => {
    const { CONNECTION_TEST_PREFIX, CONNECTIONS_OK, CONNECTIONS_FAILED } = await import('@/components/portal/builder/levelSignals')
    const level3 = read('.claude/skills/build-agent/SKILL.md').split('\n').find((l) => l.startsWith('**Level 3: its keys.**'))
    // The agent itself makes one read-only request per service, never prints a value, and
    // ends on the line the page reads. The request starts with the words the page looks for.
    const message = level3.match(/sending exactly this and nothing else: "([^"]+)"/)?.[1] || ''
    expect(message.startsWith(CONNECTION_TEST_PREFIX)).toBe(true)
    expect(message).toMatch(/read-only request/)
    expect(message).toMatch(/Never print a key's value\./)
    expect(message).toContain(`\`${CONNECTIONS_OK}\``)
    expect(message).toContain(`\`${CONNECTIONS_FAILED}\``)
    // The page decides: the guide writes nothing to tick it, and claims nothing without the reply.
    expect(level3).toMatch(/nothing you write on the world and nothing you say can tick it/)
    expect(level3).toMatch(/Never say the connections work.*without that reply in front of you/)
    expect(level3).not.toMatch(/keys_tested/)
    // A failed test is help, not a dead end.
    expect(level3).toMatch(/401 or 403.*404/)
    expect(level3).toMatch(/offer the walk again/)
    // Someone testing with made-up values is told the truth and not held up.
    expect(level3).toMatch(/placeholders/)
    const doc = read('CLAUDE.md')
    expect(doc).not.toMatch(/keys_tested/)
    expect(doc).toMatch(/3\. \*\*Connected\*\*: every key one of their agents needs is set, and a test of its connections passed/)
    expect(doc).toMatch(/- Level 3:.*test the connections.*`CONNECTIONS: OK`/)
  })

  it('carries the keys and connections rules in the file it always reads, not only in a skill it may not open', async () => {
    const { CONNECTION_TEST_PREFIX } = await import('@/components/portal/builder/levelSignals')
    const doc = read('CLAUDE.md')
    const at = doc.indexOf('## Keys and connections')
    expect(at).toBeGreaterThan(-1)
    const section = doc.slice(at, doc.indexOf('\n## ', at + 1))
    expect(section).toMatch(/in any message, not only inside `\/build-agent`/)
    // The same test message as the skill, word for word, so the page finds the run either way.
    const inSkill = read('.claude/skills/build-agent/SKILL.md').match(/sending exactly this and nothing else: "([^"]+)"/)?.[1]
    const inDoc = section.match(/sending exactly this and nothing else: "([^"]+)"/)?.[1]
    expect(inDoc).toBe(inSkill)
    expect(inDoc.startsWith(CONNECTION_TEST_PREFIX)).toBe(true)
    // A new request is a new call: never a result from memory.
    expect(section).toMatch(/Never answer from an earlier test/)
    // Help is the guide looked up and its steps, never a list of key names.
    expect(section).toMatch(/`WebSearch`.*its own site only.*`WebFetch`/)
    expect(section).toMatch(/A list of key names is not help/)
    expect(section).toMatch(/Don't quote `CONNECTIONS: OK` or `CONNECTIONS: FAILED`/)
    expect(section).toMatch(/"Shall I walk you through it\?"/)
    expect(section).toMatch(/For example: "/)
    expect(section).not.toMatch(/—/)
    // It never works out the next level by counting: the page says it, and writes it down.
    expect(doc).toMatch(/Never work out which level is next by counting/)
    expect(doc).toMatch(/`fleet-state\.json`.*`levels`.*`next`/)
    expect(doc).toMatch(/A line that starts `Level 3, Connected: left for later\.`/)
    // Someone without working keys yet is not held up: Connected waits as Later, never as a tick.
    expect(section).toMatch(/`keys_later: true`/)
    expect(section).toMatch(/It is not a tick/)
    expect(section).toMatch(/Only on their yes/)
    expect(doc).toContain('"keys_later": false')
  })

  it("never suggests trying a playbook before its keys are set", () => {
    const level2 = read('.claude/skills/build-agent/SKILL.md').split('\n').find((l) => l.startsWith('**Level 2: its first playbook.**'))
    expect(level2).toMatch(/If its keys aren't set yet, don't suggest trying it/)
    expect(read('CLAUDE.md')).toMatch(/- Level 2:.*unless it needs a key that isn't set yet, tell them to try/)
  })

  it('has the deploy script, ready to run', () => {
    const script = join(ROOT, 'scripts/deploy-agent.sh')
    expect(existsSync(script)).toBe(true)
    expect(statSync(script).mode & 0o111).not.toBe(0)
  })

  it('no longer mentions handing the chat to the Agent Builder anywhere', () => {
    for (const file of allFiles()) {
      // Its avatar is an image, not words.
      if (/\.(webp|png|jpg)$/.test(file)) continue
      const text = readFileSync(file, 'utf8')
      expect(text, file).not.toMatch(/agent-builder|handoff|\/build-it\b/)
    }
  })
})

describe("Atlas's template: settings changes through the person's click", async () => {
  const { KNOWN_CHANGES, SHOW_ME_KEYS, FLEET_CANVAS_ID, FLEET_PROPOSAL_BLOCK, FLEET_STATE_FILE } = await import('../../src/components/portal/builder/fleetChanges')

  it('offers /fleet', () => {
    expect(read('template.yaml')).toMatch(/playbooks:[\s\S]*- name: fleet\n/)
  })

  it("names the card's block `proposal` every time, with the new id inside it, shown in full", () => {
    const skill = read('.claude/skills/fleet/SKILL.md')
    expect(skill).toMatch(/The block's `id` is always `proposal`.*new `id` goes inside the payload/)
    expect(skill).toMatch(/"blocks": \[\{ "id": "proposal", "kind": "json", "payload": \{ "id": "share-2", "action": "share"/)
  })

  it('proposes exactly the changes and places the page knows, one at a time', () => {
    const skill = read('.claude/skills/fleet/SKILL.md')
    for (const action of Object.keys(KNOWN_CHANGES)) expect(skill, action).toContain(`\`${action}\``)
    for (const key of SHOW_ME_KEYS) expect(skill, key).toContain(`\`${key}\``)
    for (const name of [FLEET_CANVAS_ID, FLEET_PROPOSAL_BLOCK, FLEET_STATE_FILE]) expect(skill, name).toContain(name)
    expect(skill).toContain('one change')
    // The page's three replies.
    for (const reply of ['Done:', 'Not now:', "That didn't work:", 'Not sure it worked:']) expect(skill, reply).toContain(reply)
  })

  it('walks people to sharing, calling and schedules, naming what to light, and knows schedules need Autonomy', async () => {
    const { SHOW_ME_WALKS } = await import('../../src/components/onboarding/showMeWalk')
    const skill = read('.claude/skills/fleet/SKILL.md')
    expect(SHOW_ME_WALKS).toEqual(['credentials', 'people', 'permissions', 'schedules'])
    expect(skill).toMatch(/`credentials`, `people`, `permissions` and `schedules` are walks/)
    expect(skill).toMatch(/"key": "permissions", "agent": "[a-z-]+", "to": "[a-z-]+"/)
    expect(skill).toMatch(/"key": "schedules", "agent": "[a-z-]+", "schedule_id": "/)
    expect(skill).toMatch(/never put an email in it/i)
    expect(skill).toContain('`autonomy`')
    expect(skill).toMatch(/Autonomy is off/)
    expect(skill).toMatch(/## After a walk/)
  })

  it('helps with Level 2 by asking the agent to write the playbook into itself, never editing its files', () => {
    const doc = read('CLAUDE.md')
    const section = doc.slice(doc.indexOf('## Levels'), doc.indexOf('\n## ', doc.indexOf('## Levels') + 1))
    expect(section).toMatch(/- Level 2:.*`chat_with_agent`/)
    expect(section).toMatch(/- Level 2:.*\.claude\/skills\/<name>\/SKILL\.md/)
    expect(section).toMatch(/- Level 2:.*own chat/)
    expect(section).toMatch(/- Level 2:.*playbooks it came with/)
    // A newcomer gets a plain sentence to send, never a file path.
    expect(section).toMatch(/- Level 2:.*"Please add a playbook called \/[a-z-]+ that/)
    // The build rule says so too, so the two never disagree.
    const rule = doc.split('\n').find((l) => l.startsWith('2. **You find out what they want'))
    expect(rule).toMatch(/new playbook for an agent they already have/)
  })

  it('helps with Levels 4 to 8 by walking them there first, a card as the one-click option', () => {
    const doc = read('CLAUDE.md')
    const section = doc.slice(doc.indexOf('## Levels'), doc.indexOf('\n## ', doc.indexOf('## Levels') + 1))
    expect(section).toMatch(/- Level 3:.*Show me `credentials`/)
    expect(section).toMatch(/- Level 4:.*Show me `people`.*Show me `permissions`/)
    expect(section).toMatch(/- Level 5:.*Show me `schedules`.*Autonomy/)
    expect(section).toMatch(/- Level 7:.*Show me `permissions`/)
    expect(section).toMatch(/- Level 8:.*Show me `permissions`/)
    // The last level is the one that has to really run: the guide gives the message to send and says where the approval lands.
    expect(section).toMatch(/- Level 8:.*don't stop at the permissions.*click \*\*<lead>\*\* under Agents.*Inbox/)
    expect(section).toMatch(/For Levels 4 to 8, offer the walk first/)
    expect(section).toMatch(/one click/)
  })

  it('never changes a setting with its own key, and sends people to /fleet for settings', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toContain('Never change a setting with your own key')
    expect(guide).toMatch(/\| .*(setting|who can).* \| `\/fleet`/i)
  })

  it('opens its first reply with who it is and the first agent it is there to build, and only then', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/\*\*Your first reply to them opens with who you are and what you're here for\*\*/)
    expect(guide).toContain("Hi, I'm Atlas, your guide. We're here to build your first agent together")
    expect(guide).toMatch(/Only in your first reply: don't introduce yourself again/)
  })

  it("knows what each of the Build page's welcome buttons sends", () => {
    const guide = read('CLAUDE.md')
    const lines = Object.values(WELCOME_STARTS).map((s) => s.message)
    expect(lines).toHaveLength(5)
    for (const line of lines) expect(guide).toContain(`\`${line}\``)
  })

  it('runs the audit when they chose it, and never starts it when they skipped it', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/\| `Let's do the AI audit now\.` .*\| `\/company-audit`.*Don't skip any of it to build sooner/)
    expect(guide).toContain("When they skipped the audit, don't start it and don't ask its questions")
  })

  it('does what the person asked rather than swapping in its own idea, and only claims a call the page shows', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/\*\*Their idea is the one you set up\.\*\*/)
    expect(guide).toContain('Never answer their idea by offering yours again')
    expect(guide).toContain('the question you end on names their choice, never yours')
    expect(guide).toMatch(/Only say one agent can call another when `may_call` in `fleet-state\.json` shows it/)
  })

  it('closes the levels: done, much more to Trinity, certified to get started, and no push', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toContain('ends `That was the last level.`')
    expect(guide).toContain("there is so much more to Trinity, but they're certified to get started")
    expect(guide).toMatch(/Don't propose a next step/)
  })

  it("writes into every agent that uses a key where that key lives, so it never looks in another store", () => {
    const guide = read('CLAUDE.md')
    expect(guide).toMatch(/\*\*Tell the agent where its keys are\.\*\*/)
    expect(guide).toContain('environment variables in its own container')
    expect(guide).toContain('never with `fetch_credential`')
    expect(read('.claude/skills/build-agent/SKILL.md')).toContain('never with `fetch_credential`')
  })

  it('proposes new agents from what it knows, and never gives an existing agent\'s own job as an example', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toContain('propose before you ask')
    expect(guide).toContain("Never say you don't know what the team should do")
    expect(guide).toContain('would they hand it to a different person than the agents they have?')
    expect(guide).toContain('never an example of a new agent')
  })

  it('judges playbook or agent itself, every time, and says so before building a separate agent', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toContain('This call is yours to make, every time, for your own suggestions and for theirs')
    expect(guide).toContain('would the same person do this job as one of the agents they have?')
    expect(guide).toContain('say so before you plan anything and recommend the playbook')
    expect(guide).toContain('they decide, but never without having heard this from you first')
  })

  it('has the agent look at its data once its keys work, keep a map of it, and write playbooks from the map', () => {
    const guide = read('CLAUDE.md')
    const skill = read('.claude/skills/build-agent/SKILL.md')
    expect(guide).toContain('## Look at the data before a playbook relies on it')
    expect(guide).toContain('Look at your data, changing nothing in it:')
    expect(guide).toContain('Save what you find as `data-map.md` in your home folder')
    // The look is read-only and never mistaken for the connection test the page reads.
    expect(guide).not.toMatch(/sending exactly this: "Test your connections[^"]*data-map/)
    expect(guide).toContain('It is a look, not a playbook')
    for (const text of [guide, skill]) expect(text).toContain('Read `~/data-map.md` first and use its names')
    expect(skill).toContain('correct it from the map right after the test passes')
  })

  it('declares an avatar prompt, without which the deploy never installs the bundled face', () => {
    expect(existsSync(join(ROOT, 'avatar.webp'))).toBe(true)
    expect(read('template.yaml')).toMatch(/^avatar_prompt: \S/m)
  })
})
