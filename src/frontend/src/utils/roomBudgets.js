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

// ---------------------------------------------------------------------------
// The in-room notice (#2620)
// ---------------------------------------------------------------------------

/** Fraction of a budget at which a room starts saying something. */
export const BUDGET_WARN_AT = 0.8
/** Messages remaining at which the notice stops being ambient and blocks the eye. */
export const BUDGET_CRITICAL_REMAINING = 5

/**
 * What a room should tell the person in it about its budget.
 *
 * The old signal was the ratio alone — a small amber `59/60 messages` in the
 * header. That is the fact and not the CONSEQUENCE, and the consequence is the
 * part nobody can guess: reaching the cap closes the room **permanently**
 * (`close_room` is a one-way CAS; there is no reopen path), taking the thread
 * with it. Someone reading "59/60" has no way to know they are one message from
 * losing the conversation, which is exactly what happened to the operator whose
 * report opened #2620.
 *
 * So the notice names the remaining count, says what happens, and — only when
 * it is nearly out — says what to do about it. Returns null when there is
 * nothing worth saying, because a permanent gauge is how a warning gets
 * ignored.
 */
export function budgetNotice(room) {
  if (!room || room.status !== 'open') return null

  const used = Number(room.message_count) || 0
  const maxMsgs = Number(room.max_messages) || 0
  if (maxMsgs > 0) {
    const remaining = Math.max(0, maxMsgs - used)
    if (used / maxMsgs >= BUDGET_WARN_AT) {
      const critical = remaining <= BUDGET_CRITICAL_REMAINING
      return {
        kind: 'messages',
        level: critical ? 'critical' : 'warn',
        remaining,
        // Plural spelled out: this is the line that has to land.
        headline: remaining === 0
          ? 'This room has reached its message limit'
          : remaining === 1
            ? '1 message left in this room'
            : `${remaining} messages left in this room`,
        detail: critical
          ? 'At the limit the room closes for good — the transcript stays readable, but nobody can post again. Start a new chat to carry on.'
          : `It closes permanently at ${maxMsgs} messages. Each agent reply counts, so a question that wakes three agents spends four.`,
      }
    }
  }

  // Cost is the second budget and is usually unset; when an operator has set
  // one it can end the room first, so it gets the same treatment.
  const cost = Number(room.cost) || 0
  const maxCost = Number(room.max_cost_usd) || 0
  if (maxCost > 0 && cost / maxCost >= BUDGET_WARN_AT) {
    return {
      kind: 'cost',
      level: cost / maxCost >= 0.95 ? 'critical' : 'warn',
      remaining: Math.max(0, maxCost - cost),
      headline: `$${cost.toFixed(2)} of $${maxCost.toFixed(2)} spent in this room`,
      detail: 'At the limit the room closes for good. Start a new chat to carry on.',
    }
  }

  return null
}
