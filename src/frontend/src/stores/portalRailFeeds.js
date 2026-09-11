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
 *     backend (`_read_inbox`, a docker exec) and is fetched only on request —
 *     while Files is the open active tab, after an upload from ANY surface
 *     (#2582, via `noteUpload`), and after a delete. It DOES signal since
 *     #2582: `portalRail.js::filesSignalItems` projects it onto the same
 *     `created_at` key the Files dot already reads, so the two collections stay
 *     separate and only the SIGNAL merges them;
 *   * push-driven refreshes are DEBOUNCED (trailing 2s, capped at 4s): a
 *     100-run loop emits 100 `loop_run_completed` events, and each would
 *     otherwise cost every participant a canvas + documents round trip. The cap
 *     is what keeps the trailing edge honest once a high-frequency writer
 *     (ent#532's canvas triggers) shares the timer — see `scheduleRefresh`.
 *
 * Every request goes through the client-portal store's existing fetchers
 * (`portalHttp` + the portal auth header), so the roster gate and the
 * server-side audience / inbox narrowing are exactly the drawer's and the
 * agent page's.
 */
const PUSH_DEBOUNCE_MS = 2000
// ent#532 — how long a pending read may be deferred by a re-arming burst.
// DERIVED from the debounce so the two cannot drift apart.
const PUSH_MAX_WAIT_MS = 2 * PUSH_DEBOUNCE_MS

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
  let _debounceSince = 0      // ent#532 — when the current burst started
  // #2582 — `noteUpload`'s coalescing state. Leading AND trailing: a second
  // note that arrives while a read is in flight must NOT join that read (its
  // listing was snapshotted before the second file landed), it must queue one
  // more read after it. `_notePending` is the per-agent trailing set.
  const _noteInFlight = new Set()
  const _notePending = new Set()
  // The chat's identity. Bumped ONLY by a participant change or a clear —
  // never by `refresh()` — because an inbox read must not be cancelled just
  // because some other fetch cycle started.
  let _scopeToken = 0
  // agent -> monotonic counter of inbox reads. This, not `_fetchToken`, is what
  // orders `refresh`'s upload listing against `noteUpload`'s.
  //
  // A single shared token cannot do this job, and getting it wrong is silent:
  // a room's drop fans one file out to THREE agents, so three `noteUpload`s run
  // concurrently, and under one counter each would invalidate the last — two of
  // the three listings discarded, the earlier agents left stale. The two
  // questions are genuinely different ("has the chat moved on?" is global;
  // "is this agent's listing still the newest?" is per agent), so they need two
  // counters. Found by the room fan-out test, not by reading.
  const _uploadEpoch = new Map()

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
    _scopeToken++
    _noteInFlight.clear()
    _notePending.clear()
    _uploadEpoch.clear()
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
    // Snapshot the per-agent inbox epochs BEFORE the awaits. An upload noted
    // while this refresh is in flight makes this refresh's listing for that
    // agent stale on arrival — and `nextUploads` is rebuilt from a map read
    // AFTER the awaits, so without this the stale rows win silently.
    const epochAt = new Map(_uploadEpoch)
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
        else if ((_uploadEpoch.get(name) || 0) === (epochAt.get(name) || 0)) {
          nextUploads[name] = rows
          nextUploadsLoaded[name] = true
        }
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

  /**
   * Trailing-edge debounce for push-driven refreshes, with a max wait (ent#532).
   *
   * The trailing edge is the point: the LAST event of a burst must be the one
   * the read reflects, which is why this is not leading-edge. But a pure
   * trailing debounce re-arms on every call, so under a stream of events closer
   * together than the delay it fires only when the stream ENDS. That was fine
   * while every caller fired once per execution (`loop_*`, terminal
   * `agent_activity`). It is not fine for canvas writes — `patch_canvas` during
   * a streaming run, the voice panel writing per tool call — and because this
   * timer is SHARED, an uncapped burst would also defer the loop/activity reads
   * that ride it, making a shipped signal slower.
   *
   * So a pending read is never deferred past `PUSH_MAX_WAIT_MS` from the FIRST
   * event of the burst: a sustained stream refreshes on that bound instead of
   * never. A single event is unaffected — `_debounceSince` is `now`, so the
   * wait is the full delay, byte for byte what it was.
   */
  function scheduleRefresh(delay = PUSH_DEBOUNCE_MS) {
    const now = Date.now()
    if (!_debounceTimer) _debounceSince = now
    const wait = Math.max(0, Math.min(delay, PUSH_MAX_WAIT_MS - (now - _debounceSince)))
    if (_debounceTimer) clearTimeout(_debounceTimer)
    _debounceTimer = setTimeout(() => {
      _debounceTimer = null
      refresh()
    }, wait)
  }

  /**
   * Re-read ONE participant's inbox after a file landed in it (#2582).
   *
   * Three properties, each of which a simpler shape gets wrong:
   *
   *   * it takes a per-agent EPOCH, which `refresh()` snapshots before its
   *     awaits. Without that ordering a `refresh({uploads: true})` issued
   *     before the upload but resolving after this read clobbers the fresh
   *     listing with the pre-upload one — and it rebuilds `nextUploads` from a
   *     map read after its own awaits, so the clobber is silent. Per AGENT and
   *     not one shared counter: a room's drop runs three of these concurrently
   *     for three different agents, and a shared counter lets each invalidate
   *     the last.
   *   * it coalesces LEADING and TRAILING. A note arriving while a read is in
   *     flight cannot join that read: the listing was taken before the newer
   *     file existed, which is defect 1 reproduced exactly. It queues one more
   *     read instead.
   *   * it is a no-op for an agent that is not a participant, and it re-checks
   *     the chat's identity AFTER the await, because the chat can switch
   *     mid-read.
   */
  async function noteUpload(agentName) {
    if (!agentName || !participants.value.includes(agentName)) return
    if (_noteInFlight.has(agentName)) { _notePending.add(agentName); return }
    _noteInFlight.add(agentName)
    const portal = useClientPortalStore()
    const scope = _scopeToken
    const epoch = (_uploadEpoch.get(agentName) || 0) + 1
    _uploadEpoch.set(agentName, epoch)
    try {
      const rows = await portal.fetchUploads(agentName)
      if (scope !== _scopeToken) return                    // the chat moved on
      if (_uploadEpoch.get(agentName) !== epoch) return    // a newer read of THIS inbox won
      if (!participants.value.includes(agentName)) return
      uploads.value = { ...uploads.value, [agentName]: Array.isArray(rows) ? rows : [] }
      uploadsLoaded.value = { ...uploadsLoaded.value, [agentName]: true }
      version.value++
    } catch { /* the upload succeeded; the list catches up on the next read */
    } finally {
      _noteInFlight.delete(agentName)
      if (_notePending.delete(agentName)) void noteUpload(agentName)
    }
  }

  /**
   * Send a file to a participant, then re-read that agent's inbox. Rethrows so
   * the body can show the server's named reason next to the control (#1926).
   *
   * The re-read is NOT this function's. `uploadDocument` queues the agent for
   * the rail owner (#2582) and the owner drains it — so calling `noteUpload`
   * here as well does not make the listing safer, it just buys a SECOND docker
   * exec: the drain's note arrives while this one is in flight, becomes the
   * trailing re-fire, and every drop-zone upload costs two container reads
   * instead of one (a four-file batch, eight). Send, and let the one funnel
   * that every upload surface already shares do the telling.
   */
  async function upload(agentName, file) {
    const portal = useClientPortalStore()
    return portal.uploadDocument(agentName, file)
  }

  /**
   * Platform sessions: the fleet-wide `/ws`. Three families reach here. Two say
   * "a participant just finished doing something" — loop progress (#1106) and
   * the terminal `agent_activity` that closes every execution (schedule, chat,
   * background task). The third (ent#532) says it directly: `canvas_updated`
   * and `file_shared`, emitted beside the write, for the case the other two
   * miss entirely — a canvas rewritten with no execution behind it, which
   * otherwise waited for the next turn or tab open. All are thin triggers; the
   * data comes back through the access-controlled REST reads above (the #918
   * rule). An event that cannot name its agent is not ours to act on (the
   * ent#458 review finding).
   */
  function handleWebSocketEvent(data) {
    if (!data || typeof data !== 'object') return
    if (!participants.value.length) return
    const name = data.agent_name
    if (!name || !participants.value.includes(name)) return
    const isLoop = data.type === 'loop_run_completed' || data.type === 'loop_completed'
    const isTerminalActivity = data.type === 'agent_activity'
      && typeof data.activity_state === 'string' && data.activity_state !== 'started'
    // ent#532: the backend now says so directly — a canvas write or a new
    // share, ids only. Same debounced re-read; nothing is rendered from the
    // event itself.
    const isFeedTrigger = data.type === 'canvas_updated' || data.type === 'file_shared'
    if (isLoop || isTerminalActivity || isFeedTrigger) scheduleRefresh()
  }

  function clear() {
    if (_debounceTimer) { clearTimeout(_debounceTimer); _debounceTimer = null }
    _noteInFlight.clear()
    _notePending.clear()
    _uploadEpoch.clear()
    _scopeToken++
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
    setParticipants, setFeeds, refresh, scheduleRefresh, upload, noteUpload,
    handleWebSocketEvent, clear,
  }
})
