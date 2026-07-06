<template>
  <!--
    Brain tab content (#60, #73). Launch button + real settings. First setting:
    post-voice-session processing (#73) — an on/off flag + the prompt that runs
    (headless) over each finished voice transcript. Config is agent-owned; Trinity
    brokers read/write via /api/agents/{name}/brain-orb/postprocess (owner-only).
  -->
  <div class="max-w-3xl">
    <div class="flex items-start gap-4">
      <div class="flex-shrink-0 w-14 h-14 rounded-full flex items-center justify-center bg-state-autonomous-50 dark:bg-state-autonomous-900/30 border border-state-autonomous-200 dark:border-state-autonomous-700 text-state-autonomous-500 dark:text-state-autonomous-400">
        <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" stroke-width="1.6" />
          <path stroke-width="1.4" stroke-linecap="round" d="M3 12h18" opacity="0.7" />
          <path stroke-width="1.4" stroke-linecap="round" d="M12 3c3.2 2.4 3.2 15.6 0 18M12 3c-3.2 2.4-3.2 15.6 0 18" opacity="0.7" />
        </svg>
      </div>
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2">
          <h3 class="text-lg font-semibold text-gray-900 dark:text-white">Brain Orb</h3>
          <span class="px-1.5 py-0.5 text-[10px] font-bold rounded bg-state-autonomous-100 dark:bg-state-autonomous-900/40 text-state-autonomous-700 dark:text-state-autonomous-400 leading-none">BETA</span>
        </div>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
          The Self-Rendering Mind — a live 3D knowledge-graph view of {{ name }}'s memory,
          with a client-held voice tile to explore it by talking.
        </p>
      </div>
    </div>

    <!-- Post-voice processing (#73) -->
    <div class="mt-6 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-4">
      <div class="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-2">Post-voice processing</div>

      <template v-if="!writeAvailable">
        <p class="text-sm text-gray-500 dark:text-gray-400">
          Enable the Brain Orb write surface (Settings → General → Brain Orb → KB-write actions)
          to configure post-voice processing.
        </p>
      </template>
      <template v-else-if="!running">
        <p class="text-sm text-gray-500 dark:text-gray-400">Start the agent to configure post-voice processing.</p>
      </template>
      <template v-else>
        <p class="text-sm text-gray-600 dark:text-gray-400 mb-3">
          When on, each finished voice conversation is run through this prompt (headless) and the
          result is saved back as a note — so a session becomes durable, processed memory.
        </p>

        <label class="flex items-center gap-2 cursor-pointer select-none mb-3">
          <input type="checkbox" v-model="enabled" :disabled="loading || saving"
                 class="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
          <span class="text-sm text-gray-800 dark:text-gray-200">Run a processing step after each voice session</span>
        </label>

        <textarea v-model="prompt" :disabled="loading || saving" rows="5"
          placeholder="e.g. Summarize this voice conversation: 3 key insights, then any action items. Output only the note body."
          class="w-full text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 p-2 focus:ring-1 focus:ring-blue-500 focus:border-blue-500"></textarea>

        <div class="mt-3 flex items-center gap-3">
          <button @click="save" :disabled="loading || saving"
            class="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm font-medium bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
            {{ saving ? 'Saving…' : 'Save' }}
          </button>
          <span v-if="saved" class="text-xs text-green-600 dark:text-green-400">Saved</span>
          <span v-if="error" class="text-xs text-red-500">{{ error }}</span>
          <span v-if="enabled && !prompt.trim()" class="text-xs text-amber-500">Add a prompt for processing to run.</span>
        </div>
      </template>
    </div>

    <!-- Launch -->
    <div class="mt-6">
      <button
        @click="openBrain"
        :disabled="!running"
        class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        :class="running
          ? 'bg-blue-600 hover:bg-blue-700 text-white shadow-sm'
          : 'bg-gray-200 dark:bg-gray-700 text-gray-400 dark:text-gray-500 cursor-not-allowed'"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" stroke-width="1.6" />
          <path stroke-width="1.4" stroke-linecap="round" d="M3 12h18" opacity="0.8" />
          <path stroke-width="1.4" stroke-linecap="round" d="M12 3c3.2 2.4 3.2 15.6 0 18M12 3c-3.2 2.4-3.2 15.6 0 18" opacity="0.8" />
        </svg>
        Open Brain Orb
      </button>
      <p v-if="!running" class="mt-2 text-xs text-gray-400 dark:text-gray-500">
        Start the agent to open its Brain Orb.
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useSessionsStore } from '../stores/sessions'

const props = defineProps({
  name: { type: String, required: true },
  running: { type: Boolean, default: false },
})

const router = useRouter()
const authStore = useAuthStore()
const sessionsStore = useSessionsStore()

const enabled = ref(false)
const prompt = ref('')
const loading = ref(false)
const saving = ref(false)
const saved = ref(false)
const error = ref('')

// The post-voice surface is exec-adjacent, so it's gated on the write flag (#73).
const writeAvailable = ref(!!sessionsStore.brainOrbWriteAvailable)

function openBrain() {
  router.push({ name: 'AgentBrainOrb', params: { name: props.name } })
}

async function load() {
  if (!props.running || !writeAvailable.value) return
  loading.value = true; error.value = ''
  try {
    const r = await axios.get(`/api/agents/${props.name}/brain-orb/postprocess`, { headers: authStore.authHeader })
    enabled.value = !!r.data?.enabled
    prompt.value = r.data?.prompt || ''
  } catch (e) {
    // 404 (flag/agent) is fine — leave defaults; only surface real errors
    if (e.response?.status && e.response.status !== 404) error.value = 'Could not load settings'
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true; saved.value = false; error.value = ''
  try {
    await axios.put(`/api/agents/${props.name}/brain-orb/postprocess`,
      { enabled: enabled.value, prompt: prompt.value },
      { headers: authStore.authHeader })
    saved.value = true
    setTimeout(() => { saved.value = false }, 2500)
  } catch (e) {
    error.value = e.response?.data?.detail || 'Could not save'
  } finally {
    saving.value = false
  }
}

onMounted(() => { sessionsStore.loadFeatureFlags?.().then(() => { writeAvailable.value = !!sessionsStore.brainOrbWriteAvailable; load() }) })
watch(() => props.running, () => load())
</script>
