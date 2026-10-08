/**
 * Agent assignments — who an agent serves (trinity-enterprise#810).
 *
 * The domain store over the ent#500 endpoints under `/api/enterprise/assignments`
 * (enterprise-gated, writes admin-only). One roster per agent, keyed by name,
 * so switching agents never shows the previous one's people.
 *
 * Writes re-read the roster rather than patching it locally: the server decides
 * the view (display names, consent, drift), and a reload is one cheap GET.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { replacePrimarySteps, writeError } from '../utils/assignments'

const base = (agent) => `/api/enterprise/assignments/agents/${encodeURIComponent(agent)}`

export const useAssignmentsStore = defineStore('assignments', () => {
  // agentName -> { roster, hasLoaded, error, lastLoadedAt }
  const byAgent = ref({})
  // The instance users an admin can pick from (GET /api/users is admin-only).
  const users = ref([])
  const usersLoaded = ref(false)
  const usersError = ref(null)
  // ent#817: the seats each agent's canon defines — agentName -> the
  // CanonRolesRead answer. Fail-soft: a failed read is a named reason.
  const canonRoles = ref({})

  function entry(agent) {
    if (!byAgent.value[agent]) {
      byAgent.value[agent] = { roster: null, hasLoaded: false, error: null, lastLoadedAt: null }
    }
    return byAgent.value[agent]
  }

  // Read without creating — safe inside a computed.
  const EMPTY = Object.freeze({ roster: null, hasLoaded: false, error: null, lastLoadedAt: null })
  function peek(agent) {
    return byAgent.value[agent] || EMPTY
  }

  async function load(agent) {
    const e = entry(agent)
    try {
      const { data } = await api.get(base(agent))
      e.roster = data
      e.hasLoaded = true
      e.error = null
      e.lastLoadedAt = Date.now()
    } catch (err) {
      // A failed REFRESH keeps the roster on screen; the section says it is stale.
      e.error = writeError(err, "Couldn't load who this agent serves.")
    }
    return e
  }

  async function loadUsers() {
    try {
      const { data } = await api.get('/api/users')
      users.value = (Array.isArray(data) ? data : []).filter((u) => !u.suspended_at)
      usersLoaded.value = true
      usersError.value = null
    } catch (err) {
      usersError.value = writeError(err, "Couldn't load the instance's users.")
    }
  }

  async function run(agent, step) {
    if (step.op === 'create') return api.post(base(agent), step.body)
    if (step.op === 'update') return api.patch(`${base(agent)}/${encodeURIComponent(step.id)}`, step.body)
    return api.delete(`${base(agent)}/${encodeURIComponent(step.id)}`)
  }

  async function add(agent, body) {
    await run(agent, { op: 'create', body })
    await load(agent)
  }

  async function setKind(agent, id, kind) {
    await run(agent, { op: 'update', id, body: { kind } })
    await load(agent)
  }

  async function remove(agent, id) {
    await run(agent, { op: 'delete', id })
    await load(agent)
  }

  /** ent#817: the seats the agent's canon defines (id, title, updated). Never throws. */
  async function loadCanonRoles(agent) {
    try {
      const { data } = await api.get(`/api/agents/${encodeURIComponent(agent)}/canon/roles`)
      canonRoles.value = { ...canonRoles.value, [agent]: data }
    } catch (err) {
      canonRoles.value = {
        ...canonRoles.value,
        [agent]: { roles: [], unavailable: null, reason: 'unreadable', message: writeError(err, "Couldn't read this agent's role files.") },
      }
    }
  }

  /** ent#811: set a row's seat, or clear it with `null` (an explicit null clears). */
  async function setRole(agent, id, roleId) {
    await run(agent, { op: 'update', id, body: { role_id: roleId || null } })
    await load(agent)
  }

  /** ent#811 (R50a): record the agent itself as the holder of a seat. */
  async function setSeatHolder(agent, roleId) {
    await api.put(`${base(agent)}/seat`, { role_id: roleId })
    await load(agent)
  }

  async function clearSeatHolder(agent) {
    await api.delete(`${base(agent)}/seat`)
    await load(agent)
  }

  /**
   * Replace the primary as one flow (see `replacePrimarySteps`). If the new
   * primary cannot be written, step one is undone so the agent keeps the
   * primary it had, and the server's reason is rethrown for the form.
   */
  async function replacePrimary(agent, opts) {
    const roster = entry(agent).roster
    const { steps, rollback } = replacePrimarySteps(roster?.assignments || [], opts)
    let done = 0
    try {
      for (const step of steps) {
        await run(agent, step)
        done += 1
      }
    } catch (err) {
      if (done > 0) {
        for (const step of rollback) {
          try { await run(agent, step) } catch { /* reported below by the reload */ }
        }
      }
      await load(agent)
      throw err
    }
    await load(agent)
  }

  return {
    byAgent, users, usersLoaded, usersError, canonRoles, loadCanonRoles,
    entry, peek, load, loadUsers, add, setKind, remove, replacePrimary,
    setRole, setSeatHolder, clearSeatHolder,
  }
})
