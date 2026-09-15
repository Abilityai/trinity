/**
 * Carrying a composer's attachments from a 1:1 into the room it escalates to
 * (#2794). Pure.
 *
 * The gap this closes: `PortalConversation` uploads each dropped file straight
 * into the CURRENT agent's inbox as it is attached, and the `escalate-to-room`
 * event carried only `{ agents, message }` — so a message that @mentioned a
 * second agent moved to a room and the file did not. The person had watched a
 * chip say the upload succeeded, so they believed both agents had it; only the
 * original one ever did, and the room showed no trace of a file at all.
 *
 * The rule the issue states, and the one everything here follows: *whatever a
 * user could do inside a room, escalating into one from a 1:1 must produce the
 * same result.* A room-native drop is one upload per participant
 * (`PortalRoom.vue`), so an escalation owes the participants that have not
 * already received the file exactly that — no more (the origin agent must not
 * be sent the same file twice) and no less.
 *
 * Every rule is a pure function because `vitest.config.js` pins
 * `environment: 'node'` with no component-mount harness: a decision made inside
 * an SFC is a decision no test can reach. `PortalConversation.vue`,
 * `Portal.vue` and `PortalRoom.vue` are dispatchers over this module.
 */

import { attachmentState } from '@/composables/usePortalFileDrop'

/**
 * Split a composer's entries into what can travel and what cannot.
 *
 * `carried` is the files that actually reached the origin agent AND still hold
 * a readable handle — both are required, and the second is not paranoia: an
 * entry restored across a failed escalation, or one built by an older code
 * path, has no `file`, and a plan built from it would fan out `undefined`.
 *
 * `dropped` is everything else, kept rather than discarded so the person can
 * be TOLD. Silence is the one outcome the AC forbids.
 *
 * Anything still uploading counts as dropped — callers are expected to await
 * `settled()` first, so an in-flight entry reaching here means the wait was
 * skipped, and reporting it is strictly better than assuming it landed.
 */
export function partitionAttachments(entries) {
  const carried = []
  const dropped = []
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (!entry || !entry.name) continue
    if (attachmentState(entry) === 'sent' && entry.file) carried.push(entry)
    else dropped.push(entry)
  }
  return { carried, dropped }
}

/**
 * Who still needs each carried file.
 *
 * `origin` already has it — that is what the 1:1 upload did — so it is excluded
 * by name rather than by position: the shell builds `agents` as
 * `[origin, ...mentioned]`, and a plan that trusted that order would re-send
 * the file to the origin agent the day the order changes. Duplicate mentions
 * collapse for the same reason a room's own fan-out iterates participants
 * rather than mentions: the cost is one upload per RECIPIENT.
 *
 * A file nobody new needs yields no entry at all, so the caller's loop is
 * empty rather than uploading to zero agents and reporting a success.
 *
 * @returns {Array<{entry: object, file: File, name: string, agents: string[]}>}
 */
export function fanOutPlan(carried, { origin, participants } = {}) {
  const skip = new Set([origin].filter(Boolean))
  const targets = []
  const seen = new Set()
  for (const name of Array.isArray(participants) ? participants : []) {
    if (!name || skip.has(name) || seen.has(name)) continue
    seen.add(name)
    targets.push(name)
  }
  if (!targets.length) return []
  return (Array.isArray(carried) ? carried : [])
    .filter((e) => e && e.file)
    .map((entry) => ({ entry, file: entry.file, name: entry.name, agents: targets.slice() }))
}

/** `a`, `a and b`, `a, b and c` — the recipient list, read aloud. */
export function nameList(names) {
  const rows = (Array.isArray(names) ? names : []).filter(Boolean)
  if (!rows.length) return ''
  if (rows.length === 1) return rows[0]
  return `${rows.slice(0, -1).join(', ')} and ${rows[rows.length - 1]}`
}

/**
 * What the room says about the files that came with the escalated message.
 *
 * Three facts, and the order is the reader's priority: what arrived and for
 * whom, what did not arrive, and what never left the 1:1 at all. `null` when
 * there is nothing to say — an escalation with no attachments must not grow a
 * line about attachments.
 *
 * A partial failure is named per FILE and per AGENT, because "some uploads
 * failed" tells the person nothing they can act on, and the action here is
 * concrete: drop that one file into the room again.
 *
 * @param {object[]} carried   entries that travelled
 * @param {object[]} dropped   entries that could not travel
 * @param {Array<{name: string, agents: string[]}>} failures  per-file misses
 * @param {string[]} recipients  the agents the fan-out targeted
 */
export function carriedNotice({ carried = [], dropped = [], failures = [], recipients = [] } = {}) {
  const parts = []
  const failedNames = new Set(failures.map((f) => f && f.name).filter(Boolean))
  const delivered = carried.filter((e) => e && !failedNames.has(e.name))

  if (delivered.length && recipients.length) {
    parts.push(`Sent with your message: ${nameList(delivered.map((e) => e.name))} `
      + `— also delivered to ${nameList(recipients)}.`)
  } else if (delivered.length) {
    parts.push(`Sent with your message: ${nameList(delivered.map((e) => e.name))}.`)
  }

  for (const f of failures) {
    if (!f || !f.name || !(f.agents || []).length) continue
    parts.push(`${f.name} didn't reach ${nameList(f.agents)} — attach it again here to retry.`)
  }

  if (dropped.length) {
    parts.push(`${nameList(dropped.map((e) => e.name))} `
      + `${dropped.length === 1 ? 'was' : 'were'} not carried over — `
      + `${dropped.length === 1 ? 'it' : 'they'} never finished uploading.`)
  }

  return parts.length ? parts.join(' ') : null
}

/**
 * Is this notice about a failure? Decides whether the room renders it as a
 * warning or as an ordinary delivery line — the same verdict-not-a-pair-of-
 * booleans shape `attachmentState` uses.
 */
export function noticeIsProblem({ dropped = [], failures = [] } = {}) {
  return Boolean(dropped.length || failures.some((f) => f && (f.agents || []).length))
}
