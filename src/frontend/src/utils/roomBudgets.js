/**
 * Room budget default helpers (ent#387).
 *
 * The PUT is a partial update: an omitted field means "leave it alone". That
 * makes "no cost cap" impossible to express by omission — emptying the box would
 * silently keep the previous cap — so clearing is explicit, and the two cases are
 * separated here rather than inline in the panel, where they cannot be tested.
 */

export const ROOM_COST_KEY = 'room_default_max_cost_usd'

/**
 * Build the PUT body for a change from `state` (server truth) to `form` (edits).
 * Returns only what changed; an unchanged form yields `{ clear: [] }`.
 */
export function buildBudgetUpdate(form, state) {
  const body = { clear: [] }
  if (!state) return body

  if (Number(form.max_messages) !== Number(state.max_messages)) {
    body.max_messages = Number(form.max_messages)
  }
  if (Number(form.ttl_hours) !== Number(state.ttl_hours)) {
    body.ttl_hours = Number(form.ttl_hours)
  }

  const cost = String(form.max_cost_usd ?? '').trim()
  if (cost === '') {
    // Only ask to clear when there is something to clear — an empty box over an
    // already-uncapped default is not a change.
    if (state.max_cost_usd != null) body.clear.push(ROOM_COST_KEY)
  } else if (Number(cost) !== Number(state.max_cost_usd)) {
    body.max_cost_usd = Number(cost)
  }
  return body
}

/** Whether the form differs from server truth (drives the Save button). */
export function isBudgetDirty(form, state) {
  if (!state) return false
  const body = buildBudgetUpdate(form, state)
  return Object.keys(body).length > 1 || body.clear.length > 0
}
