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
      <section v-if="ctx.demoAgent">
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

      <section>
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
import { ref } from 'vue'
import { useNetworkStore } from '../../../stores/network'
import { useProductTelemetryStore } from '../../../stores/productTelemetry'
import BaseButton from '../../base/BaseButton.vue'
import CreateAgentModal from '../../CreateAgentModal.vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'

const props = defineProps({ ctx: { type: Object, default: () => ({}) } })
const emit = defineEmits(['complete', 'skip'])

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
