<template>
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg p-6">
    <h2 class="text-lg font-medium text-gray-900 dark:text-white">Personal GitHub Token</h2>
    <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
      Add your own GitHub Personal Access Token so you can create agents from
      repositories you have access to. When set, new agents you create from a
      <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">github:</code> repo
      use your token instead of the platform's shared token.
    </p>

    <div class="mt-4 max-w-xl">
      <label for="user-github-pat" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Personal Access Token
      </label>
      <div class="mt-1 flex gap-2">
        <div class="relative flex-1">
          <input
            :type="showPat ? 'text' : 'password'"
            id="user-github-pat"
            v-model="pat"
            :placeholder="status.configured ? '•••••••••• (configured)' : 'ghp_... or github_pat_...'"
            :disabled="saving"
            :class="[SETTINGS_TEXT_INPUT_CLASS, 'w-full pr-10']"
          />
          <button
            type="button"
            @click="showPat = !showPat"
            class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          >
            <svg v-if="showPat" class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
            </svg>
            <svg v-else class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
            </svg>
          </button>
        </div>
        <button
          @click="save"
          :disabled="!pat || saving"
          class="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
        >
          <svg v-if="saving" class="animate-spin -ml-1 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
          </svg>
          Save
        </button>
        <button
          v-if="status.configured"
          @click="remove"
          :disabled="removing"
          class="inline-flex items-center px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md shadow-sm text-sm font-medium text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600 disabled:opacity-50"
        >
          Remove
        </button>
      </div>

      <!-- Status / result line -->
      <p v-if="message" :class="['mt-2 text-sm', error ? 'text-status-danger-600 dark:text-status-danger-400' : 'text-status-success-600 dark:text-status-success-400']">
        {{ message }}
      </p>
      <p v-else-if="status.configured" class="mt-2 text-sm text-gray-600 dark:text-gray-400">
        A personal token is configured. Your new agents use it to clone GitHub repos.
      </p>
      <p v-else-if="!status.has_global" class="mt-2 text-sm text-status-warning-700 dark:text-status-warning-400">
        No personal token, and the platform has no shared token — creating an agent from a repo will need one of these.
      </p>
      <p v-else class="mt-2 text-sm text-gray-500 dark:text-gray-400">
        No personal token — your new agents fall back to the platform's shared token.
      </p>

      <p class="mt-3 text-xs text-gray-400 dark:text-gray-500">
        Stored encrypted; never shown again after saving. A classic token needs the
        <code class="bg-gray-100 dark:bg-gray-700 px-1 rounded">repo</code> scope, or use a fine-grained token with Contents read access.
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { SETTINGS_TEXT_INPUT_CLASS } from './fieldStyles'
import axios from 'axios'

const pat = ref('')
const showPat = ref(false)
const saving = ref(false)
const removing = ref(false)
const message = ref('')
const error = ref(false)
const status = reactive({ configured: false, has_global: false })

const loadStatus = async () => {
  try {
    const { data } = await axios.get('/api/users/me/github-pat')
    status.configured = !!data.configured
    status.has_global = !!data.has_global
  } catch (e) {
    // Non-fatal: leave defaults, the panel still lets the user set a token.
    console.error('Failed to load GitHub token status:', e)
  }
}

const save = async () => {
  saving.value = true
  message.value = ''
  error.value = false
  try {
    const { data } = await axios.put('/api/users/me/github-pat', { pat: pat.value.trim() })
    status.configured = true
    error.value = false
    message.value = data.github_username
      ? `Saved — verified as GitHub user "${data.github_username}".`
      : 'Personal GitHub token saved.'
    pat.value = ''
  } catch (e) {
    error.value = true
    // Honest surfacing: 400 = GitHub rejected it; 503 = we couldn't reach GitHub.
    message.value = e?.response?.data?.detail || 'Failed to save token.'
  } finally {
    saving.value = false
  }
}

const remove = async () => {
  removing.value = true
  message.value = ''
  error.value = false
  try {
    await axios.delete('/api/users/me/github-pat')
    status.configured = false
    message.value = 'Personal token removed — new agents will use the platform token.'
  } catch (e) {
    error.value = true
    message.value = e?.response?.data?.detail || 'Failed to remove token.'
  } finally {
    removing.value = false
  }
}

onMounted(loadStatus)
</script>
