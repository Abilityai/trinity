<!--
  The onboarding window: opens by itself on someone's first visit to the Workspace and
  walks them through the steps one at a time. It can't be clicked away or closed with
  Esc. "Skip for now" is the way out, and that leaves the profile showing in red, from
  where this window opens again.

  The questions run in the browser, so they answer instantly and spend nothing. The
  guide's first turn starts when the window has closed on the Build page. The AI audit
  itself is a conversation, so the last step here only asks whether to do it now.
-->
<template>
  <BaseModal
    :model-value="open"
    :close-on-backdrop="false"
    labelledby="build-onboarding-title"
    panel-class="relative flex flex-col w-full max-w-xl h-[480px] max-h-full overflow-hidden rounded-[10px] border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 shadow-2xl"
    data-testid="build-onboarding"
  >
    <div class="shrink-0 px-8 pt-6 max-sm:px-4">
      <p class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="build-onboarding-progress">Step {{ stepIndex + 1 }} of {{ steps.length }}</p>
      <div class="mt-2 h-1 rounded-full bg-gray-200 dark:bg-gray-750" aria-hidden="true">
        <div class="h-1 rounded-full bg-action-primary-600 dark:bg-action-primary-500 transition-[width] duration-200 motion-reduce:transition-none" :style="{ width: `${((stepIndex + 1) / steps.length) * 100}%` }"></div>
      </div>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto px-8 py-6 max-sm:px-4">
      <div v-if="step === 'welcome'" data-testid="build-onboarding-welcome">
        <h2 id="build-onboarding-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Let's get started</h2>
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
          We'll set you up to build real agents for your company. Here's what happens next:
        </p>
        <ol class="mt-4 space-y-3">
          <li v-for="(line, i) in WELCOME_LINES" :key="line.title" class="flex items-start gap-3">
            <span class="shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-xs font-semibold tabular-nums border border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300">{{ i + 1 }}</span>
            <span class="min-w-0">
              <span class="block text-sm font-medium text-gray-900 dark:text-gray-100">{{ line.title }}</span>
              <span class="block text-xs text-gray-500 dark:text-gray-400">{{ line.detail }}</span>
            </span>
          </li>
        </ol>
      </div>

      <form v-else-if="question && !question.choices" :data-testid="`build-onboarding-${question.id}`" @submit.prevent="answer(draft)">
        <label id="build-onboarding-title" for="build-onboarding-input" class="block text-lg font-semibold text-gray-900 dark:text-gray-100">{{ question.prompt }}</label>
        <p v-if="question.hint" class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ question.hint }}</p>
        <p v-if="researchNote" class="mt-2 text-sm text-status-danger-700 dark:text-status-danger-400" data-testid="build-onboarding-research-failed">{{ researchNote }}</p>
        <div class="mt-4">
          <BaseInput
            id="build-onboarding-input"
            v-model="draft"
            :placeholder="question.placeholder"
            data-testid="build-onboarding-input"
          />
        </div>
      </form>

      <div v-else-if="question" :data-testid="`build-onboarding-${question.id}`">
        <h2 id="build-onboarding-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ question.prompt }}</h2>
        <div class="mt-4 flex flex-col gap-2" data-testid="build-onboarding-choices">
          <BaseButton
            v-for="choice in question.choices"
            :key="choice.id"
            :variant="intake.answers[question.id] === choice.id ? 'primary' : 'secondary'"
            @click="answer(choice.id)"
          >{{ choice.label }}</BaseButton>
        </div>
      </div>

      <WhatIsAnAgent
        v-else-if="step === 'explain'"
        title-id="build-onboarding-title"
        :software="intake.answers.software"
        @done="explained"
      />

      <div v-else-if="step === 'connect'" data-testid="build-onboarding-connect">
        <h2 id="build-onboarding-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Connect Claude</h2>
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
          Your answers are saved. The guide and the agents it builds think with Claude, and it isn't connected to this instance yet. It takes about two minutes, using your Claude plan or an API key.
        </p>
        <div class="mt-4 flex flex-wrap items-center gap-2">
          <a
            href="/?onboarding=1"
            target="_blank"
            rel="noopener"
            class="inline-flex items-center rounded-md bg-action-primary-600 hover:bg-action-primary-700 px-3.5 py-2 text-sm font-medium text-white"
            data-testid="build-onboarding-connect-open"
          >Connect Claude</a>
          <BaseButton variant="secondary" data-testid="build-onboarding-connect-recheck" @click="recheckClaude">I've connected it</BaseButton>
        </div>
        <p v-if="recheckedMissing" class="mt-3 text-sm text-gray-600 dark:text-gray-300" role="status" data-testid="build-onboarding-connect-missing">
          Claude isn't showing as connected yet. Finish in the other tab, then try again.
        </p>
      </div>

      <div v-else-if="step === 'setup'" data-testid="build-onboarding-setup">
        <h2 id="build-onboarding-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Set up your guide</h2>
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
          You build here with your own guide: an agent of yours on this instance that you talk to, and that builds the agents with you.
        </p>
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
          It will be allowed to create agents on this instance, and it asks you to approve each one first.
        </p>
        <BaseButton class="mt-4" :loading="deploying" data-testid="build-onboarding-deploy" @click="deploy">
          {{ deploying ? 'Setting up…' : 'Set up the guide' }}
        </BaseButton>
        <InlineError v-if="deployError" class="mt-4" :message="deployError" @dismiss="deployError = ''" />
      </div>

      <div v-else data-testid="build-onboarding-audit">
        <h2 id="build-onboarding-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Find the right first agent</h2>
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">{{ auditLine }}</p>
        <p v-if="researchNote" class="mt-2 text-sm text-status-danger-700 dark:text-status-danger-400" data-testid="build-onboarding-research-failed">{{ researchNote }}</p>
        <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
          You'll chat with your guide on the left and see everything take shape on the right. Anything you build is real, and it stays on your Trinity instance.
        </p>
        <div class="mt-4 flex flex-wrap items-center gap-2">
          <BaseButton v-if="agentFirst" data-testid="build-onboarding-audit-agent" @click="$emit('finish', 'agent')">Talk it through with {{ BUILD_GUIDE_NAME }}</BaseButton>
          <BaseButton :variant="agentFirst ? 'secondary' : 'primary'" data-testid="build-onboarding-audit-start" @click="$emit('finish', 'start')">Start the AI audit</BaseButton>
          <BaseButton v-if="!agentFirst" variant="secondary" data-testid="build-onboarding-audit-skip" @click="$emit('finish', 'skip')">Skip it and build a test agent</BaseButton>
        </div>
      </div>
    </div>

    <footer class="shrink-0 flex flex-wrap items-center gap-2 border-t border-gray-200 dark:border-gray-750 px-8 py-3.5 max-sm:px-4">
      <BaseButton v-if="stepIndex > 0" variant="ghost" size="sm" data-testid="build-onboarding-back" @click="back">Back</BaseButton>
      <button type="button" class="text-xs text-gray-500 dark:text-gray-400 hover:underline underline-offset-2" data-testid="build-onboarding-help" @click="$emit('help')">Need help?</button>
      <span class="flex-1"></span>
      <BaseButton v-if="step !== 'audit'" variant="ghost" size="sm" data-testid="build-onboarding-skip" @click="skip">Skip for now</BaseButton>
      <BaseButton v-if="question?.optional && !question.choices && !draft.trim()" variant="secondary" data-testid="build-onboarding-no-website" @click="next">We don't have one</BaseButton>
      <BaseButton v-if="step === 'welcome'" data-testid="build-onboarding-start" @click="next">Get started</BaseButton>
      <BaseButton
        v-else-if="question && !question.choices"
        :disabled="!draft.trim()"
        data-testid="build-onboarding-continue"
        @click="answer(draft)"
      >Continue</BaseButton>
    </footer>
  </BaseModal>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import InlineError from '@/components/InlineError.vue'
import WhatIsAnAgent from '@/components/onboarding/WhatIsAnAgent.vue'
import { useAuthStore } from '@/stores/auth'
import { useClientPortalStore } from '@/stores/clientPortal'
import { useSessionsStore } from '@/stores/sessions'
import {
  BUILD_GUIDE_NAME,
  INTAKE_QUESTIONS,
  auditEmphasis,
  auditLead,
  intakeGroupDone,
  myGuideCard,
  needsAgentExplainer,
  onboardingStartStep,
  onboardingSteps,
  shouldStartResearch,
} from './portalBuild'
import { startCompanyResearch } from './useCompanyResearch'
import { deployMyGuide, myGuideName } from './useMyGuide'

const WELCOME_LINES = Object.freeze([
  Object.freeze({ title: 'Tell us about you', detail: 'A few quick questions, so your guide can start at the right level.' }),
  Object.freeze({ title: 'Tell us your company', detail: 'Your guide researches it from public sources while you carry on.' }),
  Object.freeze({ title: 'Do the AI audit', detail: 'Your guide asks how your team works and finds where an agent would save the most time.' }),
  Object.freeze({ title: 'Build your first agent', detail: 'Your guide builds it with you, explains each step, and asks before creating anything.' }),
])

const props = defineProps({
  open: { type: Boolean, default: false },
  intake: { type: Object, required: true },
  roster: { type: Array, default: () => [] },
})
const emit = defineEmits(['answer', 'skip', 'close', 'finish', 'research-started', 'research-failed', 'help', 'explained'])

const store = useClientPortalStore()
const sessions = useSessionsStore()
const authStore = useAuthStore()

const claudeMissing = computed(() => sessions.featureFlagsLoaded && !sessions.featureFlagsFailed && !sessions.claudeAuthConfigured)
// Their own guide, never someone else's shared with them.
const guideMissing = computed(() => !myGuideCard(props.roster))

const step = ref('welcome')
const draft = ref('')
const deploying = ref(false)
const deployError = ref('')
const recheckedMissing = ref(false)
const researchNote = ref('')

const steps = computed(() => onboardingSteps({
  claudeMissing: claudeMissing.value,
  guideMissing: guideMissing.value,
  explain: needsAgentExplainer(props.intake),
  current: step.value,
}))
const stepIndex = computed(() => Math.max(steps.value.indexOf(step.value), 0))
const question = computed(() => INTAKE_QUESTIONS.find((q) => q.id === step.value) || null)
const auditLine = computed(() => auditLead(props.intake))
// They know exactly what to build: talking it through comes first, the audit stays offered.
const agentFirst = computed(() => auditEmphasis(props.intake) === 'agent')

function goTo(key) {
  step.value = key
  draft.value = props.intake.answers[key] || ''
  focusField()
}

function focusField() {
  // After the modal's own first focus, which lands on the first button: Skip.
  setTimeout(() => {
    const target = document.getElementById('build-onboarding-input')
      || document.querySelector('[data-testid="build-onboarding-start"]')
    target?.focus()
  })
}

function next() {
  const after = steps.value[stepIndex.value + 1]
  if (after === 'audit') startResearch()
  if (after) goTo(after)
}

// Leaving the website question: the company and its site are known, so the research
// starts here and carries on while they talk. If it can't start yet, the Build page
// tries again when the chat opens.
async function startResearch() {
  // The last answer reaches this window's props on the next tick.
  await nextTick()
  if (claudeMissing.value || guideMissing.value || !shouldStartResearch(props.intake)) return
  researchNote.value = ''
  emit('research-started', '')
  try {
    const username = authStore.principalId
    emit('research-started', await startCompanyResearch(store, props.intake, { guideName: myGuideName({ roster: props.roster, username }), username }))
  } catch (e) {
    researchNote.value = e.message
    emit('research-failed')
  }
}

function back() {
  const before = steps.value[stepIndex.value - 1]
  if (before) goTo(before)
}

async function answer(value) {
  if (typeof value === 'string' && !value.trim()) return
  emit('answer', step.value, value)
  // The answer reaches this window's props on the next tick, and the step after it can
  // depend on it: "What's an agent?" follows "I've never built one".
  await nextTick()
  next()
}

function explained() {
  emit('explained')
  next()
}

// Leaving with a question still unanswered leaves the profile incomplete, whichever step
// it's from. With them all answered, leaving only postpones what's left.
function skip() {
  if (intakeGroupDone(props.intake, 'you') && intakeGroupDone(props.intake, 'company')) emit('close')
  else emit('skip')
}

async function recheckClaude() {
  await sessions.loadFeatureFlags(true)
  recheckedMissing.value = claudeMissing.value
  if (!claudeMissing.value && step.value === 'connect') next()
}

async function deploy() {
  deploying.value = true
  deployError.value = ''
  try {
    deployError.value = (await deployMyGuide(store, authStore.principalId)).failure
    await store.fetchRoster()
  } finally {
    deploying.value = false
  }
}

// These two steps finish outside this window, so it moves on when they have.
watch(guideMissing, (missing) => {
  if (!missing && step.value === 'setup' && !deployError.value) next()
})
watch(claudeMissing, (missing) => {
  if (!missing && step.value === 'connect') next()
  // A reload can open on the company before the flags have arrived.
  if (missing && step.value === 'company' && !props.intake.answers.company) goTo('connect')
})

// A reload picks up at the question they had reached.
watch(() => props.open, (open) => {
  if (!open) return
  sessions.loadFeatureFlags()
  recheckedMissing.value = false
  deployError.value = ''
  goTo(onboardingStartStep(props.intake))
}, { immediate: true })

function onWindowFocus() {
  if (props.open && step.value === 'connect') sessions.loadFeatureFlags(true)
}
onMounted(() => window.addEventListener('focus', onWindowFocus))
onBeforeUnmount(() => window.removeEventListener('focus', onWindowFocus))
</script>
