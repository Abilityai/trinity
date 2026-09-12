// Workspace voice mode (trinity-enterprise#534) — the orb takes the conversation.
//
// Every rule the conversation and the shell decide about a voice CALL lives
// here as a pure function: vitest runs `environment: 'node'` with no
// component-mount harness, so a rule kept inside a `.vue` file is a rule no
// test can reach (the ent#440 lesson, kept). The component and the shell are
// dispatchers over these.
//
// Provider-neutral on purpose (ent#354 may add a second realtime provider):
// nothing here names Gemini; the roster's `realtime_voice` field and the
// WebSocket frames are the whole contract.

// ---- The entry control ----------------------------------------------------

// A portal-token (external) client never sees the control — the audio socket
// authenticates with the platform JWT they do not hold, and a disabled button
// explaining a limitation that is not theirs would be a dead affordance with a
// footnote. A platform user sees it always: enabled when the instance can, or
// disabled WITH the reason when it cannot (the "never a dead button" AC).
export const VOICE_UNAVAILABLE_FALLBACK = 'Voice is not available on this instance.'

export function voiceEntryState({ isPlatform = false, realtimeVoice = null } = {}) {
  if (!isPlatform) return { render: false, enabled: false, reason: '' }
  const available = realtimeVoice?.available === true
  if (available) return { render: true, enabled: true, reason: '' }
  return { render: true, enabled: false, reason: realtimeVoice?.reason || VOICE_UNAVAILABLE_FALLBACK }
}

// ---- The Talk door: `?voice=1` (trinity#2559) --------------------------------

export const VOICE_QUERY_KEY = 'voice'

// The intent is armed IN THE APP, by Agent Detail's Talk button, and consumed
// once. A module-scoped `let` is the whole point: it lives exactly as long as
// the document, so a pasted / bookmarked / mailed `?voice=1` — which always
// arrives on a FRESH document — can never be armed, while the same-document
// hand-off from Agent Detail (and the inline sign-in re-bootstrap that may
// follow it) both keep it.
//
// `navigator.userActivation` cannot do this job, and reaching for it is the
// trap: it is an audio-playback heuristic rather than a provenance check, it is
// sticky per document, and the sign-in click sets it for the attacker.
// `Portal.vue` runs `bootstrap()` only while signed in, so a pasted link
// survives unconsumed until exactly the click that satisfies the browser's own
// check — a link plus a sign-in was a hot mic. It is also fail-open on `null`.
//
// Chosen over `sessionStorage` (precedent: `stores/clientPortal.js`
// `FALLBACK_SUPPRESSED_KEY`) because sessionStorage SURVIVES a reload, so a
// one-shot there needs correct explicit deletion on every path; the module
// `let` dies with the document, which is the semantics we want, for free.
let armed = false
export function armVoiceAutoStart() { armed = true }
export function disarmVoiceAutoStart() { armed = false }
export function voiceAutoStartArmed() { return armed }

// vue-router hands back a string, or an array when the key repeats.
export function voiceQueryRequested(value) {
  const v = Array.isArray(value) ? value[0] : value
  return v === '1'
}

// No `strip` verdict here: stripping is keyed on the key's PRESENCE and owned
// by `Portal.vue::bootstrap()` — one site, every exit. A helper that stripped
// on its own verdict left `?voice=0` resident, because a rejected value is
// still present.
export function voiceAutoStart({ query = null, landed = false, isPlatform = false, armed: isArmed = false } = {}) {
  if (!voiceQueryRequested(query?.[VOICE_QUERY_KEY])) return { start: false, why: '' }
  if (!isArmed) return { start: false, why: 'unarmed' }
  if (!landed) return { start: false, why: 'unreachable' }
  if (!isPlatform) return { start: false, why: 'principal' }
  return { start: true, why: '' }
}

// ---- Pre-flight, before any request leaves the browser ----------------------

export const VOICE_INSECURE_REASON =
  'Voice needs a secure (https) page — this browser can’t reach a microphone here. You can still type.'
export const VOICE_NO_MIC_REASON =
  'This browser has no microphone access. You can still type.'

// `null` means "go ahead"; a string is the sentence to show instead of starting.
export function voicePreflight({ canCapture = false, secureContext = true } = {}) {
  if (!secureContext) return VOICE_INSECURE_REASON
  if (!canCapture) return VOICE_NO_MIC_REASON
  return null
}

// ---- The header line while a call is on ------------------------------------

export const VOICE_STATE_LABELS = Object.freeze({
  connecting: 'Connecting…',
  listening: 'Listening',
  speaking: 'Speaking',
  tool_calling: 'Working',
  ended: 'Call ended',
  idle: '',
})

// What the one status line says. `toolName` is shown beside "Working" so the
// person sees WHAT the agent is doing (the amber badge on the orb says the
// same); a muted mic overrides "Listening", since it is not. Background tasks
// (ent#551) are appended: they outlive the tool call that started them, so the
// line keeps saying so while the conversation moves on.
export function voiceHeaderLine({ status = 'idle', toolName = null, muted = false, error = '', backgroundTasks = 0 } = {}) {
  if (error) return error
  let line
  if (status === 'tool_calling') {
    line = toolName ? `Working: ${String(toolName).replace(/_/g, ' ')}` : 'Working…'
  } else if (status === 'listening' && muted) {
    line = 'Muted'
  } else {
    line = VOICE_STATE_LABELS[status] ?? ''
  }
  const tasks = backgroundTasksLabel(backgroundTasks)
  if (!tasks) return line
  return line ? `${line} · ${tasks}` : tasks
}

// ---- Background tasks (ent#551) ---------------------------------------------

// The bridge's `task` frame: `{state: started|finished|failed, task_id, label}`.
// A list keyed on the task id, never a count — two tasks can be in flight and
// the first to finish must not clear the badge (the same reason the backend
// keeps a dict). A frame for an unknown id is a no-op; a repeated `started` is
// idempotent.
export function applyTaskFrame(tasks = [], frame = {}) {
  const id = frame?.task_id
  if (!id) return tasks
  if (frame.state === 'started') {
    if (tasks.some((t) => t.taskId === id)) return tasks
    return [...tasks, { taskId: id, label: frame.label || '', status: frame.status || 'running' }]
  }
  // ent#551 QA: tasks run one at a time per call (the thread admits one turn);
  // `running` is the moment a queued task takes its turn.
  if (frame.state === 'running') {
    return tasks.map((t) => (t.taskId === id ? { ...t, status: 'running' } : t))
  }
  return tasks.filter((t) => t.taskId !== id)
}

// One line per task for the orb's list: the label, and "queued" while another
// task holds the thread. Separate items, never bunched — the operator's note.
export function taskItemLabel(task = {}) {
  const label = clip(task.label, TASK_LABEL_MAX) || 'task'
  return task.status === 'queued' ? `${label} · queued` : label
}

// The badge / header words for work in flight: WHAT is running, in one line,
// not how many things are (the operator's first-run note — "a task" tells the
// person nothing). Given the list, one task is its own label; several are the
// count followed by their labels. Given only a count, the count.
export const TASK_LABEL_MAX = 48
export const TASKS_LINE_MAX = 96

function clip(text, max) {
  const t = String(text || '').trim()
  return t.length > max ? `${t.slice(0, max - 1)}…` : t
}

export function backgroundTasksLabel(tasks = []) {
  if (!Array.isArray(tasks)) {
    const n = Math.max(0, Number(tasks) || 0)
    return n === 0 ? '' : n === 1 ? '1 task running' : `${n} tasks running`
  }
  if (tasks.length === 0) return ''
  if (tasks.length === 1) return clip(tasks[0].label, TASK_LABEL_MAX) || '1 task running'
  const labels = tasks.map((t) => clip(t.label, TASK_LABEL_MAX)).filter(Boolean).join(' · ')
  return clip(`${tasks.length} tasks · ${labels}`, TASKS_LINE_MAX)
}

// A typed row written by a task the agent ran during a voice call carries the
// call's id but NOT `source: 'voice'` (it was not spoken) — so it renders as an
// ordinary turn, outside the collapsed block, with this caption on the ask.
export const VOICE_TASK_CAPTION = 'asked during a voice call'

export function voiceTaskCaption(message = {}) {
  if (!message?.voiceCallId || message.source === VOICE_SOURCE) return ''
  return message.role === 'user' ? VOICE_TASK_CAPTION : ''
}

// The sentence for a call that ended other than by the person pressing End.
// The server's own words win when it sent any; the reason is the fallback key.
export const END_REASON_TEXT = Object.freeze({
  cap: 'The call reached its time limit.',
  error: 'The voice provider returned an error, so the call ended.',
  provider_closed: 'The voice connection closed, so the call ended.',
})

export function endedNotice({ reason = null, message = '' } = {}) {
  if (!reason) return ''
  return message || END_REASON_TEXT[reason] || 'The call ended.'
}

// A failed start, in words. `detail` is the server's `detail` string when the
// request answered; `status` its HTTP status.
export function startFailureReason({ status = null, detail = '' } = {}) {
  if (detail) return detail
  if (status === 404) return 'This conversation could not be found for a voice call.'
  if (status === 429) return 'Too many voice calls started just now — wait a moment.'
  if (status === 503) return VOICE_UNAVAILABLE_FALLBACK
  return 'The voice call could not start.'
}

// ---- The transcript block ---------------------------------------------------

export const VOICE_SOURCE = 'voice'

// Fold a thread's rows into render items: typed rows pass through one by one;
// every row carrying a `voiceCallId` joins that call's block, placed where the
// call's FIRST row sits. Keyed on the call id, never on an opener row: the
// history window returns the newest 100 rows and a long call is more, so an
// opener may be off-screen — and a typed turn that landed mid-call must not
// split the block in two. The trailing `system` row the call writes is the
// label; when the window cut it off, the label is derived from the turns.
export function groupVoiceBlocks(messages = []) {
  const items = []
  const blocks = new Map()
  messages.forEach((m, index) => {
    const callId = m?.voiceCallId || null
    if (!callId || m.source !== VOICE_SOURCE) { items.push({ kind: 'message', message: m, index }); return }
    let block = blocks.get(callId)
    if (!block) {
      block = { kind: 'voice-call', callId, turns: [], label: '', ended: '' }
      blocks.set(callId, block)
      items.push(block)
    }
    if (m.role === 'system') {
      block.label = m.content || ''
    } else {
      block.turns.push(m)
    }
  })
  for (const b of blocks.values()) {
    if (!b.label) b.label = voiceCallLabelFromTurns(b.turns)
  }
  return items
}

export function voiceCallLabel(seconds, { capped = false, capMinutes = null } = {}) {
  const minutes = Math.max(1, Math.round((Number(seconds) || 0) / 60))
  let label = `Voice call · ${minutes} min`
  if (capped) label += ` · ended at the ${capMinutes || minutes}-minute limit`
  return label
}

// When the window dropped the call's summary row, name the block by what is
// left of it — never an invented duration.
export function voiceCallLabelFromTurns(turns = []) {
  const n = turns.length
  if (!n) return 'Voice call'
  return `Voice call · ${n} spoken ${n === 1 ? 'turn' : 'turns'}`
}

// ---- Escape right after a call --------------------------------------------------

// For a few seconds after a call ends, Escape does not cancel a turn. When the
// call ends, the thread reattaches to any task the call left running, and
// Escape on that surface means "stop the turn" — so the second press of the
// key that just ended the call (habit, or "did that take?") cancelled the task
// the person had just asked for. Twice in the eighth QA run, 3 s after End.
// The Stop button stays live: a click is deliberate in a way a repeated key
// is not.
export const ESCAPE_COOLDOWN_AFTER_CALL_MS = 5000

export function escapeCoolingDownAfterCall({ callEndedAt = 0, now = Date.now() } = {}) {
  if (!callEndedAt) return false
  return now - callEndedAt < ESCAPE_COOLDOWN_AFTER_CALL_MS
}

// ---- The mute hotkey ----------------------------------------------------------

// M toggles the mic while a call is on. Plain M only — a modifier means some
// other shortcut (⌘M minimises a window); a key already claimed by an overlay
// (`defaultPrevented`, the #2582 protocol) is not ours; and a key typed into a
// field is text, not a command (the composer is inert during a call, but the
// rename field and the picker's search are not).
export function isMuteHotkey(event, { callActive = false } = {}) {
  if (!callActive || !event || event.defaultPrevented) return false
  if (event.key !== 'm' && event.key !== 'M') return false
  if (event.metaKey || event.ctrlKey || event.altKey) return false
  const tag = String(event.target?.tagName || '').toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || event.target?.isContentEditable) return false
  return true
}

// ---- Leaving mid-call by a click ----------------------------------------------

// Every way out of the stage OTHER than the End button asks first. End call is
// the person saying "end it"; a click on an agent in the rail, a thread, a
// room, New chat or ⌘J is the person going somewhere, and a call ending as a
// side effect of that — silently, transcript or not — is the bug the operator
// hit twice (a rail click on the very agent they were talking to). The copy
// says what will happen and what is kept.
export function leaveCallCopy(agentName = '') {
  const who = agentName ? `with ${agentName}` : ''
  return Object.freeze({
    title: 'End the call?',
    message: `You're on a voice call ${who}`.trim() + '. Leaving here ends it. What was said stays in the chat.',
    confirmText: 'End call and leave',
    cancelText: 'Stay on the call',
    variant: 'warning',
  })
}

// ---- Leaving mid-call ---------------------------------------------------------

// Does a change of agent / thread props end the call? A route-driven thread
// change (browser back, a deep link) does — the call ends first, its transcript
// kept. But a call started from a BRAND-NEW chat creates its thread first, and
// adopting that thread replaces the route, so the `sessionId` prop changes from
// null to the very thread the call is bound to a moment after the call starts.
// That is not a thread change. Found live (ent#551 QA): every first call from a
// new chat died at exactly 5 s — the premature stop waited out the `saved`
// frame timeout, then closed the socket.
export function threadChangeEndsCall({ callActive = false, agentChanged = false, newSessionId = null, boundSessionId = null } = {}) {
  if (!callActive) return false
  if (agentChanged) return true
  if (!newSessionId) return true                   // the thread went away under the call
  return newSessionId !== boundSessionId
}

// ---- Layout --------------------------------------------------------------------

// The retired `/agents/:name/workspace` page's proportions: orb left 40%, the
// canvas right 60%. Percent of the stage at `sm` and up; below `sm` the orb
// takes the stage and the canvas stays behind the strip's Canvas tab
// (mobile is trinity#710).
export const VOICE_SPLIT = Object.freeze({ orb: 40, canvas: 60 })

// Which header controls go inert while the call is on. Data, so the guard
// spec can check the template disables every one of them and no test has to
// mount anything.
export const VOICE_LOCKED_CONTROLS = Object.freeze([
  'new-chat', 'agent-picker', 'star', 'reset-main', 'chat-tabs', 'composer', 'mic', 'attach', 'send',
  // ent#403: the model picker is part of the composer, and the call runs on the
  // voice provider's own model — leaving it live would offer a choice the call
  // cannot honour.
  'model-picker',
])

// ---- The canvas column's refresh rule -----------------------------------------

// The call is a WebSocket: the bridge emits a `tool_result` frame for every
// panel verb, so the column refetches on those and keeps a slow safety poll
// for anything else that rewrote the board (the agent's own `set_canvas` from
// a `run_task`). 300 ms polling — the retired page's rule — was ~6,000 reads
// per 30-minute call for a surface the socket already narrates.
export const PANEL_TOOL_NAMES = Object.freeze([
  'show_markdown', 'update_panel', 'append_to_panel', 'clear_panel', 'show_diagram', 'show_image',
])
export const CANVAS_SAFETY_POLL_MS = 3000

export function isPanelTool(name) { return PANEL_TOOL_NAMES.includes(name) }

// `true` when a fetched canvas should replace what is on screen: a newer
// `updated_at`, or the first read. Equal stamps are the same board.
export function canvasChanged(previous, next) {
  if (!next) return false
  if (!previous) return true
  return (previous.updated_at || '') !== (next.updated_at || '')
}
