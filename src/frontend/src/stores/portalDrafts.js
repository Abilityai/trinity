import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { useClientPortalStore } from './clientPortal'
import { safeStorage } from '../utils/safeStorage'
import {
  MAX_PERSISTED_CHARS, hasDraftText, draftStorageKey, loadDrafts, persistDraft,
  removeDraftsBucket, agentOfNewChatKey,
} from '../components/portal/portalDrafts'

/**
 * The Workspace's unsent drafts (trinity-enterprise#657): one entry per
 * conversation, for the person the roster says is signed in.
 *
 * Owned here rather than by the shell because the two composers
 * (`PortalConversation`, `PortalRoom`) write it and the sidebar, the tab strip
 * and the shell's `decorate()` read it — the `portalWork.js` / `portalLoops.js`
 * shape. The rules are in `components/portal/portalDrafts.js`; this file is
 * the reactive map plus its mirror in localStorage.
 *
 * Identity is `clientPortal.clientEmail` — the server-reported principal
 * (`get_roster` → `client_email`, both audiences), known before any composer
 * or row renders because the stage gates on `rosterLoaded`. Not the column
 * layout's `auth0_user` rule: that one had to read synchronously before first
 * paint and gives portal clients one shared bucket, which is fine for widths
 * and not for words. An identity change SWAPS the map (never merges); with no
 * identity the map is memory-only and nothing is written.
 *
 * Persistence is write-through and key-granular (`persistDraft` re-reads the
 * bucket and applies one key), and a `storage` event for the bucket re-reads
 * it — so two Workspace tabs (ent#456) cannot clobber each other's drafts, and
 * a send in one clears the mark in the other. Every storage failure is silent
 * and the map keeps working in memory (AC 8).
 */
export const usePortalDraftsStore = defineStore('portalDrafts', () => {
  const portal = useClientPortalStore()
  const storage = safeStorage()

  const identity = computed(() => {
    const who = typeof portal.clientEmail === 'string' ? portal.clientEmail.trim().toLowerCase() : ''
    return who || null
  })

  // key → { text, updatedAt } for the CURRENT identity.
  const drafts = ref({})

  function reload() {
    drafts.value = identity.value ? loadDrafts(storage, identity.value) : {}
  }
  watch(identity, reload, { immediate: true })

  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('storage', (event) => {
      const bucketKey = draftStorageKey(identity.value)
      if (bucketKey && event?.key === bucketKey) reload()
    })
  }

  // A sorted-keys signature, so `keys` changes identity only when MEMBERSHIP
  // changes (Vue's computed stability on a primitive): a keystroke updates an
  // entry's text, and the sidebar and both tab strips must not re-render for
  // it — only for a draft appearing or disappearing.
  const keysSignature = computed(() => Object.keys(drafts.value).sort().join('\n'))
  const keys = computed(() => new Set(keysSignature.value ? keysSignature.value.split('\n') : []))
  const newChatDraftAgents = computed(() => new Set(
    [...keys.value].map(agentOfNewChatKey).filter(Boolean),
  ))

  function get(key) {
    return (key && drafts.value[key]?.text) || ''
  }

  function has(key) {
    return !!(key && drafts.value[key])
  }

  function set(key, text) {
    if (!key) return
    if (!hasDraftText(text)) { clear(key); return }
    const entry = { text, updatedAt: Date.now() }
    drafts.value = { ...drafts.value, [key]: entry }
    // Oversize: session-only, and the stored entry (a shorter, older version)
    // goes too, so a reload never restores something other than what was left.
    persistDraft(storage, identity.value, key, text.length > MAX_PERSISTED_CHARS ? null : entry)
  }

  function clear(key) {
    if (!key || !drafts.value[key]) return
    const next = { ...drafts.value }
    delete next[key]
    drafts.value = next
    persistDraft(storage, identity.value, key, null)
  }

  /** Carry a draft to a new identity: session adoption, Main reset (ent#523). */
  function move(fromKey, toKey) {
    if (!fromKey || !toKey || fromKey === toKey) return
    const text = get(fromKey)
    if (!text) return
    clear(fromKey)
    set(toKey, text)
  }

  /** Explicit sign-out: the person's bucket goes with their session. */
  function clearBucket() {
    removeDraftsBucket(storage, identity.value)
    drafts.value = {}
  }

  return { identity, drafts, keys, newChatDraftAgents, get, has, set, clear, move, clearBucket, reload }
})
