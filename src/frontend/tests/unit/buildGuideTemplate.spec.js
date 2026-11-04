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

  it('knows the seven levels by the page\'s own names, answers a reached level, and helps with the next', () => {
    const doc = read('CLAUDE.md')
    const section = doc.slice(doc.indexOf('## Levels'), doc.indexOf('\n## ', doc.indexOf('## Levels') + 1))
    expect(section.length).toBeGreaterThan(100)
    for (const level of LEVELS) expect(section).toContain(`${level.id}. **${level.name}**`)
    expect(section).toMatch(/A line that starts `Level reached:`/)
    expect(section).toMatch(/`Next:`/)
    expect(section).toMatch(/Level 3 is skipped/)
    expect(section).toMatch(/Show me `credentials`/)
    expect(section).toMatch(/the page checked/i)
    expect(section).toMatch(/Never say a level is reached/)
    expect(section).toMatch(/`Help me reach Level/)
    expect(section).toMatch(/\/build-agent/)
    expect(section).toMatch(/\/fleet/)
    expect(doc).toMatch(/\| `Level reached:` \.\.\. \| /)
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
    expect(skill).toContain('scripts/pack-agent.sh')
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

  it('builds a first agent one step at a time, its playbooks held back and added one by one', () => {
    const skill = read('.claude/skills/build-agent/SKILL.md')
    const section = skill.slice(skill.indexOf('## First agent: one step at a time'), skill.indexOf('\n## ', skill.indexOf('## First agent: one step at a time') + 1))
    expect(section.length).toBeGreaterThan(200)
    expect(section).toMatch(/fleet-state\.json/)
    expect(section).toMatch(/~\/builds\/<slug>\.held\//)
    expect(section).toMatch(/keeping the wizard's standard ones/)
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

  it('has the packing script, ready to run', () => {
    const script = join(ROOT, 'scripts/pack-agent.sh')
    expect(existsSync(script)).toBe(true)
    expect(statSync(script).mode & 0o111).not.toBe(0)
  })

  it('no longer mentions handing the chat to the Agent Builder anywhere', () => {
    for (const file of allFiles()) {
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

  it('helps with Levels 4 to 7 by walking them there first, a card as the one-click option', () => {
    const doc = read('CLAUDE.md')
    const section = doc.slice(doc.indexOf('## Levels'), doc.indexOf('\n## ', doc.indexOf('## Levels') + 1))
    expect(section).toMatch(/- Level 3:.*Show me `credentials`/)
    expect(section).toMatch(/- Level 4:.*Show me `people`.*Show me `permissions`/)
    expect(section).toMatch(/- Level 5:.*Show me `schedules`.*Autonomy/)
    expect(section).toMatch(/- Level 6:.*Show me `permissions`/)
    expect(section).toMatch(/- Level 7:.*Show me `permissions`/)
    expect(section).toMatch(/For Levels 4 to 7, offer the walk first/)
    expect(section).toMatch(/one click/)
  })

  it('never changes a setting with its own key, and sends people to /fleet for settings', () => {
    const guide = read('CLAUDE.md')
    expect(guide).toContain('Never change a setting with your own key')
    expect(guide).toMatch(/\| .*(setting|who can).* \| `\/fleet`/i)
  })
})
