<template>
  <!--
    Settings › Integrations — "What calls it, what it calls" (trinity#966
    step 3). This step re-homes the two cards the old flat Settings list held
    for programmatic access — Expose via MCP (#846) and the agent's own Trinity
    access key (#1854) — under Programmatic access, as the migration map has
    them. The Permissions, A2A and Payments tabs keep their own homes until
    their step lands; the rows below open them.
  -->
  <div class="space-y-6">
    <SettingsSection title="Programmatic access" description="How MCP clients reach this agent, and the key the agent uses to call Trinity.">
      <div class="px-4 py-3">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Expose via MCP</p>
        <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
          Publish this agent as its own MCP tool. Callers still need ownership or a share to chat with it.
        </p>
        <McpExposedPanel :agent-name="agentName" :notify="notify" embedded />
      </div>
      <div class="px-4 py-3">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Trinity access key</p>
        <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
          The agent-scoped key this agent uses to call Trinity's own tools. It is what makes the agent-to-agent permissions apply.
        </p>
        <AgentMcpKeyPanel :agent-name="agentName" :notify="notify" embedded />
      </div>
    </SettingsSection>

    <SettingsSection v-if="links.length" title="Still on their own tabs">
      <SettingRow v-for="l in links" :key="l.tab" :label="l.label" :hint="l.hint">
        <template #action>
          <BaseButton variant="secondary" size="sm" @click="$emit('navigate-tab', l.tab)">Open {{ l.open }}</BaseButton>
        </template>
      </SettingRow>
    </SettingsSection>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import SettingsSection from './SettingsSection.vue'
import SettingRow from './SettingRow.vue'
import BaseButton from '../base/BaseButton.vue'
import McpExposedPanel from '../McpExposedPanel.vue'
import AgentMcpKeyPanel from '../AgentMcpKeyPanel.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  // The tab ids this viewer can open, so a pointer never leads to a tab that
  // is not there (the system agent has no Permissions tab, an OSS build no A2A).
  visibleTabIds: { type: Array, default: () => [] },
  notify: { type: Function, default: null },
})
defineEmits(['navigate-tab'])

const LINKS = [
  { tab: 'permissions', label: 'Agent to agent', open: 'Permissions', hint: 'Which agents may call this one.' },
  { tab: 'a2a', label: 'A2A', open: 'A2A', hint: 'The agent card and outbound A2A endpoints.' },
  { tab: 'nevermined', label: 'Paid access', open: 'Payments', hint: 'Let outsiders call the agent, paid, and see the payment log.' },
]
const links = computed(() => LINKS.filter((l) => props.visibleTabIds.includes(l.tab)))
</script>
