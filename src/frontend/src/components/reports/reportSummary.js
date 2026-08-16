/**
 * Summarise an unrecognised report payload for a human (#2162).
 *
 * The shared renderer set's fallback used to be the raw JSON viewer. That is
 * defensible for an operator and wrong for an external client: `payload` is
 * free-form agent-authored JSON, the same category as an operator-queue ask's
 * `context`, which the Workspace refuses to expose at all because it has been a
 * credential-leak surface before (canary G-04). The Reports tab was dumping it
 * key-for-key.
 *
 * This is the readable replacement — and the whole of the fallback's logic,
 * deliberately: there is no component-mount harness in this project, so a
 * summariser written inline in a `.vue` template would be untestable. The
 * component around it is a dumb renderer.
 *
 * Three properties, each load-bearing:
 *
 *   1. **It never stringifies the payload.** Not once, not for a nested value.
 *      `JSON.stringify` anywhere in here would reintroduce the bug through the
 *      component that exists to fix it, so depth is 1 and a nested value is
 *      described ("12 items", "4 fields") rather than serialised.
 *
 *   2. **It is bounded.** ≤40 entries with a counted remainder, values truncated
 *      at ~200 chars. An agent can file 5 MiB; a summary that grows with the
 *      payload is the same unbounded page the design contract's principle 28
 *      forbids, just with nicer type.
 *
 *   3. **It redacts credential-shaped tokens at VALUE level, anywhere in the
 *      string.** Not by key name: an allow-list of "safe" keys is both a dead
 *      end here (the fallback fires precisely on payloads nobody has seen, so an
 *      allow-list blanks nearly all of them) and ineffective, because an
 *      allowed key's value can carry the secret — `{"status": "failed: sk-…"}`
 *      is the case that motivated this. Patterns mirror G-04's set.
 *
 * **Honest scope.** This is defense-in-depth on the FALLBACK path only. A
 * well-shaped `markdown` or `table` report still renders its values as authored,
 * and a key-value summary still names every top-level key — it bounds and
 * humanises the residual, it does not eliminate the class. The general fix is a
 * scrub at the portal read boundary, which is a security change to a shipping
 * read path and deserves its own review rather than riding a UI fix. The helper
 * below is written so it can move there without rework.
 */

// Entries beyond this are counted, not rendered. A report with more than 40
// top-level keys is not something a person reads as a list anyway.
export const MAX_ENTRIES = 40

// Long enough for a sentence or an id, short enough that one pathological
// value cannot own the card.
export const MAX_VALUE_CHARS = 200

// Keys are agent-authored too, so the label gets the same treatment as a value
// (bounded and redacted) — just with less room.
export const MAX_LABEL_CHARS = 80

export const REDACTED = '[redacted]'

/**
 * Credential-shaped tokens, mirroring `canary/invariants/g04_*.py`'s prefix set.
 *
 * Two deliberate differences from the detector: these consume the WHOLE token
 * (a detector only needs to prove a prefix exists; a redactor that stops after
 * one character leaves the rest of the secret on screen), and they are applied
 * as replacements rather than reported by name.
 *
 * `\b` before each prefix is what keeps "task-force" from matching `sk-`; it
 * treats `_` as a word character exactly as the Python side does, so the two
 * agree on `my_github_pat_x` (no match, no boundary) by construction.
 */
const SECRET_PATTERNS = [
  // OpenAI / Anthropic-style secret keys ("sk-…", "sk-ant-…", "sk-proj-…").
  /\bsk-[A-Za-z0-9][A-Za-z0-9_-]*/g,
  // Stripe live secret key.
  /\bsk_live_[A-Za-z0-9]+/g,
  // GitHub: classic PAT, OAuth, server, user, fine-grained.
  /\bghp_[A-Za-z0-9]+/g,
  /\bgho_[A-Za-z0-9]+/g,
  /\bghs_[A-Za-z0-9]+/g,
  /\bghu_[A-Za-z0-9]+/g,
  /\bgithub_pat_[A-Za-z0-9_]+/g,
  // Slack bot / user OAuth tokens.
  /\bxoxb-[A-Za-z0-9-]+/g,
  /\bxoxp-[A-Za-z0-9-]+/g,
  // AWS access key id.
  /\bAKIA[A-Z0-9]{4}[A-Z0-9]*/g,
  // Google API key.
  /\bAIza[A-Za-z0-9_-]+/g,
]

/**
 * Replace credential-shaped tokens with a marker.
 *
 * Runs BEFORE truncation, always: truncating first can slice a token so its
 * prefix survives without the pattern matching, which leaves a recognisable
 * fragment on screen under a value that looks handled.
 */
export function redactSecrets(text) {
  let out = String(text)
  for (const pattern of SECRET_PATTERNS) {
    // Fresh lastIndex per call — these are module-level /g regexes.
    pattern.lastIndex = 0
    out = out.replace(pattern, REDACTED)
  }
  return out
}

function clip(text, max) {
  const s = String(text)
  return s.length > max ? `${s.slice(0, max)}…` : s
}

/** Redact, then bound. Order matters — see `redactSecrets`. */
function safeText(value, max) {
  return clip(redactSecrets(value), max)
}

function plural(n, noun) {
  return `${n.toLocaleString()} ${noun}${n === 1 ? '' : 's'}`
}

/**
 * One top-level value, described at depth 1.
 *
 * Returns `{value, hint}` where `hint` is `'text' | 'count' | 'empty'` — the
 * component styles by it so a described container ("12 items") reads as
 * metadata rather than as content the agent wrote.
 */
export function describeValue(value) {
  if (value === null || value === undefined) return { value: '—', hint: 'empty' }
  if (Array.isArray(value)) return { value: plural(value.length, 'item'), hint: 'count' }
  if (typeof value === 'object') {
    return { value: plural(Object.keys(value).length, 'field'), hint: 'count' }
  }
  if (typeof value === 'boolean') return { value: value ? 'Yes' : 'No', hint: 'text' }
  if (typeof value === 'number') {
    return { value: Number.isFinite(value) ? value.toLocaleString() : String(value), hint: 'text' }
  }
  const text = safeText(value, MAX_VALUE_CHARS)
  if (!text.trim()) return { value: '—', hint: 'empty' }
  return { value: text, hint: 'text' }
}

/**
 * `total_leads` → `Total leads`, `createdAt` → `Created at`.
 *
 * Bounded and redacted like a value: a payload key is as agent-authored as a
 * payload value, and nothing stops one being a token.
 *
 * **Redaction runs on the RAW key, before humanisation** — the same ordering
 * rule as truncation, and for a sharper reason: humanising rewrites `_` and `-`
 * to spaces, which is exactly the shape every one of these patterns keys on. A
 * `ghp_…` key humanised first becomes `ghp …`, matches nothing, and the tail
 * ships. (Caught by its own test, which is why the order is stated here.)
 */
export function humaniseKey(key) {
  const redacted = redactSecrets(key)
  const spaced = redacted
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim()
  if (!spaced) return clip(redacted, MAX_LABEL_CHARS)
  const lower = spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase()
  return clip(lower, MAX_LABEL_CHARS)
}

/**
 * `payload` → `{ entries: [{key, label, value, hint}], truncated }`.
 *
 * `truncated` is how many top-level entries were dropped at the cap, so the
 * component can say "+N more" instead of silently showing a prefix — the
 * difference between a bounded view and a wrong one.
 *
 * An empty result means "nothing readable was filed", which the component
 * renders as its own sentence rather than as an empty list.
 */
export function summarizePayload(payload) {
  if (payload === null || payload === undefined) return { entries: [], truncated: 0 }

  // A root-level array is described as a whole rather than exploded into
  // `0`, `1`, `2` … — numbered keys are not a human-readable summary, they are
  // a JSON dump with the braces removed.
  if (Array.isArray(payload)) {
    if (!payload.length) return { entries: [], truncated: 0 }
    return {
      entries: [{ key: '', label: 'Items', ...describeValue(payload) }],
      truncated: 0,
    }
  }

  if (typeof payload !== 'object') {
    const described = describeValue(payload)
    if (described.hint === 'empty') return { entries: [], truncated: 0 }
    return { entries: [{ key: '', label: 'Value', ...described }], truncated: 0 }
  }

  const keys = Object.keys(payload)
  const shown = keys.slice(0, MAX_ENTRIES)
  return {
    entries: shown.map((key) => ({
      key,
      label: humaniseKey(key),
      ...describeValue(payload[key]),
    })),
    truncated: Math.max(0, keys.length - shown.length),
  }
}
