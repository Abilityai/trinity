// The rules of a Workspace Direct call (trinity-enterprise#1046), pure so they can be
// tested without a microphone: what counts as a spoken yes, what is our own echo, what
// is an unfinished sentence, and which call modes this person is offered.

import { VOICE_UNAVAILABLE_FALLBACK } from './portalVoiceMode'

export const DIRECT_PREFIX = 'vd_'
export const HOLD_UNFINISHED_MS = 3000
// A yes that starts sooner than this after the prompt ended overlapped it.
export const APPROVAL_GAP_MS = 300

export function isDirectCallId(id) {
  return typeof id === 'string' && id.startsWith(DIRECT_PREFIX)
}

const YES = new Set(['yes', 'yeah', 'yep', 'yup', 'sure', 'approve', 'approved', 'go ahead', 'do it',
  'ok', 'okay', 'yes please', 'yes go ahead', 'yes do it'])
const NO = new Set(['no', 'nope', 'deny', 'denied', 'reject', 'rejected', 'dont', 'do not', 'stop',
  'cancel', 'no thanks', 'dont do it', 'no dont'])

const normalize = (text) => String(text || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, '')
  .replace(/\s+/g, ' ').trim()

// The WHOLE utterance has to be an answer. "Yes but change the subject first" is words
// for the agent, and the ask stays open.
export function classifyAnswer(text) {
  const t = normalize(text)
  return YES.has(t) ? 'yes' : NO.has(t) ? 'no' : 'other'
}

// Which offered option a spoken answer maps to, or null when the ask cannot be answered
// by yes or no. Only an approval is: an open question needs its real answer.
export function optionForAnswer(ask, answer) {
  if (!ask || ask.kind !== 'approval' || !Array.isArray(ask.options)) return null
  const labels = ask.options.map((o) => (typeof o === 'string' ? o : o?.value ?? o?.label)).filter(Boolean)
  const want = answer === 'yes' ? /^(approve|approved|yes|allow)\b/i : answer === 'no' ? /^(reject|rejected|deny|denied|no)\b/i : null
  return want ? labels.find((l) => want.test(String(l))) ?? null : null
}

// How an ask answered on its card ended, for the spoken follow-up. Null when it ended
// some other way (expired, withdrawn): nothing should be said as though it was decided.
export function cardOutcome(ask) {
  const response = ask?.response
  if (!response) return null
  if (response === optionForAnswer(ask, 'yes')) return 'approved'
  if (response === optionForAnswer(ask, 'no')) return 'rejected'
  return null
}

const UNFINISHED_END = /\b(uh|um|er|erm|and|but|or|so|to|the|a|an|of|on|in|with|for|that|because|like)[.?!,]*$/i

export function looksUnfinished(text) {
  const t = String(text || '').trim()
  return !/[.?!…]$/.test(t) || UNFINISHED_END.test(t)
}

export function isEcho(heard, recentlySpoken = []) {
  const words = normalize(heard).split(' ').filter(Boolean)
  if (!words.length) return true
  if (words.length < 2) return false
  const said = new Set(normalize(recentlySpoken.join(' ')).split(' ').filter(Boolean))
  return words.filter((w) => said.has(w)).length / words.length >= 0.7
}

// Should this transcript answer the waiting ask? Not if it overlapped the prompt: the
// mic may have heard the prompt's own "yes or no".
export function answersAsk({ waiting, text, duringPlayback, startedAt, promptEndedAt }) {
  if (!waiting || duringPlayback) return null
  if (promptEndedAt === null || promptEndedAt === undefined) return null
  if (startedAt - promptEndedAt < APPROVAL_GAP_MS) return null
  const answer = classifyAnswer(text)
  return answer === 'other' ? null : answer
}

// The two call buttons, each with its own availability. A disabled button carries the
// reason (the "never a dead button" rule); while one call is on, the other button is
// off for the call's duration and says which call is on.
export function callButtons({ isPlatform = false, realtimeVoice = null, directVoice = null, liveActive = false, directActive = false } = {}) {
  if (!isPlatform) return { render: false, live: null, direct: null }
  const one = (voice, label, active, otherActive) => ({
    active,
    enabled: active || (voice?.available === true && !otherActive),
    label: active ? 'End the call (Esc)' : otherActive ? 'Another call is on' : (voice?.available === true ? label : (voice?.reason || VOICE_UNAVAILABLE_FALLBACK)),
  })
  return {
    render: true,
    live: one(realtimeVoice, 'Start a voice call', liveActive, directActive),
    direct: one(directVoice, 'Start a Direct call', directActive, liveActive),
  }
}

// What the orb shows for each Direct status, in the Live orb's vocabulary
// (`voiceOrbField.STATE_HUE`): green while it listens, amber while anything is being
// worked on — a transcript, the agent's turn, an open question — and blue while it
// speaks. Muted is the orb dimmed, which the orb does itself.
export function directOrbState(status) {
  switch (status) {
    case 'listening': return 'listening'
    case 'speaking': return 'speaking'
    case 'transcribing':
    case 'thinking':
    case 'awaiting_approval': return 'tool_calling'
    case 'connecting': return 'connecting'
    default: return 'idle'
  }
}

export const DIRECT_STATE_TEXT = Object.freeze({
  connecting: 'Connecting…',
  listening: 'Listening',
  transcribing: 'Catching that…',
  thinking: 'The agent is working',
  speaking: 'Speaking. Talk over it to stop.',
  awaiting_approval: 'Say yes or no, or use the card',
  ended: 'Call ended',
})

export function directStateText({ status, muted = false, tool = '' }) {
  if (muted) return 'Muted'
  if (status === 'thinking' && tool) return `The agent is working · ${tool}`
  return DIRECT_STATE_TEXT[status] || ''
}

// The line under the stage says what this call is, including when speech can only start
// once the turn is over. Gemini CLI publishes no live stream today.
export function directStatusLine({ runtime = '', provider = '', earlySpeech = true } = {}) {
  const parts = ['Direct call']
  if (runtime) parts.push(runtime)
  if (provider) parts.push(`speech by ${provider}`)
  if (earlySpeech === false) {
    parts.push(runtime === 'gemini-cli' ? 'replies take about 10 s' : 'speaks when the reply is finished')
  }
  parts.push('Esc ends')
  return parts.join(' · ')
}
