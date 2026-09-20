/**
 * Workspace drafts — the rules (trinity-enterprise#657).
 *
 * A draft is the unsent, non-whitespace text of ONE conversation. The
 * conversation and room stages remount on every switch (`views/Portal.vue`
 * keys them on `convKey` / the room id), so the text has to live outside them;
 * this module is the pure half of that — keys, the storage codec, the
 * per-agent aggregation and the rule for what the composer shows when its
 * conversation changes identity under it. No Vue, no storage import: every
 * function takes what it reads as an argument, so `portalDrafts.spec.js` runs
 * it in node. The reactive half is `stores/portalDrafts.js`; the composer
 * binding is `composables/useComposerDraft.js`.
 *
 * Keys follow the shell's own `chatKey` (`thread:<id>` / `room:<id>`, the
 * scheme `enterprise_portal_chat_state` is keyed by too) plus `new:<agent>`
 * for a chat that has no thread yet — so a later server-side draft would swap
 * only the source, not the keys.
 */

export const DRAFTS_STORAGE_PREFIX = 'trinity-workspace-drafts'
export const DRAFTS_VERSION = 1
/** Per person: the oldest past this are evicted. A bound, not a policy. */
export const MAX_DRAFTS = 100
/**
 * A draft longer than this is kept for the session only (its storage entry
 * is REMOVED, never truncated): one pasted log must not be serialized on
 * every keystroke, and a quota failure on it must not silently disable
 * persistence for every other draft in the bucket.
 */
export const MAX_PERSISTED_CHARS = 64_000

export function threadKey(sessionId) {
  return sessionId ? `thread:${sessionId}` : null
}

export function roomKey(roomId) {
  return roomId ? `room:${roomId}` : null
}

export function newChatKey(agentName) {
  return agentName ? `new:${agentName}` : null
}

export function isNewChatKey(key) {
  return typeof key === 'string' && key.startsWith('new:')
}

export function agentOfNewChatKey(key) {
  return isNewChatKey(key) ? key.slice(4) || null : null
}

/**
 * The one key a mounted composer binds to. A room by its id; a thread by its
 * session id; a deliberately fresh chat (`newChat`) by its agent; and NOTHING
 * for a conversation whose thread is not known yet — a cold `/workspace` root
 * resolves to the most recent thread inside `loadThread(null)`, and until it
 * does there is no honest place to put what is typed.
 */
export function draftKeyFor({ sessionId = null, agentName = '', newChat = false, roomId = null } = {}) {
  if (roomId) return roomKey(roomId)
  if (sessionId) return threadKey(sessionId)
  if (newChat && agentName) return newChatKey(agentName)
  return null
}

/** Whitespace-only text is not a draft: it never shows the mark, and restoring it is not a loss. */
export function hasDraftText(text) {
  return typeof text === 'string' && text.trim().length > 0
}

function normalizeIdentity(identity) {
  const who = typeof identity === 'string' ? identity.trim().toLowerCase() : ''
  return who || null
}

/**
 * Per person: the portal principal's email as the roster reports it
 * (`client_email`, both audiences). Never token material — the key is written
 * back to storage in the clear.
 */
export function draftStorageKey(identity) {
  const who = normalizeIdentity(identity)
  return who ? `${DRAFTS_STORAGE_PREFIX}:${who}` : null
}

/**
 * Coerce whatever storage held into `{ [key]: { text, updatedAt } }`. A
 * hand-edited value, a wrong version or a non-string text is dropped rather
 * than trusted — the map is rendered into the sidebar and the composer.
 */
export function normalizeDraftsMap(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  if (raw.v !== DRAFTS_VERSION) return {}
  const drafts = raw.drafts
  if (!drafts || typeof drafts !== 'object' || Array.isArray(drafts)) return {}
  const out = {}
  for (const [key, entry] of Object.entries(drafts)) {
    if (!key || !entry || typeof entry !== 'object') continue
    if (!hasDraftText(entry.text)) continue
    const updatedAt = Number.isFinite(entry.updatedAt) ? entry.updatedAt : 0
    out[key] = { text: entry.text, updatedAt }
  }
  return out
}

/** Keep the newest `max` entries by `updatedAt`. */
export function boundDrafts(map, max = MAX_DRAFTS) {
  const entries = Object.entries(map || {})
  if (entries.length <= max) return { ...(map || {}) }
  entries.sort((a, b) => (b[1].updatedAt || 0) - (a[1].updatedAt || 0))
  return Object.fromEntries(entries.slice(0, max))
}

function readBucket(storage, bucketKey) {
  try {
    const raw = storage.getItem(bucketKey)
    return normalizeDraftsMap(raw ? JSON.parse(raw) : null)
  } catch {
    return {}
  }
}

/** Read the person's bucket; any failure (no storage, private mode, bad JSON) is empty. */
export function loadDrafts(storage, identity) {
  const bucketKey = draftStorageKey(identity)
  if (!bucketKey || !storage || typeof storage.getItem !== 'function') return {}
  return readBucket(storage, bucketKey)
}

/**
 * Persist ONE key: re-read the bucket, apply this entry (null removes), bound,
 * write. Key-granular on purpose — the Workspace opens in its own tab
 * (ent#456), and a whole-map write from tab B would carry B's stale snapshot
 * of A's keys, erasing a draft A had just written or re-persisting one A had
 * just sent. Returns false on any failure (storage refused, quota); the caller
 * keeps the entry in memory either way.
 */
export function persistDraft(storage, identity, key, entry) {
  const bucketKey = draftStorageKey(identity)
  if (!bucketKey || !key || !storage || typeof storage.setItem !== 'function') return false
  try {
    const current = readBucket(storage, bucketKey)
    if (entry && hasDraftText(entry.text)) {
      current[key] = { text: entry.text, updatedAt: Number.isFinite(entry.updatedAt) ? entry.updatedAt : Date.now() }
    } else {
      delete current[key]
    }
    storage.setItem(bucketKey, JSON.stringify({ v: DRAFTS_VERSION, drafts: boundDrafts(current) }))
    return true
  } catch {
    return false
  }
}

/** Remove the person's whole bucket (explicit sign-out). */
export function removeDraftsBucket(storage, identity) {
  const bucketKey = draftStorageKey(identity)
  if (!bucketKey || !storage || typeof storage.removeItem !== 'function') return false
  try {
    storage.removeItem(bucketKey)
    return true
  } catch {
    return false
  }
}

/**
 * The agents whose row carries the mark — the `unreadByAgent` twin, over
 * threads the shell has already decorated with `hasDraft`. A room lights
 * nobody: the room row is always listed, so it is the draft's own door, and
 * `unreadByAgent` propagates only because the agent row was once the way into
 * an unread chat (ent#359). An unsaved chat has no thread row, so its agents
 * arrive as a second argument.
 */
export function agentsWithDrafts(threads, newChatDraftAgents) {
  const out = new Set()
  for (const t of Array.isArray(threads) ? threads : []) {
    if (t && !t.is_room && t.hasDraft && t.agent_name) out.add(t.agent_name)
  }
  const extra = newChatDraftAgents instanceof Set ? newChatDraftAgents : []
  for (const name of extra) if (name) out.add(name)
  return out
}

/**
 * What the composer shows, and what is written, when its conversation changes
 * identity WITHOUT a remount. Four transitions exist in `PortalConversation`:
 *
 *   → null        `openAgentPage` on the current agent nulls `pendingSession`
 *                 without bumping `convGen`; the remount follows a beat later.
 *                 Keep the composer, write nothing.
 *   null → key    the thread resolved (cold `/workspace` root). Fill only an
 *                 EMPTY composer — text typed while it resolved wins and is
 *                 persisted under the resolved key.
 *   new: → key    session adoption: the same composer, a new identity. MOVE.
 *   key → key     an in-place switch: the source is already stored by
 *                 write-through; show the destination's draft.
 *
 * `read(key)` answers the stored text ('' when none). Returns the text the
 * composer should hold and the `[key, text]` writes to apply ('' clears).
 */
export function reconcileDraftOnKeyChange({ oldKey = null, newKey = null, composerText = '', read = () => '' } = {}) {
  const text = typeof composerText === 'string' ? composerText : ''
  if (!newKey) return { composer: text, writes: [] }
  if (!oldKey) {
    if (hasDraftText(text)) return { composer: text, writes: [[newKey, text]] }
    return { composer: read(newKey) || '', writes: [] }
  }
  if (isNewChatKey(oldKey)) return { composer: text, writes: [[oldKey, ''], [newKey, text]] }
  return { composer: read(newKey) || '', writes: [] }
}

/**
 * Focus the restored composer on a fine pointer only. #2579's New chat focus
 * is an explicit gesture; arriving at a drafted chat is not, and on a phone a
 * focus pops the soft keyboard over the transcript.
 */
export function shouldFocusOnRestore(matchMedia) {
  try {
    return typeof matchMedia === 'function' && !!matchMedia('(pointer: fine)')?.matches
  } catch {
    return false
  }
}
