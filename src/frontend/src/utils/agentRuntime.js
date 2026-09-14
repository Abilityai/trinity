/**
 * Which agents earn a runtime badge in a fleet list (#2358).
 *
 * The Dashboard List rendered a `RuntimeBadge` on every single name. On a
 * single-runtime fleet — the overwhelmingly common case, since `claude-code`
 * is the platform default — that is one identical pill per row: background
 * texture in the highest-value column, carrying no information because it
 * never varies. Moving it one line down would relocate the noise, not reduce
 * it, so the rule is to badge the EXCEPTIONS: a row wears a runtime badge only
 * when its runtime is NOT the platform default.
 *
 * Anchored to the platform default rather than to fleet majority on purpose. A
 * majority rule would silently flip badges on and off as agents are created
 * and deleted, so the same agent would be "an exception" on Tuesday and not on
 * Wednesday with nothing about it having changed.
 *
 * Pure and dependency-free so a spec can reach it: `vitest.config.js` pins
 * `environment: 'node'` with no mount harness, so a decidable rule written
 * inline in an SFC is a rule no test can see.
 *
 * The tile (`AgentTile.vue`) deliberately keeps its unconditional badge — it
 * is a card with room, not a dense list column.
 */

/** The platform-default agent runtime — `RuntimeBadge.vue`'s own prop default. */
export const DEFAULT_RUNTIME = 'claude-code'

/**
 * Every id that means "the default runtime".
 *
 * This mirrors `RuntimeBadge.vue`'s own `isClaude` predicate
 * (`!runtime || runtime === 'claude-code' || runtime === 'claude'`) and must
 * keep mirroring it: if this set were narrower, a `runtime: "claude"` row
 * would be treated as an exception and wear a Claude sunburst pill on a
 * homogeneous Claude fleet — exactly the noise the rule removes.
 */
export const DEFAULT_RUNTIME_IDS = Object.freeze(new Set([DEFAULT_RUNTIME, 'claude']))

/**
 * True when the agent runs the platform-default runtime.
 *
 * Anything unreadable reads as the default: an absent or blank `runtime` (an
 * older backend's payload), a non-string value, or a bare-slug/null agent. An
 * unreadable value is not evidence of an exception, and the failure direction
 * that matters here is "do not invent a badge".
 *
 * @param {{runtime?: string|null}|string|null|undefined} agent
 * @returns {boolean}
 */
export function isDefaultRuntime(agent) {
  if (!agent || typeof agent === 'string') return true
  const runtime = typeof agent.runtime === 'string' ? agent.runtime.trim() : ''
  if (!runtime) return true
  return DEFAULT_RUNTIME_IDS.has(runtime)
}

/**
 * Should this agent's row in a fleet LIST carry a runtime badge (#2358 AC #6)?
 * Only the exceptions do.
 *
 * @param {{runtime?: string|null}|string|null|undefined} agent
 * @returns {boolean}
 */
export function showsRuntimeBadgeInList(agent) {
  return !isDefaultRuntime(agent)
}
