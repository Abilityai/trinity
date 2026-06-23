<template>
  <div class="p-6 space-y-6">
    <!-- Header -->
    <div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Access</h3>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Trinity <strong>operators</strong> (platform users) with access to this agent. External
        clients who reach the agent through channels are managed on the
        <span class="font-medium">Sharing</span> tab.
      </p>
    </div>

    <!-- Add operator -->
    <form @submit.prevent="addOperator" class="flex items-center space-x-3">
      <input
        v-model="newEmail"
        type="email"
        required
        placeholder="operator@company.com"
        :disabled="adding"
        class="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
      <button
        type="submit"
        :disabled="adding || !newEmail.trim()"
        class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
      >
        <svg v-if="adding" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z"></path>
        </svg>
        {{ adding ? 'Adding…' : 'Add operator' }}
      </button>
    </form>

    <!-- Add result -->
    <div
      v-if="message"
      :class="['p-3 rounded-lg text-sm', message.type === 'success'
        ? 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300'
        : 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300']"
    >{{ message.text }}</div>

    <!-- Roster -->
    <div v-if="loading" class="py-6 space-y-2">
      <div
        v-for="n in 4"
        :key="n"
        class="animate-pulse h-11 w-full rounded-md bg-gray-200 dark:bg-gray-700"
      ></div>
    </div>
    <div
      v-else-if="error"
      class="text-center py-6 text-sm text-status-danger-600 dark:text-status-danger-400"
    >
      Couldn't load access list.
      <button @click="load" class="ml-1 underline">Retry</button>
    </div>
    <div
      v-else-if="operators.length === 0"
      class="text-center py-8 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700"
    >
      No operators yet. Add a Trinity user by email above.
    </div>
    <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <li
        v-for="op in operators"
        :key="op.email"
        class="px-4 py-3 flex items-center justify-between bg-white dark:bg-gray-800"
      >
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ op.username || op.email }}</p>
            <!-- status -->
            <span
              :class="['inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium', op.status === 'active'
                ? 'bg-status-success-100 text-status-success-800 dark:bg-status-success-900/40 dark:text-status-success-300'
                : 'bg-state-autonomous-100 text-state-autonomous-800 dark:bg-state-autonomous-900/40 dark:text-state-autonomous-300']"
            >{{ op.status === 'active' ? 'Active' : 'Pending' }}</span>
            <!-- role -->
            <span
              v-if="op.role"
              class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200"
            >{{ op.role }}</span>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400 truncate">
            {{ op.username ? op.email : 'Invited — no account yet' }}
            <span v-if="op.last_active"> · last active {{ formatLastActive(op.last_active) }}</span>
          </p>
        </div>
        <button
          @click="removeOperator(op.email)"
          :disabled="removing === op.email"
          class="ml-3 shrink-0 text-sm text-status-danger-600 dark:text-status-danger-400 hover:underline disabled:opacity-50"
        >{{ removing === op.email ? 'Removing…' : 'Remove' }}</button>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: { type: String, required: true },
})

const agentsStore = useAgentsStore()

const operators = ref([])
const loading = ref(true)
const error = ref(false)
const adding = ref(false)
const removing = ref('')
const newEmail = ref('')
const message = ref(null)

async function load() {
  loading.value = true
  error.value = false
  try {
    operators.value = await agentsStore.getAgentAccess(props.agentName)
  } catch (e) {
    error.value = true
  } finally {
    loading.value = false
  }
}

async function addOperator() {
  const email = newEmail.value.trim()
  if (!email) return
  adding.value = true
  message.value = null
  try {
    await agentsStore.shareAgent(props.agentName, email)
    newEmail.value = ''
    message.value = { type: 'success', text: `Added ${email}.` }
    await load()
  } catch (e) {
    message.value = { type: 'error', text: e?.response?.data?.detail || 'Failed to add operator.' }
  } finally {
    adding.value = false
  }
}

async function removeOperator(email) {
  removing.value = email
  try {
    await agentsStore.unshareAgent(props.agentName, email)
    await load()
  } catch (e) {
    message.value = { type: 'error', text: e?.response?.data?.detail || 'Failed to remove operator.' }
  } finally {
    removing.value = ''
  }
}

function formatLastActive(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString()
}

onMounted(load)
watch(() => props.agentName, load)
</script>
