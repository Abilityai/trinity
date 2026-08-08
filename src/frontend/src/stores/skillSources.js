/**
 * Skill sources store (ent#237) — the admin-side skills-library repos.
 *
 * Deliberately a SEPARATE domain from `stores/skills.js`: that store owns
 * per-agent skill assignment (an owner surface), this one owns which
 * repositories the platform syncs from (an admin surface, and the grant action
 * of requirements §21.1.2). They share no state, and keeping them apart also
 * avoids colliding with the Skills-tab rebuild in ent#235.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useSkillSourcesStore = defineStore('skillSources', () => {
  // --- state ---
  const sources = ref([])
  const skillCount = ref(0)
  // Skills that exist in more than one source. Non-zero means someone is
  // running a different source's version of a skill than that source intended,
  // so it gets surfaced rather than buried (§21.1.1).
  const shadowedCount = ref(0)
  const loading = ref(false)
  const error = ref(null)
  const busyId = ref(null)

  // --- getters ---
  // The array arrives in resolution order from the backend (priority ASC, then
  // created_at ASC), so index 0 wins a name collision. Never re-sort it here:
  // the ordering is a backend contract, and a client-side sort would silently
  // misreport which source actually wins.
  const resolutionOrder = computed(() => sources.value)
  const hasDefault = computed(() => sources.value.some(s => s.is_default))
  const failing = computed(() =>
    sources.value.filter(s => s.enabled && s.last_sync_status === 'failed')
  )

  // --- actions ---
  async function fetch () {
    loading.value = true
    error.value = null
    try {
      const res = await api.get('/api/skills/sources')
      sources.value = res.data.sources || []
      skillCount.value = res.data.skill_count || 0
      shadowedCount.value = res.data.shadowed_count || 0
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to load skill sources'
      // State is left untouched on failure rather than cleared — blanking the
      // list would read as "no sources configured", which is a different and
      // alarming thing to show an admin.
    } finally {
      loading.value = false
    }
  }

  async function create (payload) {
    error.value = null
    try {
      await api.post('/api/skills/sources', payload)
      await fetch()
      return true
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to add source'
      return false
    }
  }

  async function update (id, fields) {
    error.value = null
    busyId.value = id
    try {
      await api.put(`/api/skills/sources/${encodeURIComponent(id)}`, fields)
      await fetch()
      return true
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to update source'
      return false
    } finally {
      busyId.value = null
    }
  }

  async function remove (id) {
    error.value = null
    busyId.value = id
    try {
      await api.delete(`/api/skills/sources/${encodeURIComponent(id)}`)
      await fetch()
      return true
    } catch (e) {
      error.value = e.response?.data?.detail || 'Failed to remove source'
      return false
    } finally {
      busyId.value = null
    }
  }

  async function sync (id) {
    error.value = null
    busyId.value = id
    try {
      await api.post(`/api/skills/sources/${encodeURIComponent(id)}/sync`)
      await fetch()
      return true
    } catch (e) {
      // A refused moved tag arrives here (§21.1.2). Surfacing the backend's
      // message verbatim matters — it names the tag and tells the admin to
      // point at a new one, which a generic "sync failed" would hide.
      error.value = e.response?.data?.detail || 'Sync failed'
      return false
    } finally {
      busyId.value = null
    }
  }

  async function syncAll () {
    error.value = null
    loading.value = true
    try {
      await api.post('/api/skills/library/sync')
      await fetch()
      return true
    } catch (e) {
      error.value = e.response?.data?.detail || 'Sync failed'
      return false
    } finally {
      loading.value = false
    }
  }

  return {
    sources, skillCount, shadowedCount, loading, error, busyId,
    resolutionOrder, hasDefault, failing,
    fetch, create, update, remove, sync, syncAll,
  }
})
