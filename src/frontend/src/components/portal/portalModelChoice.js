// The Workspace composer's model choice (trinity-enterprise#403) — the rules.
//
// Every decision the composer makes about the model control lives here as a
// pure function, for the reason `portalVoiceMode.js` states and this repo keeps
// re-learning: vitest runs `environment: 'node'` with no component-mount
// harness, so a rule kept inside a `.vue` file is a rule no test can reach.
// The component is a dispatcher over these.
//
// There is deliberately NO storage access in this module. The choice is the
// user's SERVER record (`user_preferences` key `workspace_model`), read and
// written through `stores/userPreferences.js` — so there is no identity term to
// resolve here, and none of the `anon`-then-email race that a browser key on
// this surface would reintroduce (`composables/useColumnResize.js`, caught live).

/** The value of the control's "inherit" option. Blank, and the server
 *  normalises blank → inherit before it validates — so the option that means
 *  "let the agent decide" is not a magic token either side has to agree on. */
export const INHERIT_VALUE = ''

/**
 * Should the control render, and with what?
 *
 * `modelDefault` is the server's whole answer to "may this person choose here":
 * it is `null` for a portal-token client and for a non-Claude runtime, so a
 * missing field, an older backend, a partial payload or a failed roster read
 * all land on the same fail-closed result. `options` being empty is the same
 * answer from the other side — a control with nothing to choose is a dead one.
 */
export function modelControlState({
  isPlatform = false,
  modelDefault = null,
  options = [],
  roomBound = false,
} = {}) {
  const list = Array.isArray(options) ? options.filter((o) => o && o.id && o.tier) : []
  if (!isPlatform || !modelDefault?.model || !list.length) {
    return { render: false, enabled: false, options: [], reason: '' }
  }
  // ent#403 / ent#361: an `@mention` diverts the send into a ROOM, and the room
  // has its own composer with no model control. A select that stayed live while
  // the draft is room-bound would display a setting it is not honouring — the
  // dead-affordance failure this codebase names repeatedly. Disabled WITH the
  // reason, never silently dropped.
  if (roomBound) {
    return {
      render: true,
      enabled: false,
      options: list,
      reason: 'This message starts a group chat — it runs on each agent’s own model.',
    }
  }
  return { render: true, enabled: true, options: list, reason: '' }
}

/**
 * The stored choice, reconciled against what is actually on offer.
 *
 * The stored value is a raw model id and the catalog churns by design (three
 * entries already carry a "Legacy" relabel). Without this, a retired id is sent
 * on every turn, refused every time, forever, across reloads, with nothing on
 * screen pointing at the stored preference as the cause. Dropping it back to
 * inherit mirrors what the backend already does for a stale
 * `public_channel_model`.
 */
export function reconcileStored(stored, options = []) {
  const ids = new Set((Array.isArray(options) ? options : []).map((o) => o?.id))
  const value = typeof stored === 'string' ? stored.trim() : ''
  return value && ids.has(value) ? value : INHERIT_VALUE
}

/**
 * This user's choice for ONE agent, out of the whole stored record.
 *
 * The record is `{agentName: modelId}` — per (user, agent) by construction,
 * since the server keys the row by user. A malformed value reads as "no choice"
 * rather than throwing: the client owns the schema of its own preference value
 * and heals what it reads (`utils/gridLayout.js::normalizeLayout`'s rule).
 */
export function storedFor(record, agentName, options = []) {
  if (!record || typeof record !== 'object' || !agentName) return INHERIT_VALUE
  return reconcileStored(record[agentName], options)
}

/**
 * The record to write back after a choice. Returns a NEW object — never a
 * mutation of the store's own record — and DELETES the entry on inherit rather
 * than storing `''`, so "no choice" is one representation instead of two.
 */
export function withChoice(record, agentName, value) {
  const next = { ...(record && typeof record === 'object' ? record : {}) }
  if (!agentName) return next
  const model = typeof value === 'string' ? value.trim() : ''
  if (model) next[agentName] = model
  else delete next[agentName]
  return next
}

/** One option's visible text: the plain-language tier ALONE.
 *
 *  Never `label — tier`. A native `<select>` sizes to its widest option, and
 *  this control sits beside a `flex-1 min-w-0` textarea on the geometry #2259
 *  tuned — so the closed state has to be short on any viewport. The model name
 *  rides the `title` instead, where it costs no width. */
export function optionText(opt) {
  return (opt?.tier || '').trim() || (opt?.label || '').trim() || (opt?.id || '')
}

/** One option's hover text — the model's real name, for anyone who wants it. */
export function optionTitle(opt) {
  const label = (opt?.label || '').trim()
  const id = (opt?.id || '').trim()
  if (label && id && label !== id) return `${label} (${id})`
  return label || id
}

/**
 * The inherit option's text — it must plainly read as the AGENT's choice, not
 * as something the user picked. Naming the resolved model is what makes it a
 * fact rather than a shrug; an unrecognised id degrades to the id itself
 * (`platform_default_model` is not catalog-validated), never to a blank.
 */
export function defaultOptionText(modelDefault) {
  const label = (modelDefault?.label || modelDefault?.model || '').trim()
  return label ? `Agent’s default (${label})` : 'Agent’s default'
}

/**
 * Should a failed turn clear this agent's stored choice?
 *
 * Only for the ONE category the server raises when the chosen model is what
 * went wrong. Every other verdict — a usage limit, capacity, a timeout, a
 * cancel — has a true and specific cause that has nothing to do with the model,
 * and clearing on those would quietly discard a deliberate choice.
 */
export function shouldClearChoice(outcome) {
  return outcome?.category === 'invalid_model'
}
