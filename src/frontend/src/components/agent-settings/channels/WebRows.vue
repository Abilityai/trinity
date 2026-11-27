<template>
  <SettingsSection title="Web">
    <ChannelConfigRow
      title="Public links"
      hint="Shareable chat pages outsiders open in a browser."
      :agent-name="agentName"
      :status-url="`/api/agents/${agentName}/public-links`"
      :derive-status="publicLinksStatus"
      :badge="false"
      @status="$emit('status', { name: 'public links', connected: $event.connected })"
    >
      <PublicLinksPanel :agent-name="agentName" />
    </ChannelConfigRow>

    <SettingRow label="Portal clients" hint="People you've shared this agent with, who sign in by email." testid="channels-portal-clients">
      <template #action>
        <BaseButton size="sm" variant="secondary" @click="portalOpen = true">Manage</BaseButton>
      </template>
    </SettingRow>
    <ChannelConfigDialog v-if="portalOpen" title="Portal clients" @close="portalOpen = false">
      <PortalClientsPanel :agent-name="agentName" />
    </ChannelConfigDialog>

    <!-- trinity-enterprise#888: file sharing governs what leaves the disk, not reach. -->
    <SettingRow label="Shared files" hint="Files you share by link are managed with the agent's storage.">
      <template #action>
        <router-link
          :to="{ query: { ...$route.query, tab: 'settings', section: 'storage' } }"
          class="text-sm font-medium text-action-primary-600 hover:text-action-primary-700 dark:text-action-primary-400 dark:hover:text-action-primary-300"
          data-testid="channels-storage-link"
        >Settings › Storage</router-link>
      </template>
    </SettingRow>
  </SettingsSection>
</template>

<script setup>
import { ref } from 'vue'
import { publicLinksStatus } from '../../../utils/channelsPane'
import BaseButton from '../../base/BaseButton.vue'
import ChannelConfigDialog from '../../ChannelConfigDialog.vue'
import ChannelConfigRow from '../../ChannelConfigRow.vue'
import PortalClientsPanel from '../../PortalClientsPanel.vue'
import PublicLinksPanel from '../../PublicLinksPanel.vue'
import SettingRow from '../SettingRow.vue'
import SettingsSection from '../SettingsSection.vue'

defineProps({ agentName: { type: String, required: true } })
defineEmits(['status'])

const portalOpen = ref(false)
</script>
