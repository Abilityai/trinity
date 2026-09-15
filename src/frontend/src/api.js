/**
 * API Client
 *
 * Provides a pre-configured axios instance with authentication headers
 * and request deduplication for GET requests (PERF-269).
 */

import axios from 'axios'
import { notifyPlatformUnauthorized, readStoredToken } from '@/utils/platformSession'

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
    const token = readStoredToken()
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
//
// #2791: this used to be the THIRD logout implementation — it removed `token`
// (leaving `auth0_user` behind), hard-reloaded to `/login` with no server-side
// revoke, and carried its own copy of the bounce predicate. What "logged out"
// meant depended on which transport happened to 401 first.
//
// It now reports to the one handler (`utils/platformSession.js`), which owns the
// verdict AND the reaction — including the `stale` arm, without which this
// interceptor would still delete a freshly re-logged-in session's token.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) notifyPlatformUnauthorized(error)
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
