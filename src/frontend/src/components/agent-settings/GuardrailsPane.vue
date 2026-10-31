<template>
  <!--
    Settings › Guardrails — "What it may do" (trinity-enterprise#890).

    Everything that bounds the agent, read in one place: the guardrail rules
    and the self-change grants (ent#756) that used to head the flat Settings
    list, a count of the approval gates set on the Skills tab (ent#754) so the
    bound is visible where bounds are read, the autonomy switch, cross-model
    validation (entitled only) and the dispatch circuit breaker.

    Unprompted work is READ-ONLY until the autonomy dial (ent#641,
    abilityai/trinity#2984) merges: this pane renders what that PR ships and
    defines none of its semantics, so before it lands it shows the current
    switch and points at the header chip, which stays the control.
  -->
  <div class="space-y-6">
    <SettingsSection title="Bounds">
      <SettingRow
        label="Guardrail rules"
        hint="Hard limits the runtime enforces on every run, whatever the instructions say. Blank means the platform default."
        testid="guardrails-rules"
      >
        <template #value>
          <span v-if="rules.state === 'loading'" class="block h-3 w-40 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading rules…</span></span>
          <span v-else-if="rules.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't load the rules.</span>
          <span v-else class="text-[12.5px] tabular-nums text-gray-700 dark:text-gray-300">{{ rulesSummary }}</span>
        </template>
        <template #action>
          <BaseButton variant="secondary" size="sm" :aria-expanded="String(rulesOpen)" @click="rulesOpen = !rulesOpen">
            {{ rulesOpen ? 'Close' : 'Manage rules' }}
          </BaseButton>
        </template>
        <template v-if="rulesOpen" #editor>
          <GuardrailsPanel :agent-name="agentName" :notify="notify" embedded @saved="onRulesSaved" />
        </template>
      </SettingRow>

      <div v-if="!grantsStore.hidden" class="px-4 py-3" data-testid="guardrails-self-change">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Permissions to change itself</p>
        <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
          What this agent may change about itself and other agents on its own. Each one is off until an
          instance admin turns it on.
        </p>
        <SelfChangePermissionsPanel :agent-name="agentName" embedded />
      </div>

      <SettingRow
        label="Skills that need approval"
        hint="Set per skill on the Skills tab. A gated skill waits for a person before it runs."
        testid="guardrails-gated-skills"
      >
        <template #value>
          <span v-if="gates.state === 'loading'" class="block h-3 w-16 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-busy="true"><span class="sr-only">Loading approval gates…</span></span>
          <span v-else-if="gates.state === 'failed'" class="text-[12.5px] text-gray-600 dark:text-gray-400">Couldn't load the gates.</span>
          <span v-else class="text-sm tabular-nums text-gray-900 dark:text-gray-100" data-testid="guardrails-gated-count">
            {{ gatedCount === 0 ? 'None gated' : `${gatedCount} gated` }}
          </span>
        </template>
        <template #action>
          <BaseButton variant="secondary" size="sm" @click="$emit('navigate-tab', 'skills')">Open Skills</BaseButton>
        </template>
      </SettingRow>
    </SettingsSection>

    <SettingsSection
      v-if="!isSystem"
      title="Unprompted work"
      description="Whether the agent acts on its own — runs its schedules and follows up without being asked."
    >
      <SettingRow
        label="Autonomy"
        hint="Flip it with the Autonomy chip in the page header; it is an operating mode, switched like Start and Stop."
        testid="guardrails-autonomy"
      >
        <template #badge>
          <BaseBadge variant="neutral" size="sm">read-only here</BaseBadge>
        </template>
        <template #value>
          <BaseBadge :variant="autonomyEnabled ? 'autonomous' : 'neutral'" size="sm" dot>
            {{ autonomyEnabled ? 'On' : 'Off' }}
          </BaseBadge>
        </template>
      </SettingRow>
      <p class="px-4 py-3 text-[12.5px] text-gray-600 dark:text-gray-400">
        The autonomy dial — an instance level, and each kind of ask graduated, on request or held — lands here
        when it ships. Until then this row shows the switch as it is.
      </p>
    </SettingsSection>

    <SettingsSection v-if="crossModelEntitled" title="Checks">
      <CrossModelValidationPanel :agent-name="agentName" embedded />
    </SettingsSection>

    <SettingsSection title="Reliability" description="What happens when runs keep failing.">
      <div class="px-4 py-3">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Dispatch circuit breaker</p>
        <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
          Fast-fails new tasks while the agent keeps failing on auth or billing, instead of burning retries, until it recovers.
        </p>
        <ReliabilityPanel :agent-name="agentName" :notify="notify" embedded />
      </div>
    </SettingsSection>

    <!-- trinity-enterprise#1022: keep this one agent out of retrieval memory. -->
    <SettingsSection title="Memory" description="What this agent carries from one run to the next.">
      <RetrievalMemoryToggle :agent-name="agentName" :notify="notify" />
    </SettingsSection>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { useCapabilityGrantsStore } from '../../stores/capabilityGrants'
import { useSkillGatesStore } from '../../stores/skillGates'
import { useEnterpriseStore } from '../../stores/enterprise'
import { useAgentRestartStore } from '../../stores/agentRestart'
import SettingsSection from './SettingsSection.vue'
import SettingRow from './SettingRow.vue'
import RetrievalMemoryToggle from './RetrievalMemoryToggle.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseBadge from '../base/BaseBadge.vue'
import GuardrailsPanel from '../GuardrailsPanel.vue'
import SelfChangePermissionsPanel from '../settings/SelfChangePermissionsPanel.vue'
import CrossModelValidationPanel from '../CrossModelValidationPanel.vue'
import ReliabilityPanel from '../ReliabilityPanel.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  autonomyEnabled: { type: Boolean, default: false },
  isSystem: { type: Boolean, default: false },
  notify: { type: Function, default: null },
})
defineEmits(['navigate-tab'])

const agentsStore = useAgentsStore()
const grantsStore = useCapabilityGrantsStore()
const gatesStore = useSkillGatesStore()
const enterprise = useEnterpriseStore()
const restartStore = useAgentRestartStore()

const crossModelEntitled = computed(() => enterprise.isEntitled('cross_model_validation'))

// ---- guardrail rules summary ------------------------------------------------
const rulesOpen = ref(false)
const rules = reactive({ state: 'loading', data: null })
const rulesSummary = computed(() => {
  const g = rules.data || {}
  const chat = g.max_turns_chat ?? 'default'
  const task = g.max_turns_task ?? 'default'
  return `Max turns: chat ${chat} · task ${task}`
})
async function loadRules() {
  if (!rules.data) rules.state = 'loading'
  try {
    const r = await agentsStore.getGuardrails(props.agentName)
    rules.data = r?.guardrails || {}
    rules.state = 'loaded'
  } catch {
    if (!rules.data) rules.state = 'failed'
  }
}
function onRulesSaved(payload) {
  rules.data = payload
  rules.state = 'loaded'
  // The runtime reads guardrails at start, so the save waits for a restart —
  // which the page's one banner now says, instead of a line in this card.
  restartStore.report(props.agentName, 'guardrails', 'Guardrail rules', true)
}

// ---- gated skills (ent#754) ------------------------------------------------
const gates = reactive({ state: 'loading' })
const gatedCount = computed(() => gatesStore.gates.length)
async function loadGates() {
  gates.state = 'loading'
  await gatesStore.load(props.agentName)
  gates.state = gatesStore.hasLoaded ? 'loaded' : 'failed'
}

function loadAll() {
  rules.data = null
  loadRules()
  loadGates()
}
onMounted(loadAll)
watch(() => props.agentName, loadAll)
</script>
