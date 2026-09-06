import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useClientPortalStore } from './clientPortal'

/**
 * The Workspace rail's feed store (trinity-enterprise#475): the canvases and
 * files of a chat's PARTICIPANTS, owned by the shell so the collapsed rail can
 * signal "updated since last view" with no tab body mounted.
 *
 * The shape is `stores/portalLoops.js`' — participant-scoped, identity-guarded
 * `setParticipants`, `allSettled` per participant so one agent's failure keeps
 * the others' data (ent#253 / #2382), a fetch token that drops a stale
 * response after a chat switch, and NO timer while idle. What differs:
 *
 *   * WHICH feeds are fetched is the shell's decision (`setFeeds`, read off
 *     `visibleTabs` through `feedsFor`) — the door gate extends from "mount"
 *     to "fetch", so a session that fails a tab's door never requests its data;
 *   * `uploads` (the viewer's own inbox) is read from the CONTAINER by the
 *     backend (`_read_inbox`, a docker exec), never signals, and is fetched
 *     only on request — while Files is the open active tab, and after an
 *     upload;
 *   * push-driven refreshes are DEBOUNCED (trailing 2s): a 100-run loop emits
 *     100 `loop_run_completed` events, and each would otherwise cost every
 *     participant a canvas + documents round trip.
 *
 * Every request goes through the client-portal store's existing fetchers
 * (`portalHttp` + the portal auth header), so the roster gate and the
 * server-side audience / inbox narrowing are exactly the drawer's and the
 * agent page's.
 */
const PUSH_DEBOUNCE_MS = 2000

export const usePortalRailFeedsStore = defineStore('portalRailFeeds', () => {
  const participants = ref([])
  const feeds = ref({ canvas: false, files: false })
  const canvases = ref({})     // agent → metadata rows (no blocks)
  const documents = ref({})    // agent → files the agent shared
  const uploads = ref({})      // agent → files the viewer sent
  const uploadsLoaded = ref({}) // agent → true once the inbox was read
  const hasLoaded = ref(false)
  const loading = ref(false)
  const error = ref(null)
  const version = ref(0)       // bumps on every successful load — what "seen" watches

  let _fetchToken = 0
  let _debounceTimer = null

  const canvasCount = computed(() => Object.values(canvases.value).reduce((n, l) => n + l.length, 0))
  const documentCount = computed(() => Object.values(documents.value).reduce((n, l) => n + l.length, 0))

  function _sameList(a, b) {
    return a.length === b.length && a.every((n, i) => b[i] === n)
  }

  function setParticipants(names) {
    const next = (names || []).filter(Boolean)
    if (_sameList(next, participants.value)) return
    participants.value = next
    canvases.value = {}
    documents.value = {}
    uploads.value = {}
    uploadsLoaded.value = {}
    hasLoaded.value = false
    error.value = null
    _fetchToken++
  }

  function setFeeds({ canvas = false, files = false } = {}) {
    const next = { canvas: canvas === true, files: files === true }
    if (next.canvas === feeds.value.canvas && next.files === feeds.value.files) return
    feeds.value = next
    // A feed that just became visible (auth settled late) has no verdict yet.
    hasLoaded.value = false
  }

  /**
   * Fetch what the shell asked for, for every participant. `uploads` is opt-in
   * (see the header). Resolves after the store is updated; never throws.
   */
  async function refresh({ uploads: withUploads = false } = {}) {
    const names = participants.value
    const wants = feeds.value
    if (!names.length || (!wants.canvas && !wants.files)) return
    const portal = useClientPortalStore()
    const token = ++_fetchToken
    loading.value = true
    try {
      const jobs = []
      for (const name of names) {
        if (wants.canvas) jobs.push({ name, kind: 'canvas', p: portal.fetchAgentCanvases(name) })
        if (wants.files) {
          jobs.push({ name, kind: 'documents', p: portal.fetchDocuments(name) })
          if (withUploads) jobs.push({ name, kind: 'uploads', p: portal.fetchUploads(name) })
        }
      }
      const results = await Promise.allSettled(jobs.map((j) => j.p))
      if (token !== _fetchToken) return          // a newer fetch (or a chat switch) already won
      const nextCanvases = { ...canvases.value }
      const nextDocuments = { ...documents.value }
      const nextUploads = { ...uploads.value }
      const nextUploadsLoaded = { ...uploadsLoaded.value }
      let anyOk = false
      let anyFailed = false
      results.forEach((r, i) => {
        const { name, kind } = jobs[i]
        if (r.status !== 'fulfilled') { anyFailed = true; return }
        anyOk = true
        const rows = Array.isArray(r.value) ? r.value : []
        if (kind === 'canvas') nextCanvases[name] = rows
        else if (kind === 'documents') nextDocuments[name] = rows
        else { nextUploads[name] = rows; nextUploadsLoaded[name] = true }
      })
      if (anyOk) {
        canvases.value = nextCanvases
        documents.value = nextDocuments
        uploads.value = nextUploads
        uploadsLoaded.value = nextUploadsLoaded
        hasLoaded.value = true
        version.value++
        // Partial failure keeps what loaded and says the view may be short.
        error.value = anyFailed ? 'Some agents could not be reached; this list may be incomplete.' : null
      } else {
        error.value = 'Could not load this tab.'
      }
    } finally {
      if (token === _fetchToken) loading.value = false
    }
  }

  /** Trailing-edge debounce for push-driven refreshes. */
  function scheduleRefresh(delay = PUSH_DEBOUNCE_MS) {
    if (_debounceTimer) clearTimeout(_debounceTimer)
    _debounceTimer = setTimeout(() => {
      _debounceTimer = null
      refresh()
    }, delay)
  }

  /**
   * Send a file to a participant, then re-read that agent's inbox. Rethrows so
   * the body can show the server's named reason next to the control (#1926);
   * the inbox re-read is skipped if the chat moved on while the upload ran.
   */
  async function upload(agentName, file) {
    const portal = useClientPortalStore()
    const res = await portal.uploadDocument(agentName, file)
    if (participants.value.includes(agentName)) {
      try {
        const rows = await portal.fetchUploads(agentName)
        if (participants.value.includes(agentName)) {
          uploads.value = { ...uploads.value, [agentName]: Array.isArray(rows) ? rows : [] }
          uploadsLoaded.value = { ...uploadsLoaded.value, [agentName]: true }
          version.value++
        }
      } catch { /* the upload succeeded; the list catches up on the next read */ }
    }
    return res
  }

  /**
   * Platform sessions: the fleet-wide `/ws`. Two families say "a participant
   * just finished doing something" — loop progress (#1106) and the terminal
   * `agent_activity` that closes every execution (schedule, chat, background
   * task). Both are thin triggers; the data comes back through the
   * access-controlled REST reads above (the #918 rule). An event that cannot
   * name its agent is not ours to act on (the ent#458 review finding).
   */
  function handleWebSocketEvent(data) {
    if (!data || typeof data !== 'object') return
    if (!participants.value.length) return
    const name = data.agent_name
    if (!name || !participants.value.includes(name)) return
    const isLoop = data.type === 'loop_run_completed' || data.type === 'loop_completed'
    const isTerminalActivity = data.type === 'agent_activity'
      && typeof data.activity_state === 'string' && data.activity_state !== 'started'
    if (isLoop || isTerminalActivity) scheduleRefresh()
  }

  function clear() {
    if (_debounceTimer) { clearTimeout(_debounceTimer); _debounceTimer = null }
    participants.value = []
    feeds.value = { canvas: false, files: false }
    canvases.value = {}
    documents.value = {}
    uploads.value = {}
    uploadsLoaded.value = {}
    hasLoaded.value = false
    loading.value = false
    error.value = null
    _fetchToken++
  }

  return {
    participants, feeds, canvases, documents, uploads, uploadsLoaded,
    hasLoaded, loading, error, version, canvasCount, documentCount,
    setParticipants, setFeeds, refresh, scheduleRefresh, upload,
    handleWebSocketEvent, clear,
  }
})
