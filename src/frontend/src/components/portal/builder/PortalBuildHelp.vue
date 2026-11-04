<!--
  How to reach the team. Building agents isn't always simple, so this is one click
  away from the sidebar, the Build page and onboarding. The addresses are in
  `BUILD_SUPPORT`.
-->
<template>
  <BaseModal
    :model-value="open"
    labelledby="build-help-title"
    panel-class="relative w-full max-w-lg rounded-[10px] border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-6 shadow-2xl"
    data-testid="build-help"
    @update:model-value="$emit('close')"
  >
    <h2 id="build-help-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Get help</h2>
    <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
      Building agents isn't always simple, and you don't have to work it out alone. Your guide can answer a lot in the chat. For anything else, here's how to reach us.
    </p>

    <ul class="mt-4 space-y-3">
      <li>
        <a :href="mailto" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-email">Email the team: {{ BUILD_SUPPORT.email }}</a>
        <p class="text-xs text-gray-500 dark:text-gray-400">Tell us what you're trying to do and where you got stuck. Please leave out passwords and keys.</p>
      </li>
      <li>
        <a :href="BUILD_SUPPORT.workshops" target="_blank" rel="noopener noreferrer" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-workshops">Join a live workshop</a>
        <p class="text-xs text-gray-500 dark:text-gray-400">Build along with the team and ask questions as you go.</p>
      </li>
      <li>
        <a :href="BUILD_SUPPORT.docs" target="_blank" rel="noopener noreferrer" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-docs">Read the documentation</a>
        <p class="text-xs text-gray-500 dark:text-gray-400">Guides to everything in Trinity.</p>
      </li>
      <li>
        <button type="button" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-intro" @click="showIntro = !showIntro">{{ showIntro ? 'Hide the intro' : 'Watch the intro again' }}</button>
        <p class="text-xs text-gray-500 dark:text-gray-400">What an agent is, and what a company run with agents looks like.</p>
      </li>
      <li>
        <button type="button" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-what-is-an-agent" @click="showExplainer = !showExplainer">{{ showExplainer ? 'Hide the explanation' : "What's an agent?" }}</button>
        <p class="text-xs text-gray-500 dark:text-gray-400">The difference between asking Claude and using an agent, with one everyday job.</p>
      </li>
    </ul>

    <div class="mt-3">
      <button type="button" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-tour" @click="$emit('tour')">Take the tour of the Workspace</button>
      <p class="text-xs text-gray-500 dark:text-gray-400">A quick look at your agents, your company page and your Inbox.</p>
    </div>
    <div v-if="canOpenDashboard" class="mt-3">
      <button type="button" class="text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" data-testid="build-help-features" @click="$emit('features')">Show me the advanced features</button>
      <p class="text-xs text-gray-500 dark:text-gray-400">A tour of the Dashboard: agents working together, schedules, channels, approvals and more.</p>
    </div>

    <div v-if="showIntro" class="mt-4"><IntroVideo /></div>
    <div v-if="showExplainer" class="mt-4"><WhatIsAnAgent :dismissible="false" /></div>

    <div class="mt-5 flex justify-end">
      <BaseButton variant="secondary" size="sm" data-testid="build-help-close" @click="$emit('close')">Close</BaseButton>
    </div>
  </BaseModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import IntroVideo from '@/components/onboarding/IntroVideo.vue'
import WhatIsAnAgent from '@/components/onboarding/WhatIsAnAgent.vue'
import { BUILD_SUPPORT, supportMailto } from './portalBuild'

const props = defineProps({
  open: { type: Boolean, default: false },
  // The feature tour is on the Dashboard, which Workspace-only people can't open.
  canOpenDashboard: { type: Boolean, default: false },
})
defineEmits(['close', 'tour', 'features'])

const showIntro = ref(false)
const showExplainer = ref(false)
const mailto = computed(() => supportMailto())
watch(() => props.open, () => { showIntro.value = false; showExplainer.value = false })
</script>
