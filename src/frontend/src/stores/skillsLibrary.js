/**
 * Fleet-level skills library store (ent#263) — backs the Library page's
 * Skills section (browse-only; assignment stays on each agent's Skills tab).
 *
 * Deliberately SEPARATE from `stores/skills.js` (the per-agent Skills tab
 * store): App.vue wraps AgentDetail in KeepAlive, so SkillsPanel's
 * `onUnmounted → clear()` never fires on nav-away (deactivated ≠ unmounted).
 * Sharing `library`/`libraryStatus`/`loading`/`error` refs would let this
 * page's writes — including a failed fetch's error — render inside the CACHED
 * per-agent Skills tab (an agent suddenly "has no skills"). A separate store
 * removes that whole coupling class. This module imports NOTHING from
 * `stores/skills.js`; the ~40 duplicated lines of fetch logic are deliberate
 * (different lifecycle exposure, different semantics: fleet browse vs
 * agent-placement join).
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useSkillsLibraryStore = defineStore('skillsLibrary', () => {
  const library = ref([])   // SkillInfo[] — the synced library (fleet browse)
  const status = ref(null)  // GET /api/skills/library/status — legacy flat
                            // fields ({configured,url,branch,cloned,last_sync,
                            // commit_sha,skill_count}); ent#237/PR #1901 keeps
                            // them verbatim and adds sources[]
  // ent#384 — GET /api/skills/assignments, the fleet skill→agents map.
  // `assignments` is {skillName: [{name, display_label}]}; a skill with no
  // holders is simply absent. `assignmentsScope` is 'all' (admin, unfiltered)
  // or 'accessible' (owned ∪ shared) and is what lets the UI word a zero
  // honestly — see the endpoint's own docstring.
  const assignments = ref({})
  const assignmentsScope = ref(null)
  const assignmentsError = ref(null)
  const assignmentsLoaded = ref(false)
  const assignmentsFetching = ref(false)

  // `fetching` is "a request is in flight"; `loading` is "there is nothing to
  // show yet". They were one flag, which is wrong for the ScanlineReveal
  // contract (§ data loading: "Loading means 'no data yet', never 'fetch in
  // flight'") — with a single flag, every revisit replays the beam over data
  // that is already on screen.
  const fetching = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)   // a load failure is an ERROR state, never "empty"
  const syncing = ref(false)
  const syncError = ref(null)

  const loading = computed(() => fetching.value && !hasLoaded.value)

  // Generation guards: the Skills panel can be unmounted and remounted by a
  // tab switch while a request is in flight, so a slow first response must not
  // land on top of a newer one.
  //
  // Two counters, not one. `loadAssignments` is independently callable (the
  // per-card retry), and sharing a counter would let that retry invalidate an
  // in-flight `load()` — whose `finally` is generation-guarded and would then
  // never clear `fetching`, stranding the flag true forever.
  let generation = 0
  let assignmentsGeneration = 0

  /**
   * Why the section has nothing to show — a FLEET-scoped 4-state
   * discriminator (error is carried separately in `error`). Do not reuse
   * `stores/skills.js`'s agent-scoped `emptyReason`: on a fleet page it would
   * report `none_assigned`, which is meaningless here.
   *   unconfigured — no library repo configured yet
   *   not_cloned   — configured but never synced (`cloned` is false)
   *   empty        — cloned, but the repo has zero skill directories
   */
  const emptyReason = computed(() => {
    if (!status.value) return null
    if (!status.value.configured) return 'unconfigured'
    if (!status.value.cloned) return 'not_cloned'
    if (library.value.length === 0) return 'empty'
    return null
  })

  async function load() {
    const mine = ++generation
    fetching.value = true
    error.value = null
    try {
      const s = await api.get('/api/skills/library/status')
      if (mine !== generation) return
      status.value = s.data
      // Only ask for the list once we know a library exists. An unconfigured
      // library is a KNOWN empty state (reported by `status.configured`);
      // anything else failing must surface as an error, never as a confident
      // wrong "no skills yet" (the no-swallow rule, stores/skills.js lesson).
      if (s.data?.configured) {
        const lib = await api.get('/api/skills/library')
        if (mine !== generation) return
        library.value = lib.data || []
        // ent#384 — awaited here, but SELF-CONTAINED: `loadAssignments`
        // swallows its own failure into `assignmentsError` and never throws,
        // so an assignments outage cannot reach this `catch` and blank the
        // library. Who-holds-what is decoration on a browse surface; the
        // library must still render (per-section failure isolation is a page
        // invariant), with the assignment rows saying so rather than reading
        // as "assigned to nobody". Keep it non-throwing — the isolation lives
        // in that function, not in this call site.
        await loadAssignments()
      } else {
        library.value = []
        assignments.value = {}
        assignmentsScope.value = null
        assignmentsLoaded.value = false
      }
      hasLoaded.value = true
    } catch (e) {
      if (mine !== generation) return
      error.value = e?.response?.data?.detail || 'Could not load the skills library'
    } finally {
      if (mine === generation) fetching.value = false
    }
  }

  /**
   * The fleet skill→agents map. Failure is contained here on purpose: it sets
   * `assignmentsError` and leaves `assignments` empty-but-UNLOADED, so the UI
   * can distinguish "nobody holds this" from "we could not find out". Those
   * two render identically if you only look at an empty map, and rendering the
   * second as the first is the confident-wrong-zero the endpoint's DB-derived
   * access set exists to prevent on the server side.
   */
  async function loadAssignments() {
    const mine = ++assignmentsGeneration
    assignmentsError.value = null
    // In-flight flag, not just bookkeeping: `AssignedAgents` renders a retry
    // control on EVERY skill card, so without it 50 cards offer 50 live
    // triggers for the same fleet-wide read.
    assignmentsFetching.value = true
    try {
      const res = await api.get('/api/skills/assignments')
      if (mine !== assignmentsGeneration) return
      assignments.value = res.data?.assignments || {}
      assignmentsScope.value = res.data?.scope || null
      assignmentsLoaded.value = true
    } catch (e) {
      if (mine !== assignmentsGeneration) return
      assignments.value = {}
      assignmentsScope.value = null
      assignmentsLoaded.value = false
      assignmentsError.value =
        e?.response?.data?.detail || 'Could not load skill assignments'
    } finally {
      if (mine === assignmentsGeneration) assignmentsFetching.value = false
    }
  }

  /**
   * Agents holding `skillName`; `[]` when nobody does.
   *
   * Sorted by the key the chips are RENDERED by (`display_label || name`), not
   * by the slug the SQL ordered on. They diverge on any fleet using labels,
   * and since the card shows only the first few before a counted overflow, a
   * slug-ordered list truncates on a key the reader cannot see — the visible
   * names look arbitrarily ordered and "+N more" hides a non-obvious set.
   */
  function agentsFor(skillName) {
    const agents = assignments.value[skillName] || []
    return [...agents].sort((a, b) =>
      (a.display_label || a.name).localeCompare(b.display_label || b.name)
    )
  }

  /**
   * Assignments whose skill is no longer in the library (ent#384).
   *
   * ent#237's documented revocation path is "cut a new tag without the
   * offending skill". After that the library listing no longer carries it, so
   * a page keyed by the listing answers "who still has it?" with silence —
   * exactly when the operator most needs the answer. The rows are still in
   * `agent_skills` and the endpoint still returns them, so surface them.
   */
  const orphanedAssignments = computed(() => {
    if (!assignmentsLoaded.value) return []
    const known = new Set(library.value.map((s) => s.name))
    return Object.entries(assignments.value)
      .filter(([name]) => !known.has(name))
      .map(([name, agents]) => ({ name, agents }))
      .sort((a, b) => a.name.localeCompare(b.name))
  })

  /**
   * Admin "Sync now". The server clones/pulls synchronously and a FIRST clone
   * can exceed api.js's 30s default timeout — so 180s here. If even that
   * elapses client-side (ECONNABORTED) the server may still have finished:
   * re-fetch status instead of claiming failure. A concurrent-sync 400 from
   * the server surfaces honestly via `syncError`.
   */
  async function sync() {
    syncing.value = true
    syncError.value = null
    try {
      await api.post('/api/skills/library/sync', {}, { timeout: 180000 })
      await load()
      return true
    } catch (e) {
      if (e?.code === 'ECONNABORTED') {
        // Client timeout ≠ server failure — trust the disk-derived status.
        await load()
        return true
      }
      syncError.value = e?.response?.data?.detail || 'Sync failed'
      return false
    } finally {
      syncing.value = false
    }
  }

  return {
    library, status, loading, fetching, hasLoaded, error, syncing, syncError,
    assignments, assignmentsScope, assignmentsError, assignmentsLoaded,
    assignmentsFetching,
    emptyReason, orphanedAssignments,
    load, loadAssignments, agentsFor, sync,
  }
})
