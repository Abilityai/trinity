<template>
  <div class="relative" ref="containerRef">
    <!-- Trigger button -->
    <button
      @click="toggleDropdown"
      class="inline-flex items-center px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
      :class="{ 'text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-900/20': isOpen }"
      title="Chat history"
    >
      <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      History
      <svg class="w-3 h-3 ml-1 transition-transform" :class="{ 'rotate-180': isOpen }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
      </svg>
    </button>

    <!-- Dropdown panel -->
    <Transition
      enter-active-class="transition ease-out duration-100"
      enter-from-class="opacity-0 scale-95"
      enter-to-class="opacity-100 scale-100"
      leave-active-class="transition ease-in duration-75"
      leave-from-class="opacity-100 scale-100"
      leave-to-class="opacity-0 scale-95"
    >
      <div
        v-if="isOpen"
        class="absolute right-0 top-8 z-50 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-lg overflow-hidden"
      >
        <!-- Header -->
        <div class="px-3 py-2 border-b border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <span class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Previous Sessions</span>
          <span class="text-xs text-gray-400 dark:text-gray-500">Logged-in chats only</span>
        </div>

        <!-- Loading state -->
        <div v-if="loading" class="px-3 py-4 text-center">
          <div class="animate-spin rounded-full h-5 w-5 border-b-2 border-indigo-500 mx-auto"></div>
        </div>

        <!-- Error state -->
        <div v-else-if="error" class="px-3 py-3 text-xs text-status-danger-500 dark:text-status-danger-400">
          {{ error }}
        </div>

        <!-- Empty state -->
        <div v-else-if="sessions.length === 0" class="px-3 py-4 text-center text-xs text-gray-400 dark:text-gray-500">
          No previous sessions found.
        </div>

        <!-- Session list -->
        <ul v-else class="max-h-64 overflow-y-auto divide-y divide-gray-50 dark:divide-gray-700">
          <li
            v-for="session in sessions"
            :key="session.id"
            @click="selectSession(session)"
            class="px-3 py-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
          >
            <div class="flex items-center justify-between mb-0.5">
              <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ formatDate(session.last_message_at) }}</span>
              <span class="text-xs text-gray-400 dark:text-gray-500">{{ session.message_count }} msg{{ session.message_count !== 1 ? 's' : '' }}</span>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ session.preview || 'No preview' }}</p>
          </li>
        </ul>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../../stores/auth'

const props = defineProps({
  token: { type: String, required: true },
})

const emit = defineEmits(['session-selected'])

const authStore = useAuthStore()
const isOpen = ref(false)
const loading = ref(false)
const error = ref(null)
const sessions = ref([])
const containerRef = ref(null)

const toggleDropdown = async () => {
  if (!isOpen.value) {
    isOpen.value = true
    await fetchSessions()
  } else {
    isOpen.value = false
  }
}

const fetchSessions = async () => {
  loading.value = true
  error.value = null

  try {
    const response = await axios.get(`/api/public/sessions/${props.token}`, {
      headers: authStore.authHeader,
    })
    sessions.value = response.data.sessions || []
  } catch (err) {
    error.value = 'Failed to load history.'
  } finally {
    loading.value = false
  }
}

const selectSession = async (session) => {
  isOpen.value = false
  loading.value = true
  error.value = null

  try {
    const response = await axios.get(`/api/public/sessions/${props.token}/${session.id}`, {
      headers: authStore.authHeader,
    })
    const messages = (response.data.messages || []).map(m => ({
      role: m.role,
      content: m.content,
    }))
    emit('session-selected', { messages, session })
  } catch (err) {
    error.value = 'Failed to load session.'
  } finally {
    loading.value = false
  }
}

const formatDate = (iso) => {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now - d
  const diffDays = Math.floor(diffMs / 86400000)

  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return `${diffDays}d ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const handleClickOutside = (e) => {
  if (containerRef.value && !containerRef.value.contains(e.target)) {
    isOpen.value = false
  }
}

onMounted(() => document.addEventListener('mousedown', handleClickOutside))
onUnmounted(() => document.removeEventListener('mousedown', handleClickOutside))
</script>
