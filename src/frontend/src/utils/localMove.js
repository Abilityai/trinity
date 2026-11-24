/**
 * The steps that bring in the agents a person already runs in Claude Code on
 * their own machine (trinity-enterprise#1353). The assistant asks for them
 * with a `local-move` card that carries nothing it wrote: every command and
 * the instance's address are written here, so a model can never put a command
 * of its own in front of the person.
 *
 * The person runs them locally: the analysis reads their agents and writes a
 * report they read first; the move shows a plan and waits for their yes, then
 * asks again before deploying here. Secrets stay on their machine: the move
 * leaves real `.env` files behind, and the assistant asks for each agent's
 * credentials in its masked field once the agents are here.
 *
 * `access` is what GET /api/settings/mcp-external-access answered: `true`
 * (on), `false` (off: an outside client is refused until an admin turns it on)
 * or `null` (could not be read: the step stays, worded as a check).
 */
export const MCP_KEYS_PATH = '/settings?tab=mcp-keys'

export const PLUGIN_COMMANDS = Object.freeze([
  '/plugin marketplace add abilityai/abilities',
  '/plugin install agent-dev@abilityai',
  '/plugin install create-agent@abilityai',
  '/plugin install trinity@abilityai',
])

export function localMoveSteps(address, access) {
  const steps = []
  if (access !== true) {
    steps.push({
      key: 'access',
      title: 'Let your Claude Code reach this instance',
      text: access === false
        ? 'Outside access is off on this instance, so your machine would be refused. An admin turns on External MCP access in Settings → MCP Keys.'
        : 'If connecting is refused with external_mcp_access_off, an admin turns on External MCP access in Settings → MCP Keys.',
      link: { label: 'Open MCP Keys', href: MCP_KEYS_PATH },
    })
  }
  steps.push(
    {
      key: 'plugins',
      title: 'In Claude Code on your machine, add the plugins',
      commands: PLUGIN_COMMANDS.slice(),
    },
    {
      key: 'connect',
      title: 'Connect to this instance',
      commands: ['/trinity:connect'],
      text: `When it asks for the address, give it ${address}. It emails a code to the address you sign in with here.`,
    },
    {
      key: 'analyse',
      title: 'Look at your agents',
      commands: ['/agent-dev:agent-fleet-analysis ~/path/to/your/agents'],
      text: 'It writes a report on your machine: which agents are worth moving and what each needs. Read it first; nothing moves yet.',
    },
    {
      key: 'move',
      title: 'Move the ones you choose',
      commands: ['/agent-dev:agent-fleet-migrate'],
      text: 'It shows a plan and waits for your yes, then asks again before anything is deployed here. Each agent goes to a GitHub repository of its own first: that needs the GitHub CLI signed in on your machine and a GitHub read token on this instance (Settings → MCP Keys → Personal GitHub Token).',
    },
  )
  return steps
}

export const LOCAL_MOVE_SECRETS =
  'Your .env files and keys stay on your machine. Once your agents are here, I ask for what each one needs in a masked field, never in this chat.'
