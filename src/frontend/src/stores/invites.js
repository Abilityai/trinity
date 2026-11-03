/**
 * Invitations (trinity-enterprise#1054): invite a person by email, and the
 * record of who accepted which terms version, when.
 *
 * Admin-only. Every write is a grant (an invitation lets a person sign in),
 * so the routes take an interactive admin session and audit each one.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

const BASE = '/api/enterprise/user-management'

export const useInvitesStore = defineStore('invites', () => {
  const invites = ref([])
  // The latest acceptance per email, `{ terms_version, accepted_at }`. Empty
  // when the read failed: the invitations still show, the version reads "—".
  const acceptanceByEmail = ref({})
  const hasLoaded = ref(false)
  const loading = ref(false)
  const error = ref(null)

  async function load() {
    loading.value = true
    try {
      const [inv, acc] = await Promise.allSettled([
        api.get(`${BASE}/invites`),
        api.get(`${BASE}/terms/acceptances`),
      ])
      if (inv.status === 'rejected') throw inv.reason
      invites.value = inv.value.data || []
      const latest = {}
      for (const row of (acc.status === 'fulfilled' ? acc.value.data : null) || []) {
        const key = (row.email || '').toLowerCase()
        if (!latest[key] || row.accepted_at > latest[key].accepted_at) latest[key] = row
      }
      acceptanceByEmail.value = latest
      hasLoaded.value = true
      error.value = null
    } catch (e) {
      error.value = e
    } finally {
      loading.value = false
    }
  }

  /** `{ email, name, role, expires_days }` → the invitation (with `email_sent`). */
  async function create(body) {
    const { data } = await api.post(`${BASE}/invites`, body)
    await load()
    return data
  }

  async function revoke(id) {
    await api.delete(`${BASE}/invites/${encodeURIComponent(id)}`)
    await load()
  }

  async function resend(id) {
    const { data } = await api.post(`${BASE}/invites/${encodeURIComponent(id)}/resend`)
    await load()
    return data
  }

  return { invites, acceptanceByEmail, hasLoaded, loading, error, load, create, revoke, resend }
})
