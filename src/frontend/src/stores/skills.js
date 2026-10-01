/**
 * Skills domain store (#235).
 *
 * The skills machinery has shipped across three planes (#182 distribute/place/
 * expose, #183 package injection with a per-skill result contract), but nothing
 * rendered it: the Agent Detail Skills tab was hidden and assignment was
 * REST/MCP-only. This store backs the tab that makes it a product surface.
 *
 * Scope is distribute + place — browse the library, assign to an agent, and see
 * honestly what actually landed. Exposure curation (what an agent advertises
 * outward) is #178 and deliberately absent.
 *
 * All HTTP goes through the shared `api` client (Invariant #7). The panel this
 * replaces called `axios` directly with a hand-built auth header, which meant it
 * silently bypassed the interceptor every other call in the app relies on.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

// #2703 — a bulk PUT that delivers several skills fires ONE `agent_skills_changed`,
// but a Library click storm fires many; consumers refetch once per burst.
export const SKILLS_CHANGED_DEBOUNCE_MS = 300
// #2703 — the server awaits delivery for up to 20 s (SKILL_DELIVERY_BUDGET_SECONDS)
// before answering `in_progress`; the axios default is 30 s, so leave margin
// for the Docker read + the response rather than racing the budget.
const ASSIGN_TIMEOUT_MS = 45000

export const useSkillsStore = defineStore('skills', () => {
  const library = ref([])              // SkillInfo[] — the synced library
  const libraryStatus = ref(null)      // {configured, url, branch, last_sync, commit_sha, skill_count}
  const assigned = ref([])             // AgentSkill[] for the current agent
  const agentName = ref(null)

  const loading = ref(false)
  const saving = ref(false)
  const injecting = ref(false)
  const error = ref(null)

  // Per-skill outcome of the LAST injection: {name: {success, status, files_written, error, warnings}}
  // Kept separate from `assigned` because assignment is durable state while an
  // injection result describes one moment — conflating them is how a UI ends up
  // showing a stale green tick.
  const injectionResults = ref({})
  const lastInjectionAt = ref(null)

  const assignedNames = computed(() => new Set(assigned.value.map(s => s.skill_name)))

  // ent#530 — skill sets. `sets` is the agent's assigned sets with their honest
  // status (GET /skill-sets); `librarySets` is what could be assigned. Both are
  // context beside the skills list, so a failed read is its own named state
  // (`setsError`) and never blanks the skills.
  const sets = ref([])
  const librarySets = ref([])
  const setsError = ref(null)
  const setsLoaded = ref(false)
  const setBusy = ref(null)          // the set name being written, not a boolean
  const setWriteError = ref(null)
  const lastSetResult = ref(null)    // {set_name, members_added, status, suggested_schedules}
  const removalDeferred = ref(null)  // the set whose unassign could not remove its members yet

  // Names present ONLY because an assigned set names them. The bulk PUT sends
  // the individual list; these are shown ticked-and-locked, since unticking one
  // cannot remove it while its set is assigned.
  const setOnlyNames = computed(() => new Set(
    assigned.value.filter(s => s.individual === false).map(s => s.skill_name)
  ))
  const individualNames = computed(() => new Set(
    assigned.value.filter(s => s.individual !== false).map(s => s.skill_name)
  ))
  function viaSets(name) {
    return assigned.value.find(s => s.skill_name === name)?.via_sets || []
  }
  const assignableSets = computed(() => {
    const held = new Set(sets.value.map(x => x.name))
    return librarySets.value.filter(x => !held.has(x.name))
  })

  // #2703 — the delivery report of the LAST save: {status, reason?, skills:{...}}
  // — see `utils/skillDelivery.js` for the wording. Kept beside
  // `injectionResults` for the same reason that one is separate from
  // `assigned`: a delivery describes one moment, not durable state.
  const lastDelivery = ref(null)

  // #2703 — per-agent "the listing changed" ticks, driven by the thin
  // `agent_skills_changed` WS trigger. Consumers that own a `loadPlaybooks()`
  // (ChatPanel, PlaybooksPanel, usePlaybookAutocomplete) watch their agent's
  // entry and refetch through the access-controlled route; the payload carries
  // no skill names (the #918 / ent#305 rule). Debounced per agent here so the
  // consumers stay dumb.
  const changedAt = ref({})
  const _pendingTicks = new Map()
  function noteSkillsChanged(name) {
    if (!name) return
    if (_pendingTicks.has(name)) clearTimeout(_pendingTicks.get(name))
    _pendingTicks.set(name, setTimeout(() => {
      _pendingTicks.delete(name)
      changedAt.value = { ...changedAt.value, [name]: Date.now() }
    }, SKILLS_CHANGED_DEBOUNCE_MS))
  }

  /** Library entries that are assigned to this agent, joined with their contract. */
  const assignedSkills = computed(() =>
    library.value.filter(s => assignedNames.value.has(s.name))
  )

  // #2914 — names whose assignment row carries `delivery_status: 'conflict'`:
  // the agent has its own `.claude/skills/<name>/`, the platform refused to
  // write over it, and the agent's copy is what runs. Durable (it rides the
  // row, not the session's last injection), so the tab shows it on load.
  const conflictNames = computed(() => new Set(
    assigned.value.filter(s => s.delivery_status === 'conflict').map(s => s.skill_name)
  ))

  /**
   * Why the tab has nothing to show, as a single discriminator so the panel
   * never renders a dead empty state (explicit AC).
   */
  const emptyReason = computed(() => {
    if (!libraryStatus.value) return null
    if (!libraryStatus.value.configured) return 'library_unconfigured'
    if (library.value.length === 0) return 'library_empty'
    if (assigned.value.length === 0) return 'none_assigned'
    return null
  })

  function setAgent(name) {
    if (agentName.value !== name) {
      agentName.value = name
      assigned.value = []
      sets.value = []
      setsLoaded.value = false
      lastSetResult.value = null
      injectionResults.value = {}
      lastInjectionAt.value = null
    }
  }

  async function load(name) {
    setAgent(name)
    loading.value = true
    error.value = null
    try {
      // Library reads are shared platform state; the assignment read is
      // per-agent. Fetched together so the tab renders in one paint.
      const [status, mine] = await Promise.all([
        api.get('/api/skills/library/status'),
        api.get(`/api/agents/${name}/skills`),
      ])
      libraryStatus.value = status.data
      assigned.value = mine.data || []

      // Only ask for the list once we know a library exists. The previous
      // shape — `.catch(() => ({data: []}))` — swallowed EVERY error, so a 500
      // or an auth failure rendered as "the library is configured but has no
      // skills yet": a confident, wrong empty state pointing the operator at
      // the wrong problem. An unconfigured library is a known empty state and
      // is already reported by `status.configured`; anything else is a real
      // failure and must say so.
      if (libraryStatus.value?.configured) {
        const lib = await api.get('/api/skills/library')
        library.value = lib.data || []
        await loadSets()
      } else {
        library.value = []
      }
    } catch (e) {
      error.value = e?.response?.data?.detail || 'Could not load skills'
    } finally {
      loading.value = false
    }
  }

  /** ent#530 — never throws: a set outage is named in `setsError`, the skills still render. */
  async function loadSets() {
    const name = agentName.value
    try {
      const [mine, lib] = await Promise.all([
        // probe: the Skills tab is where missing credentials are flagged (one exec per load).
        api.get(`/api/agents/${name}/skill-sets`, { params: { probe: true } }),
        api.get('/api/skills/library/sets'),
      ])
      if (name !== agentName.value) return
      sets.value = mine.data || []
      librarySets.value = lib.data || []
      setsError.value = null
      setsLoaded.value = true
    } catch (e) {
      if (name !== agentName.value) return
      setsError.value = detailText(e, 'Could not load skill sets')
    }
  }

  async function _refreshRows() {
    try {
      const { data } = await api.get(`/api/agents/${agentName.value}/skills`)
      assigned.value = data || []
    } catch { /* keep the previous rows; loadSets reports its own failure */ }
    await loadSets()
  }

  async function assignSet(setName) {
    setBusy.value = setName
    setWriteError.value = null
    try {
      const { data } = await api.post(
        `/api/agents/${agentName.value}/skill-sets/${encodeURIComponent(setName)}`,
        {}, { timeout: ASSIGN_TIMEOUT_MS },
      )
      lastSetResult.value = data || null
      lastDelivery.value = data?.delivery ?? null
      await _refreshRows()
      return true
    } catch (e) {
      setWriteError.value = detailText(e, `Could not assign set ${setName}`)
      return false
    } finally {
      setBusy.value = null
    }
  }

  async function unassignSet(setName) {
    setBusy.value = setName
    setWriteError.value = null
    try {
      const { data } = await api.delete(`/api/agents/${agentName.value}/skill-sets/${encodeURIComponent(setName)}`)
      if (lastSetResult.value?.set_name === setName) lastSetResult.value = null
      removalDeferred.value = data?.removal_deferred ? setName : null
      await _refreshRows()
      return true
    } catch (e) {
      setWriteError.value = detailText(e, `Could not unassign set ${setName}`)
      return false
    } finally {
      setBusy.value = null
    }
  }

  /** Bulk save — the whole assignment set in one PUT (AC: bulk save supported). */
  async function saveAssignments(names) {
    saving.value = true
    error.value = null
    try {
      const { data: saved } = await api.put(
        `/api/agents/${agentName.value}/skills`, { skills: names }, { timeout: ASSIGN_TIMEOUT_MS },
      )
      // #2703: the PUT now delivers; `null` means nothing was added.
      lastDelivery.value = saved?.delivery ?? null
      const { data } = await api.get(`/api/agents/${agentName.value}/skills`)
      assigned.value = data || []
      return true
    } catch (e) {
      error.value = e?.response?.data?.detail || 'Could not save skill assignments'
      return false
    } finally {
      saving.value = false
    }
  }

  /**
   * Manual sync — a repair action (`force=True` server-side), so it re-injects
   * unconditionally rather than skipping version-unchanged skills.
   *
   * The result is stored per skill, NOT flattened to a boolean: #183 reports
   * `injected | unchanged | fallback | failed` plus named warnings
   * (`missing_binary:*`, `missing_env:*`, `multi_file_dropped_old_image`, …),
   * and the AC is explicit that a partial injection must never render as a
   * green check.
   */
  async function inject() {
    injecting.value = true
    error.value = null
    try {
      const { data } = await api.post(`/api/agents/${agentName.value}/skills/inject`)
      injectionResults.value = data?.results || {}
      lastInjectionAt.value = new Date().toISOString()
      // #2914: the sync also rewrites each row's durable verdict (`conflict`
      // stamped or cleared), and the conflict badge reads the ROW so it
      // survives a reload — so re-read the rows, not just this run's results.
      // Best-effort: the results above are already the honest answer.
      try {
        const { data: rows } = await api.get(`/api/agents/${agentName.value}/skills`)
        assigned.value = rows || []
      } catch { /* keep the previous rows; the injection results still render */ }
      return data
    } catch (e) {
      // 409 = an injection is already running (SkillInjectionBusy). Say so
      // rather than reporting a generic failure the operator can't act on.
      error.value = e?.response?.status === 409
        ? 'A skill sync is already running for this agent. Try again in a moment.'
        : (e?.response?.data?.detail || 'Skill sync failed')
      return null
    } finally {
      injecting.value = false
    }
  }

  function clear() {
    agentName.value = null
    assigned.value = []
    sets.value = []
    librarySets.value = []
    setsError.value = null
    setsLoaded.value = false
    setWriteError.value = null
    lastSetResult.value = null
    removalDeferred.value = null
    library.value = []
    libraryStatus.value = null
    injectionResults.value = {}
    lastInjectionAt.value = null
    lastDelivery.value = null
    error.value = null
  }

  return {
    library, libraryStatus, assigned, agentName,
    loading, saving, injecting, error,
    injectionResults, lastInjectionAt, lastDelivery,
    changedAt, noteSkillsChanged,
    assignedNames, assignedSkills, conflictNames, emptyReason,
    setAgent, load, saveAssignments, inject, clear,
    sets, librarySets, setsError, setsLoaded, setBusy, setWriteError, lastSetResult, removalDeferred,
    setOnlyNames, individualNames, assignableSets, viaSets,
    loadSets, assignSet, unassignSet,
  }
})

/** A named refusal carries `{code, message}` (ent#530); older routes a string. */
export function detailText(e, fallback) {
  const d = e?.response?.data?.detail
  if (d && typeof d === 'object') return d.message || fallback
  return d || fallback
}
