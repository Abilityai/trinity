import { isMacLike, nextActiveIndex } from './portalUtils'

/**
 * The Workspace key map (ent#621) — one declaration, one resolver.
 *
 * Every key the Workspace answers is declared HERE, including the ones this
 * module never dispatches: the reserved `⌘K` (ent#577 Spotlight) and the
 * component-owned protocol keys (Esc, the call's `M`, the preview's arrows,
 * tab roving, a dialog's Tab cycle, the typeahead's bare arrows). Declaring
 * them is what lets `keymapCollisions` see the WHOLE surface — a shell chord
 * that shadows a protocol key is the 2026-09-07 class, and a map that lists
 * only its own keys cannot tell you about it.
 *
 * Matching is deliberately platform-free: `primary` is meta XOR ctrl on every
 * platform (the shipped ⌘J semantics, ent#451), and a chord matches by `key`
 * OR by physical `code`. The `code` arm is the non-US fallback — on a layout
 * whose `key` is non-Latin (Cyrillic, Greek, Hebrew, Arabic) the US position
 * still works — and on a US Mac it is the ONLY arm for `⌥.`, which types `≥`.
 * `platform` is therefore an argument of the LABELS (`chordLabel`,
 * `ariaKeyshortcuts`), never of the matcher.
 *
 * Shift is accepted on a `key` match only (`shiftOnKey`, Decision 29): DE's
 * `Shift+7` → `/` and FR's shifted `.` must work, while macOS `⌘?` (Help
 * search, `key: '?'`) must stay the browser's.
 */

/** Dispatched by the shell. */
export const OWNER_SHELL = 'shell'
/** Owned by a component, declared here so collisions are visible. */
export const OWNER_PROTOCOL = 'overlay-protocol'

/** Keys that CHANGE what is on stage — suppressed during a voice call. */
export const SCOPE_MOVING = 'moving'
/** Keys that DO something where you are. */
export const SCOPE_DOING = 'doing'
/** Component-owned keys (Esc and friends). Never dispatched here. */
export const SCOPE_PROTOCOL = 'protocol'

const chord = (c) => Object.freeze({
  key: null, code: null, primary: false, alt: false, shift: false, shiftOnKey: false, ...c,
})

export const WORKSPACE_KEYMAP = Object.freeze([
  Object.freeze({
    id: 'find-anything',
    action: 'find-anything',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_DOING,
    // ent#577 owns the surface. Reserved means exactly this: nothing binds the
    // chord, so the browser's own Ctrl+K keeps working until Spotlight ships.
    // It is hidden from the key list rather than listed as "soon" — a dead row
    // in a help dialog is worse than an undocumented key.
    reserved: true,
    label: 'Find anything',
    note: 'Reserved for search (not yet available).',
    chord: chord({ key: 'k', code: 'KeyK', primary: true }),
  }),
  Object.freeze({
    id: 'new-chat',
    action: 'new-chat',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_DOING,
    label: 'New chat with this agent',
    chord: chord({ key: 'j', code: 'KeyJ', primary: true }),
  }),
  Object.freeze({
    id: 'rail-toggle',
    action: 'rail-toggle',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_DOING,
    // A call takes the rail's column for the voice canvas, so the key has
    // nothing to show: silent no-op, like every moving key (T8).
    callSuppressed: true,
    needsRail: true,
    label: 'Show or hide the rail',
    chord: chord({ key: '.', code: 'Period', primary: true }),
  }),
  Object.freeze({
    id: 'key-list',
    action: 'key-list',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_DOING,
    label: 'Keyboard shortcuts',
    chord: chord({ key: '/', code: 'Slash', primary: true, shiftOnKey: true }),
  }),
  Object.freeze({
    id: 'agent-next',
    action: 'agent-next',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_MOVING,
    callSuppressed: true,
    label: 'Next agent',
    note: 'One agent per press — a held key does not walk the list.',
    chord: chord({ key: 'ArrowDown', alt: true }),
  }),
  Object.freeze({
    id: 'agent-prev',
    action: 'agent-prev',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_MOVING,
    callSuppressed: true,
    label: 'Previous agent',
    chord: chord({ key: 'ArrowUp', alt: true }),
  }),
  Object.freeze({
    id: 'chat-next',
    action: 'chat-next',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_MOVING,
    callSuppressed: true,
    label: "Next chat with this agent",
    chord: chord({ key: 'ArrowDown', alt: true, shift: true }),
  }),
  Object.freeze({
    id: 'chat-prev',
    action: 'chat-prev',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_MOVING,
    callSuppressed: true,
    label: 'Previous chat with this agent',
    chord: chord({ key: 'ArrowUp', alt: true, shift: true }),
  }),
  Object.freeze({
    id: 'rail-tab-next',
    action: 'rail-tab-next',
    surface: 'workspace',
    owner: OWNER_SHELL,
    scope: SCOPE_MOVING,
    callSuppressed: true,
    needsRail: true,
    label: 'Next rail tab',
    chord: chord({ key: '.', code: 'Period', alt: true }),
  }),
  // --- protocol: owned where the learnings put them, declared here ----------
  Object.freeze({
    id: 'close-top',
    action: 'close-top',
    surface: 'workspace',
    owner: OWNER_PROTOCOL,
    scope: SCOPE_PROTOCOL,
    context: 'the innermost thing first — popup, reply chip, dialog, sheet, then a running turn',
    label: 'Close or cancel',
    chords: [chord({ key: 'Escape' })],
  }),
  Object.freeze({
    id: 'call-mute',
    action: 'call-mute',
    surface: 'workspace',
    owner: OWNER_PROTOCOL,
    scope: SCOPE_PROTOCOL,
    context: 'during a voice call',
    label: 'Mute or unmute',
    chords: [chord({ key: 'm', code: 'KeyM' })],
  }),
  Object.freeze({
    id: 'preview-step',
    action: 'preview-step',
    surface: 'workspace',
    owner: OWNER_PROTOCOL,
    scope: SCOPE_PROTOCOL,
    context: 'in the file preview',
    label: 'Previous or next file',
    chords: [chord({ key: 'ArrowLeft' }), chord({ key: 'ArrowRight' })],
  }),
  Object.freeze({
    id: 'tab-roving',
    action: 'tab-roving',
    surface: 'workspace',
    owner: OWNER_PROTOCOL,
    scope: SCOPE_PROTOCOL,
    context: 'once a tab strip has focus',
    label: 'Move between tabs',
    chords: [
      chord({ key: 'ArrowLeft' }), chord({ key: 'ArrowRight' }),
      chord({ key: 'Home' }), chord({ key: 'End' }),
    ],
  }),
  Object.freeze({
    id: 'dialog-focus-cycle',
    action: 'dialog-focus-cycle',
    surface: 'workspace',
    owner: OWNER_PROTOCOL,
    scope: SCOPE_PROTOCOL,
    context: 'inside an open dialog (the focus trap)',
    label: 'Move focus',
    chords: [chord({ key: 'Tab' }), chord({ key: 'Tab', shift: true })],
  }),
  Object.freeze({
    id: 'typeahead-step',
    action: 'typeahead-step',
    surface: 'workspace',
    owner: OWNER_PROTOCOL,
    scope: SCOPE_PROTOCOL,
    // Decision 36: BARE arrows only. The popup claiming `⌥↓` was a handler
    // claiming a chord it never declared.
    context: 'while the @ or / popup is open',
    label: 'Move through the suggestions',
    chords: [chord({ key: 'ArrowUp' }), chord({ key: 'ArrowDown' })],
  }),
])

/** Every chord an entry answers, single- or multi-chord. */
export function entryChords(entry) {
  if (!entry) return []
  if (Array.isArray(entry.chords)) return entry.chords.filter(Boolean)
  return entry.chord ? [entry.chord] : []
}

/** The entry for an action id, or null. */
export function findBinding(action, map = WORKSPACE_KEYMAP) {
  const list = Array.isArray(map) ? map : []
  return list.find((e) => e && e.action === action) || null
}

/**
 * Does this keyboard event answer this chord?
 *
 * Tolerant of a plain object (the existing ⌘J truth table passes one), so a
 * missing `code` simply disables the physical arm rather than throwing.
 */
export function matchesChord(e, c) {
  if (!e || !c) return false
  const meta = !!e.metaKey
  const ctrl = !!e.ctrlKey
  const primaryOk = c.primary ? (meta !== ctrl) : (!meta && !ctrl)
  if (!primaryOk) return false
  if (!!e.altKey !== !!c.alt) return false
  const shiftOk = !!e.shiftKey === !!c.shift
  const byKey = !!c.key && typeof e.key === 'string'
    && e.key.toLowerCase() === String(c.key).toLowerCase()
    && (c.shiftOnKey || shiftOk)
  // Decision 29: shift is never accepted on the physical arm — macOS `⌘?`
  // (Help search) sits on `Shift`+`Slash` and must stay the browser's.
  //
  // (merge-train 2026-10-06) The physical arm answers only when the printed
  // key CANNOT name the chord itself: a non-Latin layout (`о` on `KeyJ`), or a
  // key the modifier re-maps (`⌥.` types `≥` on a US Mac). A printable ASCII
  // `key` names itself, and on a Latin non-QWERTY layout the position and the
  // character disagree on purpose — Dvorak's ⌘V arrives as `key: 'v'` on
  // `Period`, ⌘Z as `key: 'z'` on `Slash` — so letting the position out-vote
  // the character made the rail toggle eat paste and the key list eat undo.
  const byCode = !!c.code && typeof e.code === 'string' && e.code === c.code && shiftOk
    && !printsAscii(e.key)
  return byKey || byCode
}

// One printable ASCII character (space through tilde): a `key` that names
// itself, on any Latin layout.
const printsAscii = (key) => typeof key === 'string' && key.length === 1
  && key.charCodeAt(0) >= 0x20 && key.charCodeAt(0) <= 0x7e

/**
 * The action this event asks for, or null.
 *
 * Reserved and protocol entries resolve to NULL on purpose: the shell must not
 * `preventDefault` a key it does not handle (⌘K stays the browser's until
 * ent#577), and Esc is a capture-phase protocol the shell never dispatches.
 */
export function resolveWorkspaceKey(e, map = WORKSPACE_KEYMAP) {
  if (!e) return null
  // A held key must not remount the conversation N times a second.
  if (e.repeat) return null
  return workspaceChord(e, map)
}

/**
 * The action whose CHORD this event is — a press or its auto-repeat alike.
 *
 * `resolveWorkspaceKey` answers "should something happen", and for a repeat
 * the answer is no: one press is one action. But a repeat of a chord the shell
 * claimed is still not the browser's, and the dispatcher cannot say so with a
 * null: left alone, a held ⌥. types `≥` into the message field on a Mac and a
 * held Ctrl+J opens the browser's Downloads. This is the half of the question
 * the repeat can still answer — WHOSE chord it is — and the dispatcher pairs it
 * with the press it claimed.
 *
 * Reserved and protocol entries are null here for the same reason they are
 * null there, and an IME composition is never a chord.
 */
export function workspaceChord(e, map = WORKSPACE_KEYMAP) {
  if (!e) return null
  if (e.isComposing || e.keyCode === 229) return null
  const list = Array.isArray(map) ? map : []
  for (const entry of list) {
    if (!entry || entry.reserved || entry.owner !== OWNER_SHELL) continue
    if (entryChords(entry).some((c) => matchesChord(e, c))) return entry.action
  }
  return null
}

const chordSignatures = (c) => {
  const mods = `|p:${!!c.primary}|a:${!!c.alt}`
  const out = []
  if (c.key) {
    const k = `k:${String(c.key).toLowerCase()}${mods}`
    if (c.shiftOnKey) out.push(`${k}|s:true`, `${k}|s:false`)
    else out.push(`${k}|s:${!!c.shift}`)
  }
  if (c.code) out.push(`c:${c.code}${mods}|s:${!!c.shift}`)
  return out
}

/**
 * Pairs of entries that answer the same chord.
 *
 * Two protocol entries MAY share one (the preview's arrows and a tab strip's
 * roving are the same keys in different places, each owned by the component
 * that has focus) — what must never happen is a shell key shadowing another
 * shell key, or shadowing a protocol key the learnings already assigned.
 */
export function keymapCollisions(map = WORKSPACE_KEYMAP) {
  const list = (Array.isArray(map) ? map : []).filter(Boolean)
  const found = []
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      const a = list[i]
      const b = list[j]
      if (a.owner !== OWNER_SHELL && b.owner !== OWNER_SHELL) continue
      const sigs = new Set(entryChords(a).flatMap(chordSignatures))
      const clash = entryChords(b).flatMap(chordSignatures).some((s) => sigs.has(s))
      if (clash) found.push([a.id, b.id])
    }
  }
  return found
}

/**
 * Should the shell stay out of the way?
 *
 * Anything modal — a dialog, the file preview, the agent picker, the mobile
 * drawer — suppresses EVERY shell key, ⌘J included (today it remounts the
 * conversation under an open file preview). A voice call suppresses the keys
 * that would change what is on stage, plus the rail toggle; ⌘J keeps its own
 * "leave the call?" ask (ent#534/551) and ⌘/ is allowed, because opening a
 * dialog leaves nothing.
 *
 * Per-action exemptions are the CALLER's: it passes `hasModalOpen`'s `ignore`
 * so rail keys do not see the rail's own sheet and ⌘/ can close its own list,
 * `railAvailable` for the two entries marked `needsRail`, and `resumed` for the
 * pass that re-runs a parked ⌘J after the leave-call confirm.
 */
export function keymapSuppressed({
  action, modalOpen = false, drawerOpen = false, callActive = false, defaultPrevented = false,
  railAvailable = true, resumed = false,
} = {}, map = WORKSPACE_KEYMAP) {
  const entry = findBinding(action, map)
  // Not ours to dispatch → suppressed, which fails closed for an action id
  // that only exists in a future map.
  if (!entry || entry.reserved || entry.owner !== OWNER_SHELL) return true
  // A nearer owner already claimed this event (the Esc protocol, the
  // typeahead's bare arrows): yielding is the protocol. `resumed` is the one
  // exception — ⌘J during a call `preventDefault`s and parks the SAME event
  // behind the leave-call ask, so on the resumed pass `defaultPrevented` is
  // the shell's own mark, not a nearer owner's. (merge-train 2026-10-06: read
  // as a claim, it made ⌘J dead exactly when the person had said "end the
  // call and leave".) It waives this rung only; everything below still runs.
  if (defaultPrevented && !resumed) return true
  if (modalOpen || drawerOpen) return true
  // A rail key on a page with no rail is suppressed rather than dispatched to a
  // no-op: the difference is `preventDefault`. An action the shell will not
  // perform must leave the chord to the browser, or `⌘.` silently eats
  // Safari's Stop on every page that has no rail.
  if (entry.needsRail && !railAvailable) return true
  return !!(callActive && entry.callSuppressed)
}

/**
 * Is something modal open right now?
 *
 * A DOM probe rather than a registry: it sees every overlay that is honest
 * about being modal, including ones that do not exist yet, and an overlay that
 * forgets to register is the whole 2026-09-07 class. The mobile drawer is NOT
 * `aria-modal` (an `aria-modal` with no focus trap and no Esc is an AT trap,
 * #1923) — the shell passes its own flag for that one.
 */
export function hasModalOpen(root, { ignore = [] } = {}) {
  if (!root || typeof root.querySelector !== 'function') return false
  const skip = (Array.isArray(ignore) ? ignore : [ignore]).filter(Boolean)
  const selector = `[aria-modal="true"]${skip.map((s) => `:not(${s})`).join('')}`
  return !!root.querySelector(selector)
}

/**
 * Wrapped index. The same arithmetic the typeahead's roving selection uses —
 * one modulo in the codebase, not two.
 */
export function cycleIndex(current, delta, length) {
  return nextActiveIndex(current, delta, length)
}

/**
 * The agent a switch key lands on, by name.
 *
 * `order` is the list the SIDEBAR shows (`orderRosterAgents`), so the keys walk
 * what the eye sees. One agent (or none) is a silent no-op per the AC.
 */
export function nextAgent(order, activeName, delta) {
  const list = (Array.isArray(order) ? order : [])
    .map((a) => (typeof a === 'string' ? a : a && a.name))
    .filter((n) => typeof n === 'string' && n)
  if (list.length < 2) return null
  const i = list.indexOf(activeName)
  const next = list[cycleIndex(i, delta, list.length)]
  return next || null
}

/**
 * The "last open chat per agent" writer, as a rule rather than four call sites.
 *
 * `pendingSession` is a LOAD INSTRUCTION, not the chat on stage: clicking an
 * agent row nulls it while the agent is unchanged, and recording that would
 * forget the chat the person was just reading. So a null under an UNCHANGED
 * agent is ignored; a null under a NEW agent is real (an unsent new chat), and
 * recording it lets `agentLanding`'s own arms 3–5 return to the draft.
 */
export function recordLastOpen(map, { agentName, sessionId = null, keyChanged = false } = {}) {
  if (!map || typeof map.set !== 'function') return false
  if (!agentName || typeof agentName !== 'string') return false
  if (!keyChanged && !sessionId) return false
  map.set(agentName, sessionId || null)
  return true
}

/**
 * The rail tab `⌥.` moves to. Walks only the tabs this session may SEE
 * (`visibleTabs`), never `RAIL_TAB_ORDER` — a door-failed tab must stay
 * unreachable by key as surely as by click.
 */
export function nextRailTab(tabs, currentId, delta = 1) {
  const list = (Array.isArray(tabs) ? tabs : []).filter(Boolean)
  if (!list.length) return null
  const i = list.findIndex((t) => t && t.id === currentId)
  const next = list[cycleIndex(i, delta, list.length)]
  return (next && next.id) || null
}

const GLYPHS = Object.freeze({
  ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Escape: 'Esc',
})

const keyGlyph = (key) => {
  if (!key) return ''
  if (GLYPHS[key]) return GLYPHS[key]
  return key.length === 1 ? key.toUpperCase() : key
}

/** What a person reads: `⌥⇧↓` on a Mac, `Alt+Shift+↓` everywhere else. */
export function chordLabel(c, platform) {
  if (!c) return ''
  const mac = isMacLike(platform)
  const parts = []
  if (c.alt) parts.push(mac ? '⌥' : 'Alt')
  if (c.shift) parts.push(mac ? '⇧' : 'Shift')
  if (c.primary) parts.push(mac ? '⌘' : 'Ctrl')
  parts.push(keyGlyph(c.key))
  return mac ? parts.join('') : parts.join('+')
}

// WAI-ARIA 1.2: the PRINTED character for printable keys (`Alt+.`), key names
// only for non-printables (`Alt+ArrowDown`). Alternatives are space-separated.
const ariaKey = (key) => (key && key.length === 1 ? key.toUpperCase() : key || '')

/** What assistive tech reads — `aria-keyshortcuts`, in ARIA's own spelling. */
export function ariaKeyshortcuts(chordOrChords, platform) {
  const list = Array.isArray(chordOrChords) ? chordOrChords : [chordOrChords]
  return list.filter(Boolean).map((c) => {
    const parts = []
    if (c.primary) parts.push(isMacLike(platform) ? 'Meta' : 'Control')
    if (c.alt) parts.push('Alt')
    if (c.shift) parts.push('Shift')
    parts.push(ariaKey(c.key))
    return parts.join('+')
  }).join(' ')
}

/**
 * The key list's rows, rendered FROM the map — nothing hand-typed, so a key
 * that changes cannot leave a help dialog lying about it.
 *
 * The reserved entry is dropped (no dead rows). Where the page has no rail,
 * the rail rows say so rather than disappearing: a key that is absent here and
 * silent there is the one a person retries forever.
 */
export function keyListRows(map = WORKSPACE_KEYMAP, platform, { railAvailable = true } = {}) {
  return (Array.isArray(map) ? map : []).filter((e) => e && !e.reserved).map((e) => {
    const chords = entryChords(e)
    const notes = []
    if (e.note) notes.push(e.note)
    if (e.context) notes.push(e.context)
    if (e.needsRail && !railAvailable) notes.push('This page has no rail.')
    return {
      action: e.action,
      scope: e.scope,
      owner: e.owner,
      label: e.label,
      keys: chords.map((c) => chordLabel(c, platform)).join(' / '),
      ariaKeyshortcuts: ariaKeyshortcuts(chords, platform),
      note: notes.join(' ') || null,
    }
  })
}

/**
 * The platform string the LABELS read.
 *
 * One reader, because a tooltip that says `Ctrl+.` while the key list says
 * `⌘.` is worse than no tooltip: five components asking `navigator` their own
 * way is five chances to disagree about the same keyboard.
 */
export function hostPlatform(nav = typeof navigator !== 'undefined' ? navigator : null) {
  return (nav && (nav.platform || nav.userAgent)) || ''
}

const bindingsFor = (actions, map) =>
  (Array.isArray(actions) ? actions : [actions])
    .map((a) => findBinding(a, map))
    .filter(Boolean)
    .flatMap(entryChords)

/**
 * What a tooltip says for an action (or a pair of them): `⌥↑ / ⌥↓`.
 *
 * Every visible key hint in the Workspace comes through here, so a chord that
 * changes in the map changes on the control the same commit — a hand-typed
 * glyph is a second declaration, and the one that goes stale is always the one
 * nobody is testing.
 */
export function keyHint(actions, platform, map = WORKSPACE_KEYMAP) {
  return bindingsFor(actions, map).map((c) => chordLabel(c, platform)).join(' / ')
}

/** The same binding as `aria-keyshortcuts` — ARIA's spelling, space-separated. */
export function keyShortcutsFor(actions, platform, map = WORKSPACE_KEYMAP) {
  return ariaKeyshortcuts(bindingsFor(actions, map), platform)
}
