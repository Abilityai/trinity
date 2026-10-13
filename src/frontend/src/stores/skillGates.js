/**
 * The agent's skill-gate map (trinity-enterprise#753), as the Skills tab reads
 * and writes it (trinity-enterprise#754). Agent-scoped, like `stores/skills.js`.
 *
 * - The map loads first; the in-agent hook probe (`?probe=true`, one live
 *   `/health` read honoured only for the agent's managers) runs AFTER, in the
 *   background, so a slow agent never holds the toggles back.
 * - A write sends the selected approver KIND (never a bodiless PUT that would
 *   reset to `primary`), then re-reads the map FOR THE AGENT IT WROTE TO —
 *   AgentDetail is KeepAlive'd, so the page can move to another agent while a
 *   write is in flight (the 10-07 write-then-reload learning).
 * - Busy / error state is per skill, keyed by the lower-cased gate key (the
 *   backend lower-cases the names it stores). A PUT's `approver_unassigned`
 *   warning needs no state of its own: the map re-read after every write
 *   carries the same fact as `reachable: false`, which the gate line says.
 *
 * All HTTP goes through the shared `api` client (Invariant #7).
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { detailText } from './skills'

export const useSkillGatesStore = defineStore('skillGates', () => {
  const agentName = ref(null)
  const gates = ref([])
  const approvers = ref([])
  const hook = ref(null)

  const hasLoaded = ref(false)
  const error = ref(null)

  const busy = ref({})
  const errors = ref({})

  let readSeq = 0

  function reset() {
    gates.value = []
    approvers.value = []
    hook.value = null
    hasLoaded.value = false
    error.value = null
    busy.value = {}
    errors.value = {}
  }

  function setAgent(name) {
    if (agentName.value !== name) {
      agentName.value = name
      reset()
    }
  }

  function apply(data) {
    gates.value = Array.isArray(data?.gates) ? data.gates : []
    approvers.value = Array.isArray(data?.approvers) ? data.approvers : []
  }

  async function read(name) {
    const seq = ++readSeq
    try {
      const { data } = await api.get(`/api/agents/${name}/skill-gates`)
      if (seq !== readSeq || name !== agentName.value) return false
      apply(data)
      error.value = null
      hasLoaded.value = true
      return true
    } catch (e) {
      if (seq !== readSeq || name !== agentName.value) return false
      error.value = detailText(e, 'Could not load the approval settings')
      return false
    }
  }

  /** The in-agent hook's state. A failed probe is "no answer", never an error. */
  async function probeHook(name) {
    try {
      const { data } = await api.get(`/api/agents/${name}/skill-gates`, { params: { probe: true } })
      if (name === agentName.value) hook.value = data?.hook ?? null
    } catch {
      /* no answer: the warning stays away rather than guess */
    }
  }

  async function load(name, { probe = false } = {}) {
    setAgent(name)
    const ok = await read(name)
    if (ok && probe) probeHook(name)
  }

  function setFor(map, key, value) {
    return { ...map, [key]: value }
  }

  async function write(skill, send) {
    const name = agentName.value
    const key = String(skill).toLowerCase()
    busy.value = setFor(busy.value, key, true)
    errors.value = setFor(errors.value, key, null)
    try {
      await send(name, key)
      if (name !== agentName.value) return false
      await read(name)
      return true
    } catch (e) {
      if (name !== agentName.value) return false
      errors.value = setFor(errors.value, key, detailText(e, 'Could not change the approval setting'))
      return false
    } finally {
      if (name === agentName.value) busy.value = setFor(busy.value, key, false)
    }
  }

  /** Require approval on `skill`, approved by `approver` (a kind). */
  function setGate(skill, approver) {
    return write(skill, (name, key) =>
      api.put(`/api/agents/${name}/skill-gates/${encodeURIComponent(key)}`, { approver }))
  }

  /** Stop requiring approval on `skill` (also clears a kept gate whose skill is gone). */
  function clearGate(skill) {
    return write(skill, (name, key) =>
      api.delete(`/api/agents/${name}/skill-gates/${encodeURIComponent(key)}`))
  }

  function dismissError(skill) {
    errors.value = setFor(errors.value, String(skill).toLowerCase(), null)
  }

  function clear() {
    agentName.value = null
    reset()
  }

  return {
    agentName, gates, approvers, hook,
    hasLoaded, error, busy, errors,
    load, probeHook, setGate, clearGate, dismissError, clear,
  }
})
