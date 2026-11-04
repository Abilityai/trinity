/**
 * The changes waiting for an agent restart (trinity-enterprise#887).
 *
 * Before this, every setting that needs a restart drew its own banner — file
 * sharing, shared folders, and the resources dialog each in its own card — so
 * a page could stack three of them and none said what the others waited on.
 * The decision on trinity#966: ONE banner at the top of Settings, naming each
 * waiting change, with one Restart button.
 *
 * The rows that know a restart is due REPORT here; the banner only reads. Each
 * report is keyed by agent and by the row's own key, so a row going back to its
 * live value (or a panel for another agent) can retract exactly its own entry.
 *
 * `epoch` counts completed restarts per agent. A reporting row watches it and
 * re-reads its status after a restart, because the backend — not this store —
 * is what knows whether the change has now applied.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useAgentRestartStore = defineStore('agentRestart', () => {
  // { [agentName]: { [key]: label } } — insertion order is report order.
  const pending = ref({})
  const epochs = ref({})

  function report(agentName, key, label, isPending) {
    if (!agentName || !key) return
    const current = { ...(pending.value[agentName] || {}) }
    if (isPending) current[key] = label
    else delete current[key]
    pending.value = { ...pending.value, [agentName]: current }
  }

  /** The waiting changes for one agent, as `{ key, label }` in report order. */
  function pendingFor(agentName) {
    const entries = pending.value[agentName] || {}
    return Object.entries(entries).map(([key, label]) => ({ key, label }))
  }

  function epochFor(agentName) {
    return epochs.value[agentName] || 0
  }

  /**
   * A restart finished. Every entry is cleared (the container now runs what is
   * stored) and the epoch moves, so each row re-reads and re-reports anything
   * that is still out of step.
   */
  function restarted(agentName) {
    pending.value = { ...pending.value, [agentName]: {} }
    epochs.value = { ...epochs.value, [agentName]: epochFor(agentName) + 1 }
  }

  return { pending, report, pendingFor, epochFor, restarted }
})
