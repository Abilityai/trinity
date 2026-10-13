/**
 * Platform accounts (trinity-enterprise#837): a person's platform role, which
 * applies on every agent, and an account made before their first sign-in.
 *
 * Admin-only writes. The components show the controls only to an admin whose
 * role the server has verified; the routes are the boundary (an interactive
 * admin only, and audited).
 */
import { defineStore } from 'pinia'
import api from '../api'

export const useUsersStore = defineStore('users', () => {
  /** Change a person's platform role (`PUT /api/users/{username}/role`). */
  async function setPlatformRole(username, role) {
    const { data } = await api.put(`/api/users/${encodeURIComponent(username)}/role`, { role })
    return data
  }

  /**
   * Give a person a platform account now. `role` is the role for a NEW member;
   * an earlier deliberate allow-list entry keeps its own, and the answer names
   * the role the account got (`{ id, username, email, role, whitelist, email_sent }`).
   */
  async function provisionMember(email, role = 'user') {
    const { data } = await api.post('/api/enterprise/user-management/members', { email, role })
    return data
  }

  return { setPlatformRole, provisionMember }
})
