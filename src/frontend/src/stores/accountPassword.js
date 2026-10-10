import { defineStore } from 'pinia'
import api from '../api'
import { TOKEN_KEY } from '@/utils/platformSession'
import { useAuthStore } from './auth'

/**
 * Changing your own password (trinity-enterprise#709).
 *
 * Two calls, both on the caller's OWN account: `verify` (step 1 — is the
 * current password right, and does a second-factor step follow?) and
 * `change` (re-checks everything and writes). Refusals come back as
 * `{ code, message, errors? }` — the backend names the failing rule.
 */
function refusal(err) {
  const detail = err?.response?.data?.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail) && detail.code) {
    return { code: detail.code, message: detail.message || 'The password could not be changed.', errors: detail.errors || [] }
  }
  return {
    code: 'request_failed',
    message: "The request didn't go through. Check your connection and try again.",
    errors: [],
  }
}

export const useAccountPasswordStore = defineStore('accountPassword', {
  state: () => ({
    submitting: false,
  }),

  actions: {
    async verifyCurrent(currentPassword) {
      this.submitting = true
      try {
        const { data } = await api.post('/api/users/me/password/verify', { current_password: currentPassword })
        return { ok: true, data }
      } catch (err) {
        return { ok: false, error: refusal(err) }
      } finally {
        this.submitting = false
      }
    },

    async change({ currentPassword, newPassword, confirmPassword, secondFactorCode }) {
      this.submitting = true
      try {
        const body = {
          current_password: currentPassword,
          new_password: newPassword,
          confirm_password: confirmPassword,
        }
        if (secondFactorCode) body.second_factor_code = secondFactorCode
        const { data } = await api.put('/api/users/me/password', body)
        // Every other session was signed out; this tab continues on the fresh
        // token. Writing storage first and adopting it is the one-source rule
        // (#2791) — sibling tabs of this browser converge on it too.
        if (data?.access_token) {
          try { localStorage.setItem(TOKEN_KEY, data.access_token) } catch { /* private mode */ }
          useAuthStore().adoptStoredSession()
        }
        return { ok: true, data }
      } catch (err) {
        return { ok: false, error: refusal(err) }
      } finally {
        this.submitting = false
      }
    },
  },
})
