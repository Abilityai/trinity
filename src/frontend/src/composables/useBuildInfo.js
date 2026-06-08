import { ref, computed } from 'vue'
import api from '@/api'

/**
 * #926 build-info composable.
 *
 * One-shot fetch of `GET /api/version` cached in module scope: build
 * metadata can't change at runtime, so every consumer (NavBar chip,
 * Settings panel, future "About" dialog) reads the same singleton.
 *
 * Returned refs are the cache itself — mutating them would corrupt
 * the singleton. Treat as read-only. `load()` is idempotent; calling
 * it more than once returns the same in-flight Promise.
 */

const info = ref(null)
const loading = ref(false)
const error = ref(null)
let inFlight = null

async function load() {
  if (info.value) return info.value
  if (inFlight) return inFlight
  loading.value = true
  error.value = null
  inFlight = (async () => {
    try {
      const { data } = await api.get('/api/version')
      info.value = data
      return data
    } catch (err) {
      error.value = err
      throw err
    } finally {
      loading.value = false
      inFlight = null
    }
  })()
  return inFlight
}

// True iff every build-provenance field is the literal "unknown" sentinel
// (no VERSION file COPY'd, no start.sh-exported git args). Drives the
// "Build metadata not available" guidance in the dialog (#958).
const isMissing = computed(() => {
  if (!info.value) return false
  const fields = [
    info.value.version,
    info.value.git_commit,
    info.value.git_branch,
    info.value.git_commit_subject,
    info.value.git_commit_timestamp,
    info.value.build_date,
  ]
  return fields.every((f) => !f || f === 'unknown')
})

export function useBuildInfo() {
  return { info, loading, error, isMissing, load }
}
