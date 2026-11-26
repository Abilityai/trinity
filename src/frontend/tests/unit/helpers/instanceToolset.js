/**
 * trinity-enterprise#1390 — the confirm-card tools as a real instance serves
 * them: the input schemas of a live Trinity instance's MCP `tools/list`
 * (captured 2026-11-25), cut to each property's type and default and the
 * required list. Descriptions are left out; nothing else is changed.
 *
 * A card in a test is built from these and checked against them
 * (`argsFit`), so a card a test confirms is one the instance's own tool takes.
 */
export const INSTANCE_TOOLS = Object.freeze({
  chat_with_agent: {
    required: ['agent_name', 'message'],
    properties: {
      agent_name: { type: 'string' },
      allowed_tools: { type: 'array' },
      async: { type: 'boolean', default: false },
      chat_session_id: { type: 'string' },
      execution_id: { type: 'string' },
      inject_result: { type: 'boolean', default: false },
      message: { type: 'string' },
      model: { type: 'string' },
      parallel: { type: 'boolean', default: false },
      project_id: { type: 'string' },
      system_prompt: { type: 'string' },
      timeout_seconds: { type: 'number' },
    },
  },
  create_agent_schedule: {
    required: ['agent_name', 'name', 'cron_expression', 'message'],
    properties: {
      agent_name: { type: 'string' },
      allowed_tools: { type: 'array' },
      cron_expression: { type: 'string' },
      deliver_to_workspace_email: { type: 'string' },
      description: { type: 'string' },
      enabled: { type: 'boolean', default: true },
      max_retries: { type: 'integer', default: 1 },
      message: { type: 'string' },
      model: { type: 'string' },
      name: { type: 'string' },
      retry_delay_seconds: { type: 'integer', default: 60 },
      timeout_seconds: { type: 'integer' },
      timezone: { type: 'string', default: 'UTC' },
      validation_enabled: { type: 'boolean' },
      validation_prompt: { type: 'string' },
      validation_timeout_seconds: { type: 'integer' },
    },
  },
  toggle_agent_schedule: {
    required: ['agent_name', 'schedule_id', 'enabled'],
    properties: { agent_name: { type: 'string' }, enabled: { type: 'boolean' }, schedule_id: { type: 'string' } },
  },
  update_agent_schedule: {
    required: ['agent_name', 'schedule_id'],
    properties: {
      agent_name: { type: 'string' },
      allowed_tools: { type: 'array' },
      cron_expression: { type: 'string' },
      deliver_to_workspace_email: { type: ['string', 'null'] },
      description: { type: 'string' },
      enabled: { type: 'boolean' },
      max_retries: { type: 'integer' },
      message: { type: 'string' },
      model: { type: 'string' },
      name: { type: 'string' },
      retry_delay_seconds: { type: 'integer' },
      schedule_id: { type: 'string' },
      timeout_seconds: { type: 'integer' },
      timezone: { type: 'string' },
      validation_enabled: { type: 'boolean' },
      validation_prompt: { type: 'string' },
      validation_timeout_seconds: { type: 'integer' },
    },
  },
  delete_agent: { required: ['name'], properties: { name: { type: 'string' } } },
  start_agent: { required: ['name'], properties: { name: { type: 'string' } } },
})

const TYPE = { string: (v) => typeof v === 'string', boolean: (v) => typeof v === 'boolean', integer: Number.isInteger,
  number: (v) => typeof v === 'number', array: Array.isArray, null: (v) => v === null }

/** Whether these arguments are ones the instance's tool takes: every required one, no unknown one, each of its type. */
export function argsFit(tool, args) {
  const schema = INSTANCE_TOOLS[tool]
  if (!schema) return false
  if (!schema.required.every((k) => Object.hasOwn(args, k))) return false
  return Object.entries(args).every(([k, v]) => {
    const type = schema.properties[k]?.type
    return type !== undefined && [].concat(type).some((t) => TYPE[t](v))
  })
}
