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
  const loading = ref(false)
  const error = ref(null)   // a load failure is an ERROR state, never "empty"
  const syncing = ref(false)
  const syncError = ref(null)

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
    loading.value = true
    error.value = null
    try {
      const s = await api.get('/api/skills/library/status')
      status.value = s.data
      // Only ask for the list once we know a library exists. An unconfigured
      // library is a KNOWN empty state (reported by `status.configured`);
      // anything else failing must surface as an error, never as a confident
      // wrong "no skills yet" (the no-swallow rule, stores/skills.js lesson).
      if (s.data?.configured) {
        const lib = await api.get('/api/skills/library')
        library.value = lib.data || []
      } else {
        library.value = []
      }
    } catch (e) {
      error.value = e?.response?.data?.detail || 'Could not load the skills library'
    } finally {
      loading.value = false
    }
  }

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
    library, status, loading, error, syncing, syncError,
    emptyReason, load, sync,
  }
})
