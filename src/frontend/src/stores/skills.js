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

  /** Library entries that are assigned to this agent, joined with their contract. */
  const assignedSkills = computed(() =>
    library.value.filter(s => assignedNames.value.has(s.name))
  )

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
      const [status, lib, mine] = await Promise.all([
        api.get('/api/skills/library/status'),
        // A library that isn't configured 404s/errors on list — that is an
        // empty state, not a failure, so it must not blank the whole tab.
        api.get('/api/skills/library').catch(() => ({ data: [] })),
        api.get(`/api/agents/${name}/skills`),
      ])
      libraryStatus.value = status.data
      library.value = lib.data || []
      assigned.value = mine.data || []
    } catch (e) {
      error.value = e?.response?.data?.detail || 'Could not load skills'
    } finally {
      loading.value = false
    }
  }

  /** Bulk save — the whole assignment set in one PUT (AC: bulk save supported). */
  async function saveAssignments(names) {
    saving.value = true
    error.value = null
    try {
      await api.put(`/api/agents/${agentName.value}/skills`, { skills: names })
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
    library.value = []
    libraryStatus.value = null
    injectionResults.value = {}
    lastInjectionAt.value = null
    error.value = null
  }

  return {
    library, libraryStatus, assigned, agentName,
    loading, saving, injecting, error,
    injectionResults, lastInjectionAt,
    assignedNames, assignedSkills, emptyReason,
    setAgent, load, saveAssignments, inject, clear,
  }
})
