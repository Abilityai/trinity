<!--
  The welcome for an account that cannot build (`accountWelcome.js`): it names the
  account, what it has and what is not part of it, then offers a short walk round the
  Workspace. An operator's walk ends on the sidebar's Dashboard link, and its button opens
  the Dashboard on one of their agents' Tasks, lit click by click.
-->
<template>
  <BaseModal
    v-if="words"
    :model-value="open && !touring"
    :close-on-backdrop="false"
    labelledby="account-welcome-title"
    panel-class="relative w-full max-w-lg rounded-[10px] border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-6 shadow-2xl"
    @update:model-value="(shown) => { if (!shown) $emit('done') }"
  >
    <div data-testid="account-welcome" :data-kind="kind">
      <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ words.account }}</p>
      <h2 id="account-welcome-title" class="mt-1 text-lg font-semibold text-gray-900 dark:text-gray-100">Welcome to Trinity</h2>
      <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">{{ words.lead }}</p>

      <p class="mt-4 text-sm font-medium text-gray-900 dark:text-gray-100">What you can do</p>
      <ul class="mt-1 space-y-1">
        <li v-for="line in words.can" :key="line" class="flex items-start gap-2 text-sm text-gray-600 dark:text-gray-300">
          <svg class="mt-0.5 w-4 h-4 shrink-0 text-status-success-700 dark:text-status-success-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" /></svg>
          <span>{{ line }}</span>
        </li>
      </ul>

      <p class="mt-4 text-sm font-medium text-gray-900 dark:text-gray-100">Not part of this account</p>
      <ul class="mt-1 space-y-1">
        <li v-for="line in words.cannot" :key="line" class="flex items-start gap-2 text-sm text-gray-600 dark:text-gray-300">
          <svg class="mt-0.5 w-4 h-4 shrink-0 text-gray-500 dark:text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M5 12h14" /></svg>
          <span>{{ line }}</span>
        </li>
      </ul>

      <p v-if="words.empty" class="mt-4 rounded-lg bg-gray-50 dark:bg-gray-900 px-3 py-2 text-sm text-gray-600 dark:text-gray-300" data-testid="account-welcome-empty">{{ words.empty }}</p>
      <p class="mt-4 text-[12.5px] text-gray-500 dark:text-gray-400">{{ words.closing }}</p>

      <div class="mt-5 flex items-center justify-end gap-2">
        <BaseButton variant="ghost" data-testid="account-welcome-skip" @click="$emit('done')">Skip</BaseButton>
        <BaseButton data-testid="account-welcome-tour" @click="touring = true">Show me around</BaseButton>
      </div>
    </div>
  </BaseModal>
  <PortalBuildTour
    v-if="words"
    :open="open && touring"
    :steps="steps"
    :min-steps="1"
    :done-label="kind === 'operator' ? 'Open the Dashboard' : 'Got it'"
    testid="account-tour"
    @close="endTour"
  />
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import PortalBuildTour from './PortalBuildTour.vue'
import { accountWelcome, welcomeTourSteps } from './accountWelcome'
import { walkLink } from '@/components/onboarding/showMeWalk'

const props = defineProps({
  open: { type: Boolean, default: false },
  // 'user' or 'operator' (`accountKind`); anything else shows nothing.
  kind: { type: String, default: null },
  // The agents they can reach, for the "nothing shared yet" line and the operator's walk.
  agents: { type: Array, default: () => [] },
})
const emit = defineEmits(['done'])

const touring = ref(false)
const words = computed(() => accountWelcome(props.kind, { agents: props.agents }))
const steps = computed(() => welcomeTourSteps(props.kind))
// Their first agent's Tasks, walked from the Dashboard; with no agent yet, the Dashboard itself.
const dashboardLink = computed(() => walkLink('tasks', props.agents[0]?.name) || '/')

function endTour(reason) {
  touring.value = false
  // The last stop's button is the way there: pressed, in the same click, so the tab opens.
  if (reason === 'done' && props.kind === 'operator') window.open(dashboardLink.value, '_blank', 'noopener')
  emit('done')
}

watch(() => props.open, (now) => { if (!now) touring.value = false })
</script>
