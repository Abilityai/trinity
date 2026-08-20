<template>
  <div class="p-6">
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Reliability — dispatch circuit breaker</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      When this agent's task dispatch keeps failing with auth/billing errors, the
      breaker trips and fast-fails new tasks (instead of burning retries) until it
      recovers. Opt-in per agent (#526).
    </p>

    <div v-if="statusLoading" class="text-sm text-gray-400">Loading…</div>

    <div v-else>
      <!-- Honest state when the platform-wide master switch is OFF (#1712).
           We name the reason (global flag) and the remedy (an admin sets it),
           rather than silently accepting the toggle and reporting success. -->
      <div
        v-if="!globalEnabled"
        class="mb-4 rounded-md bg-amber-50 dark:bg-amber-900/30 border border-amber-300 dark:border-amber-700 p-3"
      >
        <p class="text-sm font-medium text-amber-900 dark:text-amber-200">Globally disabled — this toggle won't take effect yet</p>
        <p class="mt-1 text-xs text-amber-800 dark:text-amber-300">
          The platform-wide dispatch breaker is off, so per-agent breakers never
          engage regardless of this setting.
          <span class="font-medium">Remedy:</span> an admin must set
          <code class="font-mono text-[11px] bg-amber-100 dark:bg-amber-800/60 px-1 py-0.5 rounded">DISPATCH_BREAKER_ENABLED=true</code>
          in the platform environment (then restart). Your choice below is saved
          and applies automatically once that's on.
        </p>
      </div>

      <!-- Toggle — stays operable even when globally off (the owner may pre-set
           their preference), but the state above makes the inertness explicit. -->
      <div class="flex items-start gap-3">
        <label class="relative inline-flex items-center cursor-pointer mt-0.5">
          <input
            type="checkbox"
            class="sr-only peer"
            :checked="enabled"
            :disabled="toggleLoading"
            @change="onToggle($event.target.checked)"
          />
          <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
        </label>
        <div class="min-w-0">
          <p class="text-sm text-gray-900 dark:text-gray-100">
            Enable the dispatch breaker for this agent
            <span
              v-if="enabled && !globalEnabled"
              class="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300"
            >saved · inactive</span>
            <span
              v-else-if="enabled && globalEnabled"
              class="ml-1 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-300"
            >active</span>
          </p>
          <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Both this toggle <em>and</em> the platform-wide flag must be on for the breaker to engage.
          </p>
          <p v-if="lastMessage" class="mt-1 text-xs" :class="lastMessageOk ? 'text-green-600 dark:text-green-400' : 'text-amber-700 dark:text-amber-400'">
            {{ lastMessage }}
          </p>
        </div>
      </div>
    </div>

    <!-- ent#329 — respond → resume ------------------------------------------>
    <hr class="my-6 border-gray-200 dark:border-gray-700" />

    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Wake this agent when an operator answers</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      An answer to one of this agent's parked requests always reaches it — but by
      default it is only read on the agent's <em>next</em> turn. An agent with no
      schedule has no next turn, so an approved action waits indefinitely. Turn
      this on and answering starts one turn so the agent acts on it (ent#329).
    </p>

    <div v-if="resumeLoading" class="text-sm text-gray-400">Loading…</div>

    <div v-else class="flex items-start gap-3">
      <label class="relative inline-flex items-center cursor-pointer mt-0.5">
        <input
          type="checkbox"
          class="sr-only peer"
          :checked="resumeEnabled"
          :disabled="resumeSaving"
          @change="onResumeToggle($event.target.checked)"
        />
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
      </label>
      <div class="min-w-0">
        <p class="text-sm text-gray-900 dark:text-gray-100">
          Answering a request re-triggers this agent
        </p>
        <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          Each answer then costs one agent turn, billed to you — which is why it
          is off by default and set per agent, not per request.
        </p>
        <p v-if="resumeMessage" class="mt-1 text-xs" :class="resumeMessageOk ? 'text-green-600 dark:text-green-400' : 'text-amber-700 dark:text-amber-400'">
          {{ resumeMessage }}
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import api from '../api'

const props = defineProps({
  agentName: { type: String, required: true },
  // Positional (message, type) — same contract as the other settings panels.
  notify: { type: Function, default: null },
})

function notifyUser(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

const enabled = ref(false)
const globalEnabled = ref(true)   // assume-on until we learn otherwise, so we
                                  // never flash a false "globally disabled" banner
const statusLoading = ref(false)
const toggleLoading = ref(false)
const lastMessage = ref('')
const lastMessageOk = ref(true)

async function load() {
  statusLoading.value = true
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/circuit-breaker`)
    // #1712: consume config.global_enabled from the endpoint rather than
    // inferring — honest state on every (re)load, including a persisted-but-inert "on".
    enabled.value = !!data?.config?.enabled
    globalEnabled.value = !!data?.config?.global_enabled
  } catch (e) {
    notifyUser('Failed to load circuit-breaker settings.', 'error')
  } finally {
    statusLoading.value = false
  }
}

async function onToggle(next) {
  toggleLoading.value = true
  lastMessage.value = ''
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/circuit-breaker`, { enabled: next })
    enabled.value = !!data?.enabled
    globalEnabled.value = !!data?.global_enabled
    if (data?.warning) {
      // The endpoint tells us the save is inert while the global flag is off —
      // surface it as the outcome, not a plain "Saved" (the #1712 honesty gap).
      lastMessage.value = data.warning
      lastMessageOk.value = false
    } else {
      lastMessage.value = next ? 'Enabled.' : 'Disabled.'
      lastMessageOk.value = true
    }
  } catch (e) {
    notifyUser('Failed to update the circuit breaker.', 'error')
    await load()  // re-sync the toggle to the true persisted state
  } finally {
    toggleLoading.value = false
  }
}

// ent#329 — respond → resume opt-in
const resumeEnabled = ref(false)
const resumeLoading = ref(false)
const resumeSaving = ref(false)
const resumeMessage = ref('')
const resumeMessageOk = ref(true)

async function loadResume() {
  resumeLoading.value = true
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/operator-resume`)
    resumeEnabled.value = !!data?.enabled
  } catch (e) {
    notifyUser('Failed to load the respond-to-resume setting.', 'error')
  } finally {
    resumeLoading.value = false
  }
}

async function onResumeToggle(next) {
  resumeSaving.value = true
  resumeMessage.value = ''
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/operator-resume`, { enabled: next })
    resumeEnabled.value = !!data?.enabled
    resumeMessage.value = next
      ? 'On — answers will start a turn.'
      : 'Off — answers are read on the next turn.'
    resumeMessageOk.value = true
  } catch (e) {
    notifyUser('Failed to update the respond-to-resume setting.', 'error')
    await loadResume()  // re-sync to the true persisted state
  } finally {
    resumeSaving.value = false
  }
}

onMounted(load)
onMounted(loadResume)
</script>
