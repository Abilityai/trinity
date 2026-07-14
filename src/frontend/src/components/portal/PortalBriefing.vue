<template>
  <div class="py-8 sm:py-12 text-center">
    <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="64" class="mx-auto" />
    <h1 class="mt-4 text-xl font-semibold tracking-tight">{{ agent.name }}</h1>
    <p v-if="agent.description" class="mt-1.5 mx-auto max-w-lg text-sm text-gray-500 dark:text-gray-400">
      {{ agent.description }}
    </p>
    <p v-else class="mt-1.5 text-sm text-gray-400">Start a conversation below.</p>

    <!-- Client-visible playbooks as clickable cards (pre-fill the composer, no auto-run) -->
    <div v-if="playbooks.length" class="mt-7 mx-auto max-w-2xl">
      <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2 text-left">Things you can ask</div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        <button
          v-for="(p, i) in playbooks"
          :key="i"
          class="text-left rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-3.5 hover:border-action-primary-400 dark:hover:border-action-primary-600 hover:shadow-sm transition"
          @click="$emit('use-playbook', starterFor(p))"
        >
          <div class="flex items-start gap-2">
            <svg class="w-4 h-4 mt-0.5 text-action-primary-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
            <div class="min-w-0">
              <div class="text-sm font-medium truncate">{{ p.title }}</div>
              <div v-if="p.description" class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">{{ p.description }}</div>
            </div>
          </div>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import PortalAvatar from './PortalAvatar.vue'

const props = defineProps({
  agent: { type: Object, required: true },
})
defineEmits(['use-playbook'])

const playbooks = computed(() => props.agent.playbooks || [])

// Playbooks usually take an argument — pre-fill the composer with the starter
// (never auto-run). Fall back to the title so a card always does something.
function starterFor(p) {
  return (p.starter_prompt || '').trim() || (p.title || '')
}
</script>
