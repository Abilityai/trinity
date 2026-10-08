/**
 * The agent Skills tab's rules (trinity-enterprise#754), as a pure module.
 *
 * One tab replaced two (Playbooks + the library-only Skills tab): **Own
 * skills**, from the agent's `.claude/skills/` (live, or its last-known list
 * when stopped), and **Shared skills**, the platform's library assignments.
 * Every decision lives here so it is executed by tests without a mount
 * (`skillCards.spec.js`); `components/skills/SkillsTab.vue` only renders it.
 *
 * - Every skill renders in exactly ONE section. A #2914 name conflict renders
 *   in both, as it always has: the agent's own copy is what runs, and the
 *   library assignment is still there to unassign.
 * - Run needs the agent running, the skill present in the LIVE list (a
 *   last-known list never drives Run) and `user_invocable`.
 * - The gate line is shown to everyone and names an approver KIND, never a
 *   person — the gate map is readable by every viewer and by agent keys.
 * - Approval controls go to the owner or an admin, never on an ephemeral or
 *   the system agent (ent#753 refuses gates on ghosts; the system agent keeps
 *   today's no-management rule).
 */
import { DEPRECATED_TITLE } from '../components/skills/contract'
import { isDeprecationCode } from './skillDelivery'

// The backend's `skill_packaging.SKILL_NAME_RE`: what a gate can be keyed on.
export const SKILL_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

/**
 * The author's `automation:` frontmatter, relabelled by meaning (Q4): it is a
 * declaration of how the skill behaves INSIDE its own run, which nothing
 * enforces — "approval" and "gated" belong to the enforced gate only.
 */
export const MODE_CHIPS = Object.freeze({
  autonomous: { label: 'runs unattended', variant: 'autonomous', icon: 'loop' },
  gated: { label: 'asks mid-run', variant: 'info', icon: 'pause' },
  manual: { label: 'start by hand', variant: 'neutral', icon: 'person' },
})

export function modeChip(automation) {
  if (typeof automation !== 'string' || !automation.trim()) return null
  const value = automation.trim()
  const known = MODE_CHIPS[value.toLowerCase()]
  return { ...(known || { label: value, variant: 'neutral', icon: null }), title: `automation: ${value}` }
}

const KIND_LABELS = Object.freeze({ primary: 'the primary contact', approver: 'an approver' })
const KIND_OPTION_LABELS = Object.freeze({ primary: 'Primary contact', approver: 'Approver' })

/** An approver kind as a sentence reads it. Never a person. */
export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind
}

/** An approver kind as the picker lists it. */
export function kindOptionLabel(kind) {
  return KIND_OPTION_LABELS[kind] || kind
}

const low = (s) => (typeof s === 'string' ? s.trim().toLowerCase() : '')

/** The directory of a listed skill: the agent server's `dir`, or (an image
 *  from before ent#754) the segment of `path` that holds SKILL.md. */
export function skillDir(skill) {
  if (skill && typeof skill.dir === 'string' && skill.dir) return skill.dir
  const parts = String(skill?.path || '').split('/')
  const at = parts.lastIndexOf('SKILL.md')
  return at > 0 ? parts[at - 1] : ''
}

/** The key a gate on this skill is stored under: the name when it can carry
 *  one, else the directory, lower-cased (the backend lower-cases too). */
export function gateKeyFor({ name, dir }) {
  if (typeof name === 'string' && SKILL_NAME_RE.test(name)) return name.toLowerCase()
  if (typeof dir === 'string' && SKILL_NAME_RE.test(dir)) return dir.toLowerCase()
  return null
}

/** #183 delivery statuses, as the shared card's badge says them. A partial
 *  delivery is never a green tick. */
const INJECTION_BADGES = Object.freeze({
  injected: { label: 'synced', variant: 'success' },
  unchanged: { label: 'up to date', variant: 'neutral' },
  fallback: { label: 'partial', variant: 'warning' },
  failed: { label: 'failed', variant: 'danger' },
})

/** A named delivery warning, said for this agent. */
export function warningText(w) {
  const [kind, detail] = String(w).split(':')
  if (kind === 'missing_binary') return `${detail} is not installed in this agent — the skill may not run`
  if (kind === 'missing_env') return `${detail} is not set in this agent's environment`
  if (kind === 'multi_file_dropped_old_image') {
    return 'Only SKILL.md was copied — the agent image predates multi-file skills. Rebuild the base image for the full package.'
  }
  return String(w)
}

/** The last sync's warnings minus the deprecation code (ent#672): the badge
 *  already says a skill is retired; this list is for what went wrong. */
export function runWarnings(result) {
  return (result?.warnings || []).filter((w) => !isDeprecationCode(w))
}

function runVerdict({ running, inLive, liveState, userInvocable, ownCopyRuns, gated }) {
  if (!running) return { enabled: false, title: 'Start the agent to run' }
  if (liveState !== 'live') return { enabled: false, title: "The agent isn't answering right now" }
  if (ownCopyRuns) {
    return { enabled: false, title: "The agent's own skill of this name runs: use it from Own skills" }
  }
  if (!inLive) return { enabled: false, title: 'Not in the agent yet: sync now, or start it again' }
  if (!userInvocable) return { enabled: false, title: 'This skill is not user-invocable' }
  if (gated) return { enabled: true, title: 'Asks the approver first: nothing runs until they say yes' }
  return { enabled: true, title: 'Run now' }
}

function gateLineFor({ gate, kinds, recommended, showApproval }) {
  if (gate) {
    const kind = kinds.get(gate.approver)
    if (kind?.viewer_fills) return { tone: 'locked', text: 'Needs approval: you approve this' }
    // No approvers row for the kind (an older backend, or a kind this install
    // no longer offers): the gate entry's own `approver_reachable` decides.
    const reachable = kind ? kind.reachable : gate.approver_reachable !== false
    if (!reachable) {
      return { tone: 'warn', text: `Needs approval from ${kindLabel(gate.approver)}: nobody fills it yet` }
    }
    return { tone: 'locked', text: `Needs approval from ${kindLabel(gate.approver)}` }
  }
  if (recommended && showApproval) return { tone: 'warn', text: 'Author recommends approval: not gated' }
  return null
}

/**
 * Build both sections' cards and the gates that match no card.
 *
 * @returns {{own: object[], shared: object[], unmatchedGates: object[]}}
 */
export function buildSkillCards({
  agentList = [], agentListState = 'idle', running = false,
  assigned = [], library = [], conflictNames = new Set(), injectionResults = {},
  gates = [], approvers = [],
  canManage = false, isSystem = false, isEphemeral = false,
} = {}) {
  const showApproval = Boolean(canManage) && !isSystem && !isEphemeral
  const showManage = Boolean(canManage) && !isSystem
  const kinds = new Map(approvers.map((a) => [a.kind, a]))
  const gateByKey = new Map(gates.map((g) => [low(g.skill_name), g]))
  const libByName = new Map(library.map((s) => [low(s.name), s]))
  const conflicts = new Set([...conflictNames].map(low))
  const assignedByName = new Map(assigned.map((r) => [low(r.skill_name), r]))
  const liveState = agentListState
  const listKnown = liveState === 'live' || liveState === 'last_known'

  const liveByKey = new Map()
  for (const s of agentList) {
    for (const k of [low(s.name), low(skillDir(s))]) if (k && !liveByKey.has(k)) liveByKey.set(k, s)
  }
  const matched = new Set()
  const findGate = (...keys) => {
    for (const k of keys.map(low)) {
      if (k && gateByKey.has(k)) { matched.add(k); return gateByKey.get(k) }
    }
    return null
  }

  // ---- Shared: one card per platform assignment --------------------------
  const shared = assigned.map((r) => {
    const name = r.skill_name
    const key = low(name)
    const entry = libByName.get(key) || null
    const liveEntry = liveByKey.get(key) || null
    const conflict = conflicts.has(key) || r.delivery_status === 'conflict'
    const ownCopyRuns = conflict || liveEntry?.source === 'agent'
    const result = injectionResults?.[name] || null
    const warnings = runWarnings(result)
    const viaSets = r.via_sets || []
    const setOnly = r.individual === false
    const gate = findGate(name, liveEntry ? skillDir(liveEntry) : '')
    const userInvocable = (liveEntry?.user_invocable ?? entry?.user_invocable) !== false
    const recommended = entry?.approval === 'recommended' || liveEntry?.approval === 'recommended'

    const badges = []
    if (viaSets.length) badges.push({ label: `via ${viaSets.join(', ')}`, variant: 'purple', testid: `skill-via-${name}` })
    if (entry?.deprecated) {
      badges.push({ label: 'deprecated', variant: 'warning', title: DEPRECATED_TITLE, testid: `skill-deprecated-assigned-${name}` })
    }
    if (conflict) badges.push({ label: 'name conflict', variant: 'warning', dot: true, testid: `skill-conflict-${name}` })
    else if (result && INJECTION_BADGES[result.status]) badges.push({ ...INJECTION_BADGES[result.status] })
    if (!userInvocable) badges.push({ label: 'not user-invocable', variant: 'neutral' })

    let note = null
    if (conflict) {
      note = { text: "The agent's own skill runs; this library copy was not installed", tone: 'warning',
        detail: true, testid: 'skill-conflict-note' }
    } else if (result?.error) {
      note = { text: result.error, tone: 'danger', detail: true }
    } else if (warnings.length === 1) {
      note = { text: warningText(warnings[0]), tone: 'warning', detail: true }
    } else if (warnings.length > 1) {
      note = { text: `${warnings.length} delivery warnings`, tone: 'warning', detail: true }
    } else if (entry?.deprecated && entry?.superseded_by) {
      note = { text: `Superseded by ${entry.superseded_by}`, tone: 'warning', detail: true,
        testid: `skill-superseded-assigned-${name}` }
    } else if (!entry) {
      note = { text: 'No longer in the library', tone: 'muted' }
    } else if (running && liveState === 'live' && !liveEntry) {
      note = { text: 'Not in the agent yet: sync now, or start it again', tone: 'muted' }
    }

    return {
      id: `shared:${name}`, section: 'shared', name, dir: liveEntry ? skillDir(liveEntry) : name,
      description: entry?.description ?? liveEntry?.description ?? null,
      argumentHint: liveEntry?.argument_hint ?? null,
      version: entry?.version || null,
      mode: modeChip(entry?.automation ?? liveEntry?.automation),
      badges, note, conflict, setOnly, viaSets, warnings, injection: result,
      deprecated: Boolean(entry?.deprecated), supersededBy: entry?.superseded_by || null,
      orphan: !entry,
      gateKey: gateKeyFor({ name, dir: liveEntry ? skillDir(liveEntry) : name }),
      gate, gateLine: gateLineFor({ gate, kinds, recommended, showApproval }),
      run: runVerdict({ running, inLive: Boolean(liveEntry), liveState, userInvocable, ownCopyRuns, gated: Boolean(gate) }),
      canEditRun: userInvocable,
      controls: {
        approval: showApproval,
        toggleDisabled: null,
        unassign: {
          show: showManage,
          disabled: setOnly,
          title: setOnly ? `Assigned via ${viaSets.join(', ')}: unassign the set to remove it` : 'Unassign from this agent',
        },
        clear: false,
      },
    }
  })
  for (const c of shared) {
    if (!c.gateKey && c.controls.approval) c.controls.toggleDisabled = cantCarry
  }

  // ---- Own: everything the agent has that Shared does not render ---------
  const own = []
  for (const s of agentList) {
    const k = low(s.name)
    const d = low(skillDir(s))
    const isAssigned = assignedByName.has(k) || (d && assignedByName.has(d))
    const ownCopy = conflicts.has(k) || (d && conflicts.has(d)) || s.source === 'agent'
    if (isAssigned && !ownCopy) continue
    const leftover = s.source === 'platform' && !isAssigned
    const gate = findGate(s.name, skillDir(s))
    const userInvocable = s.user_invocable !== false
    const badges = []
    if (leftover) {
      badges.push({ label: 'from library', variant: 'neutral',
        title: 'Delivered by the library but no longer assigned; the next sync removes it' })
    }
    if (!userInvocable) badges.push({ label: 'not user-invocable', variant: 'neutral' })
    const gateKey = gateKeyFor({ name: s.name, dir: skillDir(s) })
    own.push({
      id: `own:${s.name}`, section: 'own', name: s.name, dir: skillDir(s),
      description: s.description ?? null, argumentHint: s.argument_hint ?? null,
      mode: modeChip(s.automation), badges, note: null, leftover,
      gateKey, gate, gateLine: gateLineFor({ gate, kinds, recommended: s.approval === 'recommended', showApproval }),
      run: runVerdict({ running, inLive: true, liveState, userInvocable, ownCopyRuns: false, gated: Boolean(gate) }),
      canEditRun: userInvocable,
      controls: {
        approval: showApproval,
        toggleDisabled: showApproval && !gateKey ? cantCarry : null,
        unassign: { show: false, disabled: true, title: '' },
        clear: false,
      },
    })
  }

  // ---- Gates that match no card (ent#753: sticky until cleared) ----------
  const unmatchedGates = !listKnown ? [] : gates
    .filter((g) => !matched.has(low(g.skill_name)))
    .map((g) => ({
      id: `gate:${g.skill_name}`, section: 'own', name: g.skill_name, missing: true, gate: g,
      gateLine: { tone: 'warn', text: "Not in this agent's skills list: gate kept" },
      controls: { approval: false, toggleDisabled: null, unassign: { show: false, disabled: true, title: '' },
        clear: showManage },
    }))

  return { own, shared, unmatchedGates }
}

const cantCarry = "This skill's name can't carry a gate: use letters, digits, dots, dashes or underscores (up to 64)"
