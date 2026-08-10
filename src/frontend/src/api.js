/**
 * API Client
 *
 * Provides a pre-configured axios instance with authentication headers
 * and request deduplication for GET requests (PERF-269).
 */

import axios from 'axios'

// PERF-269: In-flight request deduplication map
// Key: "GET:/api/agents/context-stats" → Value: Promise
const inflightRequests = new Map()

// Create axios instance
const api = axios.create({
  baseURL: '',
  timeout: 30000,
})

// Add auth token to requests
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// Handle auth errors
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // #138: an external client on the workspace manages its own
      // (verified-email) session and must never be bounced to the operator
      // /login by a stale operator JWT — let that code handle its own 401.
      // ent#357: an internal user's workspace session IS the platform session,
      // so they DO get bounced. Same path, two session kinds — discriminate on
      // the portal token, not the URL.
      const path = window.location.pathname
      const onWorkspace = path.startsWith('/workspace') || path.startsWith('/portal')
      const externalClient = !!localStorage.getItem('trinity.portalToken')
      if (!(onWorkspace && externalClient)) {
        // Token expired or invalid - redirect to login
        localStorage.removeItem('token')
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  }
)

/**
 * Deduplicated GET request — if an identical GET is already in-flight,
 * returns the existing promise instead of firing a new request.
 * Non-GET requests pass through normally.
 */
const originalGet = api.get.bind(api)
api.get = function deduplicatedGet(url, config) {
  // Build a cache key from URL + params
  const params = config?.params ? JSON.stringify(config.params) : ''
  const key = `GET:${url}:${params}`

  if (inflightRequests.has(key)) {
    return inflightRequests.get(key)
  }

  const promise = originalGet(url, config).finally(() => {
    inflightRequests.delete(key)
  })

  inflightRequests.set(key, promise)
  return promise
}

export default api
