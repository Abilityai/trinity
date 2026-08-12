<template>
  <div class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="Start a chat">
    <div class="absolute inset-0 bg-black/40" @click="$emit('cancel')"></div>

    <div class="relative w-full max-w-md rounded-2xl bg-white dark:bg-gray-900 shadow-xl ring-1 ring-gray-200 dark:ring-gray-800 flex flex-col max-h-[80vh]">
      <div class="shrink-0 px-4 pt-4 pb-3 border-b border-gray-200 dark:border-gray-800">
        <h2 class="text-sm font-semibold">Start a chat</h2>
        <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          Pick one agent, or several to put them in the same conversation.
        </p>
      </div>

      <div class="flex-1 min-h-0 overflow-y-auto p-2">
        <p v-if="!agents.length" class="px-2 py-6 text-center text-sm text-gray-500 dark:text-gray-400">
          No agents are shared with you yet.
        </p>
        <button
          v-for="a in agents"
          :key="a.name"
          type="button"
          class="w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-800 transition"
          :class="{ 'bg-gray-50 dark:bg-gray-800': selected.includes(a.name) }"
          role="checkbox"
          :aria-checked="selected.includes(a.name)"
          @click="toggle(a.name)"
        >
          <span
            class="shrink-0 w-4 h-4 rounded border flex items-center justify-center"
            :class="selected.includes(a.name)
              ? 'bg-action-primary-600 border-action-primary-600'
              : 'border-gray-300 dark:border-gray-600'"
          >
            <svg v-if="selected.includes(a.name)" class="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
            </svg>
          </span>
          <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="26" />
          <span class="min-w-0">
            <span class="block text-sm truncate">{{ a.name }}</span>
            <span v-if="a.description" class="block text-xs text-gray-500 dark:text-gray-400 truncate">{{ a.description }}</span>
          </span>
        </button>
      </div>

      <p v-if="error" class="shrink-0 px-4 pt-3 text-xs text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>

      <div class="shrink-0 px-4 py-3 border-t border-gray-200 dark:border-gray-800 flex items-center gap-2">
        <!-- The count is the only hint that picking two behaves differently
             (a room rather than a thread); the wording stays in the user's
             vocabulary — they are choosing who is in the conversation. -->
        <span class="text-xs text-gray-500 dark:text-gray-400 flex-1">
          {{ selected.length === 0 ? 'Nobody selected'
            : selected.length === 1 ? '1 agent'
            : `${selected.length} agents — they will share this conversation` }}
        </span>
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
          @click="$emit('cancel')"
        >Cancel</button>
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-sm font-medium bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 transition"
          :disabled="!selected.length || busy"
          @click="confirm"
        >{{ busy ? 'Starting…' : 'Start' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * Agent picker for starting a chat (ent#361).
 *
 * Chat creation used to be implicit — clicking a roster row started one — which
 * meant you could never PLAN to put two agents on a problem, and the header had
 * nobody to show until someone spoke. Selecting is now an explicit act.
 *
 * The one-vs-many distinction is deliberately not surfaced as a mode: one agent
 * stays a portal thread (which resumes and streams), two or more becomes a room
 * (the only substrate that models several agents and @mention-waking). The
 * caller decides which; this component only reports who was chosen.
 */
import { ref } from 'vue'
import PortalAvatar from './PortalAvatar.vue'

const props = defineProps({
  agents: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  // Shown in place rather than closing the dialog: dismissing on failure would
  // leave the user guessing whether a chat was created.
  error: { type: String, default: null },
})
const emit = defineEmits(['confirm', 'cancel'])

const selected = ref([])

function toggle(name) {
  const i = selected.value.indexOf(name)
  if (i === -1) selected.value.push(name)
  else selected.value.splice(i, 1)
}

function confirm() {
  if (!selected.value.length) return
  emit('confirm', [...selected.value])
}
</script>
