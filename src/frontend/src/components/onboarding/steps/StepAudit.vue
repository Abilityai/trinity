<!--
  Step `audit` (Workspace Build). The audit is a conversation with the guide, so starting
  it leaves setup and opens that chat straight away. Skipping moves on to the first
  agent, which has no audit to go on.

  It follows what they told us: someone who knows exactly what to build gets talking
  that through with the guide as the main button, with the audit still recommended, and
  an experienced person can instead give the guide a short talk about their work.
-->
<template>
  <div data-testid="first-run-step-audit">
    <FirstRunStepHeader
      kicker="AI audit"
      title="Find the right first agent"
      :lead="lead"
      :badge="started ? 'Started' : 'Recommended'"
    />
    <div class="mt-5">
      <div class="flex flex-wrap items-center gap-2">
        <BaseButton v-if="agentFirst" data-testid="first-run-audit-agent" :disabled="!ctx.hasEmail" @click="meeting = 'agent'">
          Talk it through with {{ BUILD_GUIDE_NAME }}
        </BaseButton>
        <BaseButton data-testid="first-run-audit-start" :variant="agentFirst ? 'secondary' : 'primary'" :disabled="!ctx.hasEmail" @click="meeting = 'audit'">
          {{ started ? 'Go to your audit' : 'Start the AI audit' }}
        </BaseButton>
      </div>
      <p class="mt-2 text-[12.5px] text-gray-500 dark:text-gray-400">
        {{ !ctx.hasEmail
          ? 'Add your sign-in email under About you first. The chat with your guide needs it.'
          : started ? 'It carries on in the chat with your guide.' : 'This opens the chat with your guide.' }}
      </p>
      <BaseButton
        v-if="offerProfileTalk"
        class="mt-3"
        variant="ghost"
        size="sm"
        data-testid="first-run-audit-profile"
        :disabled="!ctx.hasEmail"
        @click="meeting = 'profile'"
      >Or just tell {{ BUILD_GUIDE_NAME }} about your work (about 2 minutes)</BaseButton>
    </div>
    <MeetGuideDialog
      :visible="Boolean(meeting)"
      :purpose="meeting || 'audit'"
      @update:visible="(open) => { if (!open) meeting = '' }"
      @confirm="confirm"
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import MeetGuideDialog from '../MeetGuideDialog.vue'
import BaseButton from '../../base/BaseButton.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import {
  BUILD_GUIDE_NAME,
  auditEmphasis,
  auditLead,
  chooseAudit,
  chooseFirstAgent,
  chooseProfileChat,
  experienceRoute,
} from '../../portal/builder/portalBuild'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['update-intake', 'complete', 'skip'])

const lead = computed(() => auditLead(props.ctx.buildIntake))
// Which chat the pop-up is about to open: 'audit', 'agent' or 'profile'.
const meeting = ref('')
const started = computed(() => props.ctx.buildIntake?.audit === 'start')
// Once the audit is under way, going back to it stays the main button.
const agentFirst = computed(() => !started.value && auditEmphasis(props.ctx.buildIntake) === 'agent')
const offerProfileTalk = computed(() => !started.value && experienceRoute(props.ctx.buildIntake?.answers) === 'experienced')

function confirm() {
  const purpose = meeting.value
  meeting.value = ''
  const intake = props.ctx.buildIntake
  if (purpose === 'agent') emit('update-intake', chooseFirstAgent(intake, ''))
  else if (purpose === 'profile') emit('update-intake', chooseProfileChat(intake))
  else if (!started.value) emit('update-intake', chooseAudit(intake, 'start'))
  emit('complete', { next: '/workspace/build', finish: true })
}
</script>
