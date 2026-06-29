<!--
  OnboardingWizard.vue (trinity-enterprise#52)

  Minimalistic guided first-run flow: a fresh self-hoster lands on an empty
  Dashboard and this overlay walks them from "nothing" to "a real agent they
  can chat with" in a couple of clicks.

  Deliberately NOT a click-through tour. One question — "what do you want to
  do?" — maps an intent to a starter template and deploys a tailored first
  agent. Concepts are taught by doing, not by reading.

  Soft-guides the one hard setup gate (Claude auth) without blocking, so the
  user is never stuck behind a wall of setup.
-->
<template>
  <div class="fixed inset-0 z-40 overflow-y-auto">
    <div class="flex min-h-screen items-center justify-center p-4">
      <div class="fixed inset-0 bg-gray-900/70 backdrop-blur-sm" @click="dismiss"></div>

      <div class="relative w-full max-w-2xl rounded-2xl bg-white dark:bg-gray-800 shadow-2xl ring-1 ring-black/5 dark:ring-white/10 overflow-hidden">
        <!-- Header -->
        <div class="px-6 pt-6 pb-4 sm:px-8">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-xs font-semibold uppercase tracking-wide text-action-primary-600 dark:text-action-primary-400">Welcome to Trinity</p>
              <h2 class="mt-1 text-xl font-semibold text-gray-900 dark:text-white">
                {{ deployed ? "You're up and running" : 'Launch your first agent' }}
              </h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                {{ deployed
                  ? 'Your agent is being created — say hello and watch it work.'
                  : 'Pick what you want it to do. We deploy a ready-made agent you can chat with right away.' }}
              </p>
            </div>
            <button
              @click="dismiss"
              class="ml-4 rounded-lg p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 focus:outline-none focus:ring-2 focus:ring-action-primary-500"
              aria-label="Close onboarding"
            >
              <svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        <!-- Claude-auth gate hint (non-blocking) -->
        <div
          v-if="!deployed && !claudeAuthConfigured"
          class="mx-6 mb-2 sm:mx-8 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/30 px-4 py-3"
        >
          <div class="flex items-start gap-2">
            <svg class="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <p class="text-xs text-amber-800 dark:text-amber-200">
              Agents think with Claude. Add a subscription or API key in
              <router-link to="/settings?tab=integrations" class="font-medium underline" @click="dismiss">Settings → Integrations</router-link>
              so your agent can reply. You can deploy now and add it after.
            </p>
          </div>
        </div>

        <!-- Body -->
        <div class="px-6 pb-6 sm:px-8 sm:pb-8">
          <!-- Success state -->
          <div v-if="deployed" class="py-2">
            <div class="flex items-center gap-3 rounded-xl border border-status-success-200 dark:border-status-success-800 bg-status-success-50 dark:bg-status-success-900/30 px-4 py-3">
              <svg class="h-6 w-6 flex-shrink-0 text-status-success-500" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
              </svg>
              <div>
                <p class="text-sm font-medium text-gray-900 dark:text-white">{{ deployedName }} is ready</p>
                <p class="text-xs text-gray-500 dark:text-gray-400">Open the chat to give it its first task.</p>
              </div>
            </div>
            <div class="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <button
                @click="dismiss"
                class="rounded-lg px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                Maybe later
              </button>
              <button
                @click="openChat"
                class="rounded-lg bg-action-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500"
              >
                Open chat →
              </button>
            </div>
          </div>

          <!-- Picker + deploy -->
          <div v-else>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                v-for="p in purposes"
                :key="p.key"
                type="button"
                @click="select(p)"
                :class="[
                  'flex items-start gap-3 rounded-xl border p-4 text-left transition-all',
                  selected?.key === p.key
                    ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 ring-2 ring-action-primary-500'
                    : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
                ]"
              >
                <span class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-action-primary-100 dark:bg-action-primary-900/50 text-action-primary-600 dark:text-action-primary-400">
                  <span class="text-lg leading-none" v-html="p.icon"></span>
                </span>
                <span class="min-w-0">
                  <span class="block text-sm font-medium text-gray-900 dark:text-white">{{ p.title }}</span>
                  <span class="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">{{ p.desc }}</span>
                </span>
              </button>
            </div>

            <!-- Name + deploy (revealed once a purpose is chosen) -->
            <div v-if="selected" class="mt-5 border-t border-gray-100 dark:border-gray-700 pt-5">
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Name your agent</label>
              <input
                v-model="agentName"
                type="text"
                :placeholder="selected.name"
                @keyup.enter="deploy"
                class="mt-1 block w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:border-action-primary-500 focus:ring-action-primary-500"
              />
              <p v-if="error" class="mt-2 text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>

              <div class="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <button
                  @click="dismiss"
                  class="rounded-lg px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                >
                  Skip for now
                </button>
                <button
                  @click="deploy"
                  :disabled="deploying"
                  class="inline-flex items-center justify-center rounded-lg bg-action-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-gray-800 focus:ring-action-primary-500 disabled:opacity-50"
                >
                  <svg v-if="deploying" class="-ml-1 mr-2 h-4 w-4 animate-spin text-white" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  {{ deploying ? 'Deploying…' : 'Deploy agent →' }}
                </button>
              </div>
            </div>

            <!-- Footer skip when nothing selected yet -->
            <div v-else class="mt-5 text-center">
              <button @click="dismiss" class="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                Skip for now — I'll explore on my own
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'

defineProps({
  // Whether platform Claude auth is configured (from feature-flags). Drives
  // the soft, non-blocking setup hint.
  claudeAuthConfigured: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'deployed'])

const router = useRouter()
const agentsStore = useAgentsStore()
const authStore = useAuthStore()

// Intent → starter template mapping. Each maps to a real local template
// shipped in config/agent-templates (verified to exist on mount; falls back
// to a blank Claude Code agent if a template is missing in this deploy).
const purposes = ref([
  { key: 'research', title: 'Research a market or topic', desc: 'Scans trends and competitors, summarizes findings.', icon: '🔎', template: 'local:scout', name: 'scout' },
  { key: 'strategy', title: 'Advise on strategy', desc: 'Turns inputs into clear, actionable recommendations.', icon: '🧭', template: 'local:sage', name: 'sage' },
  { key: 'writing', title: 'Write content & reports', desc: 'Drafts reports, proposals, and client deliverables.', icon: '✍️', template: 'local:scribe', name: 'scribe' },
  { key: 'blank', title: 'Start from scratch', desc: 'A blank Claude Code agent you shape yourself.', icon: '✨', template: '', name: 'assistant' },
])

const selected = ref(null)
const agentName = ref('')
const deploying = ref(false)
const deployed = ref(false)
const deployedName = ref('')
const error = ref('')

onMounted(async () => {
  // Confirm the mapped local templates actually exist; if a deploy is missing
  // one, fall back that card to a blank agent so deploy never 404s.
  try {
    const r = await axios.get('/api/templates', { headers: authStore.authHeader })
    const ids = new Set((r.data || []).map((t) => t.id))
    purposes.value.forEach((p) => {
      if (p.template && !ids.has(p.template)) p.template = ''
    })
  } catch {
    /* non-fatal — cards still work, blank fallback covers it */
  }
})

function select(p) {
  selected.value = p
  if (!agentName.value) agentName.value = p.name
  error.value = ''
}

function dismiss() {
  emit('close')
}

async function deploy() {
  if (deploying.value) return
  const name = (agentName.value || selected.value?.name || '').trim()
  if (!name) {
    error.value = 'Give your agent a name.'
    return
  }
  deploying.value = true
  error.value = ''
  try {
    const payload = { name }
    if (selected.value?.template) payload.template = selected.value.template
    await agentsStore.createAgent(payload)
    deployedName.value = name
    deployed.value = true
    emit('deployed', name)
  } catch (err) {
    const detail = err.response?.data?.detail
    if (detail && typeof detail === 'object') {
      error.value = detail.error || 'Failed to create agent.'
    } else {
      error.value = typeof detail === 'string' ? detail : 'Failed to create agent.'
    }
  } finally {
    deploying.value = false
  }
}

function openChat() {
  const name = deployedName.value
  emit('close')
  router.push({ path: `/agents/${name}`, query: { tab: 'chat' } })
}
</script>
