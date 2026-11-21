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

// `force` re-asks a read the server refused: until a person accepts an
// instance's terms of use every route answers 403 (trinity-enterprise#1327).
async function load(force = false) {
  if (info.value && !force) return info.value
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

// Display version with semver build metadata stripped: the build-stamped
// VERSION is "0.6.0+g<sha>" (#993), but every UI surface already shows the
// commit as its own field — repeating the sha inside the version string
// reads as a duplicate (e.g. "v0.6.0+gcccac44d · cccac44d").
const displayVersion = computed(() => {
  const v = info.value?.version
  if (!v || v === 'unknown') return v
  return v.split('+')[0]
})

export function useBuildInfo() {
  return { info, loading, error, isMissing, displayVersion, load }
}
