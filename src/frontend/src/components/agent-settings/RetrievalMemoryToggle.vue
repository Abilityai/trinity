<template>
  <!--
    Per-agent retrieval-memory opt-out (trinity-enterprise#1022). The platform
    switch lives in Settings → General; this row keeps ONE agent out of it.
    Honest when the platform is off: the row says so instead of letting the
    owner believe a saved choice is doing anything.
  -->
  <SettingRow
    label="Retrieval memory"
    :hint="hint"
    testid="retrieval-memory-row"
  >
    <template #value>
      <span v-if="view.state === 'loading'" class="text-xs text-gray-500 dark:text-gray-400">Loading…</span>
      <span v-else class="text-xs text-gray-500 dark:text-gray-400" data-testid="retrieval-memory-agent-status">
        <template v-if="!state.enabled_platform">Platform switch is off</template>
        <template v-else-if="state.opt_out">Kept out</template>
        <template v-else-if="!state.available">On, unavailable — {{ state.reason || 'model not loaded' }}</template>
        <template v-else>On · {{ state.fact_count }} fact{{ state.fact_count === 1 ? '' : 's' }}</template>
      </span>
    </template>
    <template #action>
      <BaseToggle
        :model-value="!state.opt_out"
        :disabled="saving || view.state === 'loading'"
        aria-label="Retrieval memory for this agent"
        data-testid="retrieval-memory-agent-toggle"
        @update:model-value="onToggle"
      />
    </template>
  </SettingRow>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../../api'
import BaseToggle from '../base/BaseToggle.vue'
import SettingRow from './SettingRow.vue'
import { viewState } from '../../utils/loadingState'

const props = defineProps({
  agentName: { type: String, required: true },
  notify: { type: Function, default: null },
})

const state = ref({ enabled_platform: false, opt_out: false, available: false, reason: null, fact_count: 0 })
const loading = ref(false)
const hasLoaded = ref(false)
const saving = ref(false)
// Loading means "no data yet" (design-system p13/p14); a reload after a
// failed save keeps the row's text on screen.
const view = computed(() => viewState({ loading: loading.value, hasLoaded: hasLoaded.value }))

const hint = computed(() => state.value.enabled_platform
  ? 'Facts from this agent\'s runs are captured and recalled. Switch off to keep this agent out.'
  : 'The platform switch (Settings → General → Retrieval memory) is off, so this agent remembers nothing across runs yet. Your choice here is saved and applies once it is on.')

function say(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

async function load() {
  loading.value = true
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/retrieval-memory`)
    state.value = data || state.value
    hasLoaded.value = true
  } catch (e) {
    say('Could not read retrieval-memory settings.', 'error')
  } finally {
    loading.value = false
  }
}

async function onToggle(nextOn) {
  saving.value = true
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/retrieval-memory`, { opt_out: !nextOn })
    state.value = data || state.value
    say(nextOn ? 'Retrieval memory on for this agent.' : 'This agent is kept out of retrieval memory.')
  } catch (e) {
    say('Could not change retrieval memory for this agent.', 'error')
    await load()
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
