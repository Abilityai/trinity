<!--
  Step `company` (Workspace Build). Comes after Connect Claude, because answering it
  starts the company research: the first real use of Claude, so it also shows whether
  the connection works. The research runs as its own job, so it keeps going while the
  person carries on and while the audit takes place.
-->
<template>
  <div data-testid="first-run-step-company">
    <FirstRunStepHeader
      kicker="Company"
      title="Your company"
      lead="Your guide researches your company from public sources: what it sells, its size, its finances where they're public, and its competitors. Then it and your agents use that, so you don't have to explain the basics."
      badge="Recommended"
    />
    <BuildQuestions group="company" :intake="ctx.buildIntake" @update-intake="$emit('update-intake', $event)" @answered="$emit('complete')" @all-done="start" />

    <div class="mt-4 text-sm" role="status" data-testid="first-run-research-status">
      <p v-if="phase === 'starting'" class="text-gray-600 dark:text-gray-300">Starting your guide and the research. This can take a minute.</p>
      <p v-else-if="phase === 'running'" class="text-gray-600 dark:text-gray-300">Your guide is researching your company. You can carry on: it keeps going in the background.</p>
      <p v-else-if="phase === 'done'" class="text-gray-600 dark:text-gray-300">Claude is working, and the research has finished. You'll find it under Findings.</p>
      <p v-else-if="phase === 'shared'" class="text-gray-600 dark:text-gray-300" data-testid="first-run-research-shared">A colleague has already told Trinity about your company, so its research is ready. Change anything here that's wrong.</p>
      <div v-else-if="phase === 'failed'">
        <p class="text-status-danger-700 dark:text-status-danger-400" data-testid="first-run-research-failed">{{ error }}</p>
        <BaseButton class="mt-2" variant="secondary" size="sm" data-testid="first-run-research-retry" @click="retry">Try again</BaseButton>
      </div>
    </div>
    <p v-if="phase === 'starting'" class="mt-2 text-[12.5px] text-gray-500 dark:text-gray-400">
      This also sets up your own guide, which will be allowed to create agents on this instance. It asks you to approve each one first.
    </p>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import BaseButton from '../../base/BaseButton.vue'
import { useAuthStore } from '../../../stores/auth'
import { useClientPortalStore } from '../../../stores/clientPortal'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import BuildQuestions from './BuildQuestions.vue'
import {
  BUILD_RESEARCH_CANVAS_ID, clearResearch, companyFromProfile, markResearchStarted, researchView, shouldStartResearch,
} from '../../portal/builder/portalBuild'
import { myGuideName } from '../../portal/builder/useMyGuide'
import { startCompanyResearch } from '../../portal/builder/useCompanyResearch'

const POLL_MS = 5000

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['update-intake', 'complete', 'skip'])

const store = useClientPortalStore()
const authStore = useAuthStore()
// The person's own guide: theirs on the roster, or the address a new one gets.
const guideName = () => myGuideName({ roster: store.agents, username: authStore.principalId })
const phase = ref(props.ctx.buildIntake?.research?.shared ? 'shared' : props.ctx.buildIntake?.research?.started ? 'running' : 'idle')
const error = ref('')
let timer = null
let executionId = props.ctx.buildIntake?.research?.executionId || ''

async function start() {
  // The last answer reaches this step's props on the next tick.
  await nextTick()
  if (!shouldStartResearch(props.ctx.buildIntake) || phase.value === 'starting') return
  phase.value = 'starting'
  error.value = ''
  emit('update-intake', markResearchStarted(props.ctx.buildIntake, '', Date.now(), guideName()))
  await nextTick()
  try {
    // The setup panel can get here before the Workspace has loaded the roster.
    if (!store.rosterLoaded) await store.fetchRoster().catch(() => {})
    executionId = await startCompanyResearch(store, props.ctx.buildIntake, { guideName: guideName(), username: authStore.principalId })
    emit('update-intake', markResearchStarted(props.ctx.buildIntake, executionId, Date.now(), guideName()))
    phase.value = 'running'
    follow()
  } catch (e) {
    emit('update-intake', clearResearch(props.ctx.buildIntake))
    phase.value = 'failed'
    error.value = e.message
  }
}

async function retry() {
  emit('update-intake', clearResearch(props.ctx.buildIntake))
  phase.value = 'idle'
  await start()
}

// Only here, where this step can say whether Claude works. The Build page takes over.
function follow() {
  clearTimeout(timer)
  timer = setTimeout(async () => {
    let canvas = null
    let execution = null
    try { canvas = await store.fetchAgentCanvas(guideName(), BUILD_RESEARCH_CANVAS_ID) } catch { /* not written yet */ }
    if (!canvas && executionId) {
      try { execution = await store.fetchGuideExecution(guideName(), executionId) } catch { /* try again next time */ }
    }
    const view = researchView({ intake: props.ctx.buildIntake, canvas, execution })
    if (view.status === 'done') phase.value = 'done'
    else if (view.status === 'failed') { phase.value = 'failed'; error.value = view.error }
    else follow()
  }, POLL_MS)
}

if (phase.value === 'running') follow()

// A colleague already told Trinity about the company: its answers are filled in from
// that, with Change beside them, and no second research runs (core-agent AC-14).
onMounted(async () => {
  if (props.ctx.buildIntake?.answers?.company) return
  try {
    const seeded = companyFromProfile(props.ctx.buildIntake, await store.fetchCompanyProfile())
    if (seeded !== props.ctx.buildIntake) {
      emit('update-intake', seeded)
      phase.value = 'shared'
    }
  } catch {
    // None yet (404): they answer the questions themselves.
  }
})
onBeforeUnmount(() => clearTimeout(timer))
</script>
