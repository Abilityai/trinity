/**
 * The four self-change permissions an admin grants an agent (trinity-enterprise#164),
 * as the agent's Settings shows them (trinity-enterprise#756).
 *
 * Pure: the copy, the order and the permission-request match live here so the
 * panel holds no rule of its own and a node spec can pin each one.
 */

/** Display order, and the plain-language line under each toggle. */
export const SELF_CHANGE_CAPABILITIES = [
  {
    id: 'skills.manage',
    label: 'Change skills',
    can: 'Add, remove and update skills on agents, its own included.',
    without: "Without it, every skill change it attempts is refused, its own included.",
  },
  {
    id: 'schedules.manage',
    label: 'Change other agents’ schedules',
    can: "Create, change, pause or delete another agent's schedules and their webhooks.",
    without: 'Without it, it can still manage its own schedules, but never another agent’s.',
  },
  {
    id: 'instructions.manage',
    label: 'Change instructions',
    can: "Write CLAUDE.md, AGENTS.md and .claude/ files (not skills), and reset an agent to main.",
    without: 'Without it, those writes are refused, its own instructions included.',
  },
  {
    id: 'agents.manage',
    label: 'Manage other agents',
    can: "Create, deploy and delete agents, and change their model, resources, timeout and guardrails — only agents its owner owns.",
    without: 'Without it, it can still spawn short-lived helpers. It can never lift its own read-only mode or guardrails.',
  },
]

/**
 * The open asks from this agent that request one of the four permissions.
 *
 * The 403 an agent gets tells it to raise an ask whose title INCLUDES the
 * permission id (`schedules.manage`), because the ask surface has no field for
 * "this is a permission request". So the match is that id in the title,
 * case-insensitive, and nothing looser — a title that merely mentions
 * "schedules" is not a request for the grant.
 *
 * @param {Array<{id: string, title?: string, status?: string}>} items
 * @returns {Array<{id: string, title: string, capability: string, created_at?: string}>}
 */
export function permissionRequests (items) {
  const out = []
  for (const item of items || []) {
    if (item?.status && item.status !== 'pending') continue
    const title = String(item?.title || '')
    const lower = title.toLowerCase()
    const cap = SELF_CHANGE_CAPABILITIES.find((c) => lower.includes(c.id))
    if (cap) out.push({ id: item.id, title, capability: cap.id, created_at: item.created_at })
  }
  return out
}
