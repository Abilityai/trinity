<template>
  <div class="max-w-5xl mx-auto px-4 py-8">
    <!-- Header -->
    <div class="mb-6">
      <div class="flex items-center gap-2 mb-1">
        <span class="text-2xl">🪟</span>
        <h1 class="text-2xl font-semibold text-gray-900 dark:text-white">Your Agents</h1>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        The agents shared with
        <span v-if="store.clientEmail" class="font-medium text-gray-700 dark:text-gray-300">{{ store.clientEmail }}</span>
        <span v-else>your account</span>.
        <span class="text-gray-400">Client Portal preview — chat &amp; documents arrive in a later slice.</span>
      </p>
    </div>

    <!-- Loading -->
    <div v-if="store.loading" class="text-center py-16">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
      <p class="mt-3 text-sm text-gray-500 dark:text-gray-400">Loading your agents…</p>
    </div>

    <!-- Error -->
    <div v-else-if="store.error" class="rounded-lg border border-status-danger-200 dark:border-status-danger-800 bg-status-danger-50 dark:bg-status-danger-900/30 p-4 text-sm text-status-danger-700 dark:text-status-danger-300">
      {{ store.error }}
      <button class="ml-2 underline" @click="store.fetchRoster()">Retry</button>
    </div>

    <!-- Empty -->
    <div v-else-if="store.agents.length === 0" class="text-center py-16 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg">
      <div class="text-4xl mb-3">🗂️</div>
      <p class="text-sm text-gray-600 dark:text-gray-300 font-medium">No agents shared with you yet</p>
      <p class="text-xs text-gray-400 mt-1">When an operator shares an agent with your email, it appears here.</p>
    </div>

    <!-- Roster grid -->
    <div v-else>
      <div class="mb-3 text-xs text-gray-400">
        {{ store.agents.length }} agent{{ store.agents.length === 1 ? '' : 's' }} shared with you
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="a in store.agents"
          :key="a.name"
          class="rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 flex flex-col"
        >
          <div class="flex items-center gap-3">
            <!-- Avatar or initials tile -->
            <img
              v-if="a.avatar_url && !failed[a.name]"
              :src="a.avatar_url"
              :alt="a.name"
              class="h-12 w-12 rounded-full object-cover bg-gray-100 dark:bg-gray-700"
              @error="failed[a.name] = true"
            />
            <div
              v-else
              class="h-12 w-12 rounded-full flex items-center justify-center text-white font-semibold"
              :style="{ background: tint(a.name) }"
            >{{ initials(a.name) }}</div>

            <div class="min-w-0">
              <div class="font-medium text-gray-900 dark:text-white truncate">{{ a.name }}</div>
              <div class="text-xs text-gray-500 dark:text-gray-400 truncate">
                <span v-if="a.owner">by {{ a.owner }}</span>
              </div>
            </div>
          </div>

          <div class="mt-3 flex items-center justify-between">
            <span class="text-[11px] text-gray-400">
              <span v-if="a.shared_at">shared {{ formatDate(a.shared_at) }}</span>
            </span>
            <button
              class="text-xs px-2.5 py-1 rounded-md bg-action-primary-600 hover:bg-action-primary-700 text-white"
              @click="chatAgent = a"
            >Chat</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Chat drawer -->
    <PortalChat v-if="chatAgent" :agent="chatAgent" @close="chatAgent = null" />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalChat from './PortalChat.vue'

const store = useClientPortalStore()
const failed = reactive({})   // avatar_url that 401'd / failed → fall back to initials
const chatAgent = ref(null)   // the agent whose chat drawer is open

function initials(name) {
  const parts = (name || '?').replace(/[^A-Za-z0-9]+/g, ' ').trim().split(' ')
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || parts[0]?.[1] || '')).toUpperCase() || '?'
}

// Deterministic tint from the name so tiles are stable + distinguishable.
function tint(name) {
  let h = 0
  for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return `hsl(${h}, 45%, 45%)`
}

function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

onMounted(() => store.fetchRoster())
</script>
