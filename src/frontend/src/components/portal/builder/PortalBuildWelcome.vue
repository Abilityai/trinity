<!--
  The Build chat before anything has been said in it: the guide says who it is, what
  it is for, and how a first agent goes, then starts it in one press. A first agent
  is walked level by level, so the steps name the levels. Someone who chose the AI audit
  starts with it, and nothing here offers to build before it. Someone with agents of
  their own is offered the next one instead.

  Rendered in the conversation's `#empty` slot, so it goes the moment a turn starts.
  The start buttons send; typing in the composer works as well.
-->
<template>
  <div class="py-8 sm:py-10" data-testid="build-welcome">
    <div class="mx-auto max-w-md">
      <div class="flex items-center gap-3">
        <PortalAvatar :name="guide.name" :avatar-url="guide.avatar_url" :size="40" />
        <div class="min-w-0">
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ guideLabel }}</div>
          <div class="text-xs text-gray-500 dark:text-gray-400">Your guide</div>
        </div>
      </div>

      <h2 class="mt-5 text-lg font-semibold text-gray-900 dark:text-gray-100">{{ copy.title }}</h2>
      <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
        {{ copy.body }}<template v-if="firstAgent">
          <!-- On a phone the page is its own tab. --><span class="hidden sm:inline">on the right.</span><span class="sm:hidden">under Your page.</span></template>
      </p>

      <template v-if="firstAgent">
        <div class="mt-5 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">How we'll go</div>
        <ol class="mt-2 space-y-3">
          <li v-for="(step, i) in copy.steps" :key="step.title" class="flex items-start gap-3">
            <span class="shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-xs font-semibold tabular-nums border border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300">{{ i + 1 }}</span>
            <span class="min-w-0">
              <span class="block text-sm font-medium text-gray-900 dark:text-gray-100">{{ step.title }}</span>
              <span class="block text-xs text-gray-500 dark:text-gray-400">{{ step.detail }}</span>
            </span>
          </li>
        </ol>
      </template>

      <div class="mt-6 flex flex-wrap items-center gap-2">
        <BaseButton data-testid="build-welcome-start" @click="$emit('start', copy.start.message)">{{ copy.start.label }}</BaseButton>
        <BaseButton v-if="copy.other" variant="secondary" @click="$emit('start', copy.other.message)">{{ copy.other.label }}</BaseButton>
      </div>
      <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">{{ copy.hint }}</p>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import BaseButton from '../../base/BaseButton.vue'
import PortalAvatar from '../PortalAvatar.vue'
import { WELCOME_STARTS } from './portalBuild'

const props = defineProps({
  guide: { type: Object, required: true },
  guideLabel: { type: String, required: true },
  // No agent of their own yet: the levels walk starts here.
  firstAgent: { type: Boolean, default: true },
  // They chose the AI audit and it isn't done yet.
  auditFirst: { type: Boolean, default: false },
})
defineEmits(['start'])

const BUILD_STEPS = [
  { title: 'I create your agent', detail: "That's Level 1. It shows up in your sidebar." },
  { title: 'We teach it its first job', detail: "That's Level 2. Keys and a schedule come later, each at its own level." },
]
const FIRST_AGENT_STEPS = [
  { title: 'We pick the job', detail: "Tell me a task you'd hand over, or I'll suggest one." },
  ...BUILD_STEPS,
]
const AUDIT_FIRST_STEPS = [
  { title: 'The AI audit', detail: "About 10 minutes of questions on how your company works. It ends with the agent I'd build first." },
  ...BUILD_STEPS,
]

const copy = computed(() => {
  if (props.firstAgent && props.auditFirst) {
    return {
      title: "Let's build your first agent",
      body: `I'm ${props.guideLabel}. I help you build agents: AI teammates that take a job off your hands, here on your company's Trinity. You chose to start with the AI audit, so that comes first. Then we build your first agent together, and you'll see it take shape `,
      steps: AUDIT_FIRST_STEPS,
      start: WELCOME_STARTS.audit,
      other: null,
      hint: "Or tell me below what you'd like to do.",
    }
  }
  if (props.firstAgent) {
    return {
      title: "Let's build your first agent",
      body: `I'm ${props.guideLabel}. I help you build agents: AI teammates that take a job off your hands, here on your company's Trinity. We'll build your first one together, one step at a time, and you'll see it take shape `,
      steps: FIRST_AGENT_STEPS,
      start: WELCOME_STARTS.first,
      other: WELCOME_STARTS.suggest,
      hint: "Or tell me below what you'd like it to do.",
    }
  }
  return {
    title: 'What shall we build next?',
    body: `I'm ${props.guideLabel}, your guide. I can plan and create a new agent with you, teach one of yours a new job, or change how one is set up, such as its schedule or who can use it.`,
    start: WELCOME_STARTS.next,
    other: WELCOME_STARTS.improve,
    hint: 'Or tell me below what you have in mind.',
  }
})
</script>
