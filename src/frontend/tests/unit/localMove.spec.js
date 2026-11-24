/**
 * The steps that bring in agents run on the person's own machine
 * (trinity-enterprise#1353): written by the page with this instance's address,
 * a card the model cannot put words into, and an opening that does not ask
 * again what was already answered.
 */
import { describe, expect, it } from 'vitest'
import { MCP_KEYS_PATH, PLUGIN_COMMANDS, localMoveSteps } from '@/utils/localMove'
import { acceptCards, cardLabel, cardNote } from '@/utils/assistantCards'
import { OPENING_GROUP, PATH_HERE, PATH_LOCAL, PATH_SCRATCH, openingGroup } from '@/utils/firstSession'

const ADDRESS = 'https://acme.trinity.example'
const keys = (steps) => steps.map((s) => s.key)

describe('localMoveSteps', () => {
  it('fills in the address the person gives /trinity:connect', () => {
    const connect = localMoveSteps(ADDRESS, true).find((s) => s.key === 'connect')
    expect(connect.commands).toEqual(['/trinity:connect'])
    expect(connect.text).toContain(ADDRESS)
  })

  it('runs plugins, connect, analysis, move, in that order', () => {
    expect(keys(localMoveSteps(ADDRESS, true))).toEqual(['plugins', 'connect', 'analyse', 'move'])
    expect(localMoveSteps(ADDRESS, true)[0].commands).toEqual(PLUGIN_COMMANDS)
    expect(PLUGIN_COMMANDS).toContain('/plugin install agent-dev@abilityai')
    expect(PLUGIN_COMMANDS).toContain('/plugin install create-agent@abilityai')   // the move composes its skills
  })

  it('puts outside access first while it is off, or could not be read', () => {
    for (const access of [false, null]) {
      const steps = localMoveSteps(ADDRESS, access)
      expect(steps[0].key).toBe('access')
      expect(steps[0].link.href).toBe(MCP_KEYS_PATH)
    }
    expect(localMoveSteps(ADDRESS, null)[0].text).toContain('external_mcp_access_off')
  })

  it('says the report comes first and nothing moves without a yes', () => {
    const steps = localMoveSteps(ADDRESS, true)
    expect(steps.find((s) => s.key === 'analyse').text).toContain('nothing moves yet')
    expect(steps.find((s) => s.key === 'move').text).toContain('waits for your yes')
  })
})

describe('the local-move card', () => {
  it('is accepted bare, and refused with anything the model added', () => {
    expect(acceptCards([{ action: 'local-move' }], [])).toEqual([{ action: 'local-move' }])
    expect(acceptCards([{ action: 'local-move', commands: ['run-this-instead.sh'] }], [])).toEqual([])
    expect(acceptCards([{ action: 'local-move', url: 'https://elsewhere.example' }], [])).toEqual([])
  })

  it('has its own words', () => {
    expect(cardLabel({ action: 'local-move' }, [])).toContain('Claude Code')
    expect(cardNote({ action: 'local-move' })).toContain('own machine')
  })
})

describe('openingGroup', () => {
  const labels = (g) => g.items.map((i) => i.label)
  it('asks both ways until something was answered', () => {
    expect(labels(openingGroup({}))).toEqual([PATH_LOCAL, PATH_SCRATCH])
    expect(labels(openingGroup({ local_agents: null }))).toEqual([PATH_LOCAL, PATH_SCRATCH])
    expect(openingGroup({}).heading).toBe(OPENING_GROUP.heading)
  })
  it('a no leaves out the local path; steps already given ask whether the agents are here', () => {
    expect(labels(openingGroup({ local_agents: 'no' }))).toEqual([PATH_SCRATCH])
    expect(labels(openingGroup({ local_agents: 'offered' }))).toEqual([PATH_HERE, PATH_SCRATCH])
  })
})
