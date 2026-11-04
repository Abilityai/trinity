<!--
  Step `agent` (ent#581) — absorbs the front desk (ent#319) and the ent#52
  wizard's intro. The three doors:

    Show me     — watch a seeded agent work; zero commitment. Only when the
                  install actually seeded something to show.
    Make me one — one question, then the REAL CreateAgentModal with the right
                  template prefilled (teach by doing — not a clone of the form).
    Bring mine  — deliberately a de-emphasised line, never a peer button: fleet
                  migration is the strongest capability and the worst first
                  impression.

  Leaving the Dashboard would leave the sequence, so a door records WHERE to go
  and the chassis goes there on "Done" (principle 27: offer the next step).
  Completion is the server's first-run flag going false once an agent of your
  own exists; "Show me" completes the step for this session only.

  Workspace Build: an admin has a guide, so a first agent starts as a conversation with
  it rather than as a form. After the audit it comes from what the audit found, so
  there's nothing to pick. Without one, starting points are all there is to offer, and
  the form stays one quiet link away.
-->
<template>
  <div data-testid="first-run-step-agent">
    <FirstRunStepHeader
      kicker="Agents"
      title="Your first agent"
      :lead="ctx.firstRun
        ? 'Trinity runs agents. Here are three ways in — none of them commit you to anything.'
        : 'You already have an agent of your own. Make another any time — or skip this.'"
      :badge="ctx.firstRun ? 'Optional' : 'Done'"
      schematic="agent"
    />

    <div class="mt-5 space-y-5">
      <!-- Workspace Build: the guide's route to a first agent, from the AI audit step. -->
      <section v-if="auditStarted" data-testid="first-run-agent-guide">
        <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">From your audit</h3>
        <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
          Your guide recommends your first agent from what the audit finds, then walks you through building it.
        </p>
        <BaseButton class="mt-2" size="sm" :disabled="!ctx.hasEmail" data-testid="first-run-agent-to-guide" @click="ask('')">
          Go to your guide
        </BaseButton>
      </section>

      <template v-else-if="ctx.isAdmin">
        <p v-if="!ctx.hasEmail" class="text-[12.5px] text-gray-500 dark:text-gray-400" data-testid="first-run-agent-needs-email">
          Add your sign-in email under About you first. The chat with your guide needs it.
        </p>
        <section data-testid="first-run-agent-scratch">
          <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">Tell your guide what you have in mind</h3>
          <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
            It asks what the agent should do, then walks you through building it.
          </p>
          <BaseButton class="mt-2" size="sm" :disabled="!ctx.hasEmail" data-testid="first-run-agent-scratch-start" @click="ask('')">
            Start from scratch
          </BaseButton>
        </section>
        <section>
          <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">Or start from a common first agent</h3>
          <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
            You skipped the audit, so these aren't tailored to you. Your guide shapes whichever you pick with you.
          </p>
          <div class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
            <BaseButton
              v-for="p in STARTING_POINTS"
              :key="p.key"
              class="w-full"
              variant="secondary"
              :disabled="!ctx.hasEmail"
              :data-testid="`first-run-idea-${p.key}`"
              @click="ask(p.idea)"
            >
              <span class="flex w-full flex-col items-start text-left">
                <span>{{ p.title }}</span>
                <span class="text-[12.5px] font-normal text-gray-500 dark:text-gray-400">{{ p.desc }}</span>
              </span>
            </BaseButton>
          </div>
        </section>
      </template>

      <section v-if="ctx.demoAgent && !auditStarted">
        <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">Show me</h3>
        <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
          Watch an agent this install already runs, before you build anything.
        </p>
        <BaseButton
          class="mt-2"
          variant="secondary"
          size="sm"
          data-testid="first-run-show-me"
          @click="showMe"
        >
          Watch {{ ctx.demoAgent }} work
        </BaseButton>
      </section>

      <section v-if="!ctx.isAdmin">
        <h3 class="text-sm font-[550] text-gray-900 dark:text-gray-100">Make me one</h3>
        <p class="mt-0.5 text-[12.5px] text-gray-500 dark:text-gray-400">
          Pick what it should do — the create form opens with the right template ready.
        </p>
        <div class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <BaseButton
            v-for="p in PURPOSES"
            :key="p.key"
            class="w-full"
            variant="secondary"
            :data-testid="`first-run-purpose-${p.key}`"
            @click="select(p)"
          >
            <span class="flex w-full flex-col items-start text-left">
              <span>{{ p.title }}</span>
              <span class="text-[12.5px] font-normal text-gray-500 dark:text-gray-400">{{ p.desc }}</span>
            </span>
          </BaseButton>
        </div>
      </section>

      <p v-if="chosen" role="status" class="text-sm text-gray-600 dark:text-gray-300" data-testid="first-run-agent-chosen">
        {{ chosen }}
      </p>

      <button
        v-if="ctx.isAdmin && !auditStarted"
        type="button"
        data-testid="first-run-agent-form"
        class="block rounded text-[12.5px] text-gray-500 underline underline-offset-2
               hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100
               focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
        @click="select(PURPOSES[3])"
      >
        Prefer a form? Create an agent yourself →
      </button>

      <!-- Bring mine: reachable, never a peer of the other two (ent#319 AC).
           Until an in-app migration surface exists it points at the docs. -->
      <a
        href="https://docs.ability.ai"
        target="_blank"
        rel="noopener noreferrer"
        data-testid="first-run-bring-mine"
        class="inline-block rounded text-[12.5px] text-gray-500 underline underline-offset-2
               hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100
               focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
      >
        Already run a fleet? Bring it over →
      </a>
    </div>

    <MeetGuideDialog v-model:visible="meeting" purpose="agent" @confirm="auditStarted ? goToGuide() : buildWithGuide(idea)" />

    <!-- The real create form, teleported so the overlay panel's overflow
         cannot clip it; it sits above the overlay and owns the keyboard. -->
    <Teleport to="body">
      <CreateAgentModal
        v-if="creating"
        :initial-template="template"
        @created="onCreated"
        @close="creating = false"
      />
    </Teleport>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { chooseFirstAgent } from '../../portal/builder/portalBuild'
import { useNetworkStore } from '../../../stores/network'
import { useProductTelemetryStore } from '../../../stores/productTelemetry'
import BaseButton from '../../base/BaseButton.vue'
import CreateAgentModal from '../../CreateAgentModal.vue'
import MeetGuideDialog from '../MeetGuideDialog.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip', 'update-intake'])

const auditStarted = computed(() => props.ctx.isAdmin && props.ctx.buildIntake?.audit === 'start')

const network = useNetworkStore()
const telemetry = useProductTelemetryStore()

// Intent → starter template (ent#52). Each maps to a real local template in
// config/agent-templates; CreateAgentModal falls back to a blank agent when a
// mapped template is missing in this deploy, so no existence pre-check here.
const PURPOSES = [
  { key: 'research', title: 'Research a market or topic', desc: 'Scans trends and competitors, summarizes findings.', template: 'local:scout' },
  { key: 'strategy', title: 'Advise on strategy', desc: 'Turns inputs into clear, actionable recommendations.', template: 'local:sage' },
  { key: 'writing', title: 'Write content & reports', desc: 'Drafts reports, proposals, and client deliverables.', template: 'local:scribe' },
  { key: 'blank', title: 'Start from scratch', desc: 'A blank Claude Code agent you shape yourself.', template: '' },
]

// Workspace Build: the same three, as ideas for the guide rather than templates.
const STARTING_POINTS = [
  { key: 'research', title: 'Research', desc: 'Scans a market or topic and summarizes it.', idea: 'researches a market or topic and summarizes what it finds' },
  { key: 'strategy', title: 'Strategy', desc: 'Turns inputs into clear recommendations.', idea: 'turns inputs into clear, actionable strategy recommendations' },
  { key: 'writing', title: 'Writing', desc: 'Drafts reports, proposals and deliverables.', idea: 'drafts reports, proposals and client deliverables' },
]

const meeting = ref(false)
const idea = ref('')

function ask(chosen) {
  idea.value = chosen
  meeting.value = true
}

function goToGuide() {
  meeting.value = false
  emit('complete', { next: '/workspace/build', finish: true })
}

function buildWithGuide(chosen) {
  emit('update-intake', chooseFirstAgent(props.ctx.buildIntake, chosen))
  goToGuide()
}

const creating = ref(false)
const template = ref('')
const chosen = ref('')

function showMe() {
  const name = props.ctx.demoAgent
  if (!name) return
  chosen.value = `Finishing setup opens ${name}'s chat, so you can watch it work.`
  emit('complete', { next: `/agents/${name}?tab=chat`, label: chosen.value })
}

function select(p) {
  template.value = p.template
  creating.value = true
  telemetry.record('setup_step_create', { purpose: p.key })
}

function onCreated(agent) {
  // CreateAgentModal emits `created` then `close`; unmounting on `created` is
  // what the wizard did, so the github validation step never renders here.
  creating.value = false
  const name = agent?.name || ''
  // The WS agent_created event can lag while the container spins up.
  network.fetchAgents()
  chosen.value = name
    ? `${name} is created. Finishing setup opens its chat.`
    : 'Your agent is created.'
  emit('complete', name ? { next: `/agents/${name}?tab=chat`, label: chosen.value } : undefined)
}
</script>
