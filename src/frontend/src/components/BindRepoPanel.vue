<template>
  <!--
    Bind to your own repo (trinity-enterprise#109).

    A separate component rather than more markup inside GitPanel.vue, for two
    reasons: GitPanel is already a 639-line file, and the #1430 raw-color
    ratchet is PER FILE — appending a whole form there would raise its counts,
    which may only shrink. Written with zero raw non-gray palette classes;
    every non-gray is a semantic token (action-primary / status-*).
  -->
  <div class="border-t border-gray-200 dark:border-gray-750 pt-4 mt-4">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h3 class="text-sm font-medium text-gray-900 dark:text-white">
          Bind to your own repo
        </h3>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
          <template v-if="isTokenless">
            This agent tracks a public template read-only, so its work can't be
            pushed anywhere. Point it at a GitHub repository you own — it keeps
            everything it has learned.
          </template>
          <template v-else>
            Move this agent to a different GitHub repository you own. Its current
            history is pushed to the new repository and
            <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">origin</code>
            is repointed there.
          </template>
        </p>
      </div>
      <button
        v-if="!showForm"
        type="button"
        @click="openForm"
        :disabled="!isRunning"
        :title="isRunning ? '' : 'The agent must be running — its current workspace is what gets pushed.'"
        class="shrink-0 inline-flex items-center px-3 py-1.5 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        Bind repo
      </button>
    </div>

    <!-- Result: kept visible after success so the user can see where it went. -->
    <div
      v-if="result"
      class="mt-3 rounded-md border border-status-success-200 dark:border-status-success-500/30 bg-status-success-50 dark:bg-status-success-500/10 p-3"
    >
      <p class="text-sm font-medium text-status-success-800 dark:text-status-success-300">
        Bound to {{ result.github_repo }}
      </p>
      <p class="mt-1 text-sm text-status-success-700 dark:text-status-success-400">
        {{ result.message }}
      </p>
      <a
        :href="result.repo_url"
        target="_blank"
        rel="noopener noreferrer"
        class="mt-2 inline-block text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
      >
        Open {{ result.github_repo }} on GitHub →
      </a>
    </div>

    <!-- Failure. `partial` is load-bearing: after the commit point the binding
         IS saved, and telling the user "it failed" full stop would be a lie
         that sends them looking in the wrong place. -->
    <div
      v-if="error"
      class="mt-3 rounded-md border p-3"
      :class="error.partial
        ? 'border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10'
        : 'border-status-danger-200 dark:border-status-danger-500/30 bg-status-danger-50 dark:bg-status-danger-500/10'"
    >
      <p
        class="text-sm font-medium"
        :class="error.partial
          ? 'text-status-warning-800 dark:text-status-warning-300'
          : 'text-status-danger-800 dark:text-status-danger-300'"
      >
        {{ error.partial ? 'Partly applied — action needed' : 'Could not bind this agent' }}
      </p>
      <p
        class="mt-1 text-sm"
        :class="error.partial
          ? 'text-status-warning-700 dark:text-status-warning-400'
          : 'text-status-danger-700 dark:text-status-danger-400'"
      >
        {{ error.message }}
      </p>
      <p v-if="error.code" class="mt-2 text-xs font-mono text-gray-500 dark:text-gray-400">
        {{ error.code }}
      </p>
    </div>

    <form v-if="showForm" class="mt-4 space-y-4" @submit.prevent="submit">
      <div>
        <label :for="`bind-dest-${agentName}`" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
          Destination repository
        </label>
        <input
          :id="`bind-dest-${agentName}`"
          v-model="destination"
          type="text"
          autocomplete="off"
          placeholder="your-github-username/my-agent-brain"
          :disabled="binding"
          class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 sm:text-sm disabled:opacity-50"
        />
        <p v-if="destinationError" class="mt-1 text-sm text-status-danger-600 dark:text-status-danger-400">
          {{ destinationError }}
        </p>
        <p v-else class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">owner/name</code>.
          Trinity creates it if it doesn't exist; an existing repository must be empty.
        </p>
      </div>

      <div>
        <label :for="`bind-pat-${agentName}`" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
          GitHub token
        </label>
        <input
          :id="`bind-pat-${agentName}`"
          v-model="pat"
          type="password"
          autocomplete="off"
          placeholder="ghp_... or github_pat_..."
          :disabled="binding"
          class="mt-1 block w-full rounded-md border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white shadow-sm focus:border-action-primary-500 focus:ring-action-primary-500 sm:text-sm disabled:opacity-50"
        />
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Needs permission to create the repository and push — a classic token with
          <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">repo</code> scope, or a
          fine-grained token with Administration + Contents write. It is stored as this
          agent's git credential; the agent can read its own git credential, so prefer
          the narrow token.
        </p>
      </div>

      <fieldset>
        <legend class="block text-sm font-medium text-gray-700 dark:text-gray-300">Visibility</legend>
        <div class="mt-2 flex gap-4">
          <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input
              type="radio"
              :name="`bind-visibility-${agentName}`"
              :value="true"
              v-model="isPrivate"
              :disabled="binding"
              class="text-action-primary-600 focus:ring-action-primary-500 border-gray-300 dark:border-gray-600"
            />
            Private <span class="text-gray-500 dark:text-gray-400">(recommended)</span>
          </label>
          <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input
              type="radio"
              :name="`bind-visibility-${agentName}`"
              :value="false"
              v-model="isPrivate"
              :disabled="binding"
              class="text-action-primary-600 focus:ring-action-primary-500 border-gray-300 dark:border-gray-600"
            />
            Public
          </label>
        </div>
      </fieldset>

      <!-- Principle 26: a non-instant action states what happens and how long. -->
      <div class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 p-3">
        <p class="text-sm font-medium text-status-warning-800 dark:text-status-warning-300">
          This restarts the agent
        </p>
        <p class="mt-1 text-sm text-status-warning-700 dark:text-status-warning-400">
          The container is rebuilt so it picks up the new repository — any work in
          flight is lost, and the agent is briefly unavailable. Files and history are
          preserved. This usually takes under a minute; leave the tab open.
        </p>
      </div>

      <div class="flex items-center gap-2">
        <button
          type="submit"
          :disabled="binding || !destination || !pat"
          class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <svg v-if="binding" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
          </svg>
          {{ binding ? 'Binding…' : 'Bind to this repo' }}
        </button>
        <button
          type="button"
          @click="closeForm"
          :disabled="binding"
          class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </form>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' },
  // True when the agent has no write credentials — the flagship case, and the
  // one where the copy should explain WHY rather than offer a lateral move.
  isTokenless: { type: Boolean, default: false },
})

const emit = defineEmits(['bound'])

const agentsStore = useAgentsStore()

const showForm = ref(false)
const destination = ref('')
const pat = ref('')
const isPrivate = ref(true)
const binding = ref(false)
const error = ref(null)
const result = ref(null)
const destinationError = ref(null)

const isRunning = computed(() => props.agentStatus === 'running')

// Mirrors the backend's `_FORK_DESTINATION_RE` so an obvious typo is named
// before a PAT is sent over the wire (principle 17: pre-validate client-side
// where the rule is known). The backend still validates — this is not the gate.
const DESTINATION_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\/[A-Za-z0-9._-]+$/

const openForm = () => {
  showForm.value = true
  error.value = null
  result.value = null
}

const closeForm = () => {
  showForm.value = false
  destinationError.value = null
  // Hygiene: never leave the token sitting in a reactive ref.
  pat.value = ''
}

const submit = async () => {
  destinationError.value = null
  const dest = destination.value.trim()

  if (!DESTINATION_RE.test(dest) || dest.includes('..')) {
    destinationError.value =
      'Use owner/name — for example your-github-username/my-agent-brain.'
    return
  }

  binding.value = true
  error.value = null
  result.value = null

  // Read the secret out of the reactive ref BEFORE the await, so it can be
  // cleared immediately regardless of how the request ends.
  const token = pat.value
  pat.value = ''

  try {
    const data = await agentsStore.bindAgentToOwnRepo(props.agentName, {
      destination_repo: dest,
      github_pat: token,
      private: isPrivate.value,
    })
    result.value = data
    showForm.value = false
    destination.value = ''
    emit('bound', data)
  } catch (e) {
    const detail = e?.response?.data?.detail
    if (detail && typeof detail === 'object') {
      error.value = {
        message: detail.error || 'Binding failed.',
        code: detail.code || null,
        partial: !!detail.partial,
      }
    } else if (e?.code === 'ECONNABORTED') {
      // The one case the status endpoint exists for: the request may well have
      // landed. Never report a timeout as a clean failure.
      error.value = {
        message:
          'The request timed out before Trinity answered. It may still have ' +
          'completed — reload this tab to see the agent\'s current repository ' +
          'before retrying.',
        code: 'BIND_TIMEOUT',
        partial: true,
      }
    } else {
      error.value = {
        message: detail || e?.message || 'Binding failed.',
        code: null,
        partial: false,
      }
    }
  } finally {
    binding.value = false
  }
}
</script>
